"""Tests unitaires : en-têtes, masquage, projets, adaptateurs, porte, vault, garde-fous, branchements."""
import io
import json
import os
import sqlite3
import tarfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from helpers import Sandbox, git

from memex import frontmatter, gate, imports, integrate, segment as segment_mod
from memex.adapters import clean_human
from memex.adapters.claude_code import ClaudeCodeAdapter
from memex.adapters.codex import CodexAdapter
from memex.adapters.hermes import HermesAdapter
from memex.agent import AgentError, parse_json_reply
from memex.config import Config, parse_env_text
from memex.index import build as build_index
from memex.lint import lint
from memex.pages import LinkIndex, dossier_of, links, load_wiki, unlink
from memex.pipeline import Server
from memex.project import ProjectResolver, exclusion_reason, normalize_remote, strip_worktree
from memex.redact import Redactor
from memex.sweep import Sweeper
from memex.validate import normalize_for_links, validate
from memex.vault import Vault


class Frontmatter(unittest.TestCase):
    def test_roundtrip_and_tolerant_parsing(self):
        text = (
            '---\ntype: decision\ntitre: "Scores : côté serveur"\ndate: 2026-11-12\nactif: true\nn: 3\n'
            "entites: [Tailscale, \"Open WebUI\"]\nsources:\n  - a_1\n  - b_2\nremplacee_par: \"[[Projets/X/Décisions/y]]\"\n---\n\nCorps.\n"
        )
        meta, body = frontmatter.parse(text)
        self.assertEqual(meta["titre"], "Scores : côté serveur")
        self.assertEqual(meta["date"], "2026-11-12")
        self.assertIs(meta["actif"], True)
        self.assertEqual(meta["n"], 3)
        self.assertEqual(meta["entites"], ["Tailscale", "Open WebUI"])
        self.assertEqual(meta["sources"], ["a_1", "b_2"])
        self.assertEqual(meta["remplacee_par"], "[[Projets/X/Décisions/y]]")
        self.assertEqual(body, "Corps.\n")
        again, body2 = frontmatter.parse(frontmatter.dump(meta, body))
        self.assertEqual(again, meta)
        self.assertEqual(body2, body)

    def test_page_without_header(self):
        self.assertEqual(frontmatter.parse("# Titre\n\ntexte"), ({}, "# Titre\n\ntexte"))
        self.assertEqual(frontmatter.parse("---\npas fermé\n"), ({}, "---\npas fermé\n"))


class Redaction(unittest.TestCase):
    def test_known_secrets_are_masked(self):
        redact = Redactor()
        samples = [
            "clé sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789",
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789AB",
            "AKIAIOSFODNN7EXAMPLE",
            "postgres://admin:hunter2secret@db.example.com/app",
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
            'EXCHANGE_API_SECRET="s3cr3t-value-123"',
            "password: motdepasse123",
            "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\ndef\n-----END OPENSSH PRIVATE KEY-----",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
            "tskey-auth-kAbCdEf12345-ABCDEFGHIJKL",
        ]
        for sample in samples:
            out = redact(sample)
            self.assertIn("[MASQUÉ]", out, sample)
        self.assertNotIn("hunter2secret", redact("postgres://admin:hunter2secret@db.example.com/app"))
        self.assertIn("[IBAN]", redact("IBAN FR76 3000 6000 0112 3456 7890 189"))
        self.assertIn("[CARTE]", redact("carte 4242 4242 4242 4242"))
        self.assertGreaterEqual(redact.total, len(samples))

    def test_ordinary_text_is_untouched(self):
        redact = Redactor()
        for text in ("Le jeton de session expire au bout d'une heure.", "max_tokens = 4096", "git@github.com:moi/depot.git",
                     "Voir https://example.com/a/b?c=d", "La clé de voûte du projet est le balayeur."):
            self.assertEqual(redact(text), text)

    def test_extra_rules_and_emails(self):
        redact = Redactor(extra=[r"NUMCLIENT-\d+"], emails=True)
        self.assertEqual(redact("client NUMCLIENT-4421, moi@example.org"), "client [MASQUÉ], [EMAIL]")


class Projects(Sandbox):
    init_server = False

    def test_remote_normalisation(self):
        for url in ("git@github.com:Moi/Depot.git", "https://github.com/moi/depot", "ssh://git@github.com/moi/depot.git", "https://user:tok@github.com/moi/depot.git"):
            self.assertEqual(normalize_remote(url), "github.com/moi/depot")
        self.assertEqual(normalize_remote("ssh://git@host:2222/a/b.git"), "host/a/b")

    def test_repo_worktree_and_deleted_worktree_share_one_identity(self):
        repo = self.make_repo("solitaire", "git@github.com:moi/solitaire.git")
        (repo / "f").write_text("x")
        git(repo, "add", "."), git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        tree = repo / ".claude" / "worktrees" / "leaderboard"
        git(repo, "worktree", "add", "-q", str(tree))
        resolver = ProjectResolver()
        main, linked = resolver.resolve(str(repo)), resolver.resolve(str(tree / "sous" / "dossier"))
        gone = resolver.resolve(str(repo / ".claude" / "worktrees" / "disparu"))
        self.assertEqual(main.slug, "solitaire")
        self.assertEqual((linked.slug, linked.remote, linked.root), (main.slug, main.remote, main.root))
        self.assertEqual(gone.slug, "solitaire")
        self.assertIsNone(resolver.resolve(str(self.root)))
        self.assertEqual(strip_worktree("/a/b/.claude/worktrees/x/y"), "/a/b")

    def test_exclusions(self):
        repo = self.make_repo("travail", "git@github.com:mon-employeur/outil.git")
        project = ProjectResolver().resolve(str(repo))
        self.assertIn("MEMEX_EXCLUDE_REMOTES", exclusion_reason(str(repo), project, [], ["*mon-employeur*"], []))
        self.assertIn("MEMEX_EXCLUDE_PATHS", exclusion_reason(str(repo), project, [str(self.root / "code")], [], []))
        self.assertIn("MEMEX_INCLUDE_PATHS", exclusion_reason(str(repo), project, [], [], ["/ailleurs"]))
        self.assertEqual(exclusion_reason(str(repo), project, ["/ailleurs"], ["autre"], [str(self.root)]), "")
        (repo / ".memex-ignore").write_text("")
        self.assertIn(".memex-ignore", exclusion_reason(str(repo), project, [], [], []))


