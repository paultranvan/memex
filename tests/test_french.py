"""A wiki written in French: one scenario from end to end, then the deterministic layer piece by piece.

French is more than the second language shipped: a French wiki was in
production before the project was translated, with its folders, its header
keys and its segments in the old format. The fixtures of this file are in
French on purpose; everywhere else the tests are in English.
"""
import contextlib
import io
import json
import unittest

from helpers import LangCase, Sandbox, git, write_pages

from memex import frontmatter, hook, i18n
from memex.index import build as build_index, compact as compact_index, p_status
from memex.lint import lint
from memex.pages import LinkIndex, folder_of, infer_type, is_decision_path, load_wiki, parse_page, safe_name
from memex.pipeline import Server
from memex.review import Reviews, restore
from memex.ship import sync
from memex.validate import normalize_for_links, validate

FRENCH = {"MEMEX_LANG": "fr"}
# A segment as the hosts wrote them before the translation, and as their `sent` folders still hold them.
LEGACY_SEGMENT = """---
format: memex/segment-1
id: claude-code_ancienne_001
source: claude-code
device: laptop
session: ancienne
seq: 1
started: 2026-11-10T08:00:00Z
ended: 2026-11-10T08:05:00Z
project: solitaire
project_name: solitaire
project_remote: github.com/me/solitaire
title: Format des sauvegardes
---

## Utilisateur — 2026-11-10T08:00:00Z

Il faut décider du format des sauvegardes de partie, et le noter durablement.

## Assistant — 2026-11-10T08:05:00Z

DECISION: Sauvegardes en JSON | Les parties sont sauvegardées en JSON. | Lisible et simple.
"""


