"""Unit tests: headers, redaction, projects, adapters, gate, vault, guardrails, integrations."""
import contextlib
import io
import json
import os
import sqlite3
import tarfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from helpers import Sandbox, git, write_pages

from memex import cli, frontmatter, gate, hook, i18n, imports, integrate, segment as segment_mod
from memex.adapters import clean_human
from memex.adapters.claude_code import ClaudeCodeAdapter
from memex.adapters.codex import CodexAdapter
from memex.adapters.hermes import HermesAdapter
from memex.agent import Agent, AgentError, AgentResult, parse_json_reply
from memex.config import DEFAULTS, Config, parse_env_text
from memex.index import build as build_index, compact as compact_index
from memex.lint import lint, summary as lint_summary
from memex.pages import LinkIndex, folder_of, links, load_wiki, unlink
from memex.pipeline import Server
from memex.project import ProjectResolver, exclusion_reason, normalize_remote, strip_worktree
from memex.redact import Redactor
from memex.review import Reviews
from memex.sweep import Sweeper
from memex.validate import normalize_for_links, validate
from memex.vault import Vault


class Frontmatter(unittest.TestCase):
    def test_roundtrip_and_tolerant_parsing(self):
        text = (
            '---\ntype: decision\ntitle: "Scores: server side"\ndate: 2026-11-12\nactive: true\nn: 3\n'
            "entities: [Tailscale, \"Open WebUI\"]\nsources:\n  - a_1\n  - b_2\nsuperseded_by: \"[[Projects/X/Decisions/y]]\"\n---\n\nBody.\n"
        )
        meta, body = frontmatter.parse(text)
        self.assertEqual(meta["title"], "Scores: server side")
        self.assertEqual(meta["date"], "2026-11-12")
        self.assertIs(meta["active"], True)
        self.assertEqual(meta["n"], 3)
        self.assertEqual(meta["entities"], ["Tailscale", "Open WebUI"])
        self.assertEqual(meta["sources"], ["a_1", "b_2"])
        self.assertEqual(meta["superseded_by"], "[[Projects/X/Decisions/y]]")
        self.assertEqual(body, "Body.\n")
        again, body2 = frontmatter.parse(frontmatter.dump(meta, body))
        self.assertEqual(again, meta)
        self.assertEqual(body2, body)

    def test_set_keys_touches_only_the_requested_fields(self):
        text = (
            "---\ntype: project\n# comment kept\nauthor:\n  name: Alex\n  role: dev\nsummary: |\n  line 1\n  line 2\n"
            "code: 007\nsources:\n  - old\nupdated: 2026-01-01\n---\nBody stuck to the header.\n"
        )
        out = frontmatter.set_keys(text, {"sources": ["old", "new"], "updated": "2026-11-16", "status": "active"})
        for kept in ("# comment kept", "author:\n  name: Alex\n  role: dev", "summary: |\n  line 1\n  line 2", "code: 007"):
            self.assertIn(kept, out)
        self.assertIn("sources:\n  - old\n  - new\nupdated: 2026-11-16\nstatus: active\n---\nBody stuck to the header.\n", out)
        self.assertEqual(frontmatter.set_keys(out, {"updated": "2026-11-16"}), out)          # idempotent
        self.assertEqual(frontmatter.set_keys(text, {}), text)
        bare = frontmatter.set_keys("# Title\n\ntext\n", {"type": "topic"})
        self.assertTrue(bare.startswith("---\ntype: topic\n---\n\n# Title"))

    def test_page_without_header(self):
        self.assertEqual(frontmatter.parse("# Title\n\ntext"), ({}, "# Title\n\ntext"))
        self.assertEqual(frontmatter.parse("---\nnot closed\n"), ({}, "---\nnot closed\n"))


class Redaction(unittest.TestCase):
    def test_known_secrets_are_masked(self):
        redact = Redactor()
        samples = [
            "key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789",
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789AB",
            "AKIAIOSFODNN7EXAMPLE",
            "postgres://admin:hunter2secret@db.example.com/app",
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
            'EXCHANGE_API_SECRET="s3cr3t-value-123"',
            "password: correcthorse123",
            "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\ndef\n-----END OPENSSH PRIVATE KEY-----",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
            "tskey-auth-kAbCdEf12345-ABCDEFGHIJKL",
        ]
        for sample in samples:
            out = redact(sample)
            self.assertIn("[REDACTED]", out, sample)
        self.assertNotIn("hunter2secret", redact("postgres://admin:hunter2secret@db.example.com/app"))
        self.assertIn("[IBAN]", redact("IBAN FR76 3000 6000 0112 3456 7890 189"))
        self.assertIn("[CARD]", redact("card 4242 4242 4242 4242"))
        self.assertGreaterEqual(redact.total, len(samples))

    def test_ordinary_text_is_untouched(self):
        redact = Redactor()
        for text in ("The session token expires after one hour.", "max_tokens = 4096", "git@github.com:me/repo.git",
                     "See https://example.com/a/b?c=d", "The key to the whole project is the sweeper."):
            self.assertEqual(redact(text), text)

    def test_extra_rules_and_emails(self):
        redact = Redactor(extra=[r"CUSTOMER-\d+"], emails=True)
        self.assertEqual(redact("customer CUSTOMER-4421, me@example.org"), "customer [REDACTED], [EMAIL]")


class Projects(Sandbox):
    init_server = False

    def test_remote_normalisation(self):
        for url in ("git@github.com:Me/Repo.git", "https://github.com/me/repo", "ssh://git@github.com/me/repo.git", "https://user:tok@github.com/me/repo.git"):
            self.assertEqual(normalize_remote(url), "github.com/me/repo")
        self.assertEqual(normalize_remote("ssh://git@host:2222/a/b.git"), "host/a/b")

    def test_repo_worktree_and_deleted_worktree_share_one_identity(self):
        repo = self.make_repo("solitaire", "git@github.com:me/solitaire.git")
        (repo / "f").write_text("x")
        git(repo, "add", "."), git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        tree = repo / ".claude" / "worktrees" / "leaderboard"
        git(repo, "worktree", "add", "-q", str(tree))
        resolver = ProjectResolver()
        main, linked = resolver.resolve(str(repo)), resolver.resolve(str(tree / "sub" / "folder"))
        gone = resolver.resolve(str(repo / ".claude" / "worktrees" / "gone"))
        self.assertEqual(main.slug, "solitaire")
        self.assertEqual((linked.slug, linked.remote, linked.root), (main.slug, main.remote, main.root))
        self.assertEqual(gone.slug, "solitaire")
        self.assertIsNone(resolver.resolve(str(self.root)))
        self.assertEqual(strip_worktree("/a/b/.claude/worktrees/x/y"), "/a/b")

    def test_exclusions(self):
        repo = self.make_repo("work", "git@github.com:my-employer/tool.git")
        project = ProjectResolver().resolve(str(repo))
        self.assertIn("MEMEX_EXCLUDE_REMOTES", exclusion_reason(str(repo), project, [], ["*my-employer*"], []))
        self.assertIn("MEMEX_EXCLUDE_PATHS", exclusion_reason(str(repo), project, [str(self.root / "code")], [], []))
        self.assertIn("MEMEX_INCLUDE_PATHS", exclusion_reason(str(repo), project, [], [], ["/elsewhere"]))
        self.assertEqual(exclusion_reason(str(repo), project, ["/elsewhere"], ["other"], [str(self.root)]), "")
        (repo / ".memex-ignore").write_text("")
        self.assertIn(".memex-ignore", exclusion_reason(str(repo), project, [], [], []))


