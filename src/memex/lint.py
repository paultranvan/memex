"""Deterministic checks of the wiki: structure, links, and leads for the weaver.

No LLM here. The report serves the curator (what is wrong) and the weaver
(missing links, concepts that would deserve a page).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import i18n
from .index import living_pages, main_page
from .pages import LinkIndex, Page, _CODE, content_pages, iter_files, links, load_wiki, unlink
from .util import slugify

_STOP = {
    "notes", "todo", "index", "projet", "project", "sujet", "topic", "tests", "test", "docs", "divers", "misc",
    "général", "general",
}


@dataclass
class LintReport:
    problems: list[dict] = field(default_factory=list)
    broken: list[dict] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    unlinked: list[dict] = field(default_factory=list)
    concepts: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "problems": self.problems, "broken_links": self.broken, "orphans": self.orphans,
            "unlinked_mentions": self.unlinked, "concept_candidates": self.concepts, "stats": self.stats,
        }

    @property
    def has_link_work(self) -> bool:
        return bool(self.unlinked or self.concepts or self.broken)


def _visible(body: str) -> str:
    return _CODE.sub(" ", body)


def lint_wiki(config, root: Path) -> LintReport:
    """`lint` with the limits of the configuration."""
    return lint(
        root, config.int("MEMEX_MAX_PAGE_CHARS"), config.int("MEMEX_WEAVER_MIN_FOLDERS"),
        config.int("MEMEX_LIVING_PAGE_CHARS"),
    )


def lint(root: Path, max_page_chars: int = 12000, min_folders: int = 2, living_page_chars: int = 0) -> LintReport:
    loc = i18n.current()
    report = LintReport()
    all_pages = load_wiki(Path(root))
    pages = content_pages(all_pages)
    living = living_pages(pages) if living_page_chars else set()
    link_index = LinkIndex(list(iter_files(Path(root))))
    inbound: dict[str, set[str]] = {path: set() for path in pages}

    for path, page in sorted(pages.items()):
        if not page.meta.get(i18n.K_TYPE):
            report.problems.append({"page": path, "problem": f"header without `{i18n.K_TYPE}` field"})
        if not page.summary and not page.is_decision:
            report.problems.append({"page": path, "problem": f"header without `{loc.k_summary}` field"})
        if page.is_decision and not page.meta.get(i18n.K_DATE):
            report.problems.append({"page": path, "problem": f"decision record without `{i18n.K_DATE}`"})
        if path in living and len(page.body) > living_page_chars:
            report.problems.append({
                "page": path,
                "problem": f"living page too long ({len(page.body)} characters, {living_page_chars} at most)",
            })
        elif path not in living and len(page.body) > max_page_chars:
            report.problems.append({
                "page": path, "problem": f"page too long ({len(page.body)} characters, {max_page_chars} at most)",
            })
        for target, _label in links(page.body):
            resolved = link_index.resolve(target)
            if resolved is None:
                report.broken.append({"page": path, "target": target})
            elif resolved in inbound and resolved != path:
                inbound[resolved].add(path)

    mains = {}
    for folder in {p.folder for p in pages.values()}:
        main = main_page(folder, pages)
        if main:
            mains[folder] = main.path
    for path, page in sorted(pages.items()):
        if path in mains.values():
            continue  # main pages are reached through the index
        if not inbound[path]:
            report.orphans.append(path)

    # ── mentions of an existing page with no link to it ───────────────────
    targets: list[tuple[str, str]] = []
    for path, page in pages.items():
        if page.is_decision:
            continue
        if path in mains.values() or path.split("/")[0] == loc.concepts:
            for term in {page.title, page.stem, *page.aliases}:
                term = term.strip()
                if len(term) >= 4 and term.lower() not in _STOP:
                    targets.append((term, path))
    for path, page in sorted(pages.items()):
        if page.is_decision:
            continue
        linked = {link_index.resolve(target) for target, _ in links(page.body)}
        text = unlink(_visible(page.body))
        seen: set[str] = set()
        for term, target in sorted(targets):
            if target == path or target in linked or target in seen:
                continue
            if page.folder == pages[target].folder and target in mains.values():
                continue  # a page need not point to the main page of its own folder
            if re.search(r"(?<![\wÀ-ÿ-])" + re.escape(term) + r"(?![\wÀ-ÿ-])", text, re.I):
                seen.add(target)
                report.unlinked.append({"page": path, "target": target, "term": term})

    # ── entities cited in several folders with no page of their own ───────
    known = set()
    for page in pages.values():
        for term in (page.title, page.stem, *page.aliases):
            known.add(slugify(term))
    seen_entities: dict[str, dict] = {}
    for path, page in pages.items():
        for entity in page.entities:
            key = slugify(entity)
            if not key or key in known or len(key) < 3:
                continue
            item = seen_entities.setdefault(key, {"entity": entity, "folders": set(), "pages": []})
            item["folders"].add(page.folder)
            item["pages"].append(path)
    for key in sorted(seen_entities):
        item = seen_entities[key]
        if len(item["folders"]) >= min_folders:
            report.concepts.append({
                "entity": item["entity"], "folders": sorted(item["folders"]), "pages": sorted(item["pages"])[:12],
                "page_count": len(item["pages"]),
            })
    # The most widely shared first: the weaver only handles a few of them per run.
    report.concepts.sort(key=lambda c: (-len(c["folders"]), -c["page_count"], slugify(c["entity"])))

    report.stats = {
        "pages": len(pages),
        "decisions": sum(1 for p in pages.values() if p.is_decision),
        "concepts": sum(1 for p in pages if p.split("/")[0] == loc.concepts),
        "folders": len({p.folder for p in pages.values() if p.folder and p.folder != loc.concepts}),
    }
    return report


def summary(report: LintReport) -> str:
    stats = report.stats
    return (
        f"{stats.get('pages', 0)} pages, {stats.get('folders', 0)} folders, {stats.get('decisions', 0)} decisions; "
        f"{len(report.problems)} problem(s), {len(report.broken)} broken link(s), {len(report.orphans)} orphan(s), "
        f"{len(report.unlinked)} unlinked mention(s), {len(report.concepts)} concept candidate(s)"
    )