class FrenchEndToEnd(Sandbox):
    overrides = FRENCH

    def wiki(self, relative):
        return (self.server_home / "wiki" / relative).read_text(encoding="utf-8")

    def test_french_wiki_from_session_to_review(self):
        solitaire = self.make_repo("solitaire", "git@github.com:me/solitaire.git")
        self.claude_session(solitaire, [
            ("user", "Nous devons choisir où stocker les scores du jeu, en local ou sur un serveur."),
            ("assistant", "DECISION: Scores côté serveur | Les scores sont stockés sur le serveur. | Classement partagé entre joueurs. @Tailscale\nDEAD_END: WebSocket | Essai en WebSocket abandonné. | Trop lourd."),
        ], title="Stockage des scores")
        self.claude_session(solitaire, [("user", "ok merci"), ("assistant", "De rien.")], title="Remerciement", start="2026-11-12T11:00:00Z")
        self.claude_session(self.make_repo("atlas", "git@github.com:me/atlas.git"), [
            ("user", "Pour le déploiement d'Atlas, il faut décider comment joindre le serveur."),
            ("assistant", "DECISION: Accès par réseau privé | Le serveur est joint par un réseau privé, comme pour Solitaire. | Pas de port public. @Tailscale"),
        ], title="Accès au serveur", start="2026-11-13T09:00:00Z")
        self.codex_session(self.root, [
            ("user", "Parlons d'épargne : un plan est-il pertinent pour moi, et où l'ouvrir ? TOPIC: Plan d'épargne"),
            ("assistant", "DECISION: Ouvrir le plan chez un courtier en ligne | Le plan d'épargne sera ouvert chez un courtier en ligne. | Frais plus bas."),
        ])
        write_pages(self.config.outbox, {"20261110T080000Z_claude-code_ancienne_001.md": LEGACY_SEGMENT})
        self.assertEqual(sync(self.config, quiet=True), 0)
        self.assertEqual(Server(self.config).run(), 0)
        wiki = self.server_home / "wiki"

        with self.subTest("the wiki records its language and gets the French conventions"):
            self.assertEqual(json.loads(self.wiki(".memex/wiki.json")), {"lang": "fr"})
            self.assertIn("# Conventions du wiki", self.wiki("AGENTS.md"))
            folders = sorted(p.name for p in wiki.iterdir() if p.is_dir() and not p.name.startswith("."))
            self.assertEqual(folders, ["Concepts", "Journal", "Projets", "Sujets"])

        with self.subTest("pages and decision records use the French folders and keys"):
            main = self.wiki("Projets/Solitaire/Solitaire.md")
            self.assertIn("type: projet\ntitre: Solitaire\n", main)
            self.assertIn("entites:\n  - Tailscale\n", main)
            self.assertRegex(main, r"\nmaj: \d{4}-\d{2}-\d{2}\n")
            record = self.wiki("Projets/Solitaire/Décisions/2026-11-12 Scores côté serveur.md")
            self.assertIn("type: decision\ntitre: Scores côté serveur\n", record)
            self.assertIn("statut: active\n", record)
            self.assertIn("sources:\n  - claude-code_", record)
            self.assertIn("Classement partagé entre joueurs.", record)
            topic = self.wiki("Sujets/Plan d'épargne/Plan d'épargne.md")
            self.assertIn("type: sujet\n", topic)
            self.assertIn("courtier en ligne", topic)
            self.assertEqual(json.loads(self.wiki(".memex/projects.json"))["github.com/me/solitaire"], "Projets/Solitaire")

        with self.subTest("a segment in the old format is integrated like the others"):
            self.assertIn("Lisible et simple.", self.wiki("Projets/Solitaire/Décisions/2026-11-10 Sauvegardes en JSON.md"))
            self.assertTrue((wiki / "Journal/2026/2026-11-10.md").is_file())

        with self.subTest("index"):
            index = self.wiki("index.md")
            self.assertTrue(index.startswith("---\ntype: meta\ntitre: Index\n---\n\n# Index du wiki\n"))
            for expected in (
                "\n## Projets\n", "\n## Sujets\n", "\n## Concepts\n", "\n## Décisions récentes\n",
                "- [[Projets/Solitaire/Solitaire|Solitaire]] — Page of Solitaire. · 2 décision(s), dont 2 active(s)\n",
                "- [[Sujets/Plan d'épargne/Plan d'épargne|Plan d'épargne]]", "- [[Concepts/Tailscale|Tailscale]]",
                "- 2026-11-12 — [[Projets/Solitaire/Décisions/2026-11-12 Scores côté serveur|Scores côté serveur]] · Solitaire\n",
            ):
                self.assertIn(expected, index)

        with self.subTest("journal"):
            journal = self.wiki("Journal/2026/2026-11-12.md")
            self.assertTrue(journal.startswith("---\ntype: journal\ntitre: Journal du 2026-11-12\n---\n\n# Journal du 2026-11-12\n"))
            self.assertIn("- **09:00** · [[Projets/Solitaire/Solitaire|Solitaire]] · claude-code sur laptop — Test session. → ", journal)
            self.assertIn("- **11:00** · solitaire · claude-code sur laptop — session courte : Remerciement `claude-code_", journal)

        with self.subTest("operations log"):
            log = self.wiki("log.md")
            self.assertTrue(log.startswith("---\ntype: meta\ntitre: Journal des opérations\n---\n\n# Journal des opérations\n"))
            self.assertIn("] archiviste | Stockage des scores\n", log)
            self.assertRegex(log, r"\] tisseur \| \d+ page\(s\) reliée\(s\)\n")

        with self.subTest("the weaver links the pages and creates the concept"):
            concept = self.wiki("Concepts/Tailscale.md")
            self.assertIn("type: concept\ntitre: Tailscale\n", concept)
            self.assertRegex(concept, r"\nmaj: \d{4}-\d{2}-\d{2}\n")
            self.assertIn("comme pour [[Projets/Solitaire/Solitaire|Solitaire]].", self.wiki("Projets/Atlas/Atlas.md"))

        with self.subTest("the agent is told the language; commits stay in English"):
            calls = self.agent_calls()
            self.assertEqual({c["lang"] for c in calls}, {"fr"})
            self.assertEqual({c["role"] for c in calls}, {"extract", "integrate", "weave"})
            self.assertEqual([c for c in calls if c["placeholders"]], [])
            commits = git(wiki, "log", "--format=%an|%s")
            self.assertIn("archivist|archivist: Stockage des scores", commits)
            self.assertRegex(commits, r"weaver\|weaver: \d+ page\(s\) linked")
            self.assertIn("memex|journal: Remerciement (short)", commits)
            self.assertFalse((wiki / "Projects").exists() or (wiki / "Topics").exists())

        with self.subTest("vault and host"):
            self.assertTrue((self.vault / "Projets/Solitaire/Décisions/2026-11-12 Scores côté serveur.md").is_file())
            self.assertEqual(sync(self.config, quiet=True), 0)
            i18n.use("en")                              # a host process starts without knowing the wiki's language
            context = hook.project_context(self.config, str(solitaire))
            self.assertIn('# Memory of project "Solitaire" (from earlier sessions)', context)
            self.assertIn("Scores côté serveur", context)
            self.assertIn("detailed decision records: `Projets/Solitaire/Décisions/`", context)

        with self.subTest("curator, review and restore"):
            write_pages(self.vault, {"Sujets/Duplicate/Duplicate.md": "---\ntype: sujet\ntitre: Doublon\nresume: Page en double.\n---\n\nCopie.\n"})
            server = Server(self.config)
            self.assertEqual(server.run(), 0)
            reviews = Reviews(server)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(reviews.run(), 0)
            review = reviews.pending()
            self.assertEqual([a["kind"] for a in review["actions"]], ["delete"])
            self.assertIn("1 action(s) applied, 0 rejected", reviews.decide())
            self.assertIn(
                f"] vérificateur | revue {review['id']} : 1 action(s) appliquée(s)\n- suppression : Delete Duplicate.md\n"
                "- page supprimée : `Sujets/Duplicate/Duplicate.md` (retrouvable dans l'historique)\n", self.wiki("log.md"))
            self.assertEqual(restore(Server(self.config), "Sujets/Duplicate/Duplicate.md"), '"Sujets/Duplicate/Duplicate.md" restored.')
            self.assertRegex(self.wiki("log.md"), r"\] restauration \| Sujets/Duplicate/Duplicate\.md\n- version reprise : `[0-9a-f]+\^`\n")
            self.assertEqual({c["lang"] for c in self.agent_calls()}, {"fr"})
            self.assertEqual([c for c in self.agent_calls() if c["placeholders"]], [])