class Adapters(Sandbox):
    init_server = False

    def test_claude_code_keeps_human_words_and_final_answers(self):
        repo = self.make_repo("p")
        session = self.claude_session(repo, [
            ("user", "<system-reminder>ignore</system-reminder>\nA real question from the human."),
            ("assistant", "Let me look."),
            ("tool", str(repo / "a.py")),
            ("assistant", "Final answer."),
            ("raw", {"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta"}}),
            ("raw", {"type": "user", "isCompactSummary": True, "message": {"role": "user", "content": "compaction summary"}}),
            ("raw", {"type": "user", "origin": {"kind": "task-notification"}, "message": {"role": "user", "content": "notification"}}),
            ("raw", {"type": "user", "isSidechain": True, "message": {"role": "user", "content": "sub-agent"}}),
            ("raw", {"type": "user", "message": {"role": "user", "content": "<command-name>/clear</command-name>"}}),
        ], title="AI title")
        (self.projects / "x" / session / "subagents").mkdir(parents=True)
        (self.projects / "x" / session / "subagents" / "agent.jsonl").write_text("{}\n")
        adapter = ClaudeCodeAdapter(self.projects)
        refs = list(adapter.discover())
        self.assertEqual(len(refs), 1)
        chunk = adapter.read(refs[0], 0, {})
        self.assertEqual([(m.role, m.text, m.final) for m in chunk.messages], [
            ("user", "A real question from the human.", True), ("assistant", "Let me look.", False), ("assistant", "Final answer.", True),
        ])
        self.assertEqual(chunk.meta["title"], "AI title")
        self.assertEqual(chunk.meta["files"], [str(repo / "a.py")])
        self.assertEqual(chunk.cursor, refs[0].size)
        # resuming at the cursor: nothing new, then only what follows
        self.assertEqual(adapter.read(refs[0], chunk.cursor, chunk.meta).messages, [])
        self.claude_session(repo, [("user", "What follows.")], session=session, append=True)
        ref = next(adapter.discover())
        self.assertEqual([m.text for m in adapter.read(ref, chunk.cursor, chunk.meta).messages], ["What follows."])

    def test_claude_code_reads_turns_whatever_the_key_order(self):
        """A version that wrote `type` as the first key must not make the words disappear."""
        folder = self.projects / "p"
        folder.mkdir(parents=True)
        lines = [
            {"type": "user", "message": {"role": "user", "content": "A question with type first."}, "origin": {"kind": "human"}, "cwd": "/x", "timestamp": "2026-11-12T09:00:00Z"},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Answer."}]}, "timestamp": "2026-11-12T09:01:00Z"},
            {"type": "api-request", "params": {"messages": [{"type": "user", "content": "technical noise"}]}},
            {"type": "type-unknown-from-the-future", "x": 1},
        ]
        (folder / "s1.jsonl").write_text("".join(json.dumps(line, separators=(",", ":")) + "\n" for line in lines))
        adapter = ClaudeCodeAdapter(self.projects)
        chunk = adapter.read(next(adapter.discover()), 0, {})
        self.assertEqual([m.text for m in chunk.messages], ["A question with type first.", "Answer."])

    def test_claude_code_ignores_partial_last_line(self):
        repo = self.make_repo("p")
        session = self.claude_session(repo, [("user", "Complete.")])
        path = next(self.projects.rglob(f"{session}.jsonl"))
        with open(path, "a") as handle:
            handle.write('{"parentUuid":"x","type":"user","message":{"role":"user","content":"still being wri')
        ref = next(ClaudeCodeAdapter(self.projects).discover())
        chunk = ClaudeCodeAdapter(self.projects).read(ref, 0, {})
        self.assertEqual([m.text for m in chunk.messages], ["Complete."])
        self.assertLess(chunk.cursor, ref.size)

    def test_codex_reads_items_and_skips_internal_threads(self):
        self.codex_session(self.root, [("user", "Question."), ("assistant", "Let me search.", "commentary"), ("file", "/x/a.py"), ("assistant", "Answer.")])
        self.codex_session(self.root, [("user", "Internal review.")], thread="guardian_review")
        adapter = CodexAdapter(self.codex)
        chunks = [adapter.read(ref, 0, {}) for ref in adapter.discover()]
        kept = [c for c in chunks if not c.skip]
        self.assertEqual(len(kept), 1)
        self.assertEqual([(m.role, m.text, m.final) for m in kept[0].messages], [
            ("user", "Question.", True), ("assistant", "Let me search.", False), ("assistant", "Answer.", True)])
        self.assertEqual(kept[0].meta["files"], ["/x/a.py"])
        self.assertIn("internal", [c for c in chunks if c.skip][0].skip)

    def test_hermes_reads_sqlite_incrementally(self):
        db = self.root / "hermes.db"
        conn = sqlite3.connect(db)
        self.addCleanup(conn.close)
        conn.executescript(
            "CREATE TABLE sessions(id TEXT, source TEXT, title TEXT, cwd TEXT, git_branch TEXT, started_at REAL, ended_at REAL, last_activity_at REAL);"
            "CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, tool_calls TEXT, timestamp REAL);"
            "INSERT INTO sessions VALUES('s1','telegram','Savings plan','/home/x',NULL,1794000000,NULL,1794000100);"
            "INSERT INTO sessions VALUES('s2','cron','task',NULL,NULL,1794000000,NULL,1794000100);"
            "INSERT INTO messages VALUES(1,'s1','user','Should I open a savings plan?',NULL,1794000001);"
            "INSERT INTO messages VALUES(2,'s1','assistant','Yes, and here is why.',NULL,1794000002);"
            "INSERT INTO messages VALUES(3,'s2','user','scheduled task',NULL,1794000003);"
        )
        conn.commit()
        adapter = HermesAdapter(db, ["cron"])
        refs = list(adapter.discover())
        self.assertEqual([r.session for r in refs], ["s1"])
        chunk = adapter.read(refs[0], 0, {})
        self.assertEqual([m.text for m in chunk.messages], ["Should I open a savings plan?", "Yes, and here is why."])
        self.assertEqual((chunk.cursor, chunk.meta["title"]), (2, "Savings plan"))
        conn.execute("INSERT INTO messages VALUES(4,'s1','user','And with which broker?',NULL,1794000050)")
        conn.commit()
        self.assertEqual([m.text for m in adapter.read(next(adapter.discover()), 2, chunk.meta).messages], ["And with which broker?"])

    def test_wrapper_stripping(self):
        self.assertEqual(clean_human("<local-command-caveat>x</local-command-caveat>\n<command-name>/model</command-name>"), "")
        self.assertEqual(clean_human("[Request interrupted by user]"), "")
        self.assertEqual(clean_human("Hello <b>you</b>"), "Hello <b>you</b>")


