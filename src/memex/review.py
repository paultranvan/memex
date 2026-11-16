"""Le vérificateur : il propose, tu approuves, alors seulement le wiki change.

Chaque action est un commit sur une branche de revue. Approuver une action,
c'est reporter son commit sur `main` ; la refuser l'inscrit dans la liste des
refus, pour qu'elle ne soit pas reproposée.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from . import index
from .agent import AgentError, parse_json_reply
from .lint import lint
from .notify import notify
from .pages import is_decision_path, load_wiki
from .pipeline import RESULT_FILE, Server
from .util import CommandError, LockBusy, append_line, eprint, file_lock, iso, now, parse_ts, read_json, write_json
from .validate import validate
from .wikirepo import LandConflict

ACTION_TYPES = {"fusion", "condensation", "correction", "suppression", "rangement"}
PENDING, DONE, REJECTED, EXPIRED = "en_attente", "appliquee", "refusee", "expiree"


class Reviews:
    def __init__(self, server: Server):
        self.server = server
        self.config = server.config
        self.repo = server.repo
        self.dir = self.config.state_dir / "reviews"

    # ── stockage ──────────────────────────────────────────────────────────
    def _path(self, review_id: str) -> Path:
        return self.dir / f"{review_id}.json"

    def all(self) -> list[dict]:
        if not self.dir.is_dir():
            return []
        return [r for r in (read_json(p, None) for p in sorted(self.dir.glob("*.json"))) if isinstance(r, dict)]

    def pending(self) -> Optional[dict]:
        for review in reversed(self.all()):
            if review.get("statut") == PENDING:
                return review
        return None

    def get(self, review_id: str = "") -> Optional[dict]:
        if not review_id:
            return self.pending()
        return read_json(self._path(review_id), None)

    def refused(self, root: Path) -> list[dict]:
        items = []
        try:
            with open(root / ".memex" / "refus.jsonl", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        items.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return items[-60:]

    # ── passage du vérificateur ───────────────────────────────────────────
    def run(self, force: bool = False) -> int:
        try:
            with file_lock(self.server.lock_path, wait=True):
                return self._run(force)
        except LockBusy:
            return 0

    def _run(self, force: bool) -> int:
        server, repo, config = self.server, self.repo, self.config
        server.init()
        self.expire()
        if self.pending():
            eprint("memex : une revue attend déjà ta réponse ; pas de nouveau passage.")
            return 0
        server.sync_in()
        head = repo.head("main")
        marker = config.state_dir / "verif.json"
        last = (read_json(marker, {}) or {}).get("head", "")
        if last == head and not force:
            eprint("memex : le wiki n'a pas changé depuis le dernier passage du vérificateur.")
            return 0
        report = lint(repo.dir, config.int("MEMEX_MAX_PAGE_CHARS"))
        changed = ""
        if last:
            try:
                changed = repo.git("diff", "--name-only", f"{last}..{head}", "--", "Projets", "Sujets", "Concepts")
            except CommandError:
                changed = ""
        changed = "\n".join(f"- {line}" for line in changed.splitlines()[:80]) or "(premier passage : tout le wiki)"
        refused = "\n".join(f"- {r.get('type')} · {r.get('titre')} · {', '.join(r.get('pages', []))}" for r in self.refused(repo.dir)) or "(aucune)"
        review_id = now().strftime("%Y%m%d-%H%M")
        name = f"review-{review_id}"
        path = repo.new_worktree(name)
        max_actions = config.int("MEMEX_VERIF_MAX_ACTIONS")
        prompt = server.prompt(
            "verif-diagnose", conventions=server.conventions(path),
            report=json.dumps({"problemes": report.problems[:40], "orphelines": report.orphans[:30], "stats": report.stats}, ensure_ascii=False, indent=2),
            changed=changed, index=index.compact(load_wiki(path)), refused=refused, max_actions=str(max_actions),
        )
        try:
            plan = parse_json_reply(server.agent.run("read", prompt, path, f"verif-diag-{review_id}").text)
        except AgentError as exc:
            repo.drop_worktree(name)
            eprint(f"memex : diagnostic du vérificateur impossible ({exc})")
            return 1
        repo.git("reset", "--quiet", "--hard", "HEAD", cwd=path)
        repo.git("clean", "-fdq", cwd=path)
        actions = []
        for raw in plan.get("actions") if isinstance(plan.get("actions"), list) else []:
            if not isinstance(raw, dict) or str(raw.get("type")) not in ACTION_TYPES or not raw.get("titre"):
                continue
            pages = [str(p) for p in raw.get("pages", []) if isinstance(p, str)] if isinstance(raw.get("pages"), list) else []
            if any(is_decision_path(p) for p in pages):
                continue
            actions.append({
                "type": str(raw["type"]), "titre": str(raw["titre"])[:200], "pages": pages[:10],
                "raison": str(raw.get("raison", ""))[:600], "consigne": str(raw.get("consigne", ""))[:1500],
            })
        questions = [
            {"titre": str(q.get("titre", ""))[:200], "detail": str(q.get("detail", ""))[:800]}
            for q in (plan.get("questions") if isinstance(plan.get("questions"), list) else [])
            if isinstance(q, dict) and q.get("titre")
        ][:5]
        today = now().astimezone(server.tz).strftime("%Y-%m-%d")
        applied = []
        for number, action in enumerate(actions[:max_actions], start=1):
            base = repo.head("HEAD", cwd=path)
            entry = {**action, "n": number, "commit": "", "statut": "echec", "resume": ""}
            try:
                server.agent.run("edit", server.prompt("verif-apply", conventions=server.conventions(path), action=json.dumps(action, ensure_ascii=False, indent=2)), path, f"verif-{review_id}-a{number}")
                result = read_json(path / RESULT_FILE, {}) or {}
                (path / RESULT_FILE).unlink(missing_ok=True)
                verdict = validate(repo, path, base, "verificateur", today, config.int("MEMEX_MAX_PAGE_CHARS"))
                if verdict.ok and verdict.changed:
                    server._stamp_pages(path, verdict.touched)
                    commit = repo.commit_all(path, f"vérificateur : {action['titre']}", "verificateur")
                    entry.update({
                        "commit": commit or "", "statut": PENDING, "resume": str(result.get("resume", ""))[:300],
                        "modifiees": sorted(verdict.touched), "supprimees": sorted(verdict.deleted),
                    })
                else:
                    entry["resume"] = "; ".join(verdict.errors)[:300] or "aucun changement nécessaire"
            except AgentError as exc:
                entry["resume"] = str(exc)[:300]
            if entry["statut"] != PENDING:
                repo.git("reset", "--quiet", "--hard", base, cwd=path)
                repo.git("clean", "-fdq", cwd=path)
            applied.append(entry)
        repo.drop_worktree(name, keep_branch=True)
        write_json(marker, {"head": head, "date": iso()})
        proposals = [a for a in applied if a["statut"] == PENDING]
        if not proposals and not questions:
            repo.git("branch", "-D", f"memex/{name}", check=False)
            eprint("memex : le vérificateur n'a rien à proposer.")
            return 0
        review = {
            "id": review_id, "cree": iso(), "base": head, "branche": f"memex/{name}",
            "statut": PENDING if proposals else DONE, "actions": applied, "questions": questions,
        }
        self.dir.mkdir(parents=True, exist_ok=True)
        write_json(self._path(review_id), review)
        notify(config, f"memex : le vérificateur propose {len(proposals)} action(s)", self.format(review))
        print(self.format(review))
        return 0

    def format(self, review: dict) -> str:
        lines = [f"Revue {review['id']} — {review.get('statut')}"]
        for action in review.get("actions", []):
            if action.get("statut") == "echec":
                continue
            mark = {PENDING: "", DONE: " ✓", REJECTED: " ✗"}.get(action.get("statut"), "")
            lines.append(f"\n{action['n']}. [{action['type']}] {action['titre']}{mark}")
            if action.get("raison"):
                lines.append(f"   Pourquoi : {action['raison']}")
            if action.get("supprimees"):
                lines.append(f"   Pages supprimées : {', '.join(action['supprimees'])}")
        for question in review.get("questions", []):
            lines.append(f"\n? À trancher : {question['titre']}\n   {question.get('detail', '')}")
        if review.get("statut") == PENDING:
            lines.append("\nRéponds « ok », « ok sauf 2 » ou « non ».")
        return "\n".join(lines)

    # ── approbation ───────────────────────────────────────────────────────
    def decide(self, review_id: str = "", only: Optional[list[int]] = None, skip: Optional[list[int]] = None, reject_all: bool = False) -> str:
        with file_lock(self.server.lock_path, wait=True):
            return self._decide(review_id, only, skip, reject_all)

    def _decide(self, review_id: str, only: Optional[list[int]], skip: Optional[list[int]], reject_all: bool) -> str:
        server, repo = self.server, self.repo
        review = self.get(review_id)
        if not review or review.get("statut") != PENDING:
            return "Aucune revue en attente."
        candidates = [a for a in review["actions"] if a.get("statut") == PENDING]
        chosen = [] if reject_all else [
            a for a in candidates if (only is None or a["n"] in only) and not (skip and a["n"] in skip)
        ]
        refused = [a for a in candidates if a not in chosen]
        server.sync_in()
        landed, conflicts = [], []
        name = f"apply-{review['id']}"
        path = repo.new_worktree(name)
        for action in chosen:
            try:
                repo.git("cherry-pick", "--allow-empty", "--keep-redundant-commits", action["commit"], cwd=path, author="verificateur")
                action["statut"] = DONE
                landed.append(action)
            except CommandError:
                repo.git("cherry-pick", "--abort", cwd=path, check=False)
                repo.git("reset", "--quiet", "--hard", "HEAD", cwd=path)
                action["statut"] = "conflit"
                conflicts.append(action)
        for action in refused:
            action["statut"] = REJECTED
            append_line(path / ".memex" / "refus.jsonl", json.dumps({
                "date": iso(), "type": action["type"], "titre": action["titre"], "pages": action.get("pages", []),
            }, ensure_ascii=False))
        if landed:
            lines = []
            for action in landed:
                lines.append(f"{action['type']} : {action['titre']}")
                lines.extend(f"page supprimée : `{p}` (retrouvable dans l'historique)" for p in action.get("supprimees", []))
            server._log(path, "vérificateur", f"revue {review['id']} : {len(landed)} action(s) appliquée(s)", lines)
        index.write(path, load_wiki(path))
        try:
            repo.commit_all(path, f"vérificateur : revue {review['id']} ({len(landed)} appliquée(s), {len(refused)} refusée(s))", "verificateur")
            repo.land(name)
        except LandConflict as exc:
            repo.drop_worktree(name)
            return f"Fusion impossible : {exc}"
        repo.drop_worktree(name)
        server.sync_out()
        review["statut"] = DONE if landed else REJECTED
        review["decide"] = iso()
        write_json(self._path(review["id"]), review)
        repo.git("branch", "-D", review.get("branche", ""), check=False)
        parts = [f"{len(landed)} action(s) appliquée(s)", f"{len(refused)} refusée(s)"]
        if conflicts:
            parts.append(f"{len(conflicts)} reportée(s) : la page a changé entre-temps")
        return "Revue " + review["id"] + " : " + ", ".join(parts) + "."

    def expire(self) -> None:
        limit = time.time() - self.config.int("MEMEX_REVIEW_TTL_DAYS") * 86400
        for review in self.all():
            if review.get("statut") != PENDING:
                continue
            created = parse_ts(review.get("cree"))
            if created and created.timestamp() < limit:
                review["statut"] = EXPIRED
                write_json(self._path(review["id"]), review)
                self.repo.git("branch", "-D", review.get("branche", ""), check=False)


def restore(server: Server, relative: str, rev: str = "") -> str:
    """Rétablit une page supprimée (ou une version précise) depuis l'historique."""
    repo = server.repo
    with file_lock(server.lock_path, wait=True):
        server.sync_in()
        if not rev:
            deleted = repo.last_commit_touching(relative, deleted=True)
            if deleted:
                rev = f"{deleted}^"
            else:
                return f"« {relative} » n'a pas été supprimée : précise une version avec --rev."
        content = repo.show(rev, relative)
        if content is None:
            return f"« {relative} » introuvable dans la version {rev}."
        name = "restore"
        path = repo.new_worktree(name)
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        server._log(path, "restauration", relative, [f"version reprise : `{rev}`"])
        index.write(path, load_wiki(path))
        repo.commit_all(path, f"restauration : {relative}", "memex")
        try:
            repo.land(name)
        finally:
            repo.drop_worktree(name)
        server.sync_out()
        return f"« {relative} » rétablie."
