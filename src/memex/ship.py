"""Host → server link: sending segments, fetching the wiki and the backups.

Always initiated by the host. Three modes, depending on MEMEX_SERVER_SSH:
  - empty:    offline, segments pile up in the outbox;
  - `local`:  the server is on this machine (trial, or all-in-one);
  - `u@host`: over SSH, through the forced command `memex-gate`.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import gate
from .config import Config
from .util import CommandError, atomic_write, eprint, now, read_json, run, write_json

BATCH = 200


@dataclass
class ShipReport:
    sent: int = 0
    duplicate: int = 0
    rejected: dict[str, str] = field(default_factory=dict)
    pending: int = 0
    error: str = ""
    wiki: str = ""

    def summary(self) -> str:
        parts = []
        if self.sent or self.duplicate:
            parts.append(f"{self.sent} sent" + (f", {self.duplicate} already known" if self.duplicate else ""))
        if self.rejected:
            parts.append(f"{len(self.rejected)} rejected")
        if self.pending:
            parts.append(f"{self.pending} pending")
        if self.wiki:
            parts.append(f"wiki: {self.wiki}")
        if self.error:
            parts.append(f"error: {self.error}")
        return ", ".join(parts) or "nothing to send"


class Transport:
    def __init__(self, config: Config):
        self.config = config
        self.timeout = config.int("MEMEX_NET_TIMEOUT_S")

    @property
    def enabled(self) -> bool:
        return bool(self.config.server_ssh)

    def _ssh(self) -> list[str]:
        return [self.config.get("MEMEX_SSH_CMD") or "ssh", *shlex.split(self.config.get("MEMEX_SSH_OPTS"))]

    def gate(self, args: list[str], data: Optional[bytes] = None) -> subprocess.CompletedProcess:
        if self.config.is_local_server:
            src = str(Path(__file__).resolve().parent.parent)
            env = {"PYTHONPATH": src + os.pathsep + os.environ.get("PYTHONPATH", "")}
            return run([sys.executable, "-m", "memex", "gate", *args], env=env, input=data, timeout=self.timeout, binary=True)
        remote = "memex-gate " + " ".join(shlex.quote(a) for a in args)
        return run([*self._ssh(), self.config.server_ssh, remote], input=data, timeout=self.timeout, binary=True)

    def wiki_url(self) -> str:
        if self.config.is_local_server:
            return str(self.config.wiki_repo)
        return f"{self.config.server_ssh}:{self.config.get('MEMEX_SERVER_HOME').rstrip('/')}/wiki"

    def git_env(self) -> dict:
        env = {"GIT_TERMINAL_PROMPT": "0"}
        if not self.config.is_local_server:
            env["GIT_SSH_COMMAND"] = " ".join(shlex.quote(part) for part in self._ssh())
        return env


def ship(config: Config, report: Optional[ShipReport] = None) -> ShipReport:
    report = report or ShipReport()
    files = sorted(config.outbox.glob("*.md")) if config.outbox.is_dir() else []
    transport = Transport(config)
    if not files:
        return report
    if not transport.enabled:
        report.pending = len(files)
        return report
    for start in range(0, len(files), BATCH):
        batch = files[start:start + BATCH]
        try:
            proc = transport.gate(["upload", "--device", config.device], gate.pack(batch))
            answer = json.loads(proc.stdout.decode("utf-8") or "{}")
        except (CommandError, subprocess.TimeoutExpired, ValueError, OSError) as exc:
            report.error = str(exc)[:300]
            report.pending = len(files) - start
            return report
        month = now().strftime("%Y-%m")
        accepted = set(answer.get("stored", [])) | set(answer.get("duplicate", []))
        report.sent += len(answer.get("stored", []))
        report.duplicate += len(answer.get("duplicate", []))
        for path in batch:
            if path.name in accepted:
                target = config.sent / month / path.name
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, target)
            elif path.name in answer.get("rejected", {}):
                report.rejected[path.name] = answer["rejected"][path.name]
                target = config.data_dir / "rejected" / path.name
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, target)
    return report


def resend_all(config: Config) -> int:
    """Puts every segment already sent back into the outbox (rebuilding the server)."""
    count = 0
    config.outbox.mkdir(parents=True, exist_ok=True)
    for path in sorted(config.sent.rglob("*.md")) if config.sent.is_dir() else []:
        target = config.outbox / path.name
        if not target.exists():
            atomic_write(target, path.read_bytes(), 0o600)
            count += 1
    return count


def pull(config: Config, report: Optional[ShipReport] = None) -> ShipReport:
    report = report or ShipReport()
    transport = Transport(config)
    if not transport.enabled:
        return report
    clone, env, timeout = config.wiki_clone, transport.git_env(), transport.timeout
    try:
        if not (clone / ".git").exists():
            clone.parent.mkdir(parents=True, exist_ok=True)
            run(["git", "clone", "--quiet", transport.wiki_url(), str(clone)], env=env, timeout=timeout)
            report.wiki = "cloned"
            return report
        before = run(["git", "-C", clone, "rev-parse", "HEAD"], check=False).stdout.strip()
        run(["git", "-C", clone, "remote", "set-url", "origin", transport.wiki_url()], env=env)
        run(["git", "-C", clone, "fetch", "--quiet", "--prune", "origin"], env=env, timeout=timeout)
        run(["git", "-C", clone, "reset", "--quiet", "--hard", "origin/main"], env=env)
        run(["git", "-C", clone, "clean", "-fdq"], env=env)
        after = run(["git", "-C", clone, "rev-parse", "HEAD"], check=False).stdout.strip()
        report.wiki = "up to date" if before == after else "updated"
    except (CommandError, subprocess.TimeoutExpired, OSError) as exc:
        report.error = report.error or str(exc)[:300]
    return report


def pull_backup(config: Config) -> Optional[Path]:
    """At most once a day: brings back the server's latest backup."""
    transport = Transport(config)
    if not transport.enabled or not config.bool("MEMEX_BACKUP_PULL"):
        return None
    marker = config.data_dir / "backup-pull.json"
    last = (read_json(marker, {}) or {}).get("at", 0)
    if time.time() - last < 20 * 3600:
        return None
    try:
        proc = transport.gate(["backup-latest"])
    except (CommandError, subprocess.TimeoutExpired, OSError):
        return None
    if not proc.stdout:
        return None
    folder = config.data_dir / "backups"
    target = folder / f"memex-{now().strftime('%Y%m%d')}.tar.gz"
    atomic_write(target, proc.stdout, 0o600)
    write_json(marker, {"at": time.time()})
    for old in sorted(folder.glob("memex-*.tar.gz"))[: -max(config.int("MEMEX_BACKUP_KEEP"), 1)]:
        old.unlink(missing_ok=True)
    return target


def sync(config: Config, force: tuple[str, ...] = (), force_all: bool = False, quiet: bool = False) -> int:
    from .sweep import Sweeper

    swept = Sweeper(config).run(force=force, force_all=force_all)
    report = ship(config)
    pull(config, report)
    pull_backup(config)
    write_json(config.data_dir / "last-sync.json", {"at": time.time(), "sweep": swept.summary(), "ship": report.summary()})
    if not quiet:
        print(f"sweep: {swept.summary()}")
        print(f"link:  {report.summary()}")
    elif report.error:
        eprint(f"memex sync: {report.error}")
    return 1 if report.error else 0