class Sweeping(Sandbox):
    init_server = False

    def test_exclusion_optout_redaction_and_split(self):
        self.set(MEMEX_EXCLUDE_REMOTES="*my-employer*", MEMEX_MAX_SEGMENT_CHARS="2000", MEMEX_INTERIM_MIN_CHARS="50")
        work = self.make_repo("work", "git@github.com:my-employer/tool.git")
        personal = self.make_repo("personal", "git@github.com:me/personal.git")
        self.claude_session(work, [("user", "A work session that must not be captured.")])
        self.claude_session(personal, [("user", "A private discussion #memex-off to be ignored.")])
        self.claude_session(personal, [
            ("user", "Here is my key: sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789 and a long question."),
            ("assistant", "brief"),
            ("tool", str(personal / "x.py")),
            ("assistant", "A" * 1500),
            ("user", "The rest of the discussion."),
            ("assistant", "B" * 1500),
        ], title="Key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789")
        report = Sweeper(self.config).run()
        self.assertEqual(len(report.written), 2)
        self.assertEqual(sum(report.excluded.values()), 2)
        texts = [p.read_text(encoding="utf-8") for p in sorted(self.config.outbox.glob("*.md"))]
        self.assertTrue(all("sk-ant" not in t for t in texts))
        self.assertIn("[REDACTED]", texts[0])
        self.assertNotIn("brief", texts[0])          # interim narration left out
        self.assertIn("format: memex/segment-2\n", texts[0])
        self.assertIn("\n## User — 2026-11-12T09:00:00Z\n", texts[0])
        self.assertIn("\n## Assistant — ", texts[0])
        first = segment_mod.load(sorted(self.config.outbox.glob("*.md"))[0])
        self.assertEqual((first.project, first.project_remote, first.seq, first.files), ("personal", "github.com/me/personal", 1, ["x.py"]))
        self.assertEqual(oct(sorted(self.config.outbox.glob("*.md"))[0].stat().st_mode & 0o777), "0o600")
        self.assertEqual(Sweeper(self.config).run().written, [])

    def test_active_sessions_wait_unless_forced(self):
        self.set(MEMEX_IDLE_MINUTES="30")
        session = self.claude_session(self.make_repo("p"), [("user", "A session that is still being written.")])
        self.assertEqual((Sweeper(self.config).run().active, len(list(self.config.outbox.glob("*.md")))), (1, 0))
        self.assertEqual(len(Sweeper(self.config).run(force=[session]).written), 1)

    def test_skip_existing_ignores_history_but_captures_what_follows(self):
        session = self.claude_session(self.make_repo("p"), [("user", "An old session, from before the installation.")])
        report = Sweeper(self.config).run(discard=True)
        self.assertEqual((report.written, list(self.config.outbox.glob("*.md"))), ([], []))
        self.assertEqual(Sweeper(self.config).run(force_all=True).written, [])
        self.claude_session(self.root / "code" / "p", [("user", "A follow-up, written after the installation.")], session=session, append=True)
        written = Sweeper(self.config).run().written
        self.assertEqual(len(written), 1)
        text = (self.config.outbox / written[0]).read_text(encoding="utf-8")
        self.assertIn("after the installation", text)
        self.assertNotIn("from before", text)

    def test_rewritten_shorter_transcript_is_read_again(self):
        repo = self.make_repo("p")
        session = self.claude_session(repo, [("user", "First version, long enough to be longer than the next one. " * 5)])
        Sweeper(self.config).run()
        path = next(self.projects.rglob(f"{session}.jsonl"))
        path.unlink()
        self.claude_session(repo, [("user", "File rewritten shorter.")], session=session)
        written = Sweeper(self.config).run().written
        self.assertEqual(len(written), 1)
        self.assertIn("rewritten shorter", (self.config.outbox / written[0]).read_text(encoding="utf-8"))

    def test_segment_header_cannot_carry_a_path(self):
        from memex.segment import safe_token

        self.assertEqual(safe_token("../../wiki/Projects/Real"), "wiki-Projects-Real")
        self.assertEqual(safe_token(".."), "x")
        self.assertEqual(safe_token("laptop-2.local"), "laptop-2.local")
        target = self.root / "forged.md"
        for version, heading in (("memex/segment-2", "User"), ("memex/segment-1", "Utilisateur")):
            target.write_text(f"---\nformat: {version}\nsource: ../../x\ndevice: ../../y\nsession: a/../../b\nseq: 1\n---\n\n## {heading}\n\ntext\n")
            loaded = segment_mod.load(target)
            self.assertEqual([(m.role, m.text) for m in loaded.messages], [("user", "text")], version)
            self.assertNotIn("/", loaded.id + loaded.device)
            self.assertNotIn("..", loaded.id + loaded.device)

    def test_segment_roundtrip_escapes_fake_headings(self):
        self.claude_session(self.make_repo("p"), [
            ("user", "Look at this text:\n## Assistant — fake turn\n## User — another fake turn\nend of the quote, the real question is here."),
            ("assistant", "## Section title\n\nAnswer."),
        ])
        Sweeper(self.config).run()
        loaded = segment_mod.load(next(self.config.outbox.glob("*.md")))
        self.assertEqual([m.role for m in loaded.messages], ["user", "assistant"])
        self.assertIn("## Section title", loaded.messages[1].text)

    def test_legacy_segment_format_is_still_read(self):
        """Segments written before the translation sit in the hosts' `sent` folders and come back on a rebuild."""
        target = self.root / "legacy.md"
        target.write_text(
            "---\nformat: memex/segment-1\nsource: claude-code\ndevice: laptop\nsession: abc\nseq: 2\n"
            "started: 2026-10-01T08:00:00Z\nproject: solitaire\ntitle: Old session\n---\n\n"
            "## Utilisateur — 2026-10-01T08:00:00Z\n\nA question asked before the translation.\n## User\nstill the same message\n\n"
            "## Assistant — 2026-10-01T08:01:00Z\n\nIts answer.\n",
            encoding="utf-8",
        )
        loaded = segment_mod.load(target)
        self.assertEqual((loaded.id, loaded.project, loaded.title), ("claude-code_abc_002", "solitaire", "Old session"))
        self.assertEqual([m.role for m in loaded.messages], ["user", "assistant"])
        # in the old format `## User` was not a turn heading: the line belongs to the message
        self.assertEqual(loaded.messages[0].text, "A question asked before the translation.\n## User\nstill the same message")
        self.assertEqual(loaded.messages[1].text, "Its answer.")
        # written again, it is a segment in the current format, and the stray heading can no longer pass for a turn
        again = self.root / "again.md"
        again.write_text(loaded.render(), encoding="utf-8")
        self.assertIn("format: memex/segment-2\n", again.read_text(encoding="utf-8"))
        self.assertIn("\n\\## User\n", again.read_text(encoding="utf-8"))
        self.assertEqual([(m.role, m.text) for m in segment_mod.load(again).messages],
                         [("user", "A question asked before the translation.\n\\## User\nstill the same message"), ("assistant", "Its answer.")])

    def test_both_generations_of_turn_headings_are_neutralised(self):
        from memex.segment import Message, Segment
        from memex.util import now

        quote = "Quoted:\n## Utilisateur — fake\n## User — fake\n## Assistant — fake\nend."
        text = Segment("claude-code", "laptop", "s", 1, [Message("user", quote)], now(), now()).render()
        for heading in ("Utilisateur", "User", "Assistant"):
            self.assertIn(f"\n\\## {heading} — fake\n", text)
        legacy = text.replace("format: memex/segment-2", "format: memex/segment-1").replace("\n## User\n", "\n## Utilisateur\n")
        self.assertNotEqual(legacy, text)
        for label, content in (("current reader", text), ("legacy reader", legacy)):
            target = self.root / "quoted.md"
            target.write_text(content, encoding="utf-8")
            self.assertEqual(len(segment_mod.load(target).messages), 1, label)

    def test_unknown_segment_format_is_refused(self):
        target = self.root / "future.md"
        target.write_text("---\nformat: memex/segment-9\nsource: claude-code\n---\n\n## User\n\ntext\n")
        with self.assertRaises(ValueError) as caught:
            segment_mod.load(target)
        self.assertIn("unknown segment format", str(caught.exception))


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
        good = b"---\nformat: memex/segment-2\n---\n\nbody\n"
        result = gate.upload(self.config, "laptop", self._tar([
            ("20261112T090000Z_a_1.md", good, tarfile.REGTYPE),
            ("../escape.md", good, tarfile.REGTYPE),
            ("sub/folder.md", good, tarfile.REGTYPE),
            (".cache.md", good, tarfile.REGTYPE),
            ("script.sh", b"#!/bin/sh", tarfile.REGTYPE),
            ("link.md", b"", tarfile.SYMTYPE),
            ("not-a-segment.md", b"hello", tarfile.REGTYPE),
        ]))
        self.assertEqual(result["stored"], ["20261112T090000Z_a_1.md"])
        self.assertEqual(len(result["rejected"]), 6)
        self.assertEqual(result["rejected"]["not-a-segment.md"], "not a segment")
        inbox = self.config.inbox / "laptop"
        self.assertEqual([p.name for p in inbox.iterdir()], ["20261112T090000Z_a_1.md"])
        self.assertTrue(self.config.trigger.exists())
        again = gate.upload(self.config, "laptop", self._tar([("20261112T090000Z_a_1.md", good, tarfile.REGTYPE)]))
        self.assertEqual(again["duplicate"], ["20261112T090000Z_a_1.md"])

    def test_forced_command_only_allows_the_gate_and_the_wiki(self):
        def forced(command, device="laptop"):
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
        code, _, out = forced("memex-gate ping --device another-machine")
        self.assertEqual((code, json.loads(out)["device"]), (0, "laptop"))   # the device comes from the key