class Adapters(Sandbox):
    init_server = False

    def test_claude_code_keeps_human_words_and_final_answers(self):
        repo = self.make_repo("p")
        session = self.claude_session(repo, [
            ("user", "<system-reminder>ignore</system-reminder>\nVraie question de l'humain."),
            ("assistant", "Je regarde."),
            ("tool", str(repo / "a.py")),
            ("assistant", "Réponse finale."),
            ("raw", {"type": "user", "isMeta": True, "message": {"role": "user", "content": "méta"}}),
            ("raw", {"type": "user", "isCompactSummary": True, "message": {"role": "user", "content": "résumé de compaction"}}),
            ("raw", {"type": "user", "origin": {"kind": "task-notification"}, "message": {"role": "user", "content": "notification"}}),
            ("raw", {"type": "user", "isSidechain": True, "message": {"role": "user", "content": "sous-agent"}}),
            ("raw", {"type": "user", "message": {"role": "user", "content": "<command-name>/clear</command-name>"}}),
        ], title="Titre IA")
        (self.projects / "x" / session / "subagents").mkdir(parents=True)
        (self.projects / "x" / session / "subagents" / "agent.jsonl").write_text("{}\n")
        adapter = ClaudeCodeAdapter(self.projects)
        refs = list(adapter.discover())
        self.assertEqual(len(refs), 1)
        chunk = adapter.read(refs[0], 0, {})
        self.assertEqual([(m.role, m.text, m.final) for m in chunk.messages], [
            ("user", "Vraie question de l'humain.", True), ("assistant", "Je regarde.", False), ("assistant", "Réponse finale.", True),
        ])
        self.assertEqual(chunk.meta["title"], "Titre IA")
        self.assertEqual(chunk.meta["files"], [str(repo / "a.py")])
        self.assertEqual(chunk.cursor, refs[0].size)
        # reprise au curseur : rien de neuf, puis seulement la suite
        self.assertEqual(adapter.read(refs[0], chunk.cursor, chunk.meta).messages, [])
        self.claude_session(repo, [("user", "La suite.")], session=session, append=True)
        ref = next(adapter.discover())
        self.assertEqual([m.text for m in adapter.read(ref, chunk.cursor, chunk.meta).messages], ["La suite."])

    def test_claude_code_ignores_partial_last_line(self):
        repo = self.make_repo("p")
        session = self.claude_session(repo, [("user", "Complet.")])
        path = next(self.projects.rglob(f"{session}.jsonl"))
        with open(path, "a") as handle:
            handle.write('{"parentUuid":"x","type":"user","message":{"role":"user","content":"en cours d')
        ref = next(ClaudeCodeAdapter(self.projects).discover())
        chunk = ClaudeCodeAdapter(self.projects).read(ref, 0, {})
        self.assertEqual([m.text for m in chunk.messages], ["Complet."])
        self.assertLess(chunk.cursor, ref.size)

    def test_codex_reads_items_and_skips_internal_threads(self):
        self.codex_session(self.root, [("user", "Question."), ("assistant", "Je cherche.", "commentary"), ("file", "/x/a.py"), ("assistant", "Réponse.")])
        self.codex_session(self.root, [("user", "Relecture interne.")], thread="guardian_review")
        adapter = CodexAdapter(self.codex)
        chunks = [adapter.read(ref, 0, {}) for ref in adapter.discover()]
        kept = [c for c in chunks if not c.skip]
        self.assertEqual(len(kept), 1)
        self.assertEqual([(m.role, m.text, m.final) for m in kept[0].messages], [
            ("user", "Question.", True), ("assistant", "Je cherche.", False), ("assistant", "Réponse.", True)])
        self.assertEqual(kept[0].meta["files"], ["/x/a.py"])
        self.assertIn("interne", [c for c in chunks if c.skip][0].skip)

    def test_hermes_reads_sqlite_incrementally(self):
        db = self.root / "hermes.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE sessions(id TEXT, source TEXT, title TEXT, cwd TEXT, git_branch TEXT, started_at REAL, ended_at REAL, last_activity_at REAL);"
            "CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, tool_calls TEXT, timestamp REAL);"
            "INSERT INTO sessions VALUES('s1','telegram','PEA','/home/x',NULL,1794000000,NULL,1794000100);"
            "INSERT INTO sessions VALUES('s2','cron','tâche',NULL,NULL,1794000000,NULL,1794000100);"
            "INSERT INTO messages VALUES(1,'s1','user','Faut-il ouvrir un PEA ?',NULL,1794000001);"
            "INSERT INTO messages VALUES(2,'s1','assistant','Oui, voici pourquoi.',NULL,1794000002);"
            "INSERT INTO messages VALUES(3,'s2','user','tâche planifiée',NULL,1794000003);"
        )
        conn.commit()
        adapter = HermesAdapter(db, ["cron"])
        refs = list(adapter.discover())
        self.assertEqual([r.session for r in refs], ["s1"])
        chunk = adapter.read(refs[0], 0, {})
        self.assertEqual([m.text for m in chunk.messages], ["Faut-il ouvrir un PEA ?", "Oui, voici pourquoi."])
        self.assertEqual((chunk.cursor, chunk.meta["title"]), (2, "PEA"))
        conn.execute("INSERT INTO messages VALUES(4,'s1','user','Et chez quel courtier ?',NULL,1794000050)")
        conn.commit()
        self.assertEqual([m.text for m in adapter.read(next(adapter.discover()), 2, chunk.meta).messages], ["Et chez quel courtier ?"])

    def test_wrapper_stripping(self):
        self.assertEqual(clean_human("<local-command-caveat>x</local-command-caveat>\n<command-name>/model</command-name>"), "")
        self.assertEqual(clean_human("[Request interrupted by user]"), "")
        self.assertEqual(clean_human("Bonjour <b>toi</b>"), "Bonjour <b>toi</b>")


