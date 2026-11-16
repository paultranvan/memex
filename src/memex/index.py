"""Index du wiki, régénéré par le code à chaque passage (jamais par un agent)."""
from __future__ import annotations

from pathlib import Path

from .pages import CONCEPTS, PROJECTS, TOPICS, Page, content_pages, wikilink
from .util import atomic_write

HEADER = (
    "---\ntype: meta\ntitre: Index\n---\n\n# Index du wiki\n\n"
    "> Généré automatiquement par memex à partir des en-têtes des pages. Ne pas modifier à la main.\n"
)


def _line(page: Page, indent: str = "") -> str:
    summary = f" — {page.summary}" if page.summary else ""
    return f"{indent}- {wikilink(page.path, page.title)}{summary}"


def main_page(dossier: str, pages: dict[str, Page]) -> Page | None:
    name = dossier.split("/")[-1]
    exact = pages.get(f"{dossier}/{name}.md") or pages.get(f"{dossier}.md")
    if exact:
        return exact
    roots = [p for path, p in sorted(pages.items()) if p.dossier == dossier and not p.is_decision and path.count("/") <= 2]
    return roots[0] if roots else None


def dossiers(pages: dict[str, Page], top: str) -> list[str]:
    return sorted({p.dossier for p in pages.values() if p.path.split("/")[0] == top and p.dossier}, key=str.lower)


def build(pages: dict[str, Page]) -> str:
    pages = content_pages(pages)
    out = [HEADER]
    for top, label in ((PROJECTS, "Projets"), (TOPICS, "Sujets")):
        out.append(f"\n## {label}\n")
        names = dossiers(pages, top)
        if not names:
            out.append("_(aucun pour l'instant)_")
        for dossier in names:
            members = [p for _, p in sorted(pages.items()) if p.dossier == dossier]
            main = main_page(dossier, pages)
            decisions = [p for p in members if p.is_decision]
            active = sum(1 for p in decisions if str(p.meta.get("statut", "active")).lower().startswith("activ"))
            tail = f" · {len(decisions)} décision(s), dont {active} active(s)" if decisions else ""
            if main:
                out.append(_line(main) + tail)
            else:
                out.append(f"- {dossier.split('/')[-1]}{tail}")
            for page in members:
                if page is not main and not page.is_decision:
                    out.append(_line(page, "  "))
    out.append("\n## Concepts\n")
    concepts = [p for _, p in sorted(pages.items()) if p.path.split("/")[0] == CONCEPTS]
    out.extend(_line(p) for p in concepts) if concepts else out.append("_(aucun pour l'instant)_")
    decisions = sorted(
        (p for p in pages.values() if p.is_decision),
        key=lambda p: (str(p.meta.get("date") or ""), p.path), reverse=True,
    )
    if decisions:
        out.append("\n## Décisions récentes\n")
        for page in decisions[:25]:
            status = str(p_status(page))
            mark = "" if status.startswith("activ") else f" _({status})_"
            out.append(f"- {page.meta.get('date') or '????-??-??'} — {wikilink(page.path, page.title)} · {page.dossier.split('/')[-1]}{mark}")
    return "\n".join(out).rstrip() + "\n"


def p_status(page: Page) -> str:
    return str(page.meta.get("statut") or "active").lower()


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
    """Liste courte des pages (chemin, titre, résumé) donnée aux agents pour s'orienter.

    `long_page` : au-delà de cette taille, la page est signalée comme déjà longue.
    """
    lines = []
    for path, page in sorted(content_pages(pages).items()):
        if page.is_decision:
            continue
        size = f" [déjà longue : {len(page.body)} caractères]" if long_page and len(page.body) > long_page else ""
        lines.append(f"- {path} — {page.title}{size}" + (f" : {page.summary}" if page.summary else ""))
    for dossier in sorted({p.dossier for p in content_pages(pages).values() if p.is_decision}):
        count = sum(1 for p in pages.values() if p.is_decision and p.dossier == dossier)
        lines.append(f"- {dossier}/Décisions/ — {count} fiche(s) de décision")
    text = "\n".join(lines) or "(le wiki est encore vide)"
    return text if len(text) <= limit else text[:limit] + "\n… (liste tronquée : cherche dans les fichiers)"
