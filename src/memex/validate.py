"""Garde-fous : ce qu'un agent a le droit de changer, vérifié sur le diff et non sur parole.

Chaque rôle a son périmètre. Ce qui est cosmétique est corrigé d'office
(en-tête incomplet) ; ce qui est hors périmètre mais sans danger est annulé ;
ce qui est destructif fait échouer le passage.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import frontmatter
from .pages import (
    CONCEPTS, CONTENT_DIRS, JOURNAL, MANAGED_FILES, PROTECTED_FILES, infer_type, is_decision_path, is_hidden,
    parse_page, unlink,
)
from .util import atomic_write
from .wikirepo import WikiRepo

ROLES = ("archiviste", "tisseur", "verificateur")
DECISION_MUTABLE = {"statut", "remplacee_par", "maj", "sources", "aliases", "entites"}
TISSEUR_MUTABLE = {"entites", "aliases", "maj", "liens"}
_SEE_ALSO = re.compile(r"(?ims)^#{2,3} +voir aussi\b.*?(?=^#{1,3} |\Z)")


@dataclass
class Verdict:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    touched: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def changed(self) -> bool:
        return bool(self.touched or self.deleted)

    def feedback(self) -> str:
        return "\n".join(f"- {line}" for line in self.errors)


def normalize_for_links(text: str) -> tuple[dict, str]:
    """Le texte d'une page « aux liens près » : ce que le tisseur n'a pas le droit de changer."""
    meta, body = frontmatter.parse(text)
    meta = {k: v for k, v in meta.items() if k not in TISSEUR_MUTABLE}
    body = _SEE_ALSO.sub("", unlink(body))
    return meta, re.sub(r"\s+", " ", body).strip()


def _decision_change_allowed(old: str, new: str) -> bool:
    old_meta, old_body = frontmatter.parse(old)
    new_meta, new_body = frontmatter.parse(new)
    if old_body.strip() != new_body.strip():
        return False
    keys = set(old_meta) | set(new_meta)
    return all(old_meta.get(k) == new_meta.get(k) for k in keys if k not in DECISION_MUTABLE)


def validate(repo: WikiRepo, cwd: Path, base: str, role: str, today: str, max_page_chars: int = 24000) -> Verdict:
    assert role in ROLES
    verdict = Verdict()
    cwd = Path(cwd)
    repo.stage_all(cwd)

    for status, path in repo.changes(cwd, base):
        top = path.split("/")[0]
        if is_hidden(path) or path in MANAGED_FILES or path in PROTECTED_FILES or top == JOURNAL:
            repo.restore_path(cwd, base, path)
            if not is_hidden(path):
                verdict.warnings.append(f"{path} : réservé, modification annulée")
            continue
        if top not in CONTENT_DIRS or not path.endswith(".md"):
            if status == "A":
                repo.restore_path(cwd, base, path)
                verdict.warnings.append(f"{path} : hors des dossiers du wiki, fichier retiré")
            else:
                verdict.errors.append(f"{path} : ce fichier ne doit pas être modifié")
            continue
        existing_decision = status != "A" and is_decision_path(path)

        if role == "archiviste":
            if status == "D":
                verdict.errors.append(f"{path} : l'archiviste ne supprime ni ne renomme aucune page")
                continue
            if top == CONCEPTS:
                repo.restore_path(cwd, base, path)
                verdict.warnings.append(f"{path} : les pages de concept sont tenues par le tisseur, modification annulée")
                continue
        elif role == "tisseur":
            if status == "D":
                verdict.errors.append(f"{path} : le tisseur ne supprime aucune page")
                continue
            if top != CONCEPTS:
                if status == "A":
                    verdict.errors.append(f"{path} : le tisseur ne crée de pages que dans {CONCEPTS}/")
                    continue
                if existing_decision:
                    verdict.errors.append(f"{path} : une fiche de décision ne se modifie pas")
                    continue
                old = repo.show(base, path, cwd=cwd) or ""
                new = (cwd / path).read_text(encoding="utf-8")
                if normalize_for_links(old) != normalize_for_links(new):
                    verdict.errors.append(
                        f"{path} : le tisseur a changé le texte ; il ne peut qu'ajouter des liens et une section « Voir aussi »"
                    )
                    continue
        elif role == "verificateur":
            if existing_decision:
                verdict.errors.append(f"{path} : une fiche de décision ne se modifie ni ne se supprime")
                continue

        if status == "D":
            verdict.deleted.append(path)
            continue
        if existing_decision:
            old = repo.show(base, path, cwd=cwd) or ""
            new = (cwd / path).read_text(encoding="utf-8")
            if not _decision_change_allowed(old, new):
                verdict.errors.append(
                    f"{path} : une fiche de décision existante est immuable (seuls `statut` et `remplacee_par` peuvent changer)"
                )
                continue

        # ── page ajoutée ou modifiée : en-tête minimal, corrigé d'office ──
        try:
            text = (cwd / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            verdict.errors.append(f"{path} : fichier illisible")
            continue
        page = parse_page(path, text)
        if not page.body.strip():
            verdict.errors.append(f"{path} : page vide")
            continue
        fixed = dict(page.meta)
        if not fixed.get("type"):
            fixed = {"type": infer_type(path), **fixed}
        if not fixed.get("titre"):
            fixed["titre"] = page.title
        if is_decision_path(path):
            fixed.setdefault("date", today)
            fixed.setdefault("statut", "active")
        if fixed != page.meta:
            atomic_write(cwd / path, frontmatter.dump(fixed, page.body))
        if not fixed.get("resume") and not is_decision_path(path):
            verdict.warnings.append(f"{path} : pas de champ `resume` dans l'en-tête")
        if len(page.body) > max_page_chars:
            verdict.warnings.append(f"{path} : page longue ({len(page.body)} caractères)")
        verdict.touched.append(path)
        if status == "A":
            verdict.added.append(path)

    repo.stage_all(cwd)
    return verdict
