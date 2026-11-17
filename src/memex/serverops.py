"""Server operations: backups, restore, device keys."""
from __future__ import annotations

import io
import os
import re
import shutil
import tarfile
import tempfile
from pathlib import Path

from .config import Config
from .util import atomic_write, now, run
from .wikirepo import WikiRepo

_KEY = re.compile(
    r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-[A-Za-z0-9-]+|sk-[A-Za-z0-9@.-]+) ([A-Za-z0-9+/=]{40,})(?: (.*))?$"
)
_DEVICE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def backup(config: Config) -> Path:
    """A self-contained archive: the full history of the wiki, the segments and the state."""
    config.backups.mkdir(parents=True, exist_ok=True)
    target = config.backups / f"memex-{now().strftime('%Y%m%dT%H%M%SZ')}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="memex-backup-") as tmp:
        bundle = Path(tmp) / "wiki.bundle"
        run(["git", "-C", config.wiki_repo, "bundle", "create", "--quiet", str(bundle), "--branches", "HEAD"])
        partial = target.with_suffix(".partial")
        with tarfile.open(partial, "w:gz") as archive:
            archive.add(bundle, arcname="wiki.bundle")
            for name in ("raw/archive", "raw/inbox", "raw/quarantine", "state"):
                folder = config.home / name
                if folder.is_dir():
                    archive.add(folder, arcname=name, filter=_skip_locks)
        os.chmod(partial, 0o600)
        os.replace(partial, target)
    keep = max(config.int("MEMEX_SERVER_BACKUP_KEEP"), 1)
    for old in sorted(config.backups.glob("memex-*.tar.gz"))[:-keep]:
        old.unlink(missing_ok=True)
    return target


def _skip_locks(info: tarfile.TarInfo):
    return None if info.name.endswith(".lock") else info


def restore_backup(config: Config, archive_path: Path, force: bool = False) -> str:
    """Rebuilds an empty server from a backup."""
    repo = WikiRepo(config)
    if repo.exists() and not force:
        return "The wiki already exists on this server: run again with --force to replace it."
    with tempfile.TemporaryDirectory(prefix="memex-restore-") as tmp:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                name = member.name
                if name.startswith("/") or ".." in Path(name).parts or not (member.isreg() or member.isdir()):
                    continue
                if member.isdir():
                    (Path(tmp) / name).mkdir(parents=True, exist_ok=True)
                    continue
                handle = archive.extractfile(member)
                if handle:
                    atomic_write(Path(tmp) / name, handle.read(), 0o640)
        bundle = Path(tmp) / "wiki.bundle"
        if not bundle.is_file():
            return "Invalid backup: wiki.bundle is missing."
        if repo.exists():
            shutil.rmtree(config.wiki_repo)
        config.wiki_repo.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--quiet", "--branch", "main", str(bundle), str(config.wiki_repo)])
        repo.git("remote", "remove", "origin", check=False)
        repo.git("config", "user.name", "memex")
        repo.git("config", "user.email", "memex@localhost")
        for name in ("raw/archive", "raw/inbox", "raw/quarantine", "state"):
            source = Path(tmp) / name
            if source.is_dir():
                shutil.copytree(source, config.home / name, dirs_exist_ok=True)
    # The vault manifest is now worthless: on the next run, the wiki will be the reference.
    (config.state_dir / "vault-manifest.json").unlink(missing_ok=True)
    return f"Server restored from {Path(archive_path).name}."


# ── devices: one SSH key per machine, restricted to the memex gate ────────
def _owner(config: Config):
    """The account that runs memex: the owner of MEMEX_HOME."""
    import pwd

    try:
        return pwd.getpwuid(config.home.stat().st_uid)
    except (OSError, KeyError):
        return pwd.getpwuid(os.getuid())


def _authorized_keys(config: Config) -> Path:
    custom = config.get("MEMEX_AUTHORIZED_KEYS").strip()
    if custom:
        return Path(custom).expanduser()
    return Path(_owner(config).pw_dir) / ".ssh" / "authorized_keys"


def _gate_binary(config: Config) -> str:
    return config.get("MEMEX_GATE_BIN").strip() or shutil.which("memex-gate") or "/usr/local/bin/memex-gate"


def _lines(config: Config) -> list[str]:
    try:
        return _authorized_keys(config).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _write_keys(config: Config, lines: list[str]) -> str:
    """Run as root, the file belongs to root: the memex account (hence an agent) cannot add a key for itself."""
    path = _authorized_keys(config)
    try:
        path.parent.mkdir(mode=0o755 if os.geteuid() == 0 else 0o700, parents=True, exist_ok=True)
        atomic_write(path, "\n".join(lines) + ("\n" if lines else ""), 0o644 if os.geteuid() == 0 else 0o600)
    except PermissionError:
        return f"Cannot write {path}: run the command again with sudo."
    return ""


def _device_of(line: str) -> str:
    match = re.search(r"memex-gate[^\"]* --device ([A-Za-z0-9._-]+)", line)
    return match.group(1) if match else ""


def device_add(config: Config, name: str, key: str) -> str:
    key = " ".join(key.split())
    match = _KEY.match(key)
    if not _DEVICE.match(name):
        return "Invalid device name: letters, digits, dot, hyphen and underscore only."
    if not match:
        return "Invalid public key: paste the contents of a .pub file (ssh-ed25519 AAAA…)."
    blob = match.group(2)
    kept = [line for line in _lines(config) if _device_of(line) != name and blob not in line]
    kept.append(f'restrict,command="{_gate_binary(config)} --forced --device {name}" {key}')
    error = _write_keys(config, kept)
    if error:
        return error
    inbox = config.inbox / name
    inbox.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        owner = _owner(config)
        for folder in (config.inbox, inbox):
            os.chown(folder, owner.pw_uid, owner.pw_gid)
    return f'Device "{name}" authorised.'


def device_remove(config: Config, name: str) -> str:
    lines = _lines(config)
    kept = [line for line in lines if _device_of(line) != name]
    if len(kept) == len(lines):
        return f'No device named "{name}".'
    return _write_keys(config, kept) or f'Device "{name}" removed.'


def device_list(config: Config) -> list[str]:
    return [name for name in (_device_of(line) for line in _lines(config)) if name]


__all__ = ["backup", "restore_backup", "device_add", "device_remove", "device_list", "io"]
