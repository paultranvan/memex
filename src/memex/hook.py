"""Session hooks, shared by Claude Code and Codex.

`session-start`: prints the living page of the current project and the list of
the pages linked with it (both enter the agent's context) and starts a sync in
the background, without ever making the session wait. `session-end`: forces the sweep of the session that has just
ended, so that the wiki is up to date a few minutes later.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import i18n, index
from .config import Config
from .pages import Page, load_wiki, parse_page
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


RELATED_HEADER = "Related pages elsewhere in the wiki (not loaded: read the ones this session needs):"
RELATED_SUMMARY = 160  # characters kept of each summary


def related_block(pages: dict[str, Page], folder: str, count: int, budget: int) -> str:
    """One line per page linked with the folder: its path and its summary. The pages themselves are not loaded."""
    found = index.related(pages, folder)
    lines, size = [], len(RELATED_HEADER)
    for page in found[:max(count, 0)]:
        text = page.summary or page.title
        if len(text) > RELATED_SUMMARY:
            text = text[:RELATED_SUMMARY].rsplit(" ", 1)[0] + "…"
        line = f"- `{page.path}` — {text}"
        if size + len(line) + 1 > budget:
            break
        lines.append(line)
        size += len(line) + 1
    if not lines:
        return ""
    if len(found) > len(lines):
        lines.append(f"- … and {len(found) - len(lines)} more: search the wiki for links to `{folder}`")
    return "\n".join([RELATED_HEADER, *lines]) + "\n\n"


def project_context(config: Config, cwd: str) -> str:
    clone = config.wiki_clone
    mapping = read_json(clone / ".memex" / "projects.json", {}) or {}
    project = ProjectResolver().resolve(cwd)
    if not project or not isinstance(mapping, dict):
        return ""
    folder = mapping.get(project.remote) or mapping.get(project.slug)
    if not folder:
        return ""
    name = folder.split("/")[-1]
    i18n.use_for(clone, config.get("MEMEX_LANG"))
    try:
        page = parse_page(f"{folder}/{name}.md", (clone / folder / f"{name}.md").read_text(encoding="utf-8"))
    except OSError:
        return ""
    limit = max(config.int("MEMEX_HOOK_MAX_CHARS"), 800)
    related = ""
    if config.int("MEMEX_HOOK_RELATED") > 0:
        try:
            # At most a quarter of the room: a living page within its limit is then printed whole.
            related = related_block(load_wiki(clone), folder, config.int("MEMEX_HOOK_RELATED"), limit // 4)
        except Exception:
            related = ""  # the page matters more than its neighbours
    footer = (
        f"\n\n---\n{related}"
        f"Source: memex wiki, read-only ({tilde(clone)}). This project's page: `{folder}/{name}.md`; "
        f"detailed decision records: `{folder}/{i18n.current().decisions}/`. Index of the whole wiki: `index.md`. "
        "What is decided in this session will be recorded there automatically."
    )
    header = f'# Memory of project "{page.title}" (from earlier sessions)\n\n'
    body = page.body.strip()
    room = max(limit - len(header) - len(footer), 0)
    if len(body) > room:
        body = body[:room].rsplit("\n", 1)[0] + "\n\n[… page truncated: read the file for the rest …]"
    return header + body + footer


def session_start(config: Config, raw: str) -> str:
    data = _input(raw)
    spawn_sync(config)
    try:
        return project_context(config, str(data.get("cwd") or os.getcwd()))
    except Exception:
        return ""  # a hook must never get in the way of a session starting


def session_end(config: Config, raw: str) -> None:
    data = _input(raw)
    session = str(data.get("session_id") or data.get("sessionId") or "")
    spawn_sync(config, (session,) if session else (), throttle=False)