class VaultSync(Sandbox):
    def setUp(self):
        super().setUp()
        self.wiki = self.server_home / "wiki"
        self.sync = Vault(self.config, self.wiki)

    def test_export_import_and_conflict_rules(self):
        page = self.wiki / "Topics/A/A.md"
        page.parent.mkdir(parents=True)
        page.write_text("v1")
        self.sync.export()
        self.assertEqual((self.vault / "Topics/A/A.md").read_text(), "v1")
        self.assertEqual(self.sync.import_changes(), [])
        # the agent edits: exported
        page.write_text("v2 agent")
        self.sync.export()
        self.assertEqual((self.vault / "Topics/A/A.md").read_text(), "v2 agent")
        # the human edits: imported
        (self.vault / "Topics/A/A.md").write_text("v3 human")
        self.assertEqual(self.sync.import_changes(), ["Topics/A/A.md"])
        self.assertEqual(page.read_text(), "v3 human")
        # both edit: the export does not overwrite, the next import sides with the human
        page.write_text("v4 agent")
        (self.vault / "Topics/A/A.md").write_text("v4 human")
        self.sync.export()
        self.assertEqual((self.vault / "Topics/A/A.md").read_text(), "v4 human")
        self.sync.import_changes()
        self.assertEqual(page.read_text(), "v4 human")
        # deletion on the wiki side: propagated; hidden files of the vault: ignored
        (self.vault / ".obsidian").mkdir()
        (self.vault / ".obsidian/app.json").write_text("{}")
        self.sync.export()
        page.unlink()
        self.sync.export()
        self.assertFalse((self.vault / "Topics/A/A.md").exists())
        self.assertFalse((self.vault / "Topics/A").exists())
        self.assertTrue((self.vault / ".obsidian/app.json").exists())
        self.assertEqual(self.sync.import_changes(), [])

    def test_lost_manifest_never_lets_a_stale_vault_overwrite_the_wiki(self):
        page = self.wiki / "Topics/A/A.md"
        page.parent.mkdir(parents=True)
        page.write_text("recent")
        self.vault.mkdir(exist_ok=True)
        (self.vault / "Topics/A").mkdir(parents=True)
        (self.vault / "Topics/A/A.md").write_text("stale")
        (self.vault / "Topics/A/Only here.md").write_text("to keep")
        self.sync.manifest_path.unlink(missing_ok=True)
        self.assertEqual(self.sync.import_changes(), ["Topics/A/Only here.md"])
        self.sync.export()
        self.assertEqual(page.read_text(), "recent")
        self.assertEqual((self.vault / "Topics/A/A.md").read_text(), "recent")


