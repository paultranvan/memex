"""Identité d'un projet à partir du répertoire de travail d'une session.

Une session dans un dépôt git est rattachée au dépôt (remote normalisé), y
compris depuis un worktree, même supprimé depuis. Hors dépôt : pas de projet.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .util import slugify

_WORKTREE_MARKERS = ("/.claude/worktrees/", "/.codex/worktrees/", "/.worktrees/", "/.hermes/worktrees/")


@dataclass(frozen=True)
class Project:
    slug: str
    name: str
    remote: str = ""
    root: str = ""

    def as_dict(self) -> dict:
        return {"slug": self.slug, "name": self.name, "remote": self.remote}


def normalize_remote(url: str) -> str:
    """git@github.com:owner/repo.git, https://github.com/owner/repo → github.com/owner/repo"""
    url = (url or "").strip()
    if not url:
        return ""
    url = re.sub(r"^[a-z][a-z0-9+.-]*://", "", url)
    url = re.sub(r"^[^@/]+@", "", url)
    url = re.sub(r"^([^/:]+):(?!\d+/)", r"\1/", url)
    url = re.sub(r":\d+/", "/", url)
    url = re.sub(r"\.git/?$", "", url).strip("/")
    return url.lower()


def _git(cwd: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=5,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def strip_worktree(path: str) -> str:
    for marker in _WORKTREE_MARKERS:
        index = path.find(marker)
        if index > 0:
            return path[:index]
    return path


class ProjectResolver:
    def __init__(self) -> None:
        self._cache: dict[str, Optional[Project]] = {}

    def resolve(self, cwd: str) -> Optional[Project]:
        if not cwd:
            return None
        if cwd not in self._cache:
            self._cache[cwd] = self._resolve(cwd)
        return self._cache[cwd]

    def _resolve(self, cwd: str) -> Optional[Project]:
        path = Path(os.path.expanduser(cwd))
        candidates = [path]
        stripped = strip_worktree(str(path))
        if stripped != str(path):
            candidates.append(Path(stripped))
        for candidate in candidates:
            probe = candidate
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            if not probe.is_dir():
                continue
            top = _git(probe, "rev-parse", "--show-toplevel")
            if not top:
                continue
            common = _git(probe, "rev-parse", "--path-format=absolute", "--git-common-dir")
            root = top
            if common and common.endswith("/.git"):
                root = common[: -len("/.git")]
            remote = normalize_remote(_git(probe, "config", "--get", "remote.origin.url"))
            name = remote.rsplit("/", 1)[-1] if remote else Path(root).name
            return Project(slug=slugify(name), name=name, remote=remote, root=root)
        gone = Path(stripped)
        if not gone.exists() and _looks_like_project(gone):
            # Dossier (ou worktree) supprimé depuis : on garde son nom comme identité du projet.
            return Project(slug=slugify(gone.name), name=gone.name, root=stripped)
        return None


def _looks_like_project(path: Path) -> bool:
    """Un dossier disparu compte comme projet s'il était rangé sous le répertoire personnel,
    à deux niveaux au moins, hors dossiers cachés et temporaires."""
    try:
        relative = path.relative_to(Path.home())
    except ValueError:
        return False
    parts = relative.parts
    return len(parts) >= 2 and not any(part.startswith(".") or part in ("tmp", "Downloads") for part in parts)


def _match_any(value: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        expanded = os.path.expanduser(pattern)
        if fnmatch.fnmatch(value, expanded) or fnmatch.fnmatch(value, expanded.rstrip("/") + "/*") or value == expanded.rstrip("/"):
            return True
    return False


def exclusion_reason(
    cwd: str,
    project: Optional[Project],
    exclude_paths: list[str],
    exclude_remotes: list[str],
    include_paths: list[str],
) -> str:
    """Chaîne vide si la session peut être capturée, sinon la raison de l'exclusion."""
    path = os.path.expanduser(cwd or "")
    if include_paths and not _match_any(path, include_paths):
        return "hors de MEMEX_INCLUDE_PATHS"
    if path and _match_any(path, exclude_paths):
        return "chemin exclu (MEMEX_EXCLUDE_PATHS)"
    if project and project.remote:
        for pattern in exclude_remotes:
            if fnmatch.fnmatch(project.remote, pattern.lower()) or pattern.lower() in project.remote:
                return "dépôt exclu (MEMEX_EXCLUDE_REMOTES)"
    probe = Path(project.root) if project and project.root else Path(path) if path else None
    home = Path.home()
    while probe and probe != probe.parent:
        if (probe / ".memex-ignore").exists():
            return f"fichier .memex-ignore dans {probe}"
        if probe == home:
            break
        probe = probe.parent
    return ""
