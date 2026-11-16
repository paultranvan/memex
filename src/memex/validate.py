"""Garde-fous : ce qu'un agent a le droit de changer, vérifié sur le diff et non sur parole.

Chaque rôle a son périmètre. Ce qui est cosmétique est corrigé d'office
(en-tête incomplet) ; ce qui est hors périmètre mais sans danger est annulé ;
ce qui est destructif fait échouer le passage.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import frontmatter
from .pages import (
    CONCEPTS, CONTENT_DIRS, JOURNAL, MANAGED_FILES, PROTECTED_FILES, LinkIndex, infer_type, is_decision_path,
    is_hidden, links, parse_page, safe_name, unlink,
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


def _lost_links(old: str, new: str, known) -> list[str]:
    """Liens présents avant et absents après. Corriger un lien qui ne menait nulle part reste permis."""
    def targets(text: str) -> Counter:
        body = _SEE_ALSO.sub("", frontmatter.parse(text)[1])
        return Counter(target.lower() for target, _label in links(body))

    before, after = targets(old), targets(new)
    lost = []
    for target, count in before.items():
        if after.get(target, 0) < count and (known is None or known.resolve(target) is not None):
            lost.append(target)
    return sorted(lost)


def _decision_change_allowed(old: str, new: str) -> bool:
    old_meta, old_body = frontmatter.parse(old)
    new_meta, new_body = frontmatter.parse(new)
    if old_body.strip() != new_body.strip():
        return False
    keys = set(old_meta) | set(new_meta)
    return all(old_meta.get(k) == new_meta.get(k) for k in keys if k not in DECISION_MUTABLE)


def _fix_names(repo: WikiRepo, cwd: Path, base: str, verdict: Verdict) -> None:
    """Un fichier ajouté avec un caractère refusé par Obsidian mobile (`:`…) est renommé, et les liens suivent."""
    renames = {}
    for status, path in repo.changes(cwd, base):
        if status != "A" or path.split("/")[0] not in CONTENT_DIRS or not path.endswith(".md"):
            continue
        parts = path.split("/")
        fixed = "/".join([safe_name(part) for part in parts[:-1]] + [safe_name(parts[-1])])
        if fixed != path and not (cwd / fixed).exists():
            (cwd / fixed).parent.mkdir(parents=True, exist_ok=True)
            (cwd / path).rename(cwd / fixed)
            renames[path[:-3]] = fixed[:-3]
            verdict.warnings.append(f"{path} : renommé en « {fixed.split('/')[-1]} » (caractère refusé sur mobile)")
    if not renames:
        return
    for status, path in repo.changes(cwd, base) + [("A", new + ".md") for new in renames.values()]:
        target = cwd / path
        if status == "D" or not path.endswith(".md") or not target.is_file():
            continue
        text = original = target.read_text(encoding="utf-8")
        for old, new in renames.items():
            text = text.replace(f"[[{old}", f"[[{new}")
            old_name, new_name = old.split("/")[-1], new.split("/")[-1]
            text = text.replace(f"[[{old_name}|", f"[[{new_name}|").replace(f"[[{old_name}]]", f"[[{new_name}]]")
        if text != original:
            target.write_text(text, encoding="utf-8")
    repo.stage_all(cwd)


def validate(repo: WikiRepo, cwd: Path, base: str, role: str, today: str, max_page_chars: int = 24000) -> Verdict:
    assert role in ROLES
    verdict = Verdict()
    cwd = Path(cwd)
    repo.heal_worktree(cwd)
    repo.stage_all(cwd)
    _fix_names(repo, cwd, base, verdict)
    known = LinkIndex(repo.git("ls-tree", "-r", "--name-only", "-z", base, cwd=cwd).split("\0")) if role == "tisseur" else None

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
                lost = _lost_links(old, new, known)
                if lost:
                    verdict.errors.append(
                        f"{path} : le tisseur a retiré ou redirigé un lien existant ({', '.join(lost[:3])}) ; "
                        "seul un lien cassé peut être corrigé"
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
        missing: dict = {}
        if not page.meta.get("type"):
            missing["type"] = infer_type(path)
        if not page.meta.get("titre"):
            missing["titre"] = page.title
        if is_decision_path(path):
            if not page.meta.get("date"):
                missing["date"] = today
            if not page.meta.get("statut"):
                missing["statut"] = "active"
        if missing:
            atomic_write(cwd / path, frontmatter.set_keys(text, missing))
        if not page.meta.get("resume") and not is_decision_path(path):
            verdict.warnings.append(f"{path} : pas de champ `resume` dans l'en-tête")
        if len(page.body) > max_page_chars:
            verdict.warnings.append(f"{path} : page longue ({len(page.body)} caractères)")
        verdict.touched.append(path)
        if status == "A":
            verdict.added.append(path)

    repo.stage_all(cwd)
    return verdict