class Guardrails(Sandbox):
    def setUp(self):
        super().setUp()
        self.server = Server(self.config)
        self.repo = self.server.repo
        root = self.repo.dir
        write_pages(root, {
            "Projects/P/P.md": "---\ntype: project\ntitle: P\nsummary: Project P.\n---\n\nP uses Tailscale to reach the server.\n",
            "Projects/P/Decisions/2026-01-01 Choice.md": "---\ntype: decision\ntitle: Choice\ndate: 2026-01-01\nstatus: active\n---\n\n## Decision\n\nWe do A.\n",
            "Concepts/Tailscale.md": "---\ntype: concept\ntitle: Tailscale\nsummary: Private network.\n---\n\nA private network.\n",
        })
        self.repo.commit_all(root, "base", "memex")
        self.tree = self.repo.new_worktree("t")
        self.base = self.repo.head("HEAD", cwd=self.tree)

    def check(self, role):
        return validate(self.repo, self.tree, self.base, role, "2026-11-12")

    def test_archivist_adds_and_fixes_headers(self):
        (self.tree / "Projects/P/Decisions/2026-11-12 New.md").write_text("## Decision\n\nWe do B.\n")
        (self.tree / "Projects/P/P.md").write_text((self.tree / "Projects/P/P.md").read_text() + "\nAddition.\n")
        verdict = self.check("archivist")
        self.assertTrue(verdict.ok, verdict.errors)
        self.assertEqual(sorted(verdict.touched), ["Projects/P/Decisions/2026-11-12 New.md", "Projects/P/P.md"])
        meta, _ = frontmatter.parse((self.tree / "Projects/P/Decisions/2026-11-12 New.md").read_text())
        self.assertEqual((meta["type"], meta["title"], meta["date"], meta["status"]), ("decision", "2026-11-12 New", "2026-11-12", "active"))

    def test_archivist_cannot_delete_rename_or_rewrite_decisions(self):
        (self.tree / "Projects/P/P.md").unlink()
        verdict = self.check("archivist")
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.errors, ["Projects/P/P.md: the archivist never deletes or renames a page"])
        self.assertEqual(verdict.feedback(), "- Projects/P/P.md: the archivist never deletes or renames a page")
        self.repo.git("reset", "--hard", "-q", self.base, cwd=self.tree)
        os.rename(self.tree / "Projects/P/P.md", self.tree / "Projects/P/Renamed.md")
        self.assertFalse(self.check("archivist").ok)
        self.repo.git("reset", "--hard", "-q", self.base, cwd=self.tree), self.repo.git("clean", "-fdq", cwd=self.tree)
        record = self.tree / "Projects/P/Decisions/2026-01-01 Choice.md"
        record.write_text(record.read_text().replace("We do A.", "We do Z."))
        verdict = self.check("archivist")
        self.assertFalse(verdict.ok)
        self.assertIn("an existing decision record is immutable (only `status` and `superseded_by` may change)", verdict.errors[0])
        record.write_text(record.read_text().replace("We do Z.", "We do A.").replace("status: active", 'status: superseded\nsuperseded_by: "[[Projects/P/Decisions/x]]"'))
        self.assertTrue(self.check("archivist").ok)

    def test_archivist_changes_outside_its_perimeter_are_undone(self):
        (self.tree / "Concepts/Tailscale.md").write_text("rewritten")
        (self.tree / "Concepts/New.md").write_text("created")
        (self.tree / "AGENTS.md").write_text("conventions rewritten")
        (self.tree / "Journal").mkdir(exist_ok=True)
        (self.tree / "Journal/x.md").write_text("journal")
        (self.tree / "root.md").write_text("outside any folder")
        (self.tree / "Projects/P/image.png").write_bytes(b"\x89PNG")
        verdict = self.check("archivist")
        self.assertTrue(verdict.ok, verdict.errors)
        self.assertFalse(verdict.changed)
        self.assertEqual(self.repo.git("status", "--porcelain", cwd=self.tree), "")

    def test_weaver_may_only_add_links_see_also_and_concepts(self):
        page = self.tree / "Projects/P/P.md"
        page.write_text(page.read_text().replace("Tailscale", "[[Concepts/Tailscale|Tailscale]]") + "\n## See also\n\n- [[Concepts/Tailscale]] — the network\n")
        (self.tree / "Concepts/New.md").write_text("---\ntype: concept\ntitle: New\nsummary: x.\n---\n\nText.\n")
        self.assertTrue(self.check("weaver").ok)
        page.write_text(page.read_text().replace("to reach", "in order to reach"))
        verdict = self.check("weaver")
        self.assertFalse(verdict.ok)
        self.assertIn('the weaver changed the text; it may only add links and a "See also" section', verdict.errors[0])
        self.repo.git("reset", "--hard", "-q", self.base, cwd=self.tree), self.repo.git("clean", "-fdq", cwd=self.tree)
        (self.tree / "Projects/P/Other.md").write_text("---\ntype: project\ntitle: Other\n---\n\nx\n")
        self.assertFalse(self.check("weaver").ok)
        self.repo.git("reset", "--hard", "-q", self.base, cwd=self.tree), self.repo.git("clean", "-fdq", cwd=self.tree)
        record = self.tree / "Projects/P/Decisions/2026-01-01 Choice.md"
        record.write_text(record.read_text().replace("We do A.", "We do [[Projects/P/P|A]]."))
        self.assertFalse(self.check("weaver").ok)

    def test_unsafe_file_names_are_renamed_and_links_follow(self):
        from memex.pages import safe_name

        self.assertEqual(safe_name("2026-11-13 Architecture: central VPS.md"), "2026-11-13 Architecture - central VPS.md")
        self.assertEqual(safe_name('What? "That" #1 [x].md'), "What That 1 x.md")
        self.assertEqual(safe_name("Already safe (v2).md"), "Already safe (v2).md")
        self.assertEqual(safe_name("???.md"), "untitled.md")
        bad = "Projects/P/Decisions/2026-11-12 Design: central server.md"
        (self.tree / bad).write_text("## Decision\n\nA central server.\n")
        page = self.tree / "Projects/P/P.md"
        page.write_text(page.read_text() + "\n- [[Projects/P/Decisions/2026-11-12 Design: central server|the record]] and [[2026-11-12 Design: central server]]\n")
        verdict = self.check("archivist")
        self.assertTrue(verdict.ok, verdict.errors)
        good = "Projects/P/Decisions/2026-11-12 Design - central server.md"
        self.assertIn(good, verdict.touched)
        self.assertFalse((self.tree / bad).exists())
        text = page.read_text()
        self.assertIn("[[Projects/P/Decisions/2026-11-12 Design - central server|the record]]", text)
        self.assertIn("[[2026-11-12 Design - central server]]", text)
        self.assertNotIn(": central", text)

    def test_project_reuses_a_same_name_folder_and_forgets_stale_mappings(self):
        from memex.segment import Message, Segment
        from memex.util import now, write_json

        root = self.repo.dir
        seg = Segment("claude-code", "d", "s", 1, [Message("user", "x")], now(), now(), project="atlas", project_name="atlas", project_remote="github.com/me/atlas")
        self.assertEqual(self.server.folder_for(root, seg), ("Projects/Atlas", True))
        (root / "Topics/Atlas").mkdir(parents=True)
        self.assertEqual(self.server.folder_for(root, seg), ("Topics/Atlas", False))   # opened as a topic before it had a repository
        write_json(root / ".memex/projects.json", {"github.com/me/atlas": "Projects/Gone"})
        self.assertEqual(self.server.folder_for(root, seg), ("Topics/Atlas", False))   # stale mapping ignored
        (root / "Projects/Atlas").mkdir(parents=True)
        self.assertEqual(self.server.folder_for(root, seg), ("Projects/Atlas", False))

    def test_weaver_cannot_redirect_an_existing_link_but_may_fix_a_broken_one(self):
        page = self.tree / "Projects/P/P.md"
        base_text = page.read_text() + "\nSee [[Concepts/Tailscale|the network]] and [[Missing page|something else]].\n"
        page.write_text(base_text)
        self.repo.commit_all(self.tree, "starting links", "memex")
        self.base = self.repo.head("HEAD", cwd=self.tree)
        (self.tree / "Concepts/Other.md").write_text("---\ntype: concept\ntitle: Other\nsummary: x.\n---\n\nText.\n")
        page.write_text(base_text.replace("[[Concepts/Tailscale|the network]]", "[[Concepts/Other|the network]]"))
        verdict = self.check("weaver")
        self.assertFalse(verdict.ok)
        self.assertIn("redirected", verdict.errors[0])
        page.write_text(base_text.replace("[[Missing page|something else]]", "[[Concepts/Other|something else]]"))
        self.assertTrue(self.check("weaver").ok)        # fixing a broken link is still allowed

    def test_agent_cannot_break_the_git_link_of_its_worktree(self):
        link = self.tree / ".git"
        original = link.read_text()
        link.write_text("gitdir: /dev/null\n")
        (self.tree / "Projects/P/P.md").write_text((self.tree / "Projects/P/P.md").read_text() + "\nAddition.\n")
        verdict = self.check("archivist")
        self.assertTrue(verdict.ok, verdict.errors)
        self.assertEqual(link.read_text(), original)
        link.unlink()
        link.mkdir()
        (link / "config").write_text("[core]\n")
        self.assertTrue(self.check("archivist").ok)
        self.assertEqual(link.read_text(), original)

    def test_stamping_preserves_hand_written_header_lines(self):
        target = self.repo.dir / "Topics/Main/Main.md"
        target.parent.mkdir(parents=True)
        target.write_text("---\ntype: topic\ntitle: Main\nauthor:\n  name: Alex\ncode: 007\n---\n\nText.\n")
        self.server._stamp_pages(self.repo.dir, ["Topics/Main/Main.md"], "claude-code_abc_001")
        text = target.read_text()
        self.assertIn("author:\n  name: Alex\ncode: 007\n", text)
        self.assertIn("sources:\n  - claude-code_abc_001\n", text)
        self.assertRegex(text, r"\nupdated: \d{4}-\d{2}-\d{2}\n---\n")

    def test_header_keeps_only_the_last_sources(self):
        """A living page touched by dozens of sessions must not drag an endless header along."""
        target = self.repo.dir / "Topics/Main/Main.md"
        target.parent.mkdir(parents=True)
        target.write_text("---\ntype: topic\ntitle: Main\n---\n\nText.\n")
        for number in range(1, 8):
            self.server._stamp_pages(self.repo.dir, ["Topics/Main/Main.md"], f"claude-code_s{number}_001")
        meta, _body = frontmatter.parse(target.read_text())
        self.assertEqual(meta["sources"], [f"claude-code_s{n}_001" for n in range(3, 8)])

    def test_curator_may_delete_pages_but_not_decisions(self):
        (self.tree / "Concepts/Tailscale.md").unlink()
        verdict = self.check("curator")
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.deleted, ["Concepts/Tailscale.md"])
        (self.tree / "Projects/P/Decisions/2026-01-01 Choice.md").unlink()
        self.assertFalse(self.check("curator").ok)

    def test_normalisation_ignores_links_only(self):
        a = "---\ntitle: T\nentities: [x]\n---\n\nP uses Tailscale.\n"
        b = "---\ntitle: T\nentities: [x, y]\nupdated: 2026\n---\n\nP uses [[Concepts/Tailscale|Tailscale]].\n\n## See also\n\n- [[A]]\n"
        self.assertEqual(normalize_for_links(a), normalize_for_links(b))
        self.assertNotEqual(normalize_for_links(a), normalize_for_links(b.replace("uses", "employs")))


