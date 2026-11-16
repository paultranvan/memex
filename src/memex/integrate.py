"""Branchement de memex sur Claude Code et Codex : hooks et pointeur vers le wiki.

Tout est idempotent et réversible : les entrées ajoutées se reconnaissent à la
commande `memex hook`, et le pointeur tient entre deux balises.
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
BEGIN = "<!-- memex:begin (bloc géré par memex : ne pas modifier entre ces balises) -->"
END = "<!-- memex:end -->"
_BLOCK = re.compile(r"\n*<!-- memex:begin.*?<!-- memex:end -->\n*", re.S)


def pointer(config: Config) -> str:
    wiki = tilde(config.wiki_clone)
    return f"""{BEGIN}
## Mémoire partagée (memex)

Une base de connaissance tirée de mes sessions passées, tous agents et toutes machines confondus, se trouve dans `{wiki}/`. Elle est en lecture seule.

- Avant de proposer un choix structurant, ou quand je demande pourquoi quelque chose a été décidé, consulte-la : commence par `{wiki}/index.md`, cherche ensuite dans les fichiers, puis suis les liens `[[…]]`.
- Chaque projet a une page vivante `Projets/<Nom>/<Nom>.md` et des fiches dans `Projets/<Nom>/Décisions/`. Les sujets hors code sont dans `Sujets/`, les notions communes dans `Concepts/`.
- N'y écris jamais. Ce que nous décidons ici y est reporté automatiquement après la session. Si je dis « note cette décision », confirme simplement qu'elle sera consignée.
- Pour consigner quelque chose tout de suite, hors de toute session : `memex note "texte"`.
{END}
"""


def _backup(path: Path) -> None:
    if path.exists():
        shutil.copyfile(path, path.with_name(path.name + ".memex-bak"))


def set_pointer(path: Path, block: Optional[str]) -> bool:
    """Pose (ou retire, si `block` est None) le bloc memex dans un fichier d'instructions."""
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
        return ["Claude Code : dossier de configuration absent, rien à faire"]
    settings_path = folder / "settings.json"
    if settings_path.exists() and not _load_json(settings_path):
        return [f"Claude Code : {settings_path} illisible, laissé intact"]
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
        done.append("Claude Code : hooks SessionStart/SessionEnd " + ("installés" if install else "retirés"))
    if set_pointer(folder / "CLAUDE.md", pointer(config) if install else None):
        done.append("Claude Code : pointeur " + ("ajouté à" if install else "retiré de") + " CLAUDE.md")
    return done or ["Claude Code : déjà à jour"]


def codex(config: Config, binary: str, install: bool = True) -> list[str]:
    done = []
    folder = codex_dir()
    if not folder.is_dir():
        return ["Codex : dossier de configuration absent, rien à faire"]
    hooks_path = folder / "hooks.json"
    if hooks_path.exists() and not _load_json(hooks_path):
        return [f"Codex : {hooks_path} illisible, laissé intact"]
    data = _load_json(hooks_path)
    hooks = data.setdefault("hooks", {})
    if _set_hooks(hooks, {"SessionStart": f"{binary} hook session-start" if install else None}):
        _backup(hooks_path)
        atomic_write(hooks_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        done.append("Codex : hook SessionStart " + ("installé (Codex demandera de l'approuver au prochain lancement)" if install else "retiré"))
    if set_pointer(folder / "AGENTS.md", pointer(config) if install else None):
        done.append("Codex : pointeur " + ("ajouté à" if install else "retiré de") + " AGENTS.md")
    return done or ["Codex : déjà à jour"]


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
        "claude_pointeur": pointed(claude_dir() / "CLAUDE.md"),
        "codex_hooks": has(codex_dir() / "hooks.json"),
        "codex_pointeur": pointed(codex_dir() / "AGENTS.md"),
    }


__all__ = ["claude", "codex", "status", "pointer", "set_pointer", "time"]