class Sweeping(Sandbox):
    init_server = False

    def test_exclusion_optout_redaction_and_split(self):
        self.set(MEMEX_EXCLUDE_REMOTES="*mon-employeur*", MEMEX_MAX_SEGMENT_CHARS="2000", MEMEX_INTERIM_MIN_CHARS="50")
        work = self.make_repo("travail", "git@github.com:mon-employeur/outil.git")
        perso = self.make_repo("perso", "git@github.com:moi/perso.git")
        self.claude_session(work, [("user", "Session de travail à ne pas capturer.")])
        self.claude_session(perso, [("user", "Discussion privée #memex-off à ignorer.")])
        self.claude_session(perso, [
            ("user", "Voici ma clé : sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789 et une longue question."),
            ("assistant", "court"),
            ("tool", str(perso / "x.py")),
            ("assistant", "A" * 1500),
            ("user", "Suite de la discussion."),
            ("assistant", "B" * 1500),
        ], title="Clé sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789")
        report = Sweeper(self.config).run()
        self.assertEqual(len(report.written), 2)
        self.assertEqual(sum(report.excluded.values()), 2)
        texts = [p.read_text(encoding="utf-8") for p in sorted(self.config.outbox.glob("*.md"))]
        self.assertTrue(all("sk-ant" not in t for t in texts))
        self.assertIn("[MASQUÉ]", texts[0])
        self.assertNotIn("court", texts[0])          # narration intermédiaire écartée
        first = segment_mod.load(sorted(self.config.outbox.glob("*.md"))[0])
        self.assertEqual((first.project, first.project_remote, first.seq, first.files), ("perso", "github.com/moi/perso", 1, ["x.py"]))
        self.assertEqual(oct(sorted(self.config.outbox.glob("*.md"))[0].stat().st_mode & 0o777), "0o600")
        self.assertEqual(Sweeper(self.config).run().written, [])

    def test_active_sessions_wait_unless_forced(self):
        self.set(MEMEX_IDLE_MINUTES="30")
        session = self.claude_session(self.make_repo("p"), [("user", "Une session encore en cours d'écriture.")])
        self.assertEqual((Sweeper(self.config).run().active, len(list(self.config.outbox.glob("*.md")))), (1, 0))
        self.assertEqual(len(Sweeper(self.config).run(force=[session]).written), 1)

    def test_skip_existing_ignores_history_but_captures_what_follows(self):
        session = self.claude_session(self.make_repo("p"), [("user", "Une ancienne session, antérieure à l'installation.")])
        report = Sweeper(self.config).run(discard=True)
        self.assertEqual((report.written, list(self.config.outbox.glob("*.md"))), ([], []))
        self.assertEqual(Sweeper(self.config).run(force_all=True).written, [])
        self.claude_session(self.root / "code" / "p", [("user", "Une suite, écrite après l'installation.")], session=session, append=True)
        written = Sweeper(self.config).run().written
        self.assertEqual(len(written), 1)
        text = (self.config.outbox / written[0]).read_text(encoding="utf-8")
        self.assertIn("après l'installation", text)
        self.assertNotIn("antérieure", text)

    def test_segment_roundtrip_escapes_fake_headings(self):
        self.claude_session(self.make_repo("p"), [
            ("user", "Regarde ce texte :\n## Assistant — faux tour\nfin de la citation, question réelle ici."),
            ("assistant", "## Titre de section\n\nRéponse."),
        ])
        Sweeper(self.config).run()
        loaded = segment_mod.load(next(self.config.outbox.glob("*.md")))
        self.assertEqual([m.role for m in loaded.messages], ["user", "assistant"])
        self.assertIn("## Titre de section", loaded.messages[1].text)