class WikiModel(Sandbox):
    def test_links_index_and_lint(self):
        root = self.server_home / "wiki"
        write_pages(root, {
            "Projects/Solitaire/Solitaire.md": "---\ntype: project\ntitle: Solitaire\nsummary: A game.\nentities: [Tailscale]\n---\n\nSee [[Concepts/Network|the network]] and [[Missing page]]. `[[not a link]]`\n",
            "Projects/Solitaire/Decisions/2026-11-12 Scores.md": "---\ntype: decision\ntitle: Scores\ndate: 2026-11-12\nstatus: superseded\n---\n\nx\n",
            "Projects/Atlas/Atlas.md": "---\ntype: project\ntitle: Atlas\nsummary: Finance.\nentities: [tailscale]\n---\n\nAs for Solitaire, we go through a private network.\n",
            "Concepts/Network.md": "---\ntype: concept\ntitle: Network\naliases: [private network]\n---\n\nx\n",
            "Topics/Alone/Notes.md": "---\ntype: topic\ntitle: Notes\n---\n\nx\n",
        })
        pages = load_wiki(root)
        self.assertEqual(links(pages["Projects/Solitaire/Solitaire.md"].body), [("Concepts/Network", "the network"), ("Missing page", "")])
        self.assertEqual(unlink("a [[X/Y|b]] c [[Z]]"), "a b c Z")
        self.assertEqual(folder_of("Projects/Solitaire/Decisions/x.md"), "Projects/Solitaire")
        resolver = LinkIndex(list(pages))
        self.assertEqual(resolver.resolve("network"), "Concepts/Network.md")
        self.assertEqual(resolver.resolve("Solitaire/Solitaire"), "Projects/Solitaire/Solitaire.md")
        self.assertIsNone(resolver.resolve("Missing page"))
        text = build_index(pages)
        self.assertIn("[[Projects/Solitaire/Solitaire|Solitaire]] — A game. · 1 decision(s), 0 active", text)
        self.assertIn("_(superseded)_", text)
        self.assertEqual(text, build_index(load_wiki(root)))
        report = lint(root)
        self.assertEqual(report.broken, [{"page": "Projects/Solitaire/Solitaire.md", "target": "Missing page"}])
        self.assertIn("Projects/Solitaire/Decisions/2026-11-12 Scores.md", report.orphans)
        unlinked = {(u["page"], u["target"]) for u in report.unlinked}
        self.assertIn(("Projects/Atlas/Atlas.md", "Projects/Solitaire/Solitaire.md"), unlinked)
        self.assertIn(("Projects/Atlas/Atlas.md", "Concepts/Network.md"), unlinked)
        self.assertEqual([c["entity"].lower() for c in report.concepts], ["tailscale"])
        # the report uses the documented keys
        self.assertEqual({key for item in report.unlinked for key in item}, {"page", "target", "term"})
        self.assertEqual(set(report.concepts[0]), {"entity", "folders", "pages", "page_count"})
        self.assertEqual(report.concepts[0]["folders"], ["Projects/Atlas", "Projects/Solitaire"])
        self.assertIn({"page": "Topics/Alone/Notes.md", "problem": "header without `summary` field"}, report.problems)
        self.assertEqual(report.stats, {"pages": 5, "decisions": 1, "concepts": 1, "folders": 3})
        self.assertEqual(list(report.as_dict()), ["problems", "broken_links", "orphans", "unlinked_mentions", "concept_candidates", "stats"])
        self.assertTrue(lint_summary(report).startswith("5 pages, 3 folders, 1 decisions; 2 problem(s), 1 broken link(s), "))

    def test_most_shared_concepts_come_first_and_long_pages_are_flagged(self):
        root = self.server_home / "wiki"

        def page(folder, entities, body="x"):
            name = folder.split("/")[-1]
            target = root / folder / f"{name}.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"---\ntype: project\ntitle: {name}\nsummary: r\nentities: [{', '.join(entities)}]\n---\n\n{body}\n", encoding="utf-8")

        page("Projects/A", ["Apache", "systemd"], body="word " * 400)
        page("Projects/B", ["Apache", "systemd"])
        page("Projects/C", ["systemd"])
        concepts = lint(root).concepts
        self.assertEqual([c["entity"] for c in concepts], ["systemd", "Apache"])     # three folders before two
        listing = compact_index(load_wiki(root), long_page=1000)
        self.assertIn("Projects/A/A.md — A [already long: ", listing)
        self.assertNotIn("Projects/B/B.md — B [", listing)
        self.assertNotIn("already long", compact_index(load_wiki(root)))


class Integration(Sandbox):
    init_server = False

    def test_claude_and_codex_hooks_are_merged_idempotently_and_removed_cleanly(self):
        claude, codex = self.root / "claude", self.root / "codex"
        claude.mkdir(), codex.mkdir()
        original = {"model": "opus", "hooks": {"SessionStart": [{"matcher": "x", "hooks": [{"type": "command", "command": "other-tool"}]}]},
                    "permissions": {"allow": ["Bash(ls:*)"]}}
        (claude / "settings.json").write_text(json.dumps(original))
        (claude / "CLAUDE.md").write_text("# My rules\n\nAlways answer briefly.\n")
        (codex / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "other-tool"}]}]}}))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude), "CODEX_HOME": str(codex)}):
            for _ in range(2):
                integrate.claude(self.config, "/bin/memex", True)
                integrate.codex(self.config, "/bin/memex", True)
            settings = json.loads((claude / "settings.json").read_text())
            starts = settings["hooks"]["SessionStart"]
            self.assertEqual(len(starts), 2)
            self.assertEqual(starts[0]["hooks"][0]["command"], "other-tool")
            self.assertEqual(starts[1]["hooks"][0]["command"], "/bin/memex hook session-start")
            self.assertEqual(settings["hooks"]["SessionEnd"][0]["hooks"][0]["command"], "/bin/memex hook session-end")
            self.assertEqual(len(settings["permissions"]["allow"]), 2)
            self.assertEqual((claude / "CLAUDE.md").read_text().count("memex:begin"), 1)
            self.assertTrue((claude / "CLAUDE.md").read_text().startswith("# My rules"))
            self.assertIn("## Shared memory (memex)", (claude / "CLAUDE.md").read_text())
            self.assertEqual(len(json.loads((codex / "hooks.json").read_text())["hooks"]["SessionStart"]), 2)
            self.assertEqual(integrate.status(self.config), {"claude_hooks": True, "claude_pointer": True, "codex_hooks": True, "codex_pointer": True})
            integrate.claude(self.config, "/bin/memex", False)
            integrate.codex(self.config, "/bin/memex", False)
            self.assertEqual(json.loads((claude / "settings.json").read_text()), original)
            self.assertEqual((claude / "CLAUDE.md").read_text(), "# My rules\n\nAlways answer briefly.\n")
            self.assertFalse((codex / "AGENTS.md").exists() and "memex" in (codex / "AGENTS.md").read_text())
            self.assertFalse(any(integrate.status(self.config).values()))

    def test_unreadable_settings_are_left_alone(self):
        claude = self.root / "claude"
        claude.mkdir()
        (claude / "settings.json").write_text("{ not json")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude)}):
            self.assertIn("unreadable", integrate.claude(self.config, "memex", True)[0])
        self.assertEqual((claude / "settings.json").read_text(), "{ not json")


