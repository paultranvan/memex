"""Hooks de session, communs à Claude Code et Codex.

`session-start` : imprime la page vivante du projet courant (elle entre dans le
contexte de l'agent) et lance une synchronisation en arrière-plan, sans jamais
faire attendre la session. `session-end` : force le balayage de la session qui
vient de se terminer, pour que le wiki soit à jour quelques minutes plus tard.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .config import Config
from .pages import parse_page
from .project import ProjectResolver
from .util import read_json, tilde


def _input(raw: str) -> dict:
    try:
        data = json.loads(raw) if raw and raw.strip() else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def spawn_sync(config: Config, sessions: tuple[str, ...] = (), throttle: bool = True) -> bool:
    if throttle:
        last = (read_json(config.data_dir / "last-sync.json", {}) or {}).get("at", 0)
        if time.time() - last < config.int("MEMEX_SYNC_MIN_INTERVAL_S"):
            return False
    args = [sys.executable, "-m", "memex", "sync", "--quiet"]
    for session in sessions:
        args += ["--session", session]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        subprocess.Popen(
            args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, cwd=str(Path.home()),
        )
    except OSError:
        return False
    return True


def project_context(config: Config, cwd: str) -> str:
    clone = config.wiki_clone
    mapping = read_json(clone / ".memex" / "projects.json", {}) or {}
    project = ProjectResolver().resolve(cwd)
    if not project or not isinstance(mapping, dict):
        return ""
    dossier = mapping.get(project.remote) or mapping.get(project.slug)
    if not dossier:
        return ""
    name = dossier.split("/")[-1]
    try:
        page = parse_page(f"{dossier}/{name}.md", (clone / dossier / f"{name}.md").read_text(encoding="utf-8"))
    except OSError:
        return ""
    limit = max(config.int("MEMEX_HOOK_MAX_CHARS"), 800)
    footer = (
        f"\n\n---\nSource : wiki memex, en lecture seule ({tilde(clone)}). Page de ce projet : `{dossier}/{name}.md` ; "
        f"fiches de décision détaillées : `{dossier}/Décisions/`. Index de tout le wiki : `index.md`. "
        "Ce qui sera décidé dans cette session y sera reporté automatiquement."
    )
    header = f"# Mémoire du projet « {page.title} » (issue des sessions précédentes)\n\n"
    body = page.body.strip()
    room = limit - len(header) - len(footer)
    if len(body) > room:
        body = body[:room].rsplit("\n", 1)[0] + "\n\n[… page tronquée : lis le fichier pour la suite …]"
    return header + body + footer


def session_start(config: Config, raw: str) -> str:
    data = _input(raw)
    spawn_sync(config)
    try:
        return project_context(config, str(data.get("cwd") or os.getcwd()))
    except Exception:
        return ""  # un hook ne doit jamais gêner l'ouverture d'une session


def session_end(config: Config, raw: str) -> None:
    data = _input(raw)
    session = str(data.get("session_id") or data.get("sessionId") or "")
    spawn_sync(config, (session,) if session else (), throttle=False)