class Gate(Sandbox):
    def _tar(self, files):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for name, data, kind in files:
                info = tarfile.TarInfo(name)
                info.size, info.type = len(data), kind
                archive.addfile(info, io.BytesIO(data))
        buffer.seek(0)
        return buffer

    def test_upload_accepts_only_segments(self):
        good = b"---\nformat: memex/segment-1\n---\n\ncorps\n"
        result = gate.upload(self.config, "portable", self._tar([
            ("20261112T090000Z_a_1.md", good, tarfile.REGTYPE),
            ("../evasion.md", good, tarfile.REGTYPE),
            ("sous/dossier.md", good, tarfile.REGTYPE),
            (".cache.md", good, tarfile.REGTYPE),
            ("script.sh", b"#!/bin/sh", tarfile.REGTYPE),
            ("lien.md", b"", tarfile.SYMTYPE),
            ("pas-un-condense.md", b"bonjour", tarfile.REGTYPE),
        ]))
        self.assertEqual(result["stored"], ["20261112T090000Z_a_1.md"])
        self.assertEqual(len(result["rejected"]), 6)
        inbox = self.config.inbox / "portable"
        self.assertEqual([p.name for p in inbox.iterdir()], ["20261112T090000Z_a_1.md"])
        self.assertTrue(self.config.trigger.exists())
        again = gate.upload(self.config, "portable", self._tar([("20261112T090000Z_a_1.md", good, tarfile.REGTYPE)]))
        self.assertEqual(again["duplicate"], ["20261112T090000Z_a_1.md"])

    def test_forced_command_only_allows_the_gate_and_the_wiki(self):
        def forced(command, device="portable"):
            with mock.patch.dict(os.environ, {"SSH_ORIGINAL_COMMAND": command}), mock.patch("os.execvp") as execvp, \
                    mock.patch("sys.stdout", new_callable=io.StringIO) as out, mock.patch("sys.stderr", new_callable=io.StringIO):
                code = gate.main(["--forced", "--device", device], self.config)
                return code, execvp.call_args, out.getvalue()

        self.assertEqual(forced("")[0], 1)
        self.assertEqual(forced("bash -i")[0], 1)
        self.assertEqual(forced("cat /etc/passwd")[0], 1)
        self.assertEqual(forced("git-receive-pack '" + str(self.config.wiki_repo) + "'")[0], 1)
        self.assertEqual(forced("git-upload-pack '/etc'")[0], 1)
        self.assertEqual(forced("git-upload-pack '" + str(self.config.home / "raw") + "'")[0], 1)
        code, call, _ = forced("git-upload-pack '" + str(self.config.wiki_repo) + "'")
        self.assertEqual(call[0][1], ["git-upload-pack", str(self.config.wiki_repo.resolve())])
        code, _, out = forced("memex-gate ping --device autre-machine")
        self.assertEqual((code, json.loads(out)["device"]), (0, "portable"))   # le device vient de la clé


class VaultSync(Sandbox):
    def setUp(self):
        super().setUp()
        self.wiki = self.server_home / "wiki"
        self.sync = Vault(self.config, self.wiki)

    def test_export_import_and_conflict_rules(self):
        page = self.wiki / "Sujets/A/A.md"
        page.parent.mkdir(parents=True)
        page.write_text("v1")
        self.sync.export()
        self.assertEqual((self.vault / "Sujets/A/A.md").read_text(), "v1")
        self.assertEqual(self.sync.import_changes(), [])
        # l'agent modifie : exporté
        page.write_text("v2 agent")
        self.sync.export()
        self.assertEqual((self.vault / "Sujets/A/A.md").read_text(), "v2 agent")
        # l'humain modifie : importé
        (self.vault / "Sujets/A/A.md").write_text("v3 humain")
        self.assertEqual(self.sync.import_changes(), ["Sujets/A/A.md"])
        self.assertEqual(page.read_text(), "v3 humain")
        # les deux modifient : l'export n'écrase pas, l'import suivant donne raison à l'humain
        page.write_text("v4 agent")
        (self.vault / "Sujets/A/A.md").write_text("v4 humain")
        self.sync.export()
        self.assertEqual((self.vault / "Sujets/A/A.md").read_text(), "v4 humain")
        self.sync.import_changes()
        self.assertEqual(page.read_text(), "v4 humain")
        # suppression côté wiki : propagée ; fichiers cachés du vault : ignorés
        (self.vault / ".obsidian").mkdir()
        (self.vault / ".obsidian/app.json").write_text("{}")
        self.sync.export()
        page.unlink()
        self.sync.export()
        self.assertFalse((self.vault / "Sujets/A/A.md").exists())
        self.assertFalse((self.vault / "Sujets/A").exists())
        self.assertTrue((self.vault / ".obsidian/app.json").exists())
        self.assertEqual(self.sync.import_changes(), [])

    def test_lost_manifest_never_lets_a_stale_vault_overwrite_the_wiki(self):
        page = self.wiki / "Sujets/A/A.md"
        page.parent.mkdir(parents=True)
        page.write_text("récent")
        self.vault.mkdir(exist_ok=True)
        (self.vault / "Sujets/A").mkdir(parents=True)
        (self.vault / "Sujets/A/A.md").write_text("périmé")
        (self.vault / "Sujets/A/Seulement ici.md").write_text("à garder")
        self.sync.manifest_path.unlink(missing_ok=True)
        self.assertEqual(self.sync.import_changes(), ["Sujets/A/Seulement ici.md"])
        self.sync.export()
        self.assertEqual(page.read_text(), "récent")
        self.assertEqual((self.vault / "Sujets/A/A.md").read_text(), "récent")