class Imports(Sandbox):
    init_server = False

    def test_chatgpt_and_claude_exports(self):
        chatgpt = [{
            "id": "c1", "title": "Savings plan", "create_time": 1794000000, "update_time": 1794000300, "current_node": "n3",
            "mapping": {
                "n0": {"message": None, "parent": None},
                "n1": {"parent": "n0", "message": {"author": {"role": "system"}, "content": {"content_type": "text", "parts": ["system"]}}},
                "n2": {"parent": "n1", "message": {"author": {"role": "user"}, "create_time": 1794000001, "content": {"content_type": "text", "parts": ["Should I open a savings plan? " + "details " * 60]}}},
                "nX": {"parent": "n1", "message": {"author": {"role": "user"}, "content": {"content_type": "text", "parts": ["abandoned branch"]}}},
                "n3": {"parent": "n2", "message": {"author": {"role": "assistant"}, "create_time": 1794000002, "content": {"content_type": "text", "parts": ["Yes."]}}},
            },
        }, {"id": "c2", "title": "short", "current_node": "a", "mapping": {"a": {"parent": None, "message": {"author": {"role": "user"}, "content": {"content_type": "text", "parts": ["hi"]}}}}}]
        archive = self.root / "chatgpt.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("conversations.json", json.dumps(chatgpt))
        report = imports.run(self.config, "chatgpt", archive)
        self.assertEqual((report.conversations, len(report.written), report.skipped_short), (2, 1, 1))
        loaded = segment_mod.load(next(self.config.outbox.glob("*chatgpt*.md")))
        self.assertEqual([m.role for m in loaded.messages], ["user", "assistant"])
        self.assertNotIn("abandoned branch", loaded.render())
        self.assertEqual(imports.run(self.config, "chatgpt", archive).written, [])

        claude = [{"uuid": "u1", "name": "Holidays", "updated_at": "2026-11-10T10:00:00Z", "chat_messages": [
            {"sender": "human", "text": "Where to go in May? " + "context " * 60, "created_at": "2026-11-10T09:00:00Z"},
            {"sender": "assistant", "text": "", "content": [{"type": "text", "text": "To Portugal."}], "created_at": "2026-11-10T09:01:00Z"}]}]
        path = self.root / "conversations.json"
        path.write_text(json.dumps(claude))
        self.assertEqual(len(imports.run(self.config, "claude", path).written), 1)
        claude[0]["chat_messages"].append({"sender": "human", "text": "And in June?", "created_at": "2026-11-11T09:00:00Z"})
        path.write_text(json.dumps(claude))
        again = imports.run(self.config, "claude", path)
        self.assertEqual(len(again.written), 1)
        self.assertTrue(again.written[0].endswith("_002.md"))


class Sandboxing(Sandbox):
    """The real sandbox (bubblewrap). Skipped on a machine where it cannot start."""

    overrides = {"MEMEX_SANDBOX": "auto"}
    init_server = False

    def setUp(self):
        super().setUp()
        if "active" not in Agent(self.config).sandbox_state():
            self.skipTest("bubblewrap unavailable here")
        self.work = self.root / "work"
        self.work.mkdir()
        self.evasion = Path.home() / ".memex-test-evasion"

    def tearDown(self):
        self.evasion.unlink(missing_ok=True)
        super().tearDown()

    def test_edit_agent_writes_only_inside_its_workdir(self):
        outside = self.root / "elsewhere"
        outside.mkdir()
        self.set(MEMEX_AGENT_EDIT_CMD=(
            f"echo inside > inside.txt; echo outside > {outside}/outside.txt; echo outside > {self.evasion}; "
            "ls ~/.ssh 2>/dev/null | wc -l > ssh.txt; echo finished > $MEMEX_OUT_FILE"
        ))
        result = Agent(self.config).run("edit", "prompt", self.work)
        self.assertEqual(result.text.strip(), "finished")
        self.assertEqual((self.work / "inside.txt").read_text().strip(), "inside")
        self.assertFalse((outside / "outside.txt").exists())
        self.assertFalse(self.evasion.exists())
        self.assertEqual((self.work / "ssh.txt").read_text().strip(), "0")   # the SSH keys are hidden

    def test_read_agent_cannot_write_anywhere(self):
        (self.work / "readable.txt").write_text("readable content")
        self.set(MEMEX_AGENT_READ_CMD="echo x > attempt.txt; cat readable.txt")
        result = Agent(self.config).run("read", "prompt", self.work)
        self.assertIn("readable content", result.text)
        self.assertFalse((self.work / "attempt.txt").exists())

    def test_required_sandbox_fails_closed_when_missing(self):
        self.set(MEMEX_SANDBOX="bwrap", MEMEX_AGENT_EDIT_CMD="true")
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(AgentError):
                Agent(self.config).run("edit", "prompt", self.work)
        self.set(MEMEX_SANDBOX="auto")
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(Agent(self.config).sandbox("edit", self.work, self.root), [])


class Devices(Sandbox):
    KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHk0cDg5bXhGZ0x2b1pWd2Jqc0R3eXJ0dG9mZmFrZWtleQ me@laptop"

    def test_add_replace_list_remove(self):
        from memex import serverops

        self.assertIn("authorised", serverops.device_add(self.config, "laptop", self.KEY))
        self.assertIn("authorised", serverops.device_add(self.config, "laptop", self.KEY))   # can be replayed
        other = self.KEY.replace("IHk0", "IHk1").replace("me@laptop", "me@desktop")
        serverops.device_add(self.config, "desktop", other)
        lines = (self.root / "authorized_keys").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.startswith('restrict,command="') and "--forced --device " in line for line in lines))
        self.assertEqual(serverops.device_list(self.config), ["laptop", "desktop"])
        self.assertTrue((self.config.inbox / "desktop").is_dir())
        self.assertIn("removed", serverops.device_remove(self.config, "laptop"))
        self.assertEqual(serverops.device_list(self.config), ["desktop"])
        self.assertIn("No device", serverops.device_remove(self.config, "unknown"))

    def test_rejects_bad_names_and_keys(self):
        from memex import serverops

        self.assertIn("Invalid device name", serverops.device_add(self.config, "name with space", self.KEY))
        self.assertIn("Invalid public key", serverops.device_add(self.config, "ok", 'command="sh" ' + self.KEY))
        self.assertIn("Invalid public key", serverops.device_add(self.config, "ok", "not a key"))
        self.assertFalse((self.root / "authorized_keys").exists())


class Agents(Sandbox):
    init_server = False

    def test_role_and_language_are_exported_to_the_harness(self):
        self.set(MEMEX_AGENT_EXTRACT_CMD='echo "$MEMEX_ROLE/$MEMEX_LANG/$MEMEX_PIPELINE"; cat "$MEMEX_PROMPT_FILE"')
        result = Agent(self.config).run("extract", "a prompt", self.root, role="extract")
        self.assertEqual(result.text.splitlines(), ["extract/en/1", "[memex:pipeline]", "a prompt"])
        i18n.use("fr")
        result = Agent(self.config).run("extract", "a prompt", self.root, role="weave")
        self.assertEqual(result.text.splitlines()[0], "weave/fr/1")

    def test_probe_uses_the_documented_prompt_and_role(self):
        with mock.patch.object(Agent, "run", return_value=AgentResult("memex-ok\n", 0, 0.0)) as run:
            self.assertEqual(Agent(self.config).healthy(self.root), (True, ""))
        self.assertEqual(run.call_args.args[:2], ("extract", "Reply with this single word and nothing else: memex-ok"))
        self.assertEqual(run.call_args.kwargs["role"], "probe")
        with mock.patch.object(Agent, "run", return_value=AgentResult("I would rather not.", 0, 0.0)):
            healthy, why = Agent(self.config).healthy(self.root)
        self.assertFalse(healthy)
        self.assertIn("unexpected reply to the probe", why)


