"""Scénarios de bout en bout : hôte → serveur → wiki → vault → hôte, avec un faux agent."""
import json
import os
import unittest

from helpers import Sandbox, git

from memex import hook, serverops
from memex.pipeline import Server
from memex.review import Reviews, restore
from memex.ship import pull, resend_all, ship, sync
from memex.sweep import Sweeper, write_note

LONG = "Nous devons choisir où stocker les scores du jeu, en local ou sur un serveur."


class EndToEnd(Sandbox):
    def host_sync(self):
        self.assertEqual(sync(self.config, quiet=True), 0)

    def server_run(self):
        return Server(self.config).run()

    def wiki(self, relative):
        return (self.server_home / "wiki" / relative).read_text(encoding="utf-8")

    def solitaire_session(self, **kwargs):
        repo = getattr(self, "_solitaire", None) or self.make_repo("solitaire", "git@github.com:moi/solitaire.git")
        self._solitaire = repo
        return self.claude_session(repo, [
            ("user", LONG),
            ("assistant", "Je regarde le code."),
            ("tool", str(repo / "scores.py")),
            ("assistant", "DECISION: Scores côté serveur | Les scores sont stockés sur le serveur. | Classement partagé entre joueurs. @Tailscale\nPISTE: WebSocket | Essai en WebSocket abandonné. | Trop lourd."),
        ], title="Stockage des scores", **kwargs)

    # ── le trajet nominal ─────────────────────────────────────────────────
    def test_session_becomes_wiki_pages_and_comes_back(self):
        self.solitaire_session()
        self.host_sync()
        self.assertEqual(self.server_run(), 0)

        main = self.wiki("Projets/Solitaire/Solitaire.md")
        self.assertIn("Scores côté serveur", main)
        self.assertIn("WebSocket", main)
        fiche = self.wiki("Projets/Solitaire/Décisions/2026-11-12 Scores côté serveur.md")
        self.assertIn("statut: active", fiche)
        self.assertIn("claude-code_", fiche)            # provenance posée par le code
        self.assertIn("Classement partagé", fiche)
        self.assertIn("Solitaire", self.wiki("index.md"))
        self.assertIn("Scores côté serveur", self.wiki("index.md"))
        self.assertIn("Stockage des scores", self.wiki("log.md"))
        self.assertIn("Solitaire", self.wiki("Journal/2026/2026-11-12.md"))
        mapping = json.loads(self.wiki(".memex/projects.json"))
        self.assertEqual(mapping["github.com/moi/solitaire"], "Projets/Solitaire")

        # le vault reçoit les pages, sans rien de caché ni de git
        self.assertTrue((self.vault / "Projets/Solitaire/Solitaire.md").exists())
        self.assertFalse((self.vault / ".memex").exists())
        self.assertFalse((self.vault / ".git").exists())

        # le condensé est archivé, la boîte de réception est vide, le dépôt est propre
        self.assertEqual(Server(self.config).pending(), [])
        self.assertEqual(len(list((self.server_home / "raw/archive").rglob("*.md"))), 1)
        self.assertEqual(git(self.server_home / "wiki", "status", "--porcelain"), "")
        self.assertEqual(git(self.server_home / "wiki", "worktree", "list").count("\n"), 0)

        # retour sur l'hôte : le wiki est cloné et le hook injecte la page du projet
        self.host_sync()
        context = hook.project_context(self.config, str(self._solitaire))
        self.assertIn("Mémoire du projet", context)
        self.assertIn("Scores côté serveur", context)
        self.assertEqual(hook.project_context(self.config, str(self.root)), "")

    def test_second_run_is_idempotent_and_free(self):
        self.solitaire_session()
        self.host_sync()
        self.server_run()
        head = git(self.server_home / "wiki", "rev-parse", "main")
        calls = len(self.agent_calls())
        self.host_sync()
        self.assertEqual(self.server_run(), 0)
        self.assertEqual(git(self.server_home / "wiki", "rev-parse", "main"), head)
        self.assertEqual(len(self.agent_calls()), calls)
        # un renvoi complet (reconstruction) ne crée pas de doublon
        self.assertEqual(resend_all(self.config), 1)
        self.assertEqual(ship(self.config).duplicate, 1)
        self.assertEqual(Server(self.config).pending(), [])

    def test_session_continuation_gives_a_second_segment(self):
        session = self.solitaire_session()
        self.host_sync()
        self.server_run()
        self.claude_session(self._solitaire, [
            ("user", "Autre point important : il faut décider du format des sauvegardes de partie."),
            ("assistant", "DECISION: Sauvegardes en JSON | Les parties sont sauvegardées en JSON. | Lisible et simple."),
        ], session=session, append=True)
        self.host_sync()
        self.server_run()
        names = sorted(p.name for p in (self.server_home / "raw/archive").rglob("*.md"))
        self.assertEqual(len(names), 2)
        self.assertTrue(names[1].endswith("_002.md"))
        self.assertIn("Sauvegardes en JSON", self.wiki("Projets/Solitaire/Solitaire.md"))
        self.assertTrue((self.server_home / "wiki/Projets/Solitaire/Décisions/2026-11-12 Scores côté serveur.md").exists())

    def test_short_and_empty_sessions_only_reach_the_journal(self):
        repo = self.make_repo("outil")
        self.claude_session(repo, [("user", "ok merci"), ("assistant", "De rien.")], title="Remerciement")
        self.claude_session(repo, [("user", "Explique-moi longuement comment fonctionne ce module, s'il te plaît."),
                                   ("assistant", "Voici une explication sans rien de durable.")], title="Explication", start="2026-11-12T11:00:00Z")
        self.host_sync()
        self.assertEqual(self.server_run(), 0)
        journal = self.wiki("Journal/2026/2026-11-12.md")
        self.assertIn("session courte", journal)
        self.assertIn("Session de test.", journal)
        self.assertFalse((self.server_home / "wiki/Projets/Outil").exists())
        self.assertEqual(len(self.agent_calls()), 1)   # la session courte n'a coûté aucun appel

    def test_session_without_project_goes_to_a_topic(self):
        self.codex_session(self.root, [
            ("user", "Parlons du PEA : est-ce pertinent pour moi et où l'ouvrir ? SUJET: PEA"),
            ("assistant", "Je compare.", "commentary"),
            ("assistant", "DECISION: Ouvrir un PEA chez un courtier en ligne | Le PEA sera ouvert chez un courtier en ligne. | Frais plus bas."),
        ])
        self.host_sync()
        self.server_run()
        self.assertIn("courtier en ligne", self.wiki("Sujets/PEA/PEA.md"))
        self.assertIn("PEA", self.wiki("index.md"))

    def test_note_is_always_integrated(self):
        write_note(self.config, "DECISION: Garder git | On garde git pour le wiki. | Pour annuler un passage entier.", cwd=str(self.root))
        ship(self.config)
        self.server_run()
        archived = list((self.server_home / "raw/archive").rglob("*note*.md"))
        self.assertEqual(len(archived), 1)
        self.assertTrue(any("Garder git" in p.read_text(encoding="utf-8") for p in (self.server_home / "wiki/Sujets").rglob("*.md")))

    # ── le vault et les modifications humaines ────────────────────────────
    def test_human_edit_in_vault_is_imported_and_wins(self):
        self.solitaire_session()
        self.host_sync()
        self.server_run()
        page = self.vault / "Projets/Solitaire/Solitaire.md"
        page.write_text(page.read_text(encoding="utf-8") + "\nNote ajoutée à la main dans Obsidian.\n", encoding="utf-8")
        (self.vault / "Sujets/Perso").mkdir(parents=True)
        (self.vault / "Sujets/Perso/Perso.md").write_text("---\ntype: sujet\ntitre: Perso\nresume: Écrit à la main.\n---\n\nMa page.\n", encoding="utf-8")
        (self.vault / "index.md").write_text("trafiqué", encoding="utf-8")
        self.server_run()
        self.assertIn("Note ajoutée à la main", self.wiki("Projets/Solitaire/Solitaire.md"))
        self.assertIn("Écrit à la main", self.wiki("index.md"))
        self.assertNotEqual((self.vault / "index.md").read_text(encoding="utf-8"), "trafiqué")
        log = git(self.server_home / "wiki", "log", "--format=%an %s")
        self.assertIn("humain humain : modifications depuis Obsidian", log)
        # suppression humaine dans le vault
        (self.vault / "Sujets/Perso/Perso.md").unlink()
        self.server_run()
        self.assertFalse((self.server_home / "wiki/Sujets/Perso/Perso.md").exists())

    # ── garde-fous et reprises ────────────────────────────────────────────
    def test_destructive_agent_is_rejected_then_retried_with_feedback(self):
        self.solitaire_session()
        self.host_sync()
        self.server_run()
        self.claude_session(self.make_repo("autre", "git@github.com:moi/autre.git"), [
            ("user", "Un second projet, pour lequel il faut décider du langage à utiliser."),
            ("assistant", "DECISION: Écrire en Rust | Le projet est écrit en Rust. | Pour les performances."),
        ], start="2026-11-13T09:00:00Z")
        self.host_sync()
        os.environ["FAKE_AGENT_MODE"] = "delete_once"
        self.assertEqual(self.server_run(), 0)
        self.assertTrue((self.server_home / "wiki/Projets/Solitaire/Solitaire.md").exists())
        self.assertIn("Rust", self.wiki("Projets/Autre/Autre.md"))
        integrations = [c for c in self.agent_calls() if "archiviste" in c]
        self.assertEqual(len(integrations), 3)   # 1 pour solitaire, 2 pour le second (refus puis reprise)

    def test_persistent_failure_ends_in_quarantine_without_touching_the_wiki(self):
        self.solitaire_session()
        self.host_sync()
        self.server_run()
        head = git(self.server_home / "wiki", "rev-parse", "main")
        self.claude_session(self._solitaire, [
            ("user", "Encore une décision à prendre sur ce projet, à propos du thème graphique."),
            ("assistant", "DECISION: Thème sombre | Le jeu utilise un thème sombre. | Plus lisible."),
        ], start="2026-11-14T09:00:00Z")
        self.host_sync()
        os.environ["FAKE_AGENT_MODE"] = "delete"
        for _ in range(3):
            self.assertEqual(self.server_run(), 1)
        self.assertEqual(git(self.server_home / "wiki", "rev-parse", "main"), head)
        self.assertEqual(len(list((self.server_home / "raw/quarantine").rglob("*.md"))), 1)
        self.assertEqual(Server(self.config).pending(), [])
        self.assertIn("quarantaine", (self.server_home / "logs/notifications.log").read_text(encoding="utf-8"))

    def test_reserved_files_cannot_be_changed_by_an_agent(self):
        os.environ["FAKE_AGENT_MODE"] = "outside"
        self.solitaire_session()
        self.host_sync()
        self.assertEqual(self.server_run(), 0)
        self.assertNotIn("écrasé", self.wiki("index.md"))
        self.assertFalse((self.server_home / "wiki/Notes en vrac.md").exists())
        self.assertNotIn("falsifié", self.wiki(".memex/ledger.jsonl"))
        self.assertIn("Scores côté serveur", self.wiki("Projets/Solitaire/Solitaire.md"))

    def test_existing_decision_is_immutable(self):
        self.solitaire_session()
        self.host_sync()
        self.server_run()
        before = self.wiki("Projets/Solitaire/Décisions/2026-11-12 Scores côté serveur.md")
        self.claude_session(self._solitaire, [
            ("user", "Une autre décision à prendre plus tard sur ce même projet de solitaire."),
            ("assistant", "DECISION: Thème sombre | Le jeu utilise un thème sombre. | Plus lisible."),
        ], start="2026-11-15T09:00:00Z")
        self.host_sync()
        os.environ["FAKE_AGENT_MODE"] = "edit_decision"
        self.assertEqual(self.server_run(), 1)
        after = self.wiki("Projets/Solitaire/Décisions/2026-11-12 Scores côté serveur.md")
        self.assertEqual(before, after)

    def test_forged_segment_header_cannot_write_outside_its_inbox(self):
        """Une clé d'hôte volée ne doit pas pouvoir déposer un fichier dans le wiki par un en-tête forgé."""
        self.solitaire_session()
        Sweeper(self.config).run()
        forged = next(self.config.outbox.glob("*.md"))
        text = forged.read_text(encoding="utf-8")
        forged.write_text(text.replace("device: portable", 'device: "../../wiki/Projets/Pwn"').replace("source: claude-code", 'source: "../../../wiki/x"'), encoding="utf-8")
        ship(self.config)
        self.server_run()
        home = self.server_home
        self.assertFalse((home / "wiki/Projets/Pwn").exists())
        stray = [p for p in home.rglob("*") if p.is_file() and "raw" not in p.parts and "wiki" not in p.parts and "work" not in p.parts
                 and "state" not in p.parts and "logs" not in p.parts]
        self.assertEqual(stray, [])
        archived = list((home / "raw/archive").rglob("*.md"))
        self.assertEqual([p.relative_to(home / "raw/archive").parts[0] for p in archived], ["portable"])
        self.assertEqual(git(home / "wiki", "status", "--porcelain"), "")
        self.assertTrue(all(p.parent == home / "raw/extracts" for p in (home / "raw/extracts").rglob("*.json")))

    def test_internal_error_counts_as_a_failure_and_never_blocks_the_queue(self):
        from unittest import mock

        self.solitaire_session()
        self.claude_session(self.make_repo("autre", "git@github.com:moi/autre.git"), [
            ("user", "Un second projet, pour lequel il faut décider du langage à utiliser."),
            ("assistant", "DECISION: Écrire en Rust | Le projet est écrit en Rust. | Pour les performances."),
        ], start="2026-11-13T09:00:00Z")
        self.host_sync()
        real = Server.integrate

        def flaky(server, seg, extracted):
            if seg.project == "solitaire":
                raise RuntimeError("panne simulée au milieu de l'intégration")
            return real(server, seg, extracted)

        with mock.patch.object(Server, "integrate", flaky):
            for _ in range(3):
                self.assertEqual(self.server_run(), 1)
        self.assertIn("Rust", self.wiki("Projets/Autre/Autre.md"))        # le suivant est passé quand même
        self.assertEqual(len(list((self.server_home / "raw/quarantine").rglob("*.md"))), 1)
        self.assertEqual(Server(self.config).pending(), [])
        self.assertEqual(git(self.server_home / "wiki", "status", "--porcelain"), "")

    def test_agent_outage_never_quarantines_the_queue(self):
        """Quota atteint, réseau coupé : aucun condensé n'est pénalisé, quel que soit le nombre de passages."""
        self.solitaire_session()
        self.claude_session(self.make_repo("autre", "git@github.com:moi/autre.git"), [
            ("user", "Un second projet attend son tour pendant que l'agent est hors service."),
            ("assistant", "DECISION: Langage | Le projet est écrit en Rust. | Performance."),
        ], start="2026-11-13T09:00:00Z")
        self.host_sync()
        state = self.server_home / "state"
        os.environ["FAKE_AGENT_MODE"] = "fail"
        for _ in range(5):                                   # bien plus que MEMEX_MAX_RETRIES
            self.assertEqual(self.server_run(), 1)
        self.assertEqual(len(Server(self.config).pending()), 2)
        self.assertEqual(list((self.server_home / "raw/quarantine").rglob("*.md")), [])
        self.assertEqual(Server(self.config).status()["echecs"], {})   # aucun échec compté
        self.assertTrue((state / "agent-down.json").exists())
        self.assertEqual(len(self.agent_calls()), 10)        # par passage : un essai et la sonde, puis arrêt
        notices = (self.server_home / "logs/notifications.log").read_text(encoding="utf-8")
        self.assertEqual(notices.count("agent indisponible"), 1)   # prévenu une fois, pas à chaque passage
        self.assertNotEqual(Server(self.config).status()["agent_indisponible"], "non")
        os.environ["FAKE_AGENT_MODE"] = "ok"
        self.assertEqual(self.server_run(), 0)
        self.assertEqual(Server(self.config).pending(), [])
        self.assertFalse((state / "agent-down.json").exists())
        self.assertIn("Scores côté serveur", self.wiki("Projets/Solitaire/Solitaire.md"))
        self.assertIn("Rust", self.wiki("Projets/Autre/Autre.md"))

    def test_failure_tied_to_one_segment_still_ends_in_quarantine(self):
        """L'agent répond à la sonde mais échoue sur ce condensé : là, les échecs comptent."""
        self.solitaire_session()
        self.host_sync()
        os.environ["FAKE_AGENT_MODE"] = "fail_work"
        for _ in range(3):
            self.assertEqual(self.server_run(), 1)
        self.assertEqual(Server(self.config).pending(), [])
        self.assertEqual(len(list((self.server_home / "raw/quarantine").rglob("*.md"))), 1)
        self.assertFalse((self.server_home / "state/agent-down.json").exists())

    def test_agent_that_writes_nothing_is_a_failure_not_a_silent_skip(self):
        self.solitaire_session()
        self.host_sync()
        os.environ["FAKE_AGENT_MODE"] = "silent"
        self.assertEqual(self.server_run(), 1)
        self.assertEqual(len(Server(self.config).pending()), 1)          # la session n'est pas classée « sans suite »
        self.assertEqual(Server(self.config).ledger_ids(), set())
        os.environ["FAKE_AGENT_MODE"] = "ok"
        self.assertEqual(self.server_run(), 0)
        self.assertIn("Scores côté serveur", self.wiki("Projets/Solitaire/Solitaire.md"))

    # ── tisseur ───────────────────────────────────────────────────────────
    def two_projects_sharing_an_entity(self):
        self.solitaire_session()
        self.claude_session(self.make_repo("atlas", "git@github.com:moi/atlas.git"), [
            ("user", "Pour le déploiement d'Atlas, il faut décider comment joindre le serveur."),
            ("assistant", "DECISION: Accès par réseau privé | Le serveur est joint par un réseau privé, comme pour Solitaire. | Pas de port public. @Tailscale"),
        ], start="2026-11-13T09:00:00Z")
        self.host_sync()
        self.server_run()

    def test_tisseur_links_pages_and_creates_concepts(self):
        self.two_projects_sharing_an_entity()
        concept = self.wiki("Concepts/Tailscale.md")
        self.assertIn("type: concept", concept)
        atlas = self.wiki("Projets/Atlas/Atlas.md")
        self.assertIn("[[Projets/Solitaire/Solitaire|Solitaire]]", atlas)
        self.assertIn("Tailscale", self.wiki("index.md"))
        self.assertIn("tisseur", git(self.server_home / "wiki", "log", "--format=%an"))

    def test_dismissed_concept_is_not_proposed_again_until_it_spreads(self):
        os.environ["FAKE_AGENT_MODE"] = "no_concepts"            # le tisseur lit, puis écarte le candidat
        self.two_projects_sharing_an_entity()
        state = json.loads((self.server_home / "state/tisseur.json").read_text(encoding="utf-8"))
        self.assertEqual(state["ecartes"]["tailscale"]["dossiers"], 2)
        self.assertFalse((self.server_home / "wiki/Concepts/Tailscale.md").exists())
        os.environ["FAKE_AGENT_MODE"] = "ok"
        calls = len(self.agent_calls())
        self.assertEqual(Server(self.config).run_tisseur(force=True), "")
        self.assertEqual(len(self.agent_calls()), calls)         # rien à proposer : l'agent n'est pas appelé
        self.claude_session(self.make_repo("troisieme", "git@github.com:moi/troisieme.git"), [
            ("user", "Un troisième projet doit lui aussi choisir comment joindre son serveur."),
            ("assistant", "DECISION: Réseau privé aussi | Ce projet passe par le même réseau privé. | Cohérence. @Tailscale"),
        ], start="2026-11-14T09:00:00Z")
        self.host_sync()
        self.server_run()                                        # trois dossiers au lieu de deux : reproposé
        self.assertTrue((self.server_home / "wiki/Concepts/Tailscale.md").exists())
        state = json.loads((self.server_home / "state/tisseur.json").read_text(encoding="utf-8"))
        self.assertNotIn("tailscale", state["ecartes"])

    def test_tisseur_cannot_rewrite_text(self):
        os.environ["FAKE_AGENT_MODE"] = "rewrite"
        self.two_projects_sharing_an_entity()
        self.assertNotIn("Phrase ajoutée par le tisseur", self.wiki("Projets/Atlas/Atlas.md"))
        self.assertFalse((self.server_home / "wiki/Concepts/Tailscale.md").exists())

    # ── vérificateur et revue ─────────────────────────────────────────────
    def seed_for_review(self):
        self.solitaire_session()
        self.host_sync()
        self.server_run()
        for name, body in (("Doublon", "Copie."), ("Long", "Texte.\nRépétition.\nRépétition.")):
            target = self.vault / f"Sujets/{name}/{name}.md"
            target.parent.mkdir(parents=True)
            target.write_text(f"---\ntype: sujet\ntitre: {name}\nresume: Page {name}.\n---\n\n{body}\n", encoding="utf-8")
        self.server_run()

    def test_review_is_proposed_then_partially_approved(self):
        self.seed_for_review()
        reviews = Reviews(Server(self.config))
        self.assertEqual(reviews.run(), 0)
        review = reviews.pending()
        self.assertEqual([a["type"] for a in review["actions"]], ["suppression", "condensation"])
        self.assertEqual(len(review["questions"]), 1)
        # rien n'a changé tant que tu n'as pas répondu
        self.assertTrue((self.server_home / "wiki/Sujets/Doublon/Doublon.md").exists())
        self.assertTrue((self.vault / "Sujets/Doublon/Doublon.md").exists())
        self.assertEqual(reviews.run(), 0)   # pas de seconde revue tant que la première attend
        self.assertEqual(len(reviews.all()), 1)

        message = reviews.decide(skip=[2])
        self.assertIn("1 action(s) appliquée(s), 1 refusée(s)", message)
        self.assertFalse((self.server_home / "wiki/Sujets/Doublon/Doublon.md").exists())
        self.assertFalse((self.vault / "Sujets/Doublon/Doublon.md").exists())
        self.assertIn("Répétition.", self.wiki("Sujets/Long/Long.md"))
        self.assertIn("Condenser Long.md", self.wiki(".memex/refus.jsonl"))
        self.assertIn("page supprimée", self.wiki("log.md"))
        self.assertIsNone(reviews.pending())

        # la page supprimée se retrouve dans l'historique
        self.assertIn("rétablie", restore(Server(self.config), "Sujets/Doublon/Doublon.md"))
        self.assertTrue((self.vault / "Sujets/Doublon/Doublon.md").exists())

    def test_review_reject_changes_nothing(self):
        self.seed_for_review()
        reviews = Reviews(Server(self.config))
        reviews.run()
        self.assertIn("0 action(s) appliquée(s), 2 refusée(s)", reviews.decide(reject_all=True))
        self.assertTrue((self.server_home / "wiki/Sujets/Doublon/Doublon.md").exists())
        self.assertEqual(self.wiki(".memex/refus.jsonl").count("\n"), 2)

    # ── sauvegarde et reconstruction ──────────────────────────────────────
    def test_backup_then_rebuild_on_an_empty_server(self):
        self.two_projects_sharing_an_entity()
        archive = serverops.backup(self.config)
        expected = self.wiki("Projets/Solitaire/Solitaire.md")
        commits = git(self.server_home / "wiki", "rev-list", "--count", "main")
        new_home = self.root / "nouveau-serveur"
        self.set(MEMEX_HOME=str(new_home), MEMEX_VAULT_DIR=str(self.root / "nouveau-vault"))
        self.assertIn("restauré", serverops.restore_backup(self.config, archive))
        self.assertEqual((new_home / "wiki/Projets/Solitaire/Solitaire.md").read_text(encoding="utf-8"), expected)
        self.assertEqual(git(new_home / "wiki", "rev-list", "--count", "main"), commits)
        self.assertEqual(Server(self.config).run(), 0)
        self.assertTrue((self.root / "nouveau-vault/Projets/Solitaire/Solitaire.md").exists())
        # l'hôte se rebranche sur le nouveau serveur sans rien perdre
        self.assertEqual(pull(self.config).error, "")
        self.assertTrue((self.data / "wiki/Projets/Atlas/Atlas.md").exists())

    def test_rebuild_from_hosts_alone(self):
        """Serveur perdu sans sauvegarde : les hôtes renvoient leurs condensés et le wiki se reconstruit."""
        self.two_projects_sharing_an_entity()
        self.set(MEMEX_HOME=str(self.root / "serveur-neuf"), MEMEX_VAULT_DIR="")
        self.assertEqual(resend_all(self.config), 2)
        self.assertEqual(ship(self.config).sent, 2)
        self.assertEqual(Server(self.config).run(), 0)
        self.assertTrue((self.root / "serveur-neuf/wiki/Projets/Solitaire/Solitaire.md").exists())
        self.assertTrue((self.root / "serveur-neuf/wiki/Projets/Atlas/Atlas.md").exists())

    def test_offline_host_keeps_segments_until_a_server_exists(self):
        self.set(MEMEX_SERVER_SSH="")
        self.solitaire_session()
        Sweeper(self.config).run()
        self.assertEqual(ship(self.config).pending, 1)
        self.set(MEMEX_SERVER_SSH="local")
        self.assertEqual(ship(self.config).sent, 1)


class EndToEndSandboxed(Sandbox):
    """Le trajet nominal, avec les agents réellement enfermés dans bubblewrap."""

    overrides = {"MEMEX_SANDBOX": "bwrap"}

    def test_pipeline_works_inside_the_sandbox(self):
        from memex.agent import Agent

        if "actif" not in Agent(self.config).sandbox_state():
            self.skipTest("bubblewrap indisponible ici")
        repo = self.make_repo("solitaire", "git@github.com:moi/solitaire.git")
        self.claude_session(repo, [
            ("user", LONG),
            ("assistant", "DECISION: Scores côté serveur | Les scores sont stockés sur le serveur. | Classement partagé."),
        ])
        self.assertEqual(sync(self.config, quiet=True), 0)
        self.assertEqual(Server(self.config).run(), 0)
        self.assertIn("Scores côté serveur", (self.server_home / "wiki/Projets/Solitaire/Solitaire.md").read_text(encoding="utf-8"))
        self.assertTrue((self.vault / "Projets/Solitaire/Décisions/2026-11-12 Scores côté serveur.md").exists())


if __name__ == "__main__":
    unittest.main()
