"""Le pipeline du serveur : archiviste (extraction puis intégration) et tisseur.

Principe : le modèle rédige, le code tient les comptes. Tout ce qui est
comptabilité (index, journal, registre, sources, verrou, commits) est
déterministe ; l'agent ne fait qu'écrire des pages, dans une copie de travail
jetable, et son travail n'entre dans le wiki qu'après validation.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import frontmatter, index, segment as segment_mod
from .agent import Agent, AgentError, parse_json_reply
from .config import Config
from .gate import archive_path
from .lint import lint, summary as lint_summary
from .notify import notify
from .pages import CONTENT_DIRS, PROJECTS, TOPICS, Page, load_wiki, parse_page, wikilink
from .segment import Segment
from .util import (
    LockBusy, append_line, atomic_write, eprint, file_lock, iso, local_tz, now, read_json, safe_filename,
    sha256_text, write_json,
)
from .validate import Verdict, validate
from .vault import Vault
from .wikirepo import LandConflict, WikiRepo

ELEMENT_TYPES = {"decision", "piste_abandonnee", "probleme", "fait", "preference", "etat", "synthese"}
PACKAGE = Path(__file__).resolve().parent
RESULT_FILE = ".memex-result.json"


@dataclass
class Outcome:
    segment: str
    status: str            # integre | rien | court | echec | quarantaine
    title: str = ""
    dossier: str = ""
    pages: list[str] = field(default_factory=list)
    detail: str = ""


class Server:
    def __init__(self, config: Config):
        self.config = config
        self.repo = WikiRepo(config)
        self.vault = Vault(config, self.repo.dir)
        self.agent = Agent(config)
        self.tz = local_tz(config.get("MEMEX_TZ"))
        self.lock_path = config.state_dir / "pipeline.lock"
        self.failures_path = config.state_dir / "failures.json"

    # ── mise en place ─────────────────────────────────────────────────────
    def init(self) -> bool:
        config = self.config
        for folder in (config.inbox, config.archive, config.extracts, config.quarantine, config.work,
                       config.state_dir, config.logs, config.backups):
            folder.mkdir(parents=True, exist_ok=True)
        created = self.repo.init(PACKAGE / "wiki_template")
        if created:
            index.write(self.repo.dir, load_wiki(self.repo.dir))
            self.repo.commit_all(self.repo.dir, "index initial", "memex")
            self.vault.export()
        return created

    def prompt(self, name: str, **values: str) -> str:
        custom = self.config.get("MEMEX_PROMPTS_DIR").strip()
        path = Path(custom).expanduser() / f"{name}.md" if custom else None
        if not path or not path.is_file():
            path = PACKAGE / "prompts" / f"{name}.md"
        text = path.read_text(encoding="utf-8")
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", value)
        return text

    def conventions(self, root: Path) -> str:
        try:
            return frontmatter.split((root / "AGENTS.md").read_text(encoding="utf-8"))[1].strip()
        except OSError:
            return "(pas de fichier de conventions)"

    # ── synchronisation avec le vault ─────────────────────────────────────
    def sync_in(self) -> list[str]:
        """Rapatrie les modifications humaines faites dans Obsidian et les commite."""
        if not self.repo.is_clean():
            self.repo.reset_main_checkout()
        changed = self.vault.import_changes()
        if changed:
            index.write(self.repo.dir, load_wiki(self.repo.dir))
            shown = ", ".join(changed[:5]) + ("…" if len(changed) > 5 else "")
            self.repo.commit_all(self.repo.dir, f"humain : modifications depuis Obsidian ({shown})", "humain")
        return changed

    def sync_out(self) -> None:
        self.vault.export()

    # ── registre, journal, journal des opérations ─────────────────────────
    def ledger_ids(self, root: Optional[Path] = None) -> set[str]:
        ids = set()
        try:
            with open((root or self.repo.dir) / ".memex" / "ledger.jsonl", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        ids.add(json.loads(line)["id"])
                    except (ValueError, KeyError):
                        continue
        except OSError:
            pass
        return ids

    def _ledger(self, root: Path, seg: Segment, outcome: Outcome) -> None:
        append_line(root / ".memex" / "ledger.jsonl", json.dumps({
            "id": seg.id, "fichier": seg.path.name if seg.path else "", "device": seg.device, "source": seg.source,
            "debut": iso(seg.started), "traite": iso(), "statut": outcome.status, "dossier": outcome.dossier,
            "pages": outcome.pages,
        }, ensure_ascii=False))

    def _journal(self, root: Path, seg: Segment, outcome: Outcome, pages: dict[str, Page]) -> None:
        local = seg.started.astimezone(self.tz)
        day = local.strftime("%Y-%m-%d")
        path = root / "Journal" / local.strftime("%Y") / f"{day}.md"
        if not path.exists():
            atomic_write(path, frontmatter.dump({"type": "journal", "titre": f"Journal du {day}"}, f"# Journal du {day}\n"))
        where = seg.project_name or outcome.dossier.split("/")[-1] or "hors projet"
        if outcome.dossier and f"{outcome.dossier}/{outcome.dossier.split('/')[-1]}.md" in pages:
            where = wikilink(f"{outcome.dossier}/{outcome.dossier.split('/')[-1]}.md", outcome.dossier.split("/")[-1])
        text = " ".join((outcome.detail or seg.title or "session sans titre").split())
        line = f"- **{local.strftime('%H:%M')}** · {where} · {seg.source} sur {seg.device} — {text}"
        if outcome.pages:
            links = ", ".join(wikilink(p, pages[p].title if p in pages else "") for p in outcome.pages[:6])
            line += f" → {links}"
        append_line(path, line + f" `{seg.id}`")

    def _log(self, root: Path, role: str, title: str, lines: list[str]) -> None:
        stamp = now().astimezone(self.tz).strftime("%Y-%m-%d %H:%M")
        entry = f"\n## [{stamp}] {role} | {' '.join(title.split())}\n" + "".join(f"- {line}\n" for line in lines)
        with open(root / "log.md", "a", encoding="utf-8") as handle:
            handle.write(entry)

    def _stamp_pages(self, root: Path, paths: list[str], source_id: str = "") -> None:
        """Provenance tenue par le code : chaque page touchée reçoit la source et la date."""
        today = now().astimezone(self.tz).strftime("%Y-%m-%d")
        for relative in paths:
            target = root / relative
            try:
                page = parse_page(relative, target.read_text(encoding="utf-8"))
            except OSError:
                continue
            meta = dict(page.meta)
            if source_id:
                sources = [s for s in frontmatter.as_list(meta.get("sources")) if s != source_id]
                meta["sources"] = (sources + [source_id])[-40:]
            meta["maj"] = today
            atomic_write(target, frontmatter.dump(meta, page.body))

    # ── rattachement d'une session à un dossier ───────────────────────────
    def _projects(self, root: Path) -> dict:
        data = read_json(root / ".memex" / "projects.json", {}) or {}
        return data if isinstance(data, dict) else {}

    def dossier_for(self, root: Path, seg: Segment) -> tuple[str, bool]:
        """(dossier, nouveau). Chaîne vide si la session n'est rattachée à aucun projet."""
        if not seg.project:
            return "", False
        mapping = self._projects(root)
        for key in (seg.project_remote, seg.project):
            if key and key in mapping:
                return mapping[key], False
        wanted = seg.project_name or seg.project
        name = safe_filename(wanted if any(c.isupper() for c in wanted) else wanted.replace("-", " ").replace("_", " ").capitalize())
        if (root / PROJECTS).is_dir():
            for existing in sorted((root / PROJECTS).iterdir()):
                if existing.is_dir() and existing.name.lower() == name.lower():
                    return f"{PROJECTS}/{existing.name}", False
        return f"{PROJECTS}/{name}", True

    def _remember_project(self, root: Path, seg: Segment, dossier: str) -> None:
        if not seg.project or not dossier.startswith(PROJECTS + "/") or not (root / dossier).is_dir():
            return
        mapping = self._projects(root)
        for key in (seg.project_remote, seg.project):
            if key:
                mapping[key] = dossier
        write_json(root / ".memex" / "projects.json", mapping)

    # ── extraction ────────────────────────────────────────────────────────
    def meta_block(self, seg: Segment) -> str:
        local = seg.started.astimezone(self.tz)
        rows = [
            f"- Date : {local.strftime('%Y-%m-%d %H:%M')}",
            f"- Source : {seg.source} sur {seg.device}",
            f"- Projet : {seg.project_name} ({seg.project_remote or 'sans dépôt distant'})" if seg.project else "- Projet : aucun (session hors dépôt)",
            f"- Répertoire : {seg.cwd}" if seg.cwd else "",
            f"- Branche : {seg.branch}" if seg.branch else "",
            f"- Titre : {seg.title}" if seg.title else "",
            f"- Fichiers modifiés : {', '.join(seg.files[:15])}" if seg.files else "",
        ]
        return "\n".join(row for row in rows if row)

    def extract(self, seg: Segment) -> dict:
        cache = self.config.extracts / f"{seg.id}.json"
        cached = read_json(cache, None)
        if isinstance(cached, dict) and isinstance(cached.get("elements"), list):
            return cached
        prompt = self.prompt("extract", meta=self.meta_block(seg), transcript=seg.transcript())
        reply = parse_json_reply(self.agent.run("extract", prompt, self.config.work, f"{seg.id}").text)
        elements = []
        for raw in reply.get("elements") if isinstance(reply.get("elements"), list) else []:
            if not isinstance(raw, dict):
                continue
            kind, title = str(raw.get("type", "")).strip(), str(raw.get("titre", "")).strip()
            content = str(raw.get("contenu", "")).strip()
            if kind not in ELEMENT_TYPES or not title or not content:
                continue
            elements.append({
                "type": kind, "titre": title[:160], "contenu": content[:3000],
                "raison": str(raw.get("raison", "")).strip()[:1500],
                "entites": [str(e)[:60] for e in raw.get("entites", []) if isinstance(e, (str, int))][:6]
                if isinstance(raw.get("entites"), list) else [],
                "explicite": bool(raw.get("explicite")),
            })
        result = {
            "resume": str(reply.get("resume", "")).strip()[:400],
            "sujet": str(reply.get("sujet", "")).strip()[:80],
            "elements": elements[:12],
        }
        write_json(cache, result)
        return result

    # ── intégration ───────────────────────────────────────────────────────
    def _where(self, dossier: str, new: bool, extracted: dict) -> str:
        if dossier and new:
            name = dossier.split("/")[-1]
            return (
                f"Dossier : `{dossier}` — c'est un nouveau projet. Crée sa page vivante `{dossier}/{name}.md` "
                "si au moins un élément le justifie."
            )
        if dossier:
            return f"Dossier : `{dossier}` (projet déjà suivi). Sa page vivante est `{dossier}/{dossier.split('/')[-1]}.md`."
        hint = f" Thème repéré : « {extracted.get('sujet')} »." if extracted.get("sujet") else ""
        return (
            "La session n'est rattachée à aucun projet." + hint + " Choisis dans la liste des pages le dossier "
            f"existant le plus pertinent (un projet ou un sujet). S'il n'y en a pas, crée un sujet : "
            f"`{TOPICS}/<Nom>/<Nom>.md`, avec un nom court et stable. Des éléments sans rapport entre eux peuvent "
            "aller dans des dossiers différents."
        )

    def integrate(self, seg: Segment, extracted: dict) -> tuple[Outcome, Optional[Verdict]]:
        name = f"ingest-{sha256_text(seg.id)[:12]}"
        feedback = ""
        for attempt in (1, 2):
            path = self.repo.new_worktree(name)
            base = self.repo.head("HEAD", cwd=path)
            dossier, new = self.dossier_for(path, seg)
            prompt = self.prompt(
                "integrate",
                conventions=self.conventions(path), meta=self.meta_block(seg),
                dossier=self._where(dossier, new, extracted),
                elements=json.dumps(extracted["elements"], ensure_ascii=False, indent=2),
                index=index.compact(load_wiki(path)),
            )
            if feedback:
                prompt += (
                    "\n\n## Ta tentative précédente a été refusée\n\n" + feedback +
                    "\n\nRecommence en respectant ces points. La copie de travail a été remise à zéro."
                )
            try:
                self.agent.run("edit", prompt, path, f"{seg.id}-t{attempt}")
            except AgentError as exc:
                self.repo.drop_worktree(name)
                return Outcome(seg.id, "echec", seg.title, detail=str(exc)[:300]), None
            result = read_json(path / RESULT_FILE, {}) or {}
            (path / RESULT_FILE).unlink(missing_ok=True)
            today = seg.started.astimezone(self.tz).strftime("%Y-%m-%d")
            verdict = validate(self.repo, path, base, "archiviste", today, self.config.int("MEMEX_MAX_PAGE_CHARS"))
            if verdict.ok:
                break
            feedback = verdict.feedback()
            self.repo.drop_worktree(name)
            if attempt == 2:
                return Outcome(seg.id, "echec", seg.title, detail="refusé par les garde-fous : " + "; ".join(verdict.errors)[:300]), verdict
        summary = extracted.get("resume") or seg.title
        if not verdict.changed:
            self.repo.drop_worktree(name)
            return Outcome(seg.id, "rien", seg.title, detail=summary), verdict
        chosen = dossier or self._dossier_of_result(result, verdict.touched)
        outcome = Outcome(seg.id, "integre", seg.title, chosen, sorted(verdict.touched), summary)
        self._stamp_pages(path, verdict.touched, seg.id)
        self._remember_project(path, seg, chosen)
        self._finish(path, seg, outcome, role="archiviste")
        try:
            self.repo.commit_all(path, f"archiviste : {seg.title or seg.id}\n\nsource : {seg.id}", "archiviste")
            self.repo.land(name)
        except LandConflict as exc:
            self.repo.drop_worktree(name)
            return Outcome(seg.id, "echec", seg.title, detail=f"conflit de fusion : {exc}"[:300]), verdict
        self.repo.drop_worktree(name)
        return outcome, verdict

    @staticmethod
    def _dossier_of_result(result: dict, touched: list[str]) -> str:
        claimed = str(result.get("dossier") or "").strip().strip("/")
        if claimed.split("/")[0] in (PROJECTS, TOPICS) and claimed.count("/") == 1:
            return claimed
        for path in touched:
            parts = path.split("/")
            if parts[0] in (PROJECTS, TOPICS) and len(parts) >= 3:
                return "/".join(parts[:2])
        return ""

    def _finish(self, root: Path, seg: Segment, outcome: Outcome, role: str) -> None:
        """Comptabilité d'un condensé traité : journal, registre, index, journal des opérations."""
        pages = load_wiki(root)
        self._journal(root, seg, outcome, pages)
        self._ledger(root, seg, outcome)
        if outcome.pages:
            self._log(root, role, seg.title or seg.id, [wikilink(p, pages[p].title if p in pages else "") for p in outcome.pages])
        index.write(root, load_wiki(root))

    def _record_only(self, seg: Segment, outcome: Outcome) -> None:
        """Condensé sans suite : une ligne au journal, commitée directement sur le dépôt principal."""
        self._finish(self.repo.dir, seg, outcome, role="archiviste")
        self.repo.commit_all(self.repo.dir, f"journal : {seg.title or seg.id} ({outcome.status})", "memex")

    # ── un condensé ───────────────────────────────────────────────────────
    def process(self, seg: Segment) -> Outcome:
        short = seg.user_chars < self.config.int("MEMEX_MIN_USER_CHARS") and seg.source != "note"
        if short:
            outcome = Outcome(seg.id, "court", seg.title, detail=f"session courte : {seg.title or 'sans titre'}")
            self._record_only(seg, outcome)
            return outcome
        try:
            extracted = self.extract(seg)
        except AgentError as exc:
            return Outcome(seg.id, "echec", seg.title, detail=f"extraction : {exc}"[:300])
        if not extracted["elements"]:
            outcome = Outcome(seg.id, "rien", seg.title, detail=extracted.get("resume") or seg.title)
            self._record_only(seg, outcome)
            return outcome
        outcome, _verdict = self.integrate(seg, extracted)
        if outcome.status == "rien":
            self._record_only(seg, outcome)
        return outcome

    def _archive(self, seg: Segment, quarantine: bool = False) -> None:
        if not seg.path:
            return
        target = (self.config.quarantine / seg.device / seg.path.name) if quarantine else archive_path(self.config, seg.device, seg.path.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(seg.path, target)

    def pending(self) -> list[Path]:
        found = []
        if self.config.inbox.is_dir():
            for path in self.config.inbox.glob("*/*.md"):
                if not path.name.startswith("."):
                    found.append(path)
        return sorted(found, key=lambda p: p.name)

    # ── un passage complet ────────────────────────────────────────────────
    def run(self) -> int:
        try:
            with file_lock(self.lock_path):
                return self._run()
        except LockBusy:
            eprint("memex : un passage est déjà en cours.")
            return 0

    def _run(self) -> int:
        config = self.config
        self.init()
        self.repo.cleanup_stale()
        started = time.time()
        self._sweep_local_sources()
        human = self.sync_in()
        failures = read_json(self.failures_path, {}) or {}
        done = self.ledger_ids()
        outcomes: list[Outcome] = []
        budget, batch = config.int("MEMEX_RUN_BUDGET_S"), config.int("MEMEX_BATCH_MAX")
        left_over = 0
        for path in self.pending():
            if len(outcomes) >= batch or time.time() - started > budget:
                left_over += 1
                continue
            try:
                seg = segment_mod.load(path)
            except (ValueError, OSError) as exc:
                target = config.quarantine / path.parent.name / path.name
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, target)
                outcomes.append(Outcome(path.stem, "quarantaine", path.name, detail=f"condensé illisible : {exc}"[:200]))
                continue
            if seg.id in done:
                self._archive(seg)
                continue
            outcome = self.process(seg)
            if outcome.status == "echec":
                entry = failures.get(seg.id) or {"count": 0}
                entry = {"count": entry["count"] + 1, "erreur": outcome.detail, "date": iso()}
                failures[seg.id] = entry
                if entry["count"] >= config.int("MEMEX_MAX_RETRIES"):
                    self._archive(seg, quarantine=True)
                    failures.pop(seg.id, None)
                    outcome.status = "quarantaine"
            else:
                failures.pop(seg.id, None)
                self._archive(seg)
                done.add(seg.id)
            write_json(self.failures_path, failures)
            outcomes.append(outcome)
            self.sync_out()
        tisseur = self.run_tisseur() if any(o.status == "integre" for o in outcomes) or human else ""
        self.sync_out()
        self._report(outcomes, tisseur, human, left_over)
        if left_over:
            atomic_write(config.trigger, iso() + "\n")  # relance un passage pour la suite
        return 1 if any(o.status in ("echec", "quarantaine") for o in outcomes) else 0

    def _sweep_local_sources(self) -> None:
        """Les sessions tenues sur le serveur lui-même (Hermes par messagerie) entrent par la même porte."""
        if not self.config.is_local_server or not self.config.list("MEMEX_SOURCES"):
            return
        from .ship import ship
        from .sweep import Sweeper

        try:
            Sweeper(self.config).run()
            ship(self.config)
        except Exception as exc:  # la capture locale ne doit jamais bloquer l'archivage
            eprint(f"memex : balayage local impossible ({exc})")

    def _report(self, outcomes: list[Outcome], tisseur: str, human: list[str], left_over: int) -> None:
        integrated = [o for o in outcomes if o.status == "integre"]
        failed = [o for o in outcomes if o.status in ("echec", "quarantaine")]
        quiet = [o for o in outcomes if o.status in ("rien", "court")]
        lines = []
        for outcome in integrated:
            where = outcome.dossier.split("/")[-1] if outcome.dossier else "wiki"
            lines.append(f"• {where} : {outcome.detail} ({len(outcome.pages)} page(s))")
        if tisseur:
            lines.append(f"• tisseur : {tisseur}")
        if human:
            lines.append(f"• {len(human)} modification(s) faite(s) dans Obsidian reprise(s)")
        if quiet:
            lines.append(f"• {len(quiet)} session(s) sans suite (journal seulement)")
        for outcome in failed:
            label = "mis en quarantaine" if outcome.status == "quarantaine" else "échec, nouvel essai au prochain passage"
            lines.append(f"⚠ {outcome.title or outcome.segment} : {label} — {outcome.detail}")
        if left_over:
            lines.append(f"… {left_over} condensé(s) restent à traiter")
        if not lines:
            return
        for line in lines:
            eprint(line)
        level = self.config.get("MEMEX_NOTIFY_LEVEL").strip().lower()
        wanted = (
            level == "always"
            or (level == "changes" and (integrated or failed or tisseur))
            or (level == "failures" and failed)
        )
        if wanted:
            notify(self.config, f"memex : {len(integrated)} session(s) intégrée(s)" + (f", {len(failed)} en échec" if failed else ""), "\n".join(lines))

    # ── tisseur ───────────────────────────────────────────────────────────
    def run_tisseur(self, force: bool = False) -> str:
        config = self.config
        if not config.bool("MEMEX_TISSEUR") and not force:
            return ""
        report = lint(self.repo.dir, config.int("MEMEX_MAX_PAGE_CHARS"), config.int("MEMEX_TISSEUR_MIN_DOSSIERS"))
        if report.stats.get("pages", 0) < config.int("MEMEX_TISSEUR_MIN_PAGES") and not force:
            return ""
        work = {
            "mentions_sans_lien": report.unlinked[:40],
            "concepts_candidats": report.concepts[:8],
            "liens_casses": report.broken[:20],
        }
        if not any(work.values()):
            return ""
        digest = sha256_text(json.dumps(work, sort_keys=True, ensure_ascii=False))
        marker = config.state_dir / "tisseur.json"
        if (read_json(marker, {}) or {}).get("digest") == digest and not force:
            return ""  # mêmes pistes qu'au dernier passage : inutile de redemander
        write_json(marker, {"digest": digest, "date": iso()})
        name, feedback = "tisseur", ""
        for attempt in (1, 2):
            path = self.repo.new_worktree(name)
            base = self.repo.head("HEAD", cwd=path)
            prompt = self.prompt(
                "tisseur", conventions=self.conventions(path),
                report=json.dumps(work, ensure_ascii=False, indent=2), index=index.compact(load_wiki(path)),
            )
            if feedback:
                prompt += "\n\n## Ta tentative précédente a été refusée\n\n" + feedback + "\n\nRecommence en respectant ces points."
            try:
                self.agent.run("edit", prompt, path, f"tisseur-t{attempt}")
            except AgentError as exc:
                self.repo.drop_worktree(name)
                return f"échec ({str(exc)[:120]})"
            (path / RESULT_FILE).unlink(missing_ok=True)
            today = now().astimezone(self.tz).strftime("%Y-%m-%d")
            verdict = validate(self.repo, path, base, "tisseur", today, config.int("MEMEX_MAX_PAGE_CHARS"))
            if verdict.ok:
                break
            feedback = verdict.feedback()
            self.repo.drop_worktree(name)
            if attempt == 2:
                return "refusé par les garde-fous"
        if not verdict.changed:
            self.repo.drop_worktree(name)
            return ""
        concepts = [p for p in verdict.touched if p.startswith("Concepts/")]
        self._stamp_pages(path, concepts)
        pages = load_wiki(path)
        self._log(path, "tisseur", f"{len(verdict.touched)} page(s) reliée(s)", [wikilink(p, pages[p].title if p in pages else "") for p in sorted(verdict.touched)[:30]])
        index.write(path, load_wiki(path))
        try:
            self.repo.commit_all(path, f"tisseur : {len(verdict.touched)} page(s) reliée(s)", "tisseur")
            self.repo.land(name)
        except LandConflict:
            self.repo.drop_worktree(name)
            return "conflit de fusion, reporté"
        self.repo.drop_worktree(name)
        created = [p for p in verdict.added if p.startswith("Concepts/")]
        return f"{len(verdict.touched)} page(s) reliée(s)" + (f", {len(created)} concept(s) créé(s)" if created else "")

    # ── état ──────────────────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        report = lint(self.repo.dir) if self.repo.exists() else None
        failures = read_json(self.failures_path, {}) or {}
        reviews = [r for r in (read_json(p, {}) for p in sorted((self.config.state_dir / "reviews").glob("*.json")))
                   if r and r.get("statut") == "en_attente"] if (self.config.state_dir / "reviews").is_dir() else []
        return {
            "wiki": lint_summary(report) if report else "non initialisé",
            "en_attente": len(self.pending()),
            "traites": len(self.ledger_ids()) if self.repo.exists() else 0,
            "echecs": failures,
            "quarantaine": len(list(self.config.quarantine.glob("*/*.md"))) if self.config.quarantine.is_dir() else 0,
            "revue_en_attente": [r.get("id") for r in reviews],
            "agent": self.agent.name,
            "vault": str(self.config.vault_dir) if self.config.vault_dir else "(désactivé)",
        }


def prune_extracts(config: Config, keep_days: int = 90) -> None:
    limit = time.time() - keep_days * 86400
    for path in config.extracts.glob("*.json") if config.extracts.is_dir() else []:
        try:
            if path.stat().st_mtime < limit:
                path.unlink()
        except OSError:
            continue


__all__ = ["Server", "Outcome", "CONTENT_DIRS", "shutil"]
