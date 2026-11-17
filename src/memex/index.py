"""Wiki index, rebuilt by the code on every run (never by an agent)."""
from __future__ import annotations

from pathlib import Path

from . import i18n
from .pages import Page, content_pages, wikilink
from .util import atomic_write


def _line(page: Page, indent: str = "") -> str:
    summary = f" — {page.summary}" if page.summary else ""
    return f"{indent}- {wikilink(page.path, page.title)}{summary}"


def main_page(folder: str, pages: dict[str, Page]) -> Page | None:
    name = folder.split("/")[-1]
    exact = pages.get(f"{folder}/{name}.md") or pages.get(f"{folder}.md")
    if exact:
        return exact
    roots = [
        p for path, p in sorted(pages.items())
        if p.folder == folder and not p.is_decision and path.count("/") <= 2
    ]
    return roots[0] if roots else None


def folders(pages: dict[str, Page], top: str) -> list[str]:
    return sorted({p.folder for p in pages.values() if p.path.split("/")[0] == top and p.folder}, key=str.lower)


def build(pages: dict[str, Page]) -> str:
    loc = i18n.current()
    active_status = loc.status("active")
    pages = content_pages(pages)
    out = [loc.t("index.header")]
    for top, label in ((loc.projects, loc.t("index.projects")), (loc.topics, loc.t("index.topics"))):
        out.append(f"\n## {label}\n")
        names = folders(pages, top)
        if not names:
            out.append(loc.t("index.none"))
        for folder in names:
            members = [p for _, p in sorted(pages.items()) if p.folder == folder]
            main = main_page(folder, pages)
            decisions = [p for p in members if p.is_decision]
            active = sum(1 for p in decisions if str(p.meta.get(loc.k_status, active_status)).lower() == active_status)
            tail = loc.t("index.decisions", count=len(decisions), active=active) if decisions else ""
            if main:
                out.append(_line(main) + tail)
            else:
                out.append(f"- {folder.split('/')[-1]}{tail}")
            for page in members:
                if page is not main and not page.is_decision:
                    out.append(_line(page, "  "))
    out.append(f"\n## {loc.t('index.concepts')}\n")
    concepts = [p for _, p in sorted(pages.items()) if p.path.split("/")[0] == loc.concepts]
    out.extend(_line(p) for p in concepts) if concepts else out.append(loc.t("index.none"))
    decisions = sorted(
        (p for p in pages.values() if p.is_decision),
        key=lambda p: (str(p.meta.get(i18n.K_DATE) or ""), p.path), reverse=True,
    )
    if decisions:
        out.append(f"\n## {loc.t('index.recent')}\n")
        for page in decisions[:25]:
            status = str(p_status(page))
            mark = "" if status == active_status else f" _({status})_"
            date = page.meta.get(i18n.K_DATE) or "????-??-??"
            out.append(f"- {date} — {wikilink(page.path, page.title)} · {page.folder.split('/')[-1]}{mark}")
    return "\n".join(out).rstrip() + "\n"


def p_status(page: Page) -> str:
    loc = i18n.current()
    return str(page.meta.get(loc.k_status) or loc.status("active")).lower()


def write(root: Path, pages: dict[str, Page]) -> bool:
    target = Path(root) / "index.md"
    text = build(pages)
    try:
        if target.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    atomic_write(target, text)
    return True


def compact(pages: dict[str, Page], limit: int = 12000, long_page: int = 0) -> str:
    """Short list of the pages (path, title, summary) given to the agents to find their way around.

    `long_page`: above this size, the page is flagged as already long.
    """
    loc = i18n.current()
    lines = []
    for path, page in sorted(content_pages(pages).items()):
        if page.is_decision:
            continue
        size = loc.t("compact.long", count=len(page.body)) if long_page and len(page.body) > long_page else ""
        summary = loc.t("compact.summary", summary=page.summary) if page.summary else ""
        lines.append(f"- {path} — {page.title}{size}{summary}")
    for folder in sorted({p.folder for p in content_pages(pages).values() if p.is_decision}):
        count = sum(1 for p in pages.values() if p.is_decision and p.folder == folder)
        lines.append(loc.t("compact.decisions", folder=folder, decisions=loc.decisions, count=count))
    text = "\n".join(lines) or loc.t("compact.empty")
    return text if len(text) <= limit else text[:limit] + loc.t("compact.truncated")