class FrenchWikiWithoutARecordedLanguage(Sandbox):
    """The wiki in production was created before `.memex/wiki.json` existed: only its folders say that it is French."""

    init_server = False

    def test_existing_french_wiki_keeps_working_in_french_whatever_memex_lang_says(self):
        self.set(MEMEX_LANG="fr")
        server = Server(self.config)
        server.init()
        wiki = self.server_home / "wiki"
        write_pages(wiki, {"Projets/Solitaire/Solitaire.md": "---\ntype: projet\ntitre: Solitaire\nresume: Un jeu de cartes.\n---\n\n## En bref\n\nUn jeu.\n"})
        (wiki / ".memex/wiki.json").unlink()
        server.repo.commit_all(wiki, "the wiki as it was before the language layer", "human")
        self.set(MEMEX_LANG="en")                       # the default of any installation that says nothing
        i18n.use("en")

        self.assertEqual(Server(self.config).locale.lang, "fr")
        self.assertEqual(i18n.current().lang, "fr")
        repo = self.make_repo("solitaire", "git@github.com:me/solitaire.git")
        self.claude_session(repo, [
            ("user", "Encore une décision à prendre sur ce projet, à propos du thème graphique."),
            ("assistant", "DECISION: Thème sombre | Le jeu utilise un thème sombre. | Plus lisible."),
        ], title="Thème graphique")
        self.assertEqual(sync(self.config, quiet=True), 0)
        self.assertEqual(Server(self.config).run(), 0)
        record = (wiki / "Projets/Solitaire/Décisions/2026-11-12 Thème sombre.md").read_text(encoding="utf-8")
        self.assertIn("statut: active\n", record)
        self.assertIn("Thème sombre", (wiki / "Projets/Solitaire/Solitaire.md").read_text(encoding="utf-8"))
        self.assertIn("# Index du wiki", (wiki / "index.md").read_text(encoding="utf-8"))
        self.assertIn("· 1 décision(s), dont 1 active(s)", (wiki / "index.md").read_text(encoding="utf-8"))
        self.assertFalse((wiki / "Projects").exists())
        self.assertEqual({c["lang"] for c in self.agent_calls()}, {"fr"})
        # and on the host, whose clone carries no record either
        self.assertEqual(sync(self.config, quiet=True), 0)
        i18n.use("en")
        self.assertIn("detailed decision records: `Projets/Solitaire/Décisions/`", hook.project_context(self.config, str(repo)))


