"""Runs an agent (Hermes, Claude Code, Codex or another) for one step of the pipeline.

The pipeline does not depend on any harness: it writes a prompt to a file and
runs a shell command. Three permission levels:

  - extract: no tools, text in, JSON out;
  - read:    read the files of the working directory;
  - edit:    read and write in the working directory.

Variables passed to the command: MEMEX_PROMPT_FILE, MEMEX_OUT_FILE (the final
reply, if the harness can write it), MEMEX_MODEL, MEMEX_ROLE (extract,
integrate, weave, diagnose, links, apply or probe) and MEMEX_LANG (the wiki
language).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import PIPELINE_SENTINEL, i18n
from .config import Config
from .util import now

_MODEL = '${MEMEX_MODEL:+--model "$MEMEX_MODEL"}'
_CODEX = 'codex exec --skip-git-repo-check --ephemeral -o "$MEMEX_OUT_FILE" ${MEMEX_MODEL:+-m "$MEMEX_MODEL"}'
# Hermes anchors its file tools on TERMINAL_CWD, not on the process's working directory.
_HERMES = 'TERMINAL_CWD="$PWD" hermes --ignore-rules ${MEMEX_MODEL:+-m "$MEMEX_MODEL"}'

PRESETS: dict[str, dict[str, str]] = {
    "claude": {
        "extract": f'claude -p --tools "" --no-session-persistence {_MODEL} < "$MEMEX_PROMPT_FILE"',
        "read": f'claude -p --tools "Read,Glob,Grep" --no-session-persistence {_MODEL} < "$MEMEX_PROMPT_FILE"',
        "edit": (
            'claude -p --tools "Read,Write,Edit,Glob,Grep" --permission-mode acceptEdits --no-session-persistence '
            f'{_MODEL} < "$MEMEX_PROMPT_FILE"'
        ),
    },
    "codex": {
        "extract": f'{_CODEX} --sandbox read-only - < "$MEMEX_PROMPT_FILE" > /dev/null',
        "read": f'{_CODEX} --sandbox read-only - < "$MEMEX_PROMPT_FILE" > /dev/null',
        "edit": f'{_CODEX} --sandbox workspace-write - < "$MEMEX_PROMPT_FILE" > /dev/null',
    },
    "hermes": {
        "extract": f'{_HERMES} -t todo -z "$(cat "$MEMEX_PROMPT_FILE")"',
        "read": f'{_HERMES} -t file -z "$(cat "$MEMEX_PROMPT_FILE")"',
        "edit": f'{_HERMES} -t file --yolo -z "$(cat "$MEMEX_PROMPT_FILE")"',
    },
}
# Harnesses that take the prompt as an argument are limited by the kernel (128 KB per argument).
ARGV_PRESETS = {"hermes"}
ARGV_LIMIT = 120_000
# Word expected in reply to the probe: it proves that the agent still answers.
PROBE_TOKEN = "memex-ok"


class AgentError(RuntimeError):
    pass


@dataclass
class AgentResult:
    text: str
    returncode: int
    seconds: float
    log: Optional[Path] = None


class Agent:
    def __init__(self, config: Config):
        self.config = config
        self.name = (config.get("MEMEX_AGENT") or "hermes").strip().lower()
        self.timeout = config.int("MEMEX_AGENT_TIMEOUT_S")

    def command(self, kind: str) -> str:
        override = self.config.get(f"MEMEX_AGENT_{kind.upper()}_CMD").strip()
        if override:
            return override
        if self.name not in PRESETS:
            raise AgentError(
                f"unknown MEMEX_AGENT={self.name!r}: choose hermes, claude or codex, "
                f"or set MEMEX_AGENT_{kind.upper()}_CMD"
            )
        return PRESETS[self.name][kind]

    # ── sandbox ───────────────────────────────────────────────────────────
    def sandbox(self, kind: str, cwd: Path, tmp: Path) -> list[str]:
        """bubblewrap prefix: the whole system read-only, except the working directory
        (for `edit`) and the harness's own state. SSH keys are hidden.

        MEMEX_SANDBOX: `auto` (default, used if it works), `bwrap` (required), `none`.
        """
        mode = (self.config.get("MEMEX_SANDBOX") or "auto").strip().lower()
        if mode == "none":
            return []
        binary = shutil.which("bwrap")
        base = [binary or "bwrap", "--die-with-parent", "--ro-bind", "/", "/", "--dev", "/dev", "--tmpfs", "/tmp"]
        if not binary or not self._sandbox_works(tuple(base)):
            if mode == "bwrap":
                raise AgentError("MEMEX_SANDBOX=bwrap but bubblewrap is missing or unusable here")
            return []
        home = Path.home()
        writable = self.config.list("MEMEX_SANDBOX_RW") or [
            ".hermes", ".claude", ".claude.json", ".codex", ".cache", ".config", ".local/state",
            ".local/share/claude", ".npm",
        ]
        args = list(base)
        for name in writable:
            path = home / name
            if path.exists():
                args += ["--bind", str(path), str(path)]
        for name in (".ssh", ".gnupg"):
            if (home / name).is_dir():
                args += ["--tmpfs", str(home / name)]
        args += ["--bind", str(tmp), str(tmp)]
        args += ["--bind" if kind == "edit" else "--ro-bind", str(cwd), str(cwd), "--chdir", str(cwd)]
        return args

    _sandbox_cache: dict[tuple, bool] = {}

    @classmethod
    def _sandbox_works(cls, base: tuple) -> bool:
        if base not in cls._sandbox_cache:
            try:
                ok = subprocess.run([*base, "true"], capture_output=True, timeout=20).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                ok = False
            cls._sandbox_cache[base] = ok
        return cls._sandbox_cache[base]

    def sandbox_state(self) -> str:
        mode = (self.config.get("MEMEX_SANDBOX") or "auto").strip().lower()
        if mode == "none":
            return "disabled (MEMEX_SANDBOX=none)"
        binary = shutil.which("bwrap")
        if not binary:
            return "unavailable: bubblewrap is not installed"
        base = (binary, "--die-with-parent", "--ro-bind", "/", "/", "--dev", "/dev", "--tmpfs", "/tmp")
        if self._sandbox_works(base):
            return "active (bubblewrap)"
        return "unavailable: bubblewrap does not start in this environment"

    def healthy(self, cwd: Path) -> tuple[bool, str]:
        """Probe: does the agent answer a trivial request?

        Tells "this segment is the problem" apart from "the agent no longer answers
        at all" (quota reached, network down, login expired). Returns (state, reason).
        """
        try:
            reply = self.run(
                "extract", f"Reply with this single word and nothing else: {PROBE_TOKEN}", cwd, "probe",
                timeout=min(self.timeout, 180), role="probe",
            )
        except AgentError as exc:
            return False, str(exc)
        if PROBE_TOKEN in reply.text:
            return True, ""
        return False, "unexpected reply to the probe: " + " ".join(reply.text.split())[-200:]

    def run(
        self, kind: str, prompt: str, cwd: Path, label: str = "", timeout: Optional[int] = None, role: str = "",
    ) -> AgentResult:
        if not prompt.startswith(PIPELINE_SENTINEL):
            prompt = f"{PIPELINE_SENTINEL}\n{prompt}"
        command = self.command(kind)
        uses_argv = "$(cat" in command
        if uses_argv and len(prompt.encode("utf-8")) > ARGV_LIMIT:
            raise AgentError("prompt too long for this harness (lower MEMEX_MAX_SEGMENT_CHARS)")
        logs = self.config.logs / "agent"
        logs.mkdir(parents=True, exist_ok=True)
        stamp = now().strftime("%Y%m%dT%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", label)[:80] or "run"
        log_path = logs / f"{stamp}-{kind}-{safe}.log"
        with tempfile.TemporaryDirectory(prefix="memex-agent-") as tmp:
            prompt_file, out_file = Path(tmp) / "prompt.md", Path(tmp) / "reply.txt"
            prompt_file.write_text(prompt, encoding="utf-8")
            env = dict(os.environ)
            env.update({
                "MEMEX_PROMPT_FILE": str(prompt_file), "MEMEX_OUT_FILE": str(out_file),
                "MEMEX_MODEL": self.config.get("MEMEX_AGENT_MODEL"), "MEMEX_PIPELINE": "1",
                "MEMEX_ROLE": role, "MEMEX_LANG": i18n.current().lang,
            })
            started = time.time()
            proc = subprocess.Popen(
                [*self.sandbox(kind, Path(cwd), Path(tmp)), "sh", "-c", command], cwd=str(cwd), env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout or self.timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
                stdout, stderr = proc.communicate()
                self._log(log_path, kind, command, prompt, stdout, stderr, "timed out")
                raise AgentError(f"the agent exceeded {timeout or self.timeout} s ({kind})")
            seconds = time.time() - started
            text = stdout or ""
            try:
                produced = out_file.read_text(encoding="utf-8")
                if produced.strip():
                    text = produced
            except OSError:
                pass
        self._log(log_path, kind, command, prompt, text, stderr, f"exit {proc.returncode}, {seconds:.0f} s")
        self._rotate(logs)
        if proc.returncode != 0:
            tail = (stderr or text or "").strip()[-400:]
            raise AgentError(f"the agent failed (exit {proc.returncode}): {tail}")
        return AgentResult(text=text, returncode=proc.returncode, seconds=seconds, log=log_path)

    @staticmethod
    def _log(path: Path, kind: str, command: str, prompt: str, out: str, err: str, status: str) -> None:
        body = (
            f"# {kind} — {status}\n$ {command}\n\n## prompt ({len(prompt)} characters)\n{prompt[:6000]}\n"
            f"\n## output\n{(out or '')[-6000:]}\n\n## errors\n{(err or '')[-3000:]}\n"
        )
        try:
            path.write_text(body, encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def _rotate(folder: Path, keep: int = 400) -> None:
        files = sorted(folder.glob("*.log"))
        for old in files[:-keep]:
            old.unlink(missing_ok=True)


def parse_json_reply(text: str) -> Any:
    """Extracts the JSON object from a reply, even when wrapped in text or code fences."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    for candidate in ([fenced.group(1)] if fenced else []) + [text]:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    # Objects surrounded by prose. A model that corrects itself leaves several in a row: the last
    # one is its answer. If any of them is malformed the reply is ambiguous, and failing (hence
    # retrying) is safer than settling for an earlier draft.
    decoder, found, position = json.JSONDecoder(), None, text.find("{")
    while position != -1:
        try:
            value, length = decoder.raw_decode(text[position:])
        except ValueError:
            found = None
            break
        if isinstance(value, dict):
            found = value
        position = text.find("{", position + length)
    if found is not None:
        return found
    raise AgentError("the agent's reply is not a JSON object")
