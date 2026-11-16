"""Exploitation du serveur : sauvegardes, restauration, clés des appareils."""
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

_KEY = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-[A-Za-z0-9-]+|sk-[A-Za-z0-9@.-]+) ([A-Za-z0-9+/=]{40,})(?: (.*))?$")
_DEVICE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def backup(config: Config) -> Path:
    """Une archive autonome : l'historique complet du wiki, les condensés et l'état."""
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
    """Reconstruit un serveur vide à partir d'une sauvegarde."""
    repo = WikiRepo(config)
    if repo.exists() and not force:
        return "Le wiki existe déjà sur ce serveur : relance avec --force pour le remplacer."
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
            return "Sauvegarde invalide : wiki.bundle absent."
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
    # Le manifeste du vault ne vaut plus rien : au prochain passage, le wiki fera foi.
    (config.state_dir / "vault-manifest.json").unlink(missing_ok=True)
    return f"Serveur restauré depuis {Path(archive_path).name}."


# ── appareils : une clé SSH par machine, limitée à la porte memex ─────────
def _authorized_keys() -> Path:
    return Path.home() / ".ssh" / "authorized_keys"


def _gate_binary(config: Config) -> str:
    return config.get("MEMEX_GATE_BIN").strip() or shutil.which("memex-gate") or "/usr/local/bin/memex-gate"


def _lines() -> list[str]:
    try:
        return _authorized_keys().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _device_of(line: str) -> str:
    match = re.search(r"memex-gate[^\"]* --device ([A-Za-z0-9._-]+)", line)
    return match.group(1) if match else ""


def device_add(config: Config, name: str, key: str) -> str:
    key = " ".join(key.split())
    match = _KEY.match(key)
    if not _DEVICE.match(name):
        return "Nom d'appareil invalide : lettres, chiffres, point, tiret et souligné seulement."
    if not match:
        return "Clé publique invalide : colle le contenu d'un fichier .pub (ssh-ed25519 AAAA…)."
    blob = match.group(2)
    kept = [line for line in _lines() if _device_of(line) != name and blob not in line]
    kept.append(f'restrict,command="{_gate_binary(config)} --forced --device {name}" {key}')
    path = _authorized_keys()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write(path, "\n".join(kept) + "\n", 0o600)
    (config.inbox / name).mkdir(parents=True, exist_ok=True)
    return f"Appareil « {name} » autorisé."


def device_remove(name: str) -> str:
    lines = _lines()
    kept = [line for line in lines if _device_of(line) != name]
    if len(kept) == len(lines):
        return f"Aucun appareil nommé « {name} »."
    atomic_write(_authorized_keys(), "\n".join(kept) + ("\n" if kept else ""), 0o600)
    return f"Appareil « {name} » retiré."


def device_list() -> list[str]:
    return [name for name in (_device_of(line) for line in _lines()) if name]


__all__ = ["backup", "restore_backup", "device_add", "device_remove", "device_list", "io"]