class FrenchPages(LangCase):
    lang = "fr"

    def test_french_schema_is_the_one_of_the_existing_wiki(self):
        """These names are on disk in a wiki that already exists: none of them may drift, not even towards better French."""
        fr = i18n.current()
        self.assertEqual((fr.projects, fr.topics, fr.concepts, fr.journal, fr.decisions), ("Projets", "Sujets", "Concepts", "Journal", "Décisions"))
        self.assertEqual(
            (i18n.K_TYPE, fr.k_title, fr.k_summary, fr.k_entities, i18n.K_ALIASES, i18n.K_DATE, fr.k_status, fr.k_supersedes,
             fr.k_superseded_by, i18n.K_SOURCES, fr.k_updated),
            ("type", "titre", "resume", "entites", "aliases", "date", "statut", "remplace", "remplacee_par", "sources", "maj"))
        self.assertEqual(fr.types, {
            "project": "projet", "topic": "sujet", "decision": "decision", "concept": "concept", "synthesis": "synthese",
            "journal": "journal", "meta": "meta"})
        self.assertEqual(fr.statuses, {"active": "active", "superseded": "remplacee", "abandoned": "abandonnee"})
        self.assertEqual(fr.see_also, "Voir aussi")
        self.assertEqual(fr.roles, {"archivist": "archiviste", "weaver": "tisseur", "curator": "vérificateur", "restore": "restauration"})
        self.assertEqual(fr.kinds, {
            "merge": "fusion", "condense": "condensation", "fix": "correction", "delete": "suppression", "move": "rangement"})

    def test_page_properties_read_the_french_keys(self):
        page = parse_page("Projets/Solitaire/Décisions/2026-11-12 Scores.md", (
            '---\ntype: decision\ntitre: "Scores : côté serveur"\nresume: Les scores   sont stockés sur le serveur.\n'
            'entites: [Tailscale, "Open WebUI"]\naliases: [classement]\ndate: 2026-11-12\nstatut: remplacee\n'
            'remplacee_par: "[[Projets/Solitaire/Décisions/2026-12-01 Scores locaux]]"\n---\n\nCorps.\n'
        ))
        self.assertEqual(page.title, "Scores : côté serveur")
        self.assertEqual(page.summary, "Les scores sont stockés sur le serveur.")
        self.assertEqual(page.entities, ["Tailscale", "Open WebUI"])
        self.assertEqual(page.aliases, ["classement"])
        self.assertEqual((page.kind, page.is_decision, page.folder, page.stem), ("decision", True, "Projets/Solitaire", "2026-11-12 Scores"))
        self.assertEqual(p_status(page), "remplacee")
        # the English keys mean nothing here: the title falls back on the heading, the type on the path
        english = parse_page("Sujets/Épargne/Épargne.md", "---\ntitle: Savings\nsummary: In English.\nentities: [x]\nstatus: superseded\n---\n\n# Épargne\n\nTexte.\n")
        self.assertEqual((english.title, english.summary, english.entities), ("Épargne", "", []))
        self.assertEqual((english.kind, english.is_decision, english.folder, p_status(english)), ("sujet", False, "Sujets/Épargne", "active"))
        self.assertTrue(parse_page("Sujets/Épargne/Choix.md", "---\ntype: decision\n---\n\nx\n").is_decision)

    def test_folders_and_types_follow_the_french_names(self):
        self.assertEqual(folder_of("Projets/Solitaire/Décisions/x.md"), "Projets/Solitaire")
        self.assertEqual(folder_of("Sujets/Épargne/Épargne.md"), "Sujets/Épargne")
        self.assertEqual(folder_of("Sujets/Épargne.md"), "Sujets/Épargne")
        self.assertEqual(folder_of("Concepts/Réseau.md"), "Concepts")
        self.assertEqual(folder_of("index.md"), "")
        self.assertEqual(folder_of("Projects/Solitaire/Decisions/x.md"), "Projects")     # just another folder in a French wiki
        expected = {
            "Projets/Solitaire/Solitaire.md": "projet", "Sujets/Épargne/Épargne.md": "sujet",
            "Projets/Solitaire/Décisions/2026-11-12 Scores.md": "decision", "Sujets/Épargne/Décisions/x.md": "decision",
            "Concepts/Réseau.md": "concept", "Journal/2026/2026-11-12.md": "journal", "index.md": "meta",
            "Projects/Solitaire/Solitaire.md": "meta", "Projets/Solitaire/Decisions/x.md": "projet",
        }
        self.assertEqual({path: infer_type(path) for path in expected}, expected)
        self.assertTrue(is_decision_path("Sujets/Épargne/Décisions/x.md"))
        self.assertFalse(is_decision_path("Sujets/Épargne/Decisions/x.md"))

    def test_index_and_page_list_are_written_in_french(self):
        texts = {
            "Projets/Solitaire/Solitaire.md": "---\ntype: projet\ntitre: Solitaire\nresume: Un jeu.\n---\n\nTexte.\n",
            "Projets/Solitaire/Décisions/2026-11-12 Scores.md": "---\ntype: decision\ntitre: Scores\ndate: 2026-11-12\nstatut: remplacee\n---\n\nx\n",
            "Projets/Solitaire/Décisions/2026-11-13 Thème.md": "---\ntype: decision\ntitre: Thème sombre\ndate: 2026-11-13\n---\n\nx\n",
            "Sujets/Épargne/Épargne.md": "---\ntype: sujet\ntitre: Épargne\nresume: Où placer.\n---\n\n" + "mot " * 400,
        }
        pages = {path: parse_page(path, text) for path, text in texts.items()}
        text = build_index(pages)
        self.assertTrue(text.startswith(
            "---\ntype: meta\ntitre: Index\n---\n\n# Index du wiki\n\n"
            "> Généré automatiquement par memex à partir des en-têtes des pages. Ne pas modifier à la main.\n"))
        self.assertIn("\n## Projets\n\n- [[Projets/Solitaire/Solitaire|Solitaire]] — Un jeu. · 2 décision(s), dont 1 active(s)\n", text)
        self.assertIn("\n## Sujets\n\n- [[Sujets/Épargne/Épargne|Épargne]] — Où placer.\n", text)
        self.assertIn("\n## Concepts\n\n_(aucun pour l'instant)_\n", text)
        self.assertIn(
            "\n## Décisions récentes\n\n"
            "- 2026-11-13 — [[Projets/Solitaire/Décisions/2026-11-13 Thème|Thème sombre]] · Solitaire\n"
            "- 2026-11-12 — [[Projets/Solitaire/Décisions/2026-11-12 Scores|Scores]] · Solitaire _(remplacee)_\n", text)
        listing = compact_index(pages, long_page=1000)
        self.assertEqual(listing.splitlines(), [
            "- Projets/Solitaire/Solitaire.md — Solitaire : Un jeu.",
            f"- Sujets/Épargne/Épargne.md — Épargne [déjà longue : {len(pages['Sujets/Épargne/Épargne.md'].body)} caractères] : Où placer.",
            "- Projets/Solitaire/Décisions/ — 2 fiche(s) de décision",
        ])
        self.assertNotIn("déjà longue", compact_index(pages))
        self.assertEqual(compact_index({}), "(le wiki est encore vide)")
        self.assertTrue(compact_index(pages, limit=30).endswith("\n… (liste tronquée : cherche dans les fichiers)"))

    def test_headers_and_file_names_keep_their_accents(self):
        text = (
            '---\ntype: decision\ntitre: "Scores : côté serveur"\ndate: 2026-11-12\nactif: true\nn: 3\n'
            "entites: [Tailscale, \"Open WebUI\"]\nsources:\n  - a_1\n  - b_2\nremplacee_par: \"[[Projets/X/Décisions/y]]\"\n---\n\nCorps.\n"
        )
        meta, body = frontmatter.parse(text)
        self.assertEqual(meta["titre"], "Scores : côté serveur")
        self.assertEqual(meta["entites"], ["Tailscale", "Open WebUI"])
        self.assertEqual(meta["remplacee_par"], "[[Projets/X/Décisions/y]]")
        self.assertEqual(frontmatter.parse(frontmatter.dump(meta, body)), (meta, body))
        self.assertIn("titre: Thème sombre\n", frontmatter.dump({"titre": "Thème sombre"}, "x"))      # written as is, not escaped
        self.assertEqual(safe_name("2026-11-13 Architecture : VPS central.md"), "2026-11-13 Architecture - VPS central.md")
        self.assertEqual(safe_name('Quoi ? "Ça" #1 [x].md'), "Quoi Ça 1 x.md")
        self.assertEqual(safe_name("Déjà sûr (v2).md"), "Déjà sûr (v2).md")
        self.assertEqual(safe_name("???.md"), "sans-titre.md")

    def test_weaver_comparison_ignores_the_french_bookkeeping_keys_and_voir_aussi(self):
        a = "---\ntitre: T\nentites: [x]\n---\n\nP utilise Tailscale.\n"
        b = "---\ntitre: T\nentites: [x, y]\nmaj: 2026\n---\n\nP utilise [[Concepts/Tailscale|Tailscale]].\n\n## Voir aussi\n\n- [[A]]\n"
        self.assertEqual(normalize_for_links(a), normalize_for_links(b))
        self.assertNotEqual(normalize_for_links(a), normalize_for_links(b.replace("utilise", "emploie")))
        self.assertNotEqual(normalize_for_links(a), normalize_for_links(b.replace("## Voir aussi", "## See also")))
        self.assertNotEqual(normalize_for_links(a), normalize_for_links(b.replace("maj: 2026", "updated: 2026")))


