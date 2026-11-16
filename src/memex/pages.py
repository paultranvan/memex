"""Modèle des pages du wiki : types, en-têtes, liens Obsidian."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from . import frontmatter

PROJECTS, TOPICS, CONCEPTS, JOURNAL = "Projets", "Sujets", "Concepts", "Journal"
DECISIONS = "Décisions"
CONTENT_DIRS = (PROJECTS, TOPICS, CONCEPTS)
MANAGED_FILES = ("index.md", "log.md")        # tenus par le code, jamais par un agent
PROTECTED_FILES = ("AGENTS.md",)              # conventions : modifiables par l'humain seulement
TYPES = ("projet", "sujet", "decision", "concept", "synthese", "journal", "meta")

_LINK = re.compile(r"!?\[\[([^\]\|#\n]+)(#[^\]\|\n]*)?(?:\|([^\]\n]*))?\]\]")
_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
_H1 = re.compile(r"(?m)^# +(.+?)\s*$")


@dataclass
class Page:
    path: str                      # relatif à la racine du wiki, séparateur /
    meta: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    @property
    def stem(self) -> str:
        return Path(self.path).stem

    @property
    def kind(self) -> str:
        return str(self.meta.get("type") or infer_type(self.path))

    @property
    def title(self) -> str:
        if self.meta.get("titre"):
            return str(self.meta["titre"])
        match = _H1.search(self.body)
        return match.group(1) if match else self.stem

    @property
    def summary(self) -> str:
        return " ".join(str(self.meta.get("resume") or "").split())

    @property
    def aliases(self) -> list[str]:
        return frontmatter.as_list(self.meta.get("aliases"))

    @property
    def entities(self) -> list[str]:
        return frontmatter.as_list(self.meta.get("entites"))

    @property
    def dossier(self) -> str:
        return dossier_of(self.path)

    @property
    def is_decision(self) -> bool:
        return is_decision_path(self.path) or self.meta.get("type") == "decision"

    def render(self) -> str:
        return frontmatter.dump(self.meta, self.body)


def parse_page(path: str, text: str) -> Page:
    meta, body = frontmatter.parse(text)
    return Page(path=path, meta=meta, body=body)


def is_decision_path(path: str) -> bool:
    return f"/{DECISIONS}/" in "/" + path


def dossier_of(path: str) -> str:
    """`Projets/Solitaire/Décisions/x.md` → `Projets/Solitaire` ; `Concepts/x.md` → `Concepts`."""
    parts = path.split("/")
    if parts[0] in (PROJECTS, TOPICS):
        if len(parts) >= 3:
            return "/".join(parts[:2])
        return parts[0] + "/" + Path(parts[-1]).stem
    return parts[0] if len(parts) > 1 else ""


def infer_type(path: str) -> str:
    top = path.split("/")[0]
    if is_decision_path(path):
        return "decision"
    if top == PROJECTS:
        return "projet"
    if top == TOPICS:
        return "sujet"
    if top == CONCEPTS:
        return "concept"
    if top == JOURNAL:
        return "journal"
    return "meta"


def is_hidden(relative: str) -> bool:
    return any(part.startswith(".") for part in relative.split("/"))


def iter_files(root: Path) -> Iterator[str]:
    """Tous les fichiers non cachés, chemins relatifs triés."""
    root = Path(root)
    found = []
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            relative = path.relative_to(root).as_posix()
            if not is_hidden(relative):
                found.append(relative)
    return iter(sorted(found))


def load_wiki(root: Path) -> dict[str, Page]:
    pages = {}
    for relative in iter_files(root):
        if relative.endswith(".md"):
            try:
                text = (Path(root) / relative).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            pages[relative] = parse_page(relative, text)
    return pages


def content_pages(pages: dict[str, Page]) -> dict[str, Page]:
    return {p: page for p, page in pages.items() if p.split("/")[0] in CONTENT_DIRS}


def links(body: str) -> list[tuple[str, str]]:
    """Cibles des liens `[[cible|libellé]]`, hors blocs de code."""
    stripped = _CODE.sub("", body)
    return [(m.group(1).strip(), (m.group(3) or "").strip()) for m in _LINK.finditer(stripped)]


def unlink(text: str) -> str:
    """Remplace chaque lien par son libellé visible : sert à comparer deux textes « aux liens près »."""
    def visible(match: re.Match) -> str:
        return (match.group(3) or "").strip() or Path(match.group(1).strip()).name
    return _LINK.sub(visible, text)


class LinkIndex:
    """Résolution des liens à la manière d'Obsidian : chemin exact, sinon nom de fichier."""

    def __init__(self, paths: list[str]):
        self.by_path = {p[:-3].lower(): p for p in paths if p.endswith(".md")}
        self.by_name: dict[str, list[str]] = {}
        for path in paths:
            if path.endswith(".md"):
                self.by_name.setdefault(Path(path).stem.lower(), []).append(path)
        self.others = {p.lower() for p in paths if not p.endswith(".md")}

    def resolve(self, target: str) -> Optional[str]:
        key = target.strip().lower()
        if key.endswith(".md"):
            key = key[:-3]
        if key in self.by_path:
            return self.by_path[key]
        if "/" in key:
            for known, path in self.by_path.items():
                if known.endswith("/" + key):
                    return path
            if key in self.others:
                return key
            return None
        names = self.by_name.get(key)
        if names:
            return sorted(names, key=len)[0]
        return key if any(o == key or o.endswith("/" + key) for o in self.others) else None


def wikilink(path: str, label: str = "") -> str:
    target = path[:-3] if path.endswith(".md") else path
    return f"[[{target}|{label}]]" if label and label != Path(target).name else f"[[{target}]]"
