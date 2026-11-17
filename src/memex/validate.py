"""Guardrails: what an agent is allowed to change, checked on the diff and not taken on trust.

Each role has its own scope. What is cosmetic is fixed automatically
(incomplete header); what is out of scope but harmless is reverted;
what is destructive fails the run.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import frontmatter, i18n
from .pages import (
    MANAGED_FILES, PROTECTED_FILES, LinkIndex, infer_type, is_decision_path, is_hidden, links, parse_page, safe_name,
    unlink,
)
from .util import atomic_write
from .wikirepo import WikiRepo

ROLES = ("archivist", "weaver", "curator")


def decision_mutable() -> set[str]:
    loc = i18n.current()
    return {loc.k_status, loc.k_superseded_by, loc.k_updated, i18n.K_SOURCES, i18n.K_ALIASES, loc.k_entities}


def weaver_mutable() -> set[str]:
    loc = i18n.current()
    return {loc.k_entities, i18n.K_ALIASES, loc.k_updated, "liens", "links"}


def _see_also() -> re.Pattern:
    return re.compile(r"(?ims)^#{2,3} +" + re.escape(i18n.current().see_also) + r"\b.*?(?=^#{1,3} |\Z)")


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
    """The text of a page "links aside": what the weaver is not allowed to change."""
    mutable = weaver_mutable()
    meta, body = frontmatter.parse(text)
    meta = {k: v for k, v in meta.items() if k not in mutable}
    body = _see_also().sub("", unlink(body))
    return meta, re.sub(r"\s+", " ", body).strip()


def _lost_links(old: str, new: str, known) -> list[str]:
    """Links present before and absent after. Fixing a link that led nowhere is still allowed."""
    see_also = _see_also()

    def targets(text: str) -> Counter:
        body = see_also.sub("", frontmatter.parse(text)[1])
        return Counter(target.lower() for target, _label in links(body))

    before, after = targets(old), targets(new)
    lost = []
    for target, count in before.items():
        if after.get(target, 0) < count and (known is None or known.resolve(target) is not None):
            lost.append(target)
    return sorted(lost)


def _decision_change_allowed(old: str, new: str) -> bool:
    mutable = decision_mutable()
    old_meta, old_body = frontmatter.parse(old)
    new_meta, new_body = frontmatter.parse(new)
    if old_body.strip() != new_body.strip():
        return False
    keys = set(old_meta) | set(new_meta)
    return all(old_meta.get(k) == new_meta.get(k) for k in keys if k not in mutable)


def _fix_names(repo: WikiRepo, cwd: Path, base: str, verdict: Verdict) -> None:
    """A file added with a character Obsidian mobile rejects (`:`…) is renamed, and the links follow."""
    content_dirs = i18n.current().content_dirs
    renames = {}
    for status, path in repo.changes(cwd, base):
        if status != "A" or path.split("/")[0] not in content_dirs or not path.endswith(".md"):
            continue
        parts = path.split("/")
        fixed = "/".join([safe_name(part) for part in parts[:-1]] + [safe_name(parts[-1])])
        if fixed != path and not (cwd / fixed).exists():
            (cwd / fixed).parent.mkdir(parents=True, exist_ok=True)
            (cwd / path).rename(cwd / fixed)
            renames[path[:-3]] = fixed[:-3]
            verdict.warnings.append(f"{path}: renamed to \"{fixed.split('/')[-1]}\" (character rejected on mobile)")
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


def validate(
    repo: WikiRepo, cwd: Path, base: str, role: str, today: str, max_page_chars: int = 12000,
    links_only: bool = False,
) -> Verdict:
    """`links_only`: the curator applies a `link` action, which may change links and nothing else."""
    assert role in ROLES
    loc = i18n.current()
    verdict = Verdict()
    cwd = Path(cwd)
    repo.heal_worktree(cwd)
    repo.stage_all(cwd)
    _fix_names(repo, cwd, base, verdict)
    known = None
    if role == "weaver":
        known = LinkIndex(repo.git("ls-tree", "-r", "--name-only", "-z", base, cwd=cwd).split("\0"))

    for status, path in repo.changes(cwd, base):
        top = path.split("/")[0]
        if is_hidden(path) or path in MANAGED_FILES or path in PROTECTED_FILES or top == loc.journal:
            repo.restore_path(cwd, base, path)
            if not is_hidden(path):
                verdict.warnings.append(f"{path}: reserved, change reverted")
            continue
        if top not in loc.content_dirs or not path.endswith(".md"):
            if status == "A":
                repo.restore_path(cwd, base, path)
                verdict.warnings.append(f"{path}: outside the wiki folders, file removed")
            else:
                verdict.errors.append(f"{path}: this file must not be changed")
            continue
        existing_decision = status != "A" and is_decision_path(path)

        if role == "archivist":
            if status == "D":
                verdict.errors.append(f"{path}: the archivist never deletes or renames a page")
                continue
            if top == loc.concepts:
                repo.restore_path(cwd, base, path)
                verdict.warnings.append(f"{path}: concept pages are kept by the weaver, change reverted")
                continue
        elif role == "weaver":
            if status == "D":
                verdict.errors.append(f"{path}: the weaver never deletes a page")
                continue
            if top != loc.concepts:
                if status == "A":
                    verdict.errors.append(f"{path}: the weaver only creates pages in {loc.concepts}/")
                    continue
                if existing_decision:
                    verdict.errors.append(f"{path}: a decision record cannot be changed")
                    continue
                old = repo.show(base, path, cwd=cwd) or ""
                new = (cwd / path).read_text(encoding="utf-8")
                if normalize_for_links(old) != normalize_for_links(new):
                    verdict.errors.append(
                        f"{path}: the weaver changed the text; it may only add links and a \"{loc.see_also}\" section"
                    )
                    continue
                lost = _lost_links(old, new, known)
                if lost:
                    verdict.errors.append(
                        f"{path}: the weaver removed or redirected an existing link ({', '.join(lost[:3])}); "
                        "only a broken link may be fixed"
                    )
                    continue
        elif role == "curator":
            if existing_decision:
                verdict.errors.append(f"{path}: a decision record cannot be changed or deleted")
                continue
            if links_only:
                if status != "M":
                    verdict.errors.append(f"{path}: a `link` action neither creates nor deletes a page")
                    continue
                old = repo.show(base, path, cwd=cwd) or ""
                new = (cwd / path).read_text(encoding="utf-8")
                if normalize_for_links(old) != normalize_for_links(new):
                    verdict.errors.append(
                        f"{path}: a `link` action changed the text; it may only change links "
                        f"and the \"{loc.see_also}\" section"
                    )
                    continue

        if status == "D":
            verdict.deleted.append(path)
            continue
        if existing_decision:
            old = repo.show(base, path, cwd=cwd) or ""
            new = (cwd / path).read_text(encoding="utf-8")
            if not _decision_change_allowed(old, new):
                verdict.errors.append(
                    f"{path}: an existing decision record is immutable "
                    f"(only `{loc.k_status}` and `{loc.k_superseded_by}` may change)"
                )
                continue

        # ── page added or changed: minimal header, fixed automatically ────
        try:
            text = (cwd / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            verdict.errors.append(f"{path}: unreadable file")
            continue
        page = parse_page(path, text)
        if not page.body.strip():
            verdict.errors.append(f"{path}: empty page")
            continue
        missing: dict = {}
        if not page.meta.get(i18n.K_TYPE):
            missing[i18n.K_TYPE] = infer_type(path)
        if not page.meta.get(loc.k_title):
            missing[loc.k_title] = page.title
        if is_decision_path(path):
            if not page.meta.get(i18n.K_DATE):
                missing[i18n.K_DATE] = today
            if not page.meta.get(loc.k_status):
                missing[loc.k_status] = loc.status("active")
        if missing:
            atomic_write(cwd / path, frontmatter.set_keys(text, missing))
        if not page.meta.get(loc.k_summary) and not is_decision_path(path):
            verdict.warnings.append(f"{path}: no `{loc.k_summary}` field in the header")
        if len(page.body) > max_page_chars:
            verdict.warnings.append(f"{path}: long page ({len(page.body)} characters)")
        verdict.touched.append(path)
        if status == "A":
            verdict.added.append(path)

    repo.stage_all(cwd)
    return verdict