class ServerState(Sandbox):
    def test_status_reports_the_documented_keys(self):
        status = Server(self.config).status()
        self.assertEqual(list(status), [
            "wiki", "pending", "processed", "failures", "quarantined", "pending_review", "agent", "agent_unavailable",
            "sandbox", "vault"])
        self.assertTrue(status["wiki"].startswith("0 pages, 0 folders, 0 decisions; 0 problem(s)"))
        self.assertEqual([status[key] for key in ("pending", "processed", "failures", "quarantined", "pending_review")], [0, 0, {}, 0, []])
        self.assertEqual((status["agent"], status["agent_unavailable"]), ("hermes", "no"))
        self.assertEqual(status["sandbox"], "disabled (MEMEX_SANDBOX=none)")
        self.assertEqual(status["vault"], str(self.vault))
        self.set(MEMEX_HOME=str(self.root / "no-server-yet"), MEMEX_VAULT_DIR="")
        status = Server(self.config).status()
        self.assertEqual((status["wiki"], status["vault"]), ("not initialised", "(disabled)"))

    def test_curator_proposes_at_most_the_configured_number_of_actions(self):
        self.set(MEMEX_CURATOR_MAX_ACTIONS="1")
        server = Server(self.config)
        write_pages(server.repo.dir, {
            "Topics/Duplicate/Duplicate.md": "---\ntype: topic\ntitle: Duplicate\nsummary: Page Duplicate.\n---\n\nCopy.\n",
            "Topics/Long/Long.md": "---\ntype: topic\ntitle: Long\nsummary: Page Long.\n---\n\nText.\nRepetition.\nRepetition.\n",
        })
        server.repo.commit_all(server.repo.dir, "two pages to tidy up", "human")
        reviews = Reviews(server)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(reviews.run(), 0)
        self.assertEqual([a["kind"] for a in reviews.pending()["actions"]], ["delete"])
        self.assertEqual([c["role"] for c in self.agent_calls()], ["diagnose", "apply"])


class CommandLine(Sandbox):
    def memex(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(["--config", str(self.env_file), *argv])
            except SystemExit as stop:               # the parser turned the command line down
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def test_server_commands_use_the_new_names(self):
        self.assertEqual(self.memex("server", "weave", "--force")[:2], (0, "Nothing to link.\n"))
        code, out, _err = self.memex("server", "curate", "--force")
        self.assertEqual(code, 0)
        self.assertIn("? To decide: Manual or scripted deployment?", out)
        for former in ("tisseur", "verif"):
            code, _out, err = self.memex("server", former)
            self.assertEqual(code, 2)
            self.assertIn("invalid choice", err)

    def test_lint_and_status_print_the_documented_keys(self):
        code, out, _err = self.memex("server", "lint")
        self.assertEqual(code, 0)
        first, _, rest = out.partition("\n")
        self.assertTrue(first.startswith("0 pages, 0 folders, 0 decisions; "))
        self.assertEqual(list(json.loads(rest)), ["problems", "broken_links", "orphans", "unlinked_mentions", "concept_candidates", "stats"])
        code, out, _err = self.memex("server", "status")
        self.assertEqual(code, 0)
        self.assertEqual([line.split(":")[0].strip() for line in out.splitlines()], list(Server(self.config).status()))
        self.assertIn("agent_unavailable : no\n", out)


class Hooks(Sandbox):
    init_server = False

    def test_context_is_cut_with_a_marker_and_says_where_it_comes_from(self):
        repo = self.make_repo("solitaire", "git@github.com:me/solitaire.git")
        clone = self.config.wiki_clone
        write_pages(clone, {
            ".memex/projects.json": json.dumps({"github.com/me/solitaire": "Projects/Solitaire"}),
            "Projects/Solitaire/Solitaire.md": "---\ntype: project\ntitle: Solitaire game\n---\n\n" + "A line of the living page.\n" * 400,
        })
        self.set(MEMEX_HOOK_MAX_CHARS="1500")
        context = hook.project_context(self.config, str(repo))
        self.assertTrue(context.startswith('# Memory of project "Solitaire game" (from earlier sessions)\n\nA line of the living page.\n'))
        self.assertIn("\n\n[… page truncated: read the file for the rest …]\n\n---\nSource: memex wiki, read-only (", context)
        self.assertIn("This project's page: `Projects/Solitaire/Solitaire.md`; detailed decision records: `Projects/Solitaire/Decisions/`.", context)
        self.assertLess(context.count("A line of the living page."), 60)
        self.assertEqual(hook.project_context(self.config, str(self.make_repo("unknown"))), "")


class Isolation(Sandbox):
    """The sandbox itself: nothing may reach the real configuration, sessions or wiki of whoever runs the tests."""

    init_server = False

    def test_every_path_stays_inside_the_temporary_directory(self):
        from memex import serverops

        config = self.config
        self.assertEqual(config.path, self.env_file)
        self.assertEqual(Config.load().path, self.env_file)          # what a `memex` subprocess would find
        paths = {
            "host data": config.data_dir, "outbox": config.outbox, "wiki clone": config.wiki_clone,
            "server home": config.home, "wiki": config.wiki_repo, "vault": config.vault_dir,
            "authorized_keys": serverops._authorized_keys(config),
            "Claude Code folder": integrate.claude_dir(), "Codex folder": integrate.codex_dir(),
        }
        for label, path in paths.items():
            self.assertTrue(path.is_relative_to(self.root), f"{label}: {path}")
        # every setting whose default points into the home directory or the system is overridden
        for key, default in DEFAULTS.items():
            if default.startswith(("~", "/")) and key != "MEMEX_SERVER_HOME":     # that one is a path on a remote server
                self.assertTrue(config.path_value(key).is_relative_to(self.root), key)
        self.assertEqual({key for key in os.environ if key.startswith("MEMEX_")}, {"MEMEX_CONFIG"})

    def test_background_sync_is_never_started(self):
        with self.assertRaises(AssertionError):
            hook.session_end(self.config, "{}")

    def test_language_is_english_again_whatever_the_previous_test_left(self):
        seen = []

        class Probe(Sandbox):
            init_server = False

            def runTest(self):
                seen.append(i18n.current().lang)
                i18n.use("fr")

        i18n.use("fr")
        result = unittest.TestResult()
        Probe().run(result)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self.assertEqual(seen, ["en"])
        self.assertEqual(i18n.current().lang, "en")


class Misc(unittest.TestCase):
    def test_env_parsing(self):
        values = parse_env_text('# comment\nexport MEMEX_A="x y"\nMEMEX_B=value # end\nMEMEX_C=\'a#b\'\n\nbad line\n')
        self.assertEqual(values, {"MEMEX_A": "x y", "MEMEX_B": "value", "MEMEX_C": "a#b"})
        config = Config({"MEMEX_IDLE_MINUTES": "abc", "MEMEX_DEVICE": "my laptop !"})
        self.assertEqual((config.int("MEMEX_IDLE_MINUTES"), config.device), (10, "my-laptop-"))

    def test_json_reply_parsing(self):
        self.assertEqual(parse_json_reply('Here:\n```json\n{"a": 1}\n```\nthere'), {"a": 1})
        self.assertEqual(parse_json_reply('text {"a": {"b": 2}} end'), {"a": {"b": 2}})
        with self.assertRaises(AgentError):
            parse_json_reply("no object here")

    def test_json_reply_with_several_objects_is_the_last_one_or_a_failure(self):
        # a model that corrects itself leaves several objects in a row: the last one is its answer
        self.assertEqual(parse_json_reply('A first try: {"a": 1}\nOn reflection: {"a": 2, "b": {"c": 3}}\nThat is all.'), {"a": 2, "b": {"c": 3}})
        self.assertEqual(parse_json_reply('{"a": 1} {"a": 2}'), {"a": 2})
        # one malformed object makes the reply ambiguous: failing (and retrying) beats settling for a draft
        for reply in ('{"a": 1} and then {"a": 2', 'Draft: {"a": } Final: {"a": 2}', "{not json} {\"a\": 1}"):
            with self.assertRaises(AgentError, msg=reply):
                parse_json_reply(reply)


if __name__ == "__main__":
    unittest.main()
