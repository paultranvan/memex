"""Wiki page model: types, front matter, Obsidian links."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from . import frontmatter, i18n

MANAGED_FILES = ("index.md", "log.md")        # kept by the code, never by an agent
PROTECTED_FILES = ("AGENTS.md",)              # conventions: only the human may edit them

_LINK = re.compile(r"!?\[\[([^\]\|#\n]+)(#[^\]\|\n]*)?(?:\|([^\]\n]*))?\]\]")
_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
_H1 = re.compile(r"(?m)^# +(.+?)\s*$")


@dataclass
class Page:
    path: str                      # relative to the wiki root, / as separator
    meta: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    @property
    def stem(self) -> str:
        return Path(self.path).stem

    @property
    def kind(self) -> str:
        return str(self.meta.get(i18n.K_TYPE) or infer_type(self.path))

    @property
    def title(self) -> str:
        key = i18n.current().k_title
        if self.meta.get(key):
            return str(self.meta[key])
        match = _H1.search(self.body)
        return match.group(1) if match else self.stem

    @property
    def summary(self) -> str:
        return " ".join(str(self.meta.get(i18n.current().k_summary) or "").split())

    @property
    def aliases(self) -> list[str]:
        return frontmatter.as_list(self.meta.get(i18n.K_ALIASES))

    @property
    def entities(self) -> list[str]:
        return frontmatter.as_list(self.meta.get(i18n.current().k_entities))

    @property
    def folder(self) -> str:
        return folder_of(self.path)

    @property
    def is_decision(self) -> bool:
        return is_decision_path(self.path) or self.meta.get(i18n.K_TYPE) == i18n.current().type("decision")

    def render(self) -> str:
        return frontmatter.dump(self.meta, self.body)


def parse_page(path: str, text: str) -> Page:
    meta, body = frontmatter.parse(text)
    return Page(path=path, meta=meta, body=body)


def is_decision_path(path: str) -> bool:
    return f"/{i18n.current().decisions}/" in "/" + path


def folder_of(path: str) -> str:
    """`Projects/Solitaire/Decisions/x.md` → `Projects/Solitaire`; `Concepts/x.md` → `Concepts`."""
    loc = i18n.current()
    parts = path.split("/")
    if parts[0] in (loc.projects, loc.topics):
        if len(parts) >= 3:
            return "/".join(parts[:2])
        return parts[0] + "/" + Path(parts[-1]).stem
    return parts[0] if len(parts) > 1 else ""


def infer_type(path: str) -> str:
    loc = i18n.current()
    top = path.split("/")[0]
    if is_decision_path(path):
        return loc.type("decision")
    if top == loc.projects:
        return loc.type("project")
    if top == loc.topics:
        return loc.type("topic")
    if top == loc.concepts:
        return loc.type("concept")
    if top == loc.journal:
        return loc.type("journal")
    return loc.type("meta")


def is_hidden(relative: str) -> bool:
    return any(part.startswith(".") for part in relative.split("/"))


def iter_files(root: Path) -> Iterator[str]:
    """Every file that is not hidden, as sorted relative paths."""
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
    content_dirs = i18n.current().content_dirs
    return {p: page for p, page in pages.items() if p.split("/")[0] in content_dirs}


def links(body: str) -> list[tuple[str, str]]:
    """Targets of the `[[target|label]]` links, code blocks excluded."""
    stripped = _CODE.sub("", body)
    return [(m.group(1).strip(), (m.group(3) or "").strip()) for m in _LINK.finditer(stripped)]


def unlink(text: str) -> str:
    """Replaces each link with its visible label: used to compare two texts "links aside"."""
    def visible(match: re.Match) -> str:
        return (match.group(3) or "").strip() or Path(match.group(1).strip()).name
    return _LINK.sub(visible, text)


class LinkIndex:
    """Resolves links the way Obsidian does: exact path first, file name otherwise."""

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
    """Obsidian link. With a path, the label is always given: otherwise Obsidian shows the whole path."""
    target = path[:-3] if path.endswith(".md") else path
    label = (label or Path(target).name).replace("|", "-").replace("]", ")").replace("[", "(")
    return f"[[{target}|{label}]]" if "/" in target or label != target else f"[[{target}]]"


_UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]]')


def safe_name(name: str) -> str:
    """A file name Obsidian accepts on every platform (Android and Windows reject `:`)."""
    stem, dot, ext = name.rpartition(".")
    stem = stem if dot else name
    stem = re.sub(r"\s*:\s*", " - ", stem)
    stem = re.sub(r"\s+", " ", _UNSAFE.sub(" ", stem)).strip(" .") or i18n.current().t("name.untitled")
    return f"{stem[:120].rstrip(' .')}.{ext}" if dot else stem
