"""Utilitaires partagés : temps, fichiers atomiques, verrous, sous-processus."""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

UTC = dt.timezone.utc


def now() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso(ts: Optional[dt.datetime] = None) -> str:
    ts = ts or now()
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: Any) -> Optional[dt.datetime]:
    """Accepte ISO 8601 (avec Z), epoch en secondes ou millisecondes."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        seconds = value / 1000.0 if value > 1e11 else float(value)
        try:
            return dt.datetime.fromtimestamp(seconds, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return parse_ts(float(text))
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def local_tz(name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return UTC


def atomic_write(path: Path, data: str | bytes, mode: int = 0o644) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = data.encode("utf-8") if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n")


def append_line(path: Path, line: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")


class LockBusy(RuntimeError):
    pass


@contextlib.contextmanager
def file_lock(path: Path, wait: bool = False) -> Iterator[None]:
    """Verrou exclusif inter-processus. `wait=False` lève LockBusy si pris."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise LockBusy(str(path)) from exc
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


class CommandError(RuntimeError):
    def __init__(self, cmd: Sequence[str], returncode: int, stdout: str, stderr: str):
        self.cmd, self.returncode, self.stdout, self.stderr = list(cmd), returncode, stdout, stderr
        tail = (stderr or stdout or "").strip()[-600:]
        super().__init__(f"{' '.join(map(str, cmd))!r} a échoué ({returncode}) : {tail}")


def run(
    cmd: Sequence[str],
    cwd: Optional[Path] = None,
    env: Optional[dict] = None,
    input: Optional[str | bytes] = None,
    timeout: Optional[float] = None,
    check: bool = True,
    binary: bool = False,
) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    if env:
        full_env.update({k: str(v) for k, v in env.items()})
    proc = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd else None,
        env=full_env,
        input=input,
        capture_output=True,
        text=not binary,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        out = proc.stdout if not binary else proc.stdout.decode("utf-8", "replace")
        err = proc.stderr if not binary else proc.stderr.decode("utf-8", "replace")
        raise CommandError(cmd, proc.returncode, out, err)
    return proc


def slugify(text: str, fallback: str = "x") -> str:
    norm = unicodedata.normalize("NFKD", text or "")
    norm = "".join(ch for ch in norm if not unicodedata.combining(ch)).lower()
    norm = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")
    return norm or fallback


def safe_filename(text: str, max_len: int = 80) -> str:
    """Nom de fichier lisible (accents conservés) sans caractères interdits par Obsidian."""
    cleaned = re.sub(r'[\\/:*?"<>|#^\[\]]+', " ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned[:max_len].rstrip(" .")) or "sans-titre"


def truncate_middle(text: str, limit: int, label: str = "tronqué") -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = max(limit - head, 0)
    cut = len(text) - head - tail
    return f"{text[:head]}\n[… {label} : {cut} caractères …]\n{text[len(text) - tail:] if tail else ''}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tilde(path: str | Path) -> str:
    """Remplace le répertoire personnel par ~ (les condensés ne portent pas le nom d'utilisateur)."""
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text


def split_list(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,\n]", value or "") if item.strip()]


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr, flush=True)
