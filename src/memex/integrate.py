"""Wiring memex into Claude Code and Codex: hooks and a pointer to the wiki.

Everything is idempotent and reversible: the entries added are recognised by
the `memex hook` command, and the pointer sits between two markers.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from .config import Config
from .util import atomic_write, tilde

MARK = "memex hook"
BEGIN = "<!-- memex:begin (block managed by memex: do not edit between these markers) -->"
END = "<!-- memex:end -->"
_BLOCK = re.compile(r"\n*<!-- memex:begin.*?<!-- memex:end -->\n*", re.S)


def pointer(config: Config) -> str:
    wiki = tilde(config.wiki_clone)
    return f"""{BEGIN}
## Shared memory (memex)

A knowledge base built from my past sessions, across all agents and all machines, lives in `{wiki}/`. \
It is read-only.

- Before proposing a structural choice, or when I ask why something was decided, consult it: \
start with `{wiki}/index.md`, then search the files and follow the `[[…]]` links.
- Each project and each topic has a living page and dated decision records; \
cross-cutting notions have their own pages. All of them are listed in `index.md`.
- Never write there. What we decide here is recorded there automatically after the session. \
If I say "note this decision", just confirm that it will be recorded.
- To record something right away, outside any session: `memex note "text"`.
{END}
"""


def _backup(path: Path) -> None:
    if path.exists():
        shutil.copyfile(path, path.with_name(path.name + ".memex-bak"))


def set_pointer(path: Path, block: Optional[str]) -> bool:
    """Sets (or removes, if `block` is None) the memex block in an instructions file."""
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        current = ""
    stripped = _BLOCK.sub("\n\n", current).strip("\n")
    wanted = (stripped + "\n\n" + block if stripped else block) if block else (stripped + "\n" if stripped else "")
    if wanted == current:
        return False
    if not wanted and not path.exists():
        return False
    _backup(path)
    atomic_write(path, wanted)
    return True


def _is_ours(group: dict) -> bool:
    return any(MARK in str(hook.get("command", "")) for hook in group.get("hooks", []) if isinstance(hook, dict))


def _set_hooks(hooks: dict, wanted: dict[str, Optional[str]]) -> bool:
    changed = False
    for event, command in wanted.items():
        groups = [g for g in hooks.get(event, []) if isinstance(g, dict)]
        kept = [g for g in groups if not _is_ours(g)]
        if command:
            kept.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
        if kept != hooks.get(event, []):
            changed = True
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    return changed


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()


def codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()


def claude(config: Config, binary: str, install: bool = True) -> list[str]:
    done = []
    folder = claude_dir()
    if not folder.is_dir():
        return ["Claude Code: no configuration folder, nothing to do"]
    settings_path = folder / "settings.json"
    if settings_path.exists() and not _load_json(settings_path):
        return [f"Claude Code: {settings_path} is unreadable, left untouched"]
    settings = _load_json(settings_path)
    hooks = settings.setdefault("hooks", {})
    changed = _set_hooks(hooks, {
        "SessionStart": f"{binary} hook session-start" if install else None,
        "SessionEnd": f"{binary} hook session-end" if install else None,
    })
    if not hooks:
        settings.pop("hooks", None)
    rule = f"Read({tilde(config.wiki_clone)}/**)"
    permissions = settings.setdefault("permissions", {})
    allow = [r for r in permissions.get("allow", []) if r != rule]
    if install:
        allow.append(rule)
    if allow != permissions.get("allow", []):
        changed = True
    if allow:
        permissions["allow"] = allow
    else:
        permissions.pop("allow", None)
    if not permissions:
        settings.pop("permissions", None)
    if changed:
        _backup(settings_path)
        atomic_write(settings_path, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
        done.append("Claude Code: SessionStart/SessionEnd hooks " + ("installed" if install else "removed"))
    if set_pointer(folder / "CLAUDE.md", pointer(config) if install else None):
        done.append("Claude Code: pointer " + ("added to" if install else "removed from") + " CLAUDE.md")
    return done or ["Claude Code: already up to date"]


def codex(config: Config, binary: str, install: bool = True) -> list[str]:
    done = []
    folder = codex_dir()
    if not folder.is_dir():
        return ["Codex: no configuration folder, nothing to do"]
    hooks_path = folder / "hooks.json"
    if hooks_path.exists() and not _load_json(hooks_path):
        return [f"Codex: {hooks_path} is unreadable, left untouched"]
    data = _load_json(hooks_path)
    hooks = data.setdefault("hooks", {})
    if _set_hooks(hooks, {"SessionStart": f"{binary} hook session-start" if install else None}):
        _backup(hooks_path)
        atomic_write(hooks_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        done.append("Codex: SessionStart hook " + (
            "installed (Codex will ask you to approve it the next time it starts)" if install else "removed"
        ))
    if set_pointer(folder / "AGENTS.md", pointer(config) if install else None):
        done.append("Codex: pointer " + ("added to" if install else "removed from") + " AGENTS.md")
    return done or ["Codex: already up to date"]


def status(config: Config) -> dict[str, bool]:
    def has(path: Path) -> bool:
        try:
            return MARK in path.read_text(encoding="utf-8")
        except OSError:
            return False

    def pointed(path: Path) -> bool:
        try:
            return "memex:begin" in path.read_text(encoding="utf-8")
        except OSError:
            return False

    return {
        "claude_hooks": has(claude_dir() / "settings.json"),
        "claude_pointer": pointed(claude_dir() / "CLAUDE.md"),
        "codex_hooks": has(codex_dir() / "hooks.json"),
        "codex_pointer": pointed(codex_dir() / "AGENTS.md"),
    }


__all__ = ["claude", "codex", "status", "pointer", "set_pointer", "time"]