class Guardrails(Sandbox):
    def setUp(self):
        super().setUp()
        self.server = Server(self.config)
        self.repo = self.server.repo
        root = self.repo.dir
        (root / "Projets/P/Décisions").mkdir(parents=True)
        (root / "Projets/P/P.md").write_text("---\ntype: projet\ntitre: P\nresume: Projet P.\n---\n\nP utilise Tailscale pour joindre le serveur.\n")
        (root / "Projets/P/Décisions/2026-01-01 Choix.md").write_text("---\ntype: decision\ntitre: Choix\ndate: 2026-01-01\nstatut: active\n---\n\n## Décision\n\nOn fait A.\n")
        (root / "Concepts/Tailscale.md").write_text("---\ntype: concept\ntitre: Tailscale\nresume: Réseau privé.\n---\n\nUn réseau privé.\n")
        self.repo.commit_all(root, "base", "memex")
        self.tree = self.repo.new_worktree("t")
        self.base = self.repo.head("HEAD", cwd=self.tree)

    def check(self, role):
        return validate(self.repo, self.tree, self.base, role, "2026-11-12")

    def test_archivist_adds_and_fixes_headers(self):
        (self.tree / "Projets/P/Décisions/2026-11-12 Nouveau.md").write_text("## Décision\n\nOn fait B.\n")
        (self.tree / "Projets/P/P.md").write_text((self.tree / "Projets/P/P.md").read_text() + "\nAjout.\n")
        verdict = self.check("archiviste")
        self.assertTrue(verdict.ok, verdict.errors)
        self.assertEqual(sorted(verdict.touched), ["Projets/P/Décisions/2026-11-12 Nouveau.md", "Projets/P/P.md"])
        meta, _ = frontmatter.parse((self.tree / "Projets/P/Décisions/2026-11-12 Nouveau.md").read_text())
        self.assertEqual((meta["type"], meta["titre"], meta["date"], meta["statut"]), ("decision", "2026-11-12 Nouveau", "2026-11-12", "active"))

    def test_archivist_cannot_delete_rename_or_rewrite_decisions(self):
        (self.tree / "Projets/P/P.md").unlink()
        self.assertFalse(self.check("archiviste").ok)
        self.repo.git("reset", "--hard", "-q", self.base, cwd=self.tree)
        os.rename(self.tree / "Projets/P/P.md", self.tree / "Projets/P/Renommé.md")
        self.assertFalse(self.check("archiviste").ok)
        self.repo.git("reset", "--hard", "-q", self.base, cwd=self.tree), self.repo.git("clean", "-fdq", cwd=self.tree)
        fiche = self.tree / "Projets/P/Décisions/2026-01-01 Choix.md"
        fiche.write_text(fiche.read_text().replace("On fait A.", "On fait Z."))
        self.assertFalse(self.check("archiviste").ok)
        fiche.write_text(fiche.read_text().replace("On fait Z.", "On fait A.").replace("statut: active", 'statut: remplacee\nremplacee_par: "[[Projets/P/Décisions/x]]"'))
        self.assertTrue(self.check("archiviste").ok)

    def test_archivist_changes_outside_its_perimeter_are_undone(self):
        (self.tree / "Concepts/Tailscale.md").write_text("réécrit")
        (self.tree / "Concepts/Nouveau.md").write_text("créé")
        (self.tree / "AGENTS.md").write_text("conventions réécrites")
        (self.tree / "Journal").mkdir(exist_ok=True)
        (self.tree / "Journal/x.md").write_text("journal")
        (self.tree / "racine.md").write_text("hors dossier")
        (self.tree / "Projets/P/image.png").write_bytes(b"\x89PNG")
        verdict = self.check("archiviste")
        self.assertTrue(verdict.ok, verdict.errors)
        self.assertFalse(verdict.changed)
        self.assertEqual(self.repo.git("status", "--porcelain", cwd=self.tree), "")

    def test_tisseur_may_only_add_links_see_also_and_concepts(self):
        page = self.tree / "Projets/P/P.md"
        page.write_text(page.read_text().replace("Tailscale", "[[Concepts/Tailscale|Tailscale]]") + "\n## Voir aussi\n\n- [[Concepts/Tailscale]] — le réseau\n")
        (self.tree / "Concepts/Nouveau.md").write_text("---\ntype: concept\ntitre: Nouveau\nresume: x.\n---\n\nTexte.\n")
        self.assertTrue(self.check("tisseur").ok)
        page.write_text(page.read_text().replace("pour joindre", "afin de joindre"))
        self.assertFalse(self.check("tisseur").ok)
        self.repo.git("reset", "--hard", "-q", self.base, cwd=self.tree), self.repo.git("clean", "-fdq", cwd=self.tree)
        (self.tree / "Projets/P/Autre.md").write_text("---\ntype: projet\ntitre: Autre\n---\n\nx\n")
        self.assertFalse(self.check("tisseur").ok)
        self.repo.git("reset", "--hard", "-q", self.base, cwd=self.tree), self.repo.git("clean", "-fdq", cwd=self.tree)
        fiche = self.tree / "Projets/P/Décisions/2026-01-01 Choix.md"
        fiche.write_text(fiche.read_text().replace("On fait A.", "On fait [[Projets/P/P|A]]."))
        self.assertFalse(self.check("tisseur").ok)

    def test_unsafe_file_names_are_renamed_and_links_follow(self):
        from memex.pages import safe_name

        self.assertEqual(safe_name("2026-11-13 Architecture : VPS central.md"), "2026-11-13 Architecture - VPS central.md")
        self.assertEqual(safe_name('Quoi ? "Ça" #1 [x].md'), "Quoi Ça 1 x.md")
        self.assertEqual(safe_name("Déjà sûr (v2).md"), "Déjà sûr (v2).md")
        bad = "Projets/P/Décisions/2026-11-12 Archi : serveur central.md"
        (self.tree / bad).write_text("## Décision\n\nUn serveur central.\n")
        page = self.tree / "Projets/P/P.md"
        page.write_text(page.read_text() + "\n- [[Projets/P/Décisions/2026-11-12 Archi : serveur central|la fiche]] et [[2026-11-12 Archi : serveur central]]\n")
        verdict = self.check("archiviste")
        self.assertTrue(verdict.ok, verdict.errors)
        good = "Projets/P/Décisions/2026-11-12 Archi - serveur central.md"
        self.assertIn(good, verdict.touched)
        self.assertFalse((self.tree / bad).exists())
        text = page.read_text()
        self.assertIn("[[Projets/P/Décisions/2026-11-12 Archi - serveur central|la fiche]]", text)
        self.assertIn("[[2026-11-12 Archi - serveur central]]", text)
        self.assertNotIn(" : serveur", text)

    def test_project_reuses_a_same_name_folder_and_forgets_stale_mappings(self):
        from memex.segment import Message, Segment
        from memex.util import now, write_json

        root = self.repo.dir
        seg = Segment("claude-code", "d", "s", 1, [Message("user", "x")], now(), now(), project="memex", project_name="memex", project_remote="github.com/moi/memex")
        self.assertEqual(self.server.dossier_for(root, seg), ("Projets/Memex", True))
        (root / "Sujets/Memex").mkdir(parents=True)
        self.assertEqual(self.server.dossier_for(root, seg), ("Sujets/Memex", False))   # ouvert comme sujet avant d'avoir un dépôt
        write_json(root / ".memex/projects.json", {"github.com/moi/memex": "Projets/Disparu"})
        self.assertEqual(self.server.dossier_for(root, seg), ("Sujets/Memex", False))   # correspondance périmée ignorée
        (root / "Projets/Memex").mkdir(parents=True)
        self.assertEqual(self.server.dossier_for(root, seg), ("Projets/Memex", False))

    def test_verificateur_may_delete_pages_but_not_decisions(self):
        (self.tree / "Concepts/Tailscale.md").unlink()
        verdict = self.check("verificateur")
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.deleted, ["Concepts/Tailscale.md"])
        (self.tree / "Projets/P/Décisions/2026-01-01 Choix.md").unlink()
        self.assertFalse(self.check("verificateur").ok)

    def test_normalisation_ignores_links_only(self):
        a = "---\ntitre: T\nentites: [x]\n---\n\nP utilise Tailscale.\n"
        b = "---\ntitre: T\nentites: [x, y]\nmaj: 2026\n---\n\nP utilise [[Concepts/Tailscale|Tailscale]].\n\n## Voir aussi\n\n- [[A]]\n"
        self.assertEqual(normalize_for_links(a), normalize_for_links(b))
        self.assertNotEqual(normalize_for_links(a), normalize_for_links(b.replace("utilise", "emploie")))


