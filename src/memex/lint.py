"""Contrôles déterministes du wiki : structure, liens, et pistes pour le tisseur.

Aucun LLM ici. Le rapport sert au vérificateur (ce qui cloche) et au tisseur
(les liens manquants, les concepts qui mériteraient une page).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .index import main_page
from .pages import CONCEPTS, LinkIndex, Page, _CODE, content_pages, iter_files, links, load_wiki, unlink
from .util import slugify

_STOP = {"notes", "todo", "index", "projet", "sujet", "tests", "test", "docs", "divers", "général", "general"}


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
            "problemes": self.problems, "liens_casses": self.broken, "orphelines": self.orphans,
            "mentions_sans_lien": self.unlinked, "concepts_candidats": self.concepts, "stats": self.stats,
        }

    @property
    def has_link_work(self) -> bool:
        return bool(self.unlinked or self.concepts or self.broken)


def _visible(body: str) -> str:
    return _CODE.sub(" ", body)


def lint(root: Path, max_page_chars: int = 24000, min_dossiers: int = 2) -> LintReport:
    report = LintReport()
    all_pages = load_wiki(Path(root))
    pages = content_pages(all_pages)
    link_index = LinkIndex(list(iter_files(Path(root))))
    inbound: dict[str, set[str]] = {path: set() for path in pages}

    for path, page in sorted(pages.items()):
        if not page.meta.get("type"):
            report.problems.append({"page": path, "probleme": "en-tête sans champ `type`"})
        if not page.summary and not page.is_decision:
            report.problems.append({"page": path, "probleme": "en-tête sans champ `resume`"})
        if page.is_decision and not page.meta.get("date"):
            report.problems.append({"page": path, "probleme": "fiche de décision sans `date`"})
        if len(page.body) > max_page_chars:
            report.problems.append({"page": path, "probleme": f"page trop longue ({len(page.body)} caractères)"})
        for target, _label in links(page.body):
            resolved = link_index.resolve(target)
            if resolved is None:
                report.broken.append({"page": path, "cible": target})
            elif resolved in inbound and resolved != path:
                inbound[resolved].add(path)

    mains = {}
    for dossier in {p.dossier for p in pages.values()}:
        main = main_page(dossier, pages)
        if main:
            mains[dossier] = main.path
    for path, page in sorted(pages.items()):
        if path in mains.values():
            continue  # les pages principales sont atteintes par l'index
        if not inbound[path]:
            report.orphans.append(path)

    # ── mentions d'une page existante sans lien vers elle ─────────────────
    targets: list[tuple[str, str]] = []
    for path, page in pages.items():
        if page.is_decision:
            continue
        if path in mains.values() or path.split("/")[0] == CONCEPTS:
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
            if page.dossier == pages[target].dossier and target in mains.values():
                continue  # une page n'a pas à pointer vers la page principale de son propre dossier
            if re.search(r"(?<![\wÀ-ÿ-])" + re.escape(term) + r"(?![\wÀ-ÿ-])", text, re.I):
                seen.add(target)
                report.unlinked.append({"page": path, "cible": target, "terme": term})

    # ── entités citées dans plusieurs dossiers sans page à elles ──────────
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
            item = seen_entities.setdefault(key, {"entite": entity, "dossiers": set(), "pages": []})
            item["dossiers"].add(page.dossier)
            item["pages"].append(path)
    for key in sorted(seen_entities):
        item = seen_entities[key]
        if len(item["dossiers"]) >= min_dossiers:
            report.concepts.append({
                "entite": item["entite"], "dossiers": sorted(item["dossiers"]), "pages": sorted(item["pages"])[:12],
            })

    report.stats = {
        "pages": len(pages),
        "decisions": sum(1 for p in pages.values() if p.is_decision),
        "concepts": sum(1 for p in pages if p.split("/")[0] == CONCEPTS),
        "dossiers": len({p.dossier for p in pages.values() if p.dossier and p.dossier != CONCEPTS}),
    }
    return report


def summary(report: LintReport) -> str:
    stats = report.stats
    return (
        f"{stats.get('pages', 0)} pages, {stats.get('dossiers', 0)} dossiers, {stats.get('decisions', 0)} décisions ; "
        f"{len(report.problems)} problème(s), {len(report.broken)} lien(s) cassé(s), {len(report.orphans)} orpheline(s), "
        f"{len(report.unlinked)} mention(s) sans lien, {len(report.concepts)} concept(s) candidat(s)"
    )