class FrenchWiki(Sandbox):
    overrides = FRENCH

    def test_links_index_and_lint(self):
        root = self.server_home / "wiki"
        write_pages(root, {
            "Projets/Solitaire/Solitaire.md": "---\ntype: projet\ntitre: Solitaire\nresume: Un jeu.\nentites: [Tailscale]\n---\n\nVoir [[Concepts/Réseau|le réseau]] et [[Page absente]]. `[[pas un lien]]`\n",
            "Projets/Solitaire/Décisions/2026-11-12 Scores.md": "---\ntype: decision\ntitre: Scores\ndate: 2026-11-12\nstatut: remplacee\n---\n\nx\n",
            "Projets/Solitaire/Décisions/Sans date.md": "---\ntype: decision\ntitre: Sans date\n---\n\nx\n",
            "Projets/Atlas/Atlas.md": "---\ntype: projet\ntitre: Atlas\nresume: Finance.\nentites: [tailscale]\n---\n\nComme pour Solitaire, on passe par un réseau privé.\n",
            "Concepts/Réseau.md": "---\ntype: concept\ntitre: Réseau\naliases: [réseau privé]\n---\n\nx\n",
            "Sujets/Seul/Notes.md": "---\ntitre: Notes\n---\n\nx\n",
        })
        pages = load_wiki(root)
        resolver = LinkIndex(list(pages))
        self.assertEqual(resolver.resolve("réseau"), "Concepts/Réseau.md")
        self.assertEqual(resolver.resolve("Solitaire/Solitaire"), "Projets/Solitaire/Solitaire.md")
        text = build_index(pages)
        self.assertIn("[[Projets/Solitaire/Solitaire|Solitaire]] — Un jeu. · 2 décision(s), dont 1 active(s)", text)
        self.assertIn("_(remplacee)_", text)
        report = lint(root)
        self.assertEqual(report.broken, [{"page": "Projets/Solitaire/Solitaire.md", "target": "Page absente"}])
        self.assertIn("Projets/Solitaire/Décisions/2026-11-12 Scores.md", report.orphans)
        unlinked = {(u["page"], u["target"]) for u in report.unlinked}
        self.assertIn(("Projets/Atlas/Atlas.md", "Projets/Solitaire/Solitaire.md"), unlinked)
        self.assertIn(("Projets/Atlas/Atlas.md", "Concepts/Réseau.md"), unlinked)
        self.assertEqual([(c["entity"].lower(), c["folders"]) for c in report.concepts], [("tailscale", ["Projets/Atlas", "Projets/Solitaire"])])
        # the report is in English, the keys it names are those of the wiki
        self.assertEqual(report.problems, [
            {"page": "Concepts/Réseau.md", "problem": "header without `resume` field"},
            {"page": "Projets/Solitaire/Décisions/Sans date.md", "problem": "decision record without `date`"},
            {"page": "Sujets/Seul/Notes.md", "problem": "header without `type` field"},
            {"page": "Sujets/Seul/Notes.md", "problem": "header without `resume` field"},
        ])
        self.assertEqual(report.stats, {"pages": 6, "decisions": 2, "concepts": 1, "folders": 3})

    def test_bookkeeping_uses_the_french_names(self):
        from memex.segment import Message, Segment
        from memex.util import now

        server = Server(self.config)
        root = server.repo.dir
        seg = Segment("claude-code", "d", "s", 1, [Message("user", "x")], now(), now(), project="atlas", project_name="atlas")
        self.assertEqual(server.folder_for(root, seg), ("Projets/Atlas", True))
        (root / "Sujets/Atlas").mkdir(parents=True)
        self.assertEqual(server.folder_for(root, seg), ("Sujets/Atlas", False))
        self.assertEqual(Server._folder_of_result({"folder": "Sujets/Épargne/"}, []), "Sujets/Épargne")
        self.assertEqual(Server._folder_of_result({"folder": "Topics/Savings"}, ["Projets/Atlas/Décisions/x.md"]), "Projets/Atlas")
        write_pages(root, {"Sujets/Atlas/Atlas.md": "---\ntype: sujet\ntitre: Atlas\nauteur:\n  nom: Camille\n---\n\nTexte.\n"})
        server._stamp_pages(root, ["Sujets/Atlas/Atlas.md"], "claude-code_abc_001")
        text = (root / "Sujets/Atlas/Atlas.md").read_text(encoding="utf-8")
        self.assertIn("auteur:\n  nom: Camille\nsources:\n  - claude-code_abc_001\n", text)
        self.assertRegex(text, r"\nmaj: \d{4}-\d{2}-\d{2}\n---\n")
        self.assertNotIn("updated:", text)