class WikiModel(Sandbox):
    def test_links_index_and_lint(self):
        root = self.server_home / "wiki"
        for path, text in {
            "Projets/Solitaire/Solitaire.md": "---\ntype: projet\ntitre: Solitaire\nresume: Un jeu.\nentites: [Tailscale]\n---\n\nVoir [[Concepts/Réseau|le réseau]] et [[Page absente]]. `[[pas un lien]]`\n",
            "Projets/Solitaire/Décisions/2026-11-12 Scores.md": "---\ntype: decision\ntitre: Scores\ndate: 2026-11-12\nstatut: remplacee\n---\n\nx\n",
            "Projets/Atlas/Atlas.md": "---\ntype: projet\ntitre: Atlas\nresume: Finance.\nentites: [tailscale]\n---\n\nComme pour Solitaire, on passe par un réseau privé.\n",
            "Concepts/Réseau.md": "---\ntype: concept\ntitre: Réseau\naliases: [réseau privé]\n---\n\nx\n",
            "Sujets/Seul/Notes.md": "---\ntype: sujet\ntitre: Notes\n---\n\nx\n",
        }.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(text, encoding="utf-8")
        pages = load_wiki(root)
        self.assertEqual(links(pages["Projets/Solitaire/Solitaire.md"].body), [("Concepts/Réseau", "le réseau"), ("Page absente", "")])
        self.assertEqual(unlink("a [[X/Y|b]] c [[Z]]"), "a b c Z")
        self.assertEqual(dossier_of("Projets/Solitaire/Décisions/x.md"), "Projets/Solitaire")
        resolver = LinkIndex(list(pages))
        self.assertEqual(resolver.resolve("réseau"), "Concepts/Réseau.md")
        self.assertEqual(resolver.resolve("Solitaire/Solitaire"), "Projets/Solitaire/Solitaire.md")
        self.assertIsNone(resolver.resolve("Page absente"))
        text = build_index(pages)
        self.assertIn("[[Projets/Solitaire/Solitaire|Solitaire]] — Un jeu. · 1 décision(s), dont 0 active(s)", text)
        self.assertIn("_(remplacee)_", text)
        self.assertEqual(text, build_index(load_wiki(root)))
        report = lint(root)
        self.assertEqual(report.broken, [{"page": "Projets/Solitaire/Solitaire.md", "cible": "Page absente"}])
        self.assertIn("Projets/Solitaire/Décisions/2026-11-12 Scores.md", report.orphans)
        unlinked = {(u["page"], u["cible"]) for u in report.unlinked}
        self.assertIn(("Projets/Atlas/Atlas.md", "Projets/Solitaire/Solitaire.md"), unlinked)
        self.assertIn(("Projets/Atlas/Atlas.md", "Concepts/Réseau.md"), unlinked)
        self.assertEqual([c["entite"].lower() for c in report.concepts], ["tailscale"])


