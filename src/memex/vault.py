"""Synchronisation between the wiki repository and the Obsidian vault folder.

The vault is an ordinary folder with nothing of git in it: it is the one the
LiveSync client synchronises with your devices. A manifest keeps the digest
of each file as of the last exchange, which tells who changed what:

  - the vault differs from the manifest  → human edit, to import into the wiki;
  - the wiki differs from the vault      → agent edit, to export to the vault.

When both sides changed, the human wins.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from .config import Config
from .pages import MANAGED_FILES, iter_files
from .util import atomic_write, read_json, sha256_file, write_json

MAX_IMPORT = 10 * 1024 * 1024


def scan(root: Path) -> dict[str, str]:
    result = {}
    if not root.is_dir():
        return result
    for relative in iter_files(root):
        try:
            result[relative] = sha256_file(root / relative)
        except OSError:
            continue
    return result


class Vault:
    def __init__(self, config: Config, wiki_dir: Path):
        self.dir: Optional[Path] = config.vault_dir
        self.wiki = Path(wiki_dir)
        self.manifest_path = config.state_dir / "vault-manifest.json"

    @property
    def enabled(self) -> bool:
        return self.dir is not None

    def _manifest(self) -> Optional[dict[str, str]]:
        data = read_json(self.manifest_path, None)
        return data if isinstance(data, dict) else None

    def import_changes(self) -> list[str]:
        """Vault → wiki. Returns the paths changed in the repository (for the caller to commit)."""
        if not self.enabled:
            return []
        assert self.dir is not None
        self.dir.mkdir(parents=True, exist_ok=True)
        manifest = self._manifest()
        vault_now = scan(self.dir)
        changed: list[str] = []
        if manifest is None:
            # First exchange (or lost manifest): the wiki is authoritative, only what it lacks is imported.
            wiki_now = scan(self.wiki)
            for relative in vault_now:
                if relative not in wiki_now and relative not in MANAGED_FILES:
                    if self._copy(self.dir / relative, self.wiki / relative):
                        changed.append(relative)
            return changed
        for relative, digest in vault_now.items():
            if relative in MANAGED_FILES or manifest.get(relative) == digest:
                continue
            if self._copy(self.dir / relative, self.wiki / relative):
                changed.append(relative)
                manifest[relative] = digest
        for relative in list(manifest):
            if relative not in vault_now and relative not in MANAGED_FILES:
                target = self.wiki / relative
                if target.exists():
                    target.unlink()
                    changed.append(relative)
                del manifest[relative]
        write_json(self.manifest_path, manifest)
        return changed

    def export(self) -> int:
        """Wiki → vault. Returns the number of files written or deleted."""
        if not self.enabled:
            return 0
        assert self.dir is not None
        self.dir.mkdir(parents=True, exist_ok=True)
        previous = self._manifest()
        bootstrap = previous is None
        previous = previous or {}
        wiki_now, vault_now = scan(self.wiki), scan(self.dir)
        manifest: dict[str, str] = {}
        touched = 0
        for relative, digest in wiki_now.items():
            current = vault_now.get(relative)
            if current == digest:
                manifest[relative] = digest
                continue
            human_edit = current is not None and not bootstrap and previous.get(relative) != current
            if human_edit and relative not in MANAGED_FILES:
                manifest[relative] = previous.get(relative, "")  # will be imported on the next run
                continue
            atomic_write(self.dir / relative, (self.wiki / relative).read_bytes())
            manifest[relative] = digest
            touched += 1
        for relative, digest in previous.items():
            if relative in wiki_now or relative not in vault_now:
                continue
            if vault_now[relative] == digest:
                (self.dir / relative).unlink(missing_ok=True)
                touched += 1
            else:
                manifest[relative] = digest  # edited by the human after deletion on the wiki side: the human wins
        self._prune_empty_dirs()
        write_json(self.manifest_path, {k: v for k, v in manifest.items() if v})
        return touched

    def _copy(self, source: Path, target: Path) -> bool:
        try:
            if source.stat().st_size > MAX_IMPORT:
                return False
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            return True
        except OSError:
            return False

    def _prune_empty_dirs(self) -> None:
        assert self.dir is not None
        for folder, _dirs, _files in os.walk(self.dir, topdown=False):
            path = Path(folder)
            if path == self.dir or any(part.startswith(".") for part in path.relative_to(self.dir).parts):
                continue
            try:
                path.rmdir()
            except OSError:
                pass