class FrenchGuardrails(Sandbox):
    overrides = FRENCH

    def setUp(self):
        super().setUp()
        self.server = Server(self.config)
        self.repo = self.server.repo
        write_pages(self.repo.dir, {
            "Projets/P/P.md": "---\ntype: projet\ntitre: P\nresume: Projet P.\n---\n\nP utilise Tailscale pour joindre le serveur.\n",
            "Projets/P/Décisions/2026-01-01 Choix.md": "---\ntype: decision\ntitre: Choix\ndate: 2026-01-01\nstatut: active\n---\n\n## Décision\n\nOn fait A.\n",
            "Concepts/Tailscale.md": "---\ntype: concept\ntitre: Tailscale\nresume: Réseau privé.\n---\n\nUn réseau privé.\n",
        })
        self.repo.commit_all(self.repo.dir, "base", "memex")
        self.tree = self.repo.new_worktree("t")
        self.base = self.repo.head("HEAD", cwd=self.tree)

    def check(self, role):
        return validate(self.repo, self.tree, self.base, role, "2026-11-12")

    def reset(self):
        self.repo.git("reset", "--hard", "-q", self.base, cwd=self.tree), self.repo.git("clean", "-fdq", cwd=self.tree)

    def test_decision_record_is_immutable_except_for_statut_and_remplacee_par(self):
        record = self.tree / "Projets/P/Décisions/2026-01-01 Choix.md"
        original = record.read_text()
        record.write_text(original.replace("On fait A.", "On fait Z."))
        verdict = self.check("archivist")
        self.assertEqual(verdict.errors, [
            "Projets/P/Décisions/2026-01-01 Choix.md: an existing decision record is immutable "
            "(only `statut` and `remplacee_par` may change)"])
        record.write_text(original.replace("statut: active", 'statut: remplacee\nremplacee_par: "[[Projets/P/Décisions/x]]"'))
        verdict = self.check("archivist")
        self.assertTrue(verdict.ok, verdict.errors)
        self.assertEqual(verdict.touched, ["Projets/P/Décisions/2026-01-01 Choix.md"])
        # the English keys are not the wiki's: adding them is a change like any other
        record.write_text(original.replace("statut: active", 'statut: active\nstatus: superseded\nsuperseded_by: "[[Projets/P/Décisions/x]]"'))
        self.assertFalse(self.check("archivist").ok)
        record.write_text(original)
        (self.tree / "Projets/P/Décisions/2026-01-01 Choix.md").unlink()
        self.assertFalse(self.check("curator").ok)

    def test_new_pages_get_a_french_header(self):
        write_pages(self.tree, {
            "Projets/P/Décisions/2026-11-12 Nouveau.md": "## Décision\n\nOn fait B.\n",
            "Sujets/Épargne/Épargne.md": "# Plan d'épargne\n\nOù en est-on.\n",
            "Projects/P/P.md": "---\ntype: project\ntitle: P\n---\n\nIn the wrong language.\n",
        })
        verdict = self.check("archivist")
        self.assertTrue(verdict.ok, verdict.errors)
        self.assertEqual(sorted(verdict.touched), ["Projets/P/Décisions/2026-11-12 Nouveau.md", "Sujets/Épargne/Épargne.md"])
        meta, _ = frontmatter.parse((self.tree / "Projets/P/Décisions/2026-11-12 Nouveau.md").read_text())
        self.assertEqual(meta, {"type": "decision", "titre": "2026-11-12 Nouveau", "date": "2026-11-12", "statut": "active"})
        meta, _ = frontmatter.parse((self.tree / "Sujets/Épargne/Épargne.md").read_text())
        self.assertEqual(meta, {"type": "sujet", "titre": "Plan d'épargne"})
        self.assertIn("Sujets/Épargne/Épargne.md: no `resume` field in the header", verdict.warnings)
        # `Projects/` is not a folder of a French wiki
        self.assertIn("Projects/P/P.md: outside the wiki folders, file removed", verdict.warnings)
        self.assertFalse((self.tree / "Projects/P/P.md").exists())
        self.assertNotIn("Projects/", self.repo.git("status", "--porcelain", cwd=self.tree))

    def test_weaver_may_add_links_voir_aussi_and_concepts(self):
        page = self.tree / "Projets/P/P.md"
        original = page.read_text()
        linked = original.replace("Tailscale", "[[Concepts/Tailscale|Tailscale]]")
        page.write_text(linked + "\n## Voir aussi\n\n- [[Concepts/Tailscale]] — le réseau\n")
        write_pages(self.tree, {"Concepts/Nouveau.md": "---\ntype: concept\ntitre: Nouveau\nresume: x.\n---\n\nTexte.\n"})
        verdict = self.check("weaver")
        self.assertTrue(verdict.ok, verdict.errors)
        self.assertEqual(verdict.added, ["Concepts/Nouveau.md"])
        # the section allowed is the one of the wiki's language
        page.write_text(linked + "\n## See also\n\n- [[Concepts/Tailscale]] — le réseau\n")
        verdict = self.check("weaver")
        self.assertEqual(verdict.errors, [
            'Projets/P/P.md: the weaver changed the text; it may only add links and a "Voir aussi" section'])
        page.write_text(linked.replace("pour joindre", "afin de joindre"))
        self.assertFalse(self.check("weaver").ok)
        self.reset()
        write_pages(self.tree, {"Projets/P/Autre.md": "---\ntype: projet\ntitre: Autre\n---\n\nx\n"})
        self.assertEqual(self.check("weaver").errors, ["Projets/P/Autre.md: the weaver only creates pages in Concepts/"])
        self.reset()
        record = self.tree / "Projets/P/Décisions/2026-01-01 Choix.md"
        record.write_text(record.read_text().replace("On fait A.", "On fait [[Projets/P/P|A]]."))
        self.assertEqual(self.check("weaver").errors, ["Projets/P/Décisions/2026-01-01 Choix.md: a decision record cannot be changed"])


if __name__ == "__main__":
    unittest.main()