class Integration(Sandbox):
    init_server = False

    def test_claude_and_codex_hooks_are_merged_idempotently_and_removed_cleanly(self):
        claude, codex = self.root / "claude", self.root / "codex"
        claude.mkdir(), codex.mkdir()
        original = {"model": "opus", "hooks": {"SessionStart": [{"matcher": "x", "hooks": [{"type": "command", "command": "autre-outil"}]}]},
                    "permissions": {"allow": ["Bash(ls:*)"]}}
        (claude / "settings.json").write_text(json.dumps(original))
        (claude / "CLAUDE.md").write_text("# Mes règles\n\nToujours répondre en français.\n")
        (codex / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "autre-outil"}]}]}}))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude), "CODEX_HOME": str(codex)}):
            for _ in range(2):
                integrate.claude(self.config, "/bin/memex", True)
                integrate.codex(self.config, "/bin/memex", True)
            settings = json.loads((claude / "settings.json").read_text())
            starts = settings["hooks"]["SessionStart"]
            self.assertEqual(len(starts), 2)
            self.assertEqual(starts[0]["hooks"][0]["command"], "autre-outil")
            self.assertEqual(starts[1]["hooks"][0]["command"], "/bin/memex hook session-start")
            self.assertEqual(settings["hooks"]["SessionEnd"][0]["hooks"][0]["command"], "/bin/memex hook session-end")
            self.assertEqual(len(settings["permissions"]["allow"]), 2)
            self.assertEqual((claude / "CLAUDE.md").read_text().count("memex:begin"), 1)
            self.assertTrue((claude / "CLAUDE.md").read_text().startswith("# Mes règles"))
            self.assertEqual(len(json.loads((codex / "hooks.json").read_text())["hooks"]["SessionStart"]), 2)
            self.assertTrue(all(integrate.status(self.config).values()))
            integrate.claude(self.config, "/bin/memex", False)
            integrate.codex(self.config, "/bin/memex", False)
            self.assertEqual(json.loads((claude / "settings.json").read_text()), original)
            self.assertEqual((claude / "CLAUDE.md").read_text(), "# Mes règles\n\nToujours répondre en français.\n")
            self.assertFalse((codex / "AGENTS.md").exists() and "memex" in (codex / "AGENTS.md").read_text())
            self.assertFalse(any(integrate.status(self.config).values()))

    def test_unreadable_settings_are_left_alone(self):
        claude = self.root / "claude"
        claude.mkdir()
        (claude / "settings.json").write_text("{ pas du json")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude)}):
            self.assertIn("illisible", integrate.claude(self.config, "memex", True)[0])
        self.assertEqual((claude / "settings.json").read_text(), "{ pas du json")


class Imports(Sandbox):
    init_server = False

    def test_chatgpt_and_claude_exports(self):
        chatgpt = [{
            "id": "c1", "title": "PEA", "create_time": 1794000000, "update_time": 1794000300, "current_node": "n3",
            "mapping": {
                "n0": {"message": None, "parent": None},
                "n1": {"parent": "n0", "message": {"author": {"role": "system"}, "content": {"content_type": "text", "parts": ["système"]}}},
                "n2": {"parent": "n1", "message": {"author": {"role": "user"}, "create_time": 1794000001, "content": {"content_type": "text", "parts": ["Faut-il ouvrir un PEA ? " + "détails " * 60]}}},
                "nX": {"parent": "n1", "message": {"author": {"role": "user"}, "content": {"content_type": "text", "parts": ["branche abandonnée"]}}},
                "n3": {"parent": "n2", "message": {"author": {"role": "assistant"}, "create_time": 1794000002, "content": {"content_type": "text", "parts": ["Oui."]}}},
            },
        }, {"id": "c2", "title": "court", "current_node": "a", "mapping": {"a": {"parent": None, "message": {"author": {"role": "user"}, "content": {"content_type": "text", "parts": ["salut"]}}}}}]
        archive = self.root / "chatgpt.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("conversations.json", json.dumps(chatgpt))
        report = imports.run(self.config, "chatgpt", archive)
        self.assertEqual((report.conversations, len(report.written), report.skipped_short), (2, 1, 1))
        loaded = segment_mod.load(next(self.config.outbox.glob("*chatgpt*.md")))
        self.assertEqual([m.role for m in loaded.messages], ["user", "assistant"])
        self.assertNotIn("branche abandonnée", loaded.render())
        self.assertEqual(imports.run(self.config, "chatgpt", archive).written, [])

        claude = [{"uuid": "u1", "name": "Vacances", "updated_at": "2026-11-10T10:00:00Z", "chat_messages": [
            {"sender": "human", "text": "Où partir en mai ? " + "contexte " * 60, "created_at": "2026-11-10T09:00:00Z"},
            {"sender": "assistant", "text": "", "content": [{"type": "text", "text": "Au Portugal."}], "created_at": "2026-11-10T09:01:00Z"}]}]
        path = self.root / "conversations.json"
        path.write_text(json.dumps(claude))
        self.assertEqual(len(imports.run(self.config, "claude", path).written), 1)
        claude[0]["chat_messages"].append({"sender": "human", "text": "Et en juin ?", "created_at": "2026-11-11T09:00:00Z"})
        path.write_text(json.dumps(claude))
        again = imports.run(self.config, "claude", path)
        self.assertEqual(len(again.written), 1)
        self.assertTrue(again.written[0].endswith("_002.md"))


