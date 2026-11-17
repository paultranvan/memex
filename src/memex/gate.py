"""The server's gate: the only thing a host's SSH key is allowed to run.

Installed as a forced command in `authorized_keys`:

    restrict,command="memex-gate --forced --device laptop" ssh-ed25519 AAAA…

It only accepts: dropping segments into the device's inbox, cloning the wiki
(and the tool's repository) read-only, fetching the latest backup.
"""
from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import sys
import tarfile
from pathlib import Path
from typing import Optional

from .config import Config
from .segment import valid_name
from .util import atomic_write, iso

MAX_FILE = 4 * 1024 * 1024
MAX_FILES = 2000
_DEVICE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_STAMP = re.compile(r"^(\d{4})(\d{2})\d{2}T")


def archive_path(config: Config, device: str, name: str) -> Path:
    match = _STAMP.match(name)
    year, month = (match.group(1), match.group(2)) if match else ("0000", "00")
    return config.archive / device / year / month / name


def upload(config: Config, device: str, stream) -> dict:
    result: dict = {"stored": [], "duplicate": [], "rejected": {}}
    inbox = config.inbox / device
    inbox.mkdir(parents=True, exist_ok=True)
    count = 0
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            count += 1
            name = member.name
            if count > MAX_FILES:
                result["rejected"][name] = "too many files in one upload"
                continue
            if not member.isreg() or "/" in name or "\\" in name or not valid_name(name):
                result["rejected"][name] = "file name or type rejected"
                continue
            if member.size > MAX_FILE:
                result["rejected"][name] = "file too large"
                continue
            handle = archive.extractfile(member)
            data = handle.read() if handle else b""
            if not data.startswith(b"---\n"):
                result["rejected"][name] = "not a segment"
                continue
            known = [inbox / name, archive_path(config, device, name), config.quarantine / device / name]
            if any(path.exists() for path in known):
                result["duplicate"].append(name)
                continue
            atomic_write(inbox / name, data, 0o640)
            result["stored"].append(name)
    if result["stored"]:
        atomic_write(config.trigger, iso() + "\n")
    return result


def backup_latest(config: Config, out) -> int:
    candidates = sorted(config.backups.glob("memex-*.tar.gz")) if config.backups.is_dir() else []
    if not candidates:
        return 3
    with open(candidates[-1], "rb") as handle:
        shutil.copyfileobj(handle, out)
    return 0


def _allowed_repo(config: Config, raw: str) -> Optional[Path]:
    wanted = Path(raw)
    if not wanted.is_absolute():
        wanted = config.home / wanted
    try:
        resolved = wanted.resolve()
    except OSError:
        return None
    for allowed in (config.wiki_repo, config.home / "src.git"):
        try:
            if allowed.exists() and resolved == allowed.resolve():
                return resolved
        except OSError:
            continue
    return None


def main(argv: list[str], config: Optional[Config] = None) -> int:
    config = config or Config.load()
    forced, device, rest = False, "", []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--forced":
            forced = True
        elif arg == "--device" and index + 1 < len(argv):
            index += 1
            device = argv[index]
        else:
            rest.append(arg)
        index += 1
    if forced:
        try:
            words = shlex.split(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
        except ValueError:
            words = []
        if not words:
            print("memex-gate: access limited to memex (no shell)", file=sys.stderr)
            return 1
        if words[0] == "git-upload-pack" and len(words) == 2:
            repo = _allowed_repo(config, words[1])
            if not repo:
                print("memex-gate: repository rejected", file=sys.stderr)
                return 1
            os.execvp("git-upload-pack", ["git-upload-pack", str(repo)])
        if words[0] != "memex-gate":
            print("memex-gate: command rejected", file=sys.stderr)
            return 1
        # The device comes from the key (authorized_keys), never from what the client says.
        rest, skip = [], False
        for word in words[1:]:
            if skip:
                skip = False
            elif word == "--device":
                skip = True
            elif word != "--forced":
                rest.append(word)
    verb = rest[0] if rest else ""
    if verb == "ping":
        print(json.dumps({"ok": True, "device": device, "time": iso()}))
        return 0
    if verb == "backup-latest":
        return backup_latest(config, sys.stdout.buffer)
    if verb == "upload":
        if not _DEVICE.match(device or ""):
            print("memex-gate: missing or invalid device name", file=sys.stderr)
            return 2
        try:
            result = upload(config, device, sys.stdin.buffer)
        except (tarfile.TarError, OSError) as exc:
            print(f"memex-gate: unreadable upload ({exc})", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False))
        return 0
    print("memex-gate: accepted verbs: upload, ping, backup-latest", file=sys.stderr)
    return 2


def pack(paths: list[Path]) -> bytes:
    """Host side: builds the archive sent to `upload`."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path in paths:
            info = tarfile.TarInfo(name=path.name)
            data = path.read_bytes()
            info.size = len(data)
            info.mode = 0o640
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()