class Sandboxing(Sandbox):
    """Le bac à sable réel (bubblewrap). Ignoré sur une machine où il ne peut pas démarrer."""

    overrides = {"MEMEX_SANDBOX": "auto"}
    init_server = False

    def setUp(self):
        super().setUp()
        from memex.agent import Agent

        if "actif" not in Agent(self.config).sandbox_state():
            self.skipTest("bubblewrap indisponible ici")
        self.Agent = Agent
        self.work = self.root / "work"
        self.work.mkdir()
        self.evasion = Path.home() / ".memex-test-evasion"

    def tearDown(self):
        self.evasion.unlink(missing_ok=True)
        super().tearDown()

    def test_edit_agent_writes_only_inside_its_workdir(self):
        outside = self.root / "ailleurs"
        outside.mkdir()
        self.set(MEMEX_AGENT_EDIT_CMD=(
            f"echo dedans > dedans.txt; echo dehors > {outside}/dehors.txt; echo dehors > {self.evasion}; "
            "ls ~/.ssh 2>/dev/null | wc -l > ssh.txt; echo fini > $MEMEX_OUT_FILE"
        ))
        result = self.Agent(self.config).run("edit", "invite", self.work)
        self.assertEqual(result.text.strip(), "fini")
        self.assertEqual((self.work / "dedans.txt").read_text().strip(), "dedans")
        self.assertFalse((outside / "dehors.txt").exists())
        self.assertFalse(self.evasion.exists())
        self.assertEqual((self.work / "ssh.txt").read_text().strip(), "0")   # les clés SSH sont masquées

    def test_read_agent_cannot_write_anywhere(self):
        (self.work / "lisible.txt").write_text("contenu lisible")
        self.set(MEMEX_AGENT_READ_CMD="echo x > essai.txt; cat lisible.txt")
        result = self.Agent(self.config).run("read", "invite", self.work)
        self.assertIn("contenu lisible", result.text)
        self.assertFalse((self.work / "essai.txt").exists())

    def test_required_sandbox_fails_closed_when_missing(self):
        from memex.agent import AgentError as Error

        self.set(MEMEX_SANDBOX="bwrap", MEMEX_AGENT_EDIT_CMD="true")
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(Error):
                self.Agent(self.config).run("edit", "invite", self.work)
        self.set(MEMEX_SANDBOX="auto")
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(self.Agent(self.config).sandbox("edit", self.work, self.root), [])


class Devices(Sandbox):
    KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHk0cDg5bXhGZ0x2b1pWd2Jqc0R3eXJ0dG9mZmFrZWtleQ moi@portable"

    def test_add_replace_list_remove(self):
        from memex import serverops

        self.assertIn("autorisé", serverops.device_add(self.config, "portable", self.KEY))
        self.assertIn("autorisé", serverops.device_add(self.config, "portable", self.KEY))   # rejouable
        other = self.KEY.replace("IHk0", "IHk1").replace("moi@portable", "moi@fixe")
        serverops.device_add(self.config, "fixe", other)
        lines = (self.root / "authorized_keys").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.startswith('restrict,command="') and "--forced --device " in line for line in lines))
        self.assertEqual(serverops.device_list(self.config), ["portable", "fixe"])
        self.assertTrue((self.config.inbox / "fixe").is_dir())
        self.assertIn("retiré", serverops.device_remove(self.config, "portable"))
        self.assertEqual(serverops.device_list(self.config), ["fixe"])
        self.assertIn("Aucun appareil", serverops.device_remove(self.config, "inconnu"))

    def test_rejects_bad_names_and_keys(self):
        from memex import serverops

        self.assertIn("invalide", serverops.device_add(self.config, "nom avec espace", self.KEY))
        self.assertIn("invalide", serverops.device_add(self.config, "ok", 'command="sh" ' + self.KEY))
        self.assertIn("invalide", serverops.device_add(self.config, "ok", "pas une clé"))
        self.assertFalse((self.root / "authorized_keys").exists())


class Misc(unittest.TestCase):
    def test_env_parsing(self):
        values = parse_env_text('# commentaire\nexport MEMEX_A="x y"\nMEMEX_B=valeur # fin\nMEMEX_C=\'a#b\'\n\nmauvaise ligne\n')
        self.assertEqual(values, {"MEMEX_A": "x y", "MEMEX_B": "valeur", "MEMEX_C": "a#b"})
        config = Config({"MEMEX_IDLE_MINUTES": "abc", "MEMEX_DEVICE": "mon portable !"})
        self.assertEqual((config.int("MEMEX_IDLE_MINUTES"), config.device), (10, "mon-portable-"))

    def test_json_reply_parsing(self):
        self.assertEqual(parse_json_reply('Voici :\n```json\n{"a": 1}\n```\nvoilà'), {"a": 1})
        self.assertEqual(parse_json_reply('texte {"a": {"b": 2}} fin'), {"a": {"b": 2}})
        with self.assertRaises(AgentError):
            parse_json_reply("aucun objet ici")


if __name__ == "__main__":
    unittest.main()
