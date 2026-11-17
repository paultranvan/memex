"""End-to-end scenarios: host → server → wiki → vault → host, with a fake agent."""
import json
import os
import unittest

from helpers import Sandbox, git

from memex import hook, serverops
from memex.pipeline import Server
from memex.review import Reviews, restore
from memex.ship import pull, resend_all, ship, sync
from memex.sweep import Sweeper, write_note

LONG = "We need to choose where to store the game scores, locally or on a server."
RECORD = "Projects/Solitaire/Decisions/2026-11-12 Server-side scores.md"


class EndToEnd(Sandbox):
    def host_sync(self):
        self.assertEqual(sync(self.config, quiet=True), 0)

    def server_run(self):
        return Server(self.config).run()

    def wiki(self, relative):
        return (self.server_home / "wiki" / relative).read_text(encoding="utf-8")

    def ledger(self):
        return [json.loads(line) for line in self.wiki(".memex/ledger.jsonl").splitlines()]

    def state(self, name):
        return json.loads((self.server_home / "state" / name).read_text(encoding="utf-8"))

    def solitaire_session(self, **kwargs):
        repo = getattr(self, "_solitaire", None) or self.make_repo("solitaire", "git@github.com:me/solitaire.git")
        self._solitaire = repo
        return self.claude_session(repo, [
            ("user", LONG),
            ("assistant", "Let me look at the code."),
            ("tool", str(repo / "scores.py")),
            ("assistant", "DECISION: Server-side scores | Scores are stored on the server. | Leaderboard shared between players. @Tailscale\nDEAD_END: WebSocket | A WebSocket attempt was dropped. | Too heavy."),
        ], title="Score storage", **kwargs)

    def atlas_session(self, **kwargs):
        return self.claude_session(self.make_repo("atlas", "git@github.com:me/atlas.git"), [
            ("user", "A second project, for which we must decide which language to use."),
            ("assistant", "DECISION: Write it in Rust | The project is written in Rust. | For performance."),
        ], start="2026-11-13T09:00:00Z", **kwargs)

    # ── the nominal path ──────────────────────────────────────────────────
    def test_session_becomes_wiki_pages_and_comes_back(self):
        self.solitaire_session()
        self.host_sync()
        self.assertEqual(self.server_run(), 0)

        main = self.wiki("Projects/Solitaire/Solitaire.md")
        self.assertIn("type: project", main)
        self.assertIn("Server-side scores", main)
        self.assertIn("WebSocket", main)
        record = self.wiki(RECORD)
        self.assertIn("status: active", record)
        self.assertIn("claude-code_", record)            # provenance set by the code
        self.assertRegex(record, r"\nupdated: \d{4}-\d{2}-\d{2}\n")
        self.assertIn("Leaderboard shared", record)
        self.assertIn("# Wiki index", self.wiki("index.md"))
        self.assertIn("Solitaire", self.wiki("index.md"))
        self.assertIn("Server-side scores", self.wiki("index.md"))
        self.assertIn("] archivist | Score storage", self.wiki("log.md"))
        journal = self.wiki("Journal/2026/2026-11-12.md")
        self.assertIn("# Journal for 2026-11-12", journal)
        self.assertIn("[[Projects/Solitaire/Solitaire|Solitaire]] · claude-code on laptop — Test session.", journal)
        mapping = json.loads(self.wiki(".memex/projects.json"))
        self.assertEqual(mapping["github.com/me/solitaire"], "Projects/Solitaire")
        self.assertEqual(json.loads(self.wiki(".memex/wiki.json")), {"lang": "en"})

        # the bookkeeping uses the documented keys
        [entry] = self.ledger()
        self.assertEqual(set(entry), {"id", "file", "device", "source", "started", "processed", "status", "folder", "pages"})
        self.assertEqual((entry["status"], entry["folder"], entry["device"], entry["source"]),
                         ("integrated", "Projects/Solitaire", "laptop", "claude-code"))
        self.assertEqual(entry["pages"], [RECORD, "Projects/Solitaire/Solitaire.md"])
        [cache] = (self.server_home / "raw/extracts").glob("*.json")
        extracted = json.loads(cache.read_text(encoding="utf-8"))
        self.assertEqual(set(extracted), {"summary", "topic", "items"})
        self.assertEqual([item["kind"] for item in extracted["items"]], ["decision", "dead_end"])
        self.assertEqual(set(extracted["items"][0]), {"kind", "title", "content", "reason", "entities", "explicit"})

        # the agent was told its role and the wiki language, and got complete prompts
        calls = self.agent_calls()
        self.assertEqual([c["role"] for c in calls], ["extract", "integrate"])
        self.assertEqual({c["lang"] for c in calls}, {"en"})
        self.assertEqual([c["placeholders"] for c in calls], [[], []])

        # the vault receives the pages, with nothing hidden and nothing of git
        self.assertTrue((self.vault / "Projects/Solitaire/Solitaire.md").exists())
        self.assertFalse((self.vault / ".memex").exists())
        self.assertFalse((self.vault / ".git").exists())

        # the segment is archived, the inbox is empty, the repository is clean
        self.assertEqual(Server(self.config).pending(), [])
        self.assertEqual(len(list((self.server_home / "raw/archive").rglob("*.md"))), 1)
        self.assertEqual(git(self.server_home / "wiki", "status", "--porcelain"), "")
        self.assertEqual(git(self.server_home / "wiki", "worktree", "list").count("\n"), 0)
        log = git(self.server_home / "wiki", "log", "--format=%an|%s")
        self.assertIn("archivist|archivist: Score storage", log)

        # back on the host: the wiki is cloned and the hook injects the project's page
        self.host_sync()
        context = hook.project_context(self.config, str(self._solitaire))
        self.assertIn('# Memory of project "Solitaire" (from earlier sessions)', context)
        self.assertIn("Server-side scores", context)
        self.assertIn("detailed decision records: `Projects/Solitaire/Decisions/`", context)
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
        # a full resend (rebuild) creates no duplicate
        self.assertEqual(resend_all(self.config), 1)
        self.assertEqual(ship(self.config).duplicate, 1)
        self.assertEqual(Server(self.config).pending(), [])

    def test_session_continuation_gives_a_second_segment(self):
        session = self.solitaire_session()
        self.host_sync()
        self.server_run()
        self.claude_session(self._solitaire, [
            ("user", "Another important point: we must decide on the format of the saved games."),
            ("assistant", "DECISION: Saved games in JSON | Games are saved as JSON. | Readable and simple."),
        ], session=session, append=True)
        self.host_sync()
        self.server_run()
        names = sorted(p.name for p in (self.server_home / "raw/archive").rglob("*.md"))
        self.assertEqual(len(names), 2)
        self.assertTrue(names[1].endswith("_002.md"))
        self.assertIn("Saved games in JSON", self.wiki("Projects/Solitaire/Solitaire.md"))
        self.assertTrue((self.server_home / "wiki" / RECORD).exists())

    def test_short_and_empty_sessions_only_reach_the_journal(self):
        repo = self.make_repo("tool")
        self.claude_session(repo, [("user", "ok thanks"), ("assistant", "You are welcome.")], title="Thanks")
        self.claude_session(repo, [("user", "Please explain to me at length how this module works."),
                                   ("assistant", "Here is an explanation with nothing lasting in it.")], title="Explanation", start="2026-11-12T11:00:00Z")
        self.host_sync()
        self.assertEqual(self.server_run(), 0)
        journal = self.wiki("Journal/2026/2026-11-12.md")
        self.assertIn("short session: Thanks", journal)
        self.assertIn("Test session.", journal)
        self.assertFalse((self.server_home / "wiki/Projects/Tool").exists())
        self.assertEqual(len(self.agent_calls()), 1)   # the short session cost no call at all
        self.assertEqual([(e["status"], e["folder"], e["pages"]) for e in self.ledger()], [("short", "", []), ("nothing", "", [])])
        log = git(self.server_home / "wiki", "log", "--format=%an|%s")
        self.assertIn("memex|journal: Thanks (short)", log)
        self.assertIn("memex|journal: Explanation (nothing)", log)

    def test_session_without_project_goes_to_a_topic(self):
        self.codex_session(self.root, [
            ("user", "Let us talk about a savings plan: is it right for me, and where should I open one? TOPIC: Savings plan"),
            ("assistant", "Let me compare.", "commentary"),
            ("assistant", "DECISION: Open a savings plan with an online broker | The savings plan will be opened with an online broker. | Lower fees."),
        ])
        self.host_sync()
        self.server_run()
        topic = self.wiki("Topics/Savings plan/Savings plan.md")
        self.assertIn("type: topic", topic)
        self.assertIn("online broker", topic)
        self.assertIn("Savings plan", self.wiki("index.md"))

    def test_note_is_always_integrated(self):
        self.set(MEMEX_MIN_USER_CHARS="5000")              # a note is never too short to count
        write_note(self.config, "DECISION: Keep git | We keep git for the wiki. | To undo a whole run.", cwd=str(self.root))
        ship(self.config)
        self.server_run()
        archived = list((self.server_home / "raw/archive").rglob("*note*.md"))
        self.assertEqual(len(archived), 1)
        self.assertIn("Note to record in the wiki (explicit capture):", archived[0].read_text(encoding="utf-8"))
        self.assertTrue(any("Keep git" in p.read_text(encoding="utf-8") for p in (self.server_home / "wiki/Topics").rglob("*.md")))

    # ── the vault and human edits ─────────────────────────────────────────
    def test_human_edit_in_vault_is_imported_and_wins(self):
        self.solitaire_session()
        self.host_sync()
        self.server_run()
        page = self.vault / "Projects/Solitaire/Solitaire.md"
        page.write_text(page.read_text(encoding="utf-8") + "\nNote added by hand in Obsidian.\n", encoding="utf-8")
        (self.vault / "Topics/Personal").mkdir(parents=True)
        (self.vault / "Topics/Personal/Personal.md").write_text("---\ntype: topic\ntitle: Personal\nsummary: Written by hand.\n---\n\nMy page.\n", encoding="utf-8")
        (self.vault / "index.md").write_text("tampered", encoding="utf-8")
        self.server_run()
        self.assertIn("Note added by hand", self.wiki("Projects/Solitaire/Solitaire.md"))
        self.assertIn("Written by hand", self.wiki("index.md"))
        self.assertNotEqual((self.vault / "index.md").read_text(encoding="utf-8"), "tampered")
        log = git(self.server_home / "wiki", "log", "--format=%an %s")
        self.assertIn("human human: edits from Obsidian", log)
        # a deletion made by the human in the vault
        (self.vault / "Topics/Personal/Personal.md").unlink()
        self.server_run()
        self.assertFalse((self.server_home / "wiki/Topics/Personal/Personal.md").exists())

    # ── guardrails and retries ────────────────────────────────────────────
    def test_destructive_agent_is_rejected_then_retried_with_feedback(self):
        self.solitaire_session()
        self.host_sync()
        self.server_run()
        self.atlas_session()
        self.host_sync()
        os.environ["FAKE_AGENT_MODE"] = "delete_once"
        self.assertEqual(self.server_run(), 0)
        self.assertTrue((self.server_home / "wiki/Projects/Solitaire/Solitaire.md").exists())
        self.assertIn("Rust", self.wiki("Projects/Atlas/Atlas.md"))
        integrations = [c for c in self.agent_calls() if c["role"] == "integrate"]
        self.assertEqual(len(integrations), 3)   # 1 for solitaire, 2 for the second one (rejection, then retry)
        self.assertEqual([c["retry"] for c in integrations], [False, False, True])   # the retry carries the feedback

    def test_persistent_failure_ends_in_quarantine_without_touching_the_wiki(self):
        self.solitaire_session()
        self.host_sync()
        self.server_run()
        head = git(self.server_home / "wiki", "rev-parse", "main")
        self.claude_session(self._solitaire, [
            ("user", "One more decision to take on this project, about the visual theme."),
            ("assistant", "DECISION: Dark theme | The game uses a dark theme. | Easier to read."),
        ], start="2026-11-14T09:00:00Z")
        self.host_sync()
        os.environ["FAKE_AGENT_MODE"] = "delete"
        for _ in range(3):
            self.assertEqual(self.server_run(), 1)
        self.assertEqual(git(self.server_home / "wiki", "rev-parse", "main"), head)
        self.assertEqual(len(list((self.server_home / "raw/quarantine").rglob("*.md"))), 1)
        self.assertEqual(Server(self.config).pending(), [])
        notices = (self.server_home / "logs/notifications.log").read_text(encoding="utf-8")
        self.assertIn("quarantined — rejected by the guardrails: ", notices)
        self.assertIn("the archivist never deletes or renames a page", notices)
        self.assertEqual(Server(self.config).status()["quarantined"], 1)

    def test_reserved_files_cannot_be_changed_by_an_agent(self):
        os.environ["FAKE_AGENT_MODE"] = "outside"
        self.solitaire_session()
        self.host_sync()
        self.assertEqual(self.server_run(), 0)
        self.assertNotIn("overwritten", self.wiki("index.md"))
        self.assertFalse((self.server_home / "wiki/Loose notes.md").exists())
        self.assertNotIn("forged", self.wiki(".memex/ledger.jsonl"))
        self.assertIn("Server-side scores", self.wiki("Projects/Solitaire/Solitaire.md"))

    def test_existing_decision_is_immutable(self):
        self.solitaire_session()
        self.host_sync()
        self.server_run()
        before = self.wiki(RECORD)
        self.claude_session(self._solitaire, [
            ("user", "Another decision to take later on this same solitaire project."),
            ("assistant", "DECISION: Dark theme | The game uses a dark theme. | Easier to read."),
        ], start="2026-11-15T09:00:00Z")
        self.host_sync()
        os.environ["FAKE_AGENT_MODE"] = "edit_decision"
        self.assertEqual(self.server_run(), 1)
        after = self.wiki(RECORD)
        self.assertEqual(before, after)

    def test_forged_segment_header_cannot_write_outside_its_inbox(self):
        """A stolen host key must not be able to drop a file into the wiki through a forged header."""
        self.solitaire_session()
        Sweeper(self.config).run()
        forged = next(self.config.outbox.glob("*.md"))
        text = forged.read_text(encoding="utf-8")
        forged.write_text(text.replace("device: laptop", 'device: "../../wiki/Projects/Pwn"').replace("source: claude-code", 'source: "../../../wiki/x"'), encoding="utf-8")
        self.assertIn('device: "../../wiki/Projects/Pwn"', forged.read_text(encoding="utf-8"))
        self.assertIn('source: "../../../wiki/x"', forged.read_text(encoding="utf-8"))
        ship(self.config)
        self.server_run()
        home = self.server_home
        self.assertFalse((home / "wiki/Projects/Pwn").exists())
        stray = [p for p in home.rglob("*") if p.is_file() and p.relative_to(home).parts[0] not in ("raw", "wiki", "work", "state", "logs")]
        self.assertEqual(stray, [])
        archived = list((home / "raw/archive").rglob("*.md"))
        self.assertEqual([p.relative_to(home / "raw/archive").parts[0] for p in archived], ["laptop"])
        self.assertEqual(git(home / "wiki", "status", "--porcelain"), "")
        self.assertTrue(all(p.parent == home / "raw/extracts" for p in (home / "raw/extracts").rglob("*.json")))

    def test_internal_error_counts_as_a_failure_and_never_blocks_the_queue(self):
        from unittest import mock

        self.solitaire_session()
        self.atlas_session()
        self.host_sync()
        real = Server.integrate

        def flaky(server, seg, extracted):
            if seg.project == "solitaire":
                raise RuntimeError("simulated breakdown in the middle of the integration")
            return real(server, seg, extracted)

        with mock.patch.object(Server, "integrate", flaky):
            for _ in range(3):
                self.assertEqual(self.server_run(), 1)
        self.assertIn("Rust", self.wiki("Projects/Atlas/Atlas.md"))        # the next one went through all the same
        self.assertEqual(len(list((self.server_home / "raw/quarantine").rglob("*.md"))), 1)
        self.assertEqual(Server(self.config).pending(), [])
        self.assertEqual(git(self.server_home / "wiki", "status", "--porcelain"), "")
        notices = (self.server_home / "logs/notifications.log").read_text(encoding="utf-8")
        self.assertIn("internal error: simulated breakdown", notices)

    def test_agent_outage_never_quarantines_the_queue(self):
        """Quota reached, network down: no segment is penalised, however many runs it takes."""
        self.solitaire_session()
        self.claude_session(self.make_repo("atlas", "git@github.com:me/atlas.git"), [
            ("user", "A second project waits for its turn while the agent is out of service."),
            ("assistant", "DECISION: Language | The project is written in Rust. | Performance."),
        ], start="2026-11-13T09:00:00Z")
        self.host_sync()
        state = self.server_home / "state"
        os.environ["FAKE_AGENT_MODE"] = "fail"
        for _ in range(5):                                   # far more than MEMEX_MAX_RETRIES
            self.assertEqual(self.server_run(), 1)
        self.assertEqual(len(Server(self.config).pending()), 2)
        self.assertEqual(list((self.server_home / "raw/quarantine").rglob("*.md")), [])
        self.assertEqual(Server(self.config).status()["failures"], {})   # no failure counted
        self.assertEqual(set(self.state("agent-down.json")), {"since", "error"})
        self.assertEqual([c["role"] for c in self.agent_calls()], ["extract", "probe"] * 5)   # per run: one try and the probe, then stop
        notices = (self.server_home / "logs/notifications.log").read_text(encoding="utf-8")
        self.assertEqual(notices.count("agent unavailable"), 1)   # told once, not on every run
        self.assertIn("unavailable, run stopped, nothing lost", notices)
        self.assertEqual(Server(self.config).status()["agent_unavailable"], self.state("agent-down.json"))
        os.environ["FAKE_AGENT_MODE"] = "ok"
        self.assertEqual(self.server_run(), 0)
        self.assertEqual(Server(self.config).pending(), [])
        self.assertFalse((state / "agent-down.json").exists())
        self.assertEqual(Server(self.config).status()["agent_unavailable"], "no")
        self.assertIn("Server-side scores", self.wiki("Projects/Solitaire/Solitaire.md"))
        self.assertIn("Rust", self.wiki("Projects/Atlas/Atlas.md"))

    def test_failure_tied_to_one_segment_still_ends_in_quarantine(self):
        """The agent answers the probe but fails on this segment: here, failures do count."""
        self.solitaire_session()
        self.host_sync()
        os.environ["FAKE_AGENT_MODE"] = "fail_work"
        self.assertEqual(self.server_run(), 1)
        [failure] = self.state("failures.json").values()
        self.assertEqual(set(failure), {"count", "error", "date"})
        self.assertEqual(failure["count"], 1)
        self.assertIn("extraction: the agent failed (exit 1)", failure["error"])
        for _ in range(2):
            self.assertEqual(self.server_run(), 1)
        self.assertEqual(Server(self.config).pending(), [])
        self.assertEqual(len(list((self.server_home / "raw/quarantine").rglob("*.md"))), 1)
        self.assertFalse((self.server_home / "state/agent-down.json").exists())
        self.assertEqual(self.state("failures.json"), {})

    def test_agent_that_writes_nothing_is_a_failure_not_a_silent_skip(self):
        self.solitaire_session()
        self.host_sync()
        os.environ["FAKE_AGENT_MODE"] = "silent"
        self.assertEqual(self.server_run(), 1)
        self.assertEqual(len(Server(self.config).pending()), 1)          # the session is not filed as "nothing to record"
        self.assertEqual(Server(self.config).ledger_ids(), set())
        os.environ["FAKE_AGENT_MODE"] = "ok"
        self.assertEqual(self.server_run(), 0)
        self.assertIn("Server-side scores", self.wiki("Projects/Solitaire/Solitaire.md"))

    def test_reply_that_is_not_json_counts_against_the_segment(self):
        self.solitaire_session()
        self.host_sync()
        os.environ["FAKE_AGENT_MODE"] = "garbage"
        self.assertEqual(self.server_run(), 1)
        [failure] = self.state("failures.json").values()
        self.assertIn("the agent's reply is not a JSON object", failure["error"])
        self.assertFalse((self.server_home / "state/agent-down.json").exists())   # the agent itself still answers
        self.assertEqual(len(Server(self.config).pending()), 1)
        notices = (self.server_home / "logs/notifications.log").read_text(encoding="utf-8")
        self.assertIn("memex: 0 session(s) integrated, 1 failed\n", notices)
        self.assertIn("Score storage: failed, will retry on the next run — extraction: ", notices)
        os.environ["FAKE_AGENT_MODE"] = "ok"
        self.assertEqual(self.server_run(), 0)
        self.assertEqual(self.state("failures.json"), {})
        self.assertIn("Server-side scores", self.wiki("Projects/Solitaire/Solitaire.md"))

    # ── weaver ────────────────────────────────────────────────────────────
    def two_projects_sharing_an_entity(self):
        self.solitaire_session()
        self.claude_session(self.make_repo("atlas", "git@github.com:me/atlas.git"), [
            ("user", "For the deployment of Atlas, we must decide how to reach the server."),
            ("assistant", "DECISION: Access over a private network | The server is reached over a private network, as for Solitaire. | No public port. @Tailscale"),
        ], start="2026-11-13T09:00:00Z")
        self.host_sync()
        self.server_run()

    def test_weaver_links_pages_and_creates_concepts(self):
        self.two_projects_sharing_an_entity()
        concept = self.wiki("Concepts/Tailscale.md")
        self.assertIn("type: concept", concept)
        self.assertRegex(concept, r"\nupdated: \d{4}-\d{2}-\d{2}\n")
        atlas = self.wiki("Projects/Atlas/Atlas.md")
        self.assertIn("[[Projects/Solitaire/Solitaire|Solitaire]]", atlas)
        self.assertIn("Tailscale", self.wiki("index.md"))
        self.assertIn("weaver", git(self.server_home / "wiki", "log", "--format=%an"))
        self.assertRegex(git(self.server_home / "wiki", "log", "--format=%an|%s"), r"weaver\|weaver: \d+ page\(s\) linked")
        self.assertRegex(self.wiki("log.md"), r"\] weaver \| \d+ page\(s\) linked\n")
        calls = self.agent_calls()
        self.assertEqual([c["role"] for c in calls], ["extract", "integrate", "extract", "integrate", "weave"])
        self.assertEqual([c["placeholders"] for c in calls], [[]] * 5)
        self.assertEqual(set(self.state("weaver.json")), {"digest", "date", "dismissed"})

    def test_dismissed_concept_is_not_proposed_again_until_it_spreads(self):
        os.environ["FAKE_AGENT_MODE"] = "no_concepts"            # the weaver reads, then dismisses the candidate
        self.two_projects_sharing_an_entity()
        state = self.state("weaver.json")
        self.assertEqual(state["dismissed"]["tailscale"]["folders"], 2)
        self.assertEqual(set(state["dismissed"]["tailscale"]), {"entity", "folders", "date"})
        self.assertFalse((self.server_home / "wiki/Concepts/Tailscale.md").exists())
        os.environ["FAKE_AGENT_MODE"] = "ok"
        calls = len(self.agent_calls())
        self.assertEqual(Server(self.config).run_weaver(force=True), "")
        self.assertEqual(len(self.agent_calls()), calls)         # nothing to offer: the agent is not called
        self.claude_session(self.make_repo("beacon", "git@github.com:me/beacon.git"), [
            ("user", "A third project must also choose how to reach its server."),
            ("assistant", "DECISION: Private network too | This project goes through the same private network. | Consistency. @Tailscale"),
        ], start="2026-11-14T09:00:00Z")
        self.host_sync()
        self.server_run()                                        # three folders instead of two: offered again
        self.assertTrue((self.server_home / "wiki/Concepts/Tailscale.md").exists())
        self.assertNotIn("tailscale", self.state("weaver.json")["dismissed"])

    def test_weaver_cannot_rewrite_text(self):
        os.environ["FAKE_AGENT_MODE"] = "rewrite"
        self.two_projects_sharing_an_entity()
        self.assertNotIn("Sentence added by the weaver", self.wiki("Projects/Atlas/Atlas.md"))
        self.assertFalse((self.server_home / "wiki/Concepts/Tailscale.md").exists())
        weaving = [c for c in self.agent_calls() if c["role"] == "weave"]
        self.assertEqual([c["retry"] for c in weaving], [False, True])    # rejected, retried with the feedback, rejected again

    def test_weaver_obeys_its_settings(self):
        self.set(MEMEX_WEAVER="0")
        self.two_projects_sharing_an_entity()
        self.assertNotIn("weaver", git(self.server_home / "wiki", "log", "--format=%an"))
        self.set(MEMEX_WEAVER="1", MEMEX_WEAVER_MIN_PAGES="50")
        self.assertEqual(Server(self.config).run_weaver(), "")              # too small a wiki to be worth a run
        self.set(MEMEX_WEAVER_MIN_PAGES="1", MEMEX_WEAVER_MIN_FOLDERS="3")
        self.assertEqual(Server(self.config).run_weaver(), "1 page(s) linked")
        self.assertFalse((self.server_home / "wiki/Concepts/Tailscale.md").exists())   # shared by two folders only
        self.set(MEMEX_WEAVER_MIN_FOLDERS="2")
        self.assertEqual(Server(self.config).run_weaver(), "1 page(s) linked, 1 concept(s) created")
        self.assertTrue((self.server_home / "wiki/Concepts/Tailscale.md").exists())

    # ── curator and review ────────────────────────────────────────────────
    def seed_for_review(self):
        self.solitaire_session()
        self.host_sync()
        self.server_run()
        for name, body in (("Duplicate", "Copy."), ("Long", "Text.\nRepetition.\nRepetition.")):
            target = self.vault / f"Topics/{name}/{name}.md"
            target.parent.mkdir(parents=True)
            target.write_text(f"---\ntype: topic\ntitle: {name}\nsummary: Page {name}.\n---\n\n{body}\n", encoding="utf-8")
        self.server_run()

    def test_review_is_proposed_then_partially_approved(self):
        self.seed_for_review()
        reviews = Reviews(Server(self.config))
        self.assertEqual(reviews.run(), 0)
        review = reviews.pending()
        self.assertEqual([a["kind"] for a in review["actions"]], ["delete", "condense"])
        self.assertEqual(len(review["questions"]), 1)
        self.assertEqual(set(review), {"id", "created", "base", "branch", "status", "actions", "questions"})
        self.assertEqual(set(review["actions"][0]), {
            "kind", "title", "pages", "reason", "instruction", "n", "commit", "status", "summary", "modified", "deleted"})
        self.assertEqual([a["status"] for a in review["actions"]], ["pending", "pending"])
        self.assertEqual([a["summary"] for a in review["actions"]], ["Test action applied."] * 2)     # read from the agent's result file
        self.assertEqual((review["actions"][0]["deleted"], review["actions"][1]["modified"]),
                         (["Topics/Duplicate/Duplicate.md"], ["Topics/Long/Long.md"]))
        self.assertEqual(review["questions"], [{"title": "Manual or scripted deployment?", "detail": "Two pages contradict each other."}])
        self.assertEqual(set(self.state("curator.json")), {"head", "date"})
        self.assertEqual(Server(self.config).status()["pending_review"], [review["id"]])
        shown = reviews.format(review)
        self.assertIn(f"Review {review['id']} — pending", shown)
        self.assertIn("1. [delete] Delete Duplicate.md\n   Why: Page entirely covered elsewhere.\n   Pages deleted: Topics/Duplicate/Duplicate.md", shown)
        self.assertIn("? To decide: Manual or scripted deployment?", shown)
        self.assertIn('Answer "ok", "ok except 2" or "no".', shown)
        # nothing has changed as long as you have not answered
        self.assertTrue((self.server_home / "wiki/Topics/Duplicate/Duplicate.md").exists())
        self.assertTrue((self.vault / "Topics/Duplicate/Duplicate.md").exists())
        self.assertEqual(reviews.run(), 0)   # no second review while the first one is waiting
        self.assertEqual(len(reviews.all()), 1)

        message = reviews.decide(skip=[2])
        self.assertIn("1 action(s) applied, 1 rejected", message)
        self.assertFalse((self.server_home / "wiki/Topics/Duplicate/Duplicate.md").exists())
        self.assertFalse((self.vault / "Topics/Duplicate/Duplicate.md").exists())
        self.assertIn("Repetition.", self.wiki("Topics/Long/Long.md"))
        [refusal] = [json.loads(line) for line in self.wiki(".memex/refusals.jsonl").splitlines()]
        self.assertEqual(set(refusal), {"date", "kind", "title", "pages"})
        self.assertEqual((refusal["kind"], refusal["title"]), ("condense", "Condense Long.md"))
        self.assertIn(f"] curator | review {review['id']}: 1 action(s) applied\n- delete: Delete Duplicate.md\n", self.wiki("log.md"))
        self.assertIn("page deleted: `Topics/Duplicate/Duplicate.md`", self.wiki("log.md"))
        self.assertIsNone(reviews.pending())
        decided = reviews.get(review["id"])
        self.assertEqual((decided["status"], [a["status"] for a in decided["actions"]]), ("applied", ["applied", "rejected"]))
        self.assertIn("decided", decided)
        self.assertEqual(reviews.decide(), "No pending review.")

        # the deleted page can be found again in the history
        self.assertIn("restored", restore(Server(self.config), "Topics/Duplicate/Duplicate.md"))
        self.assertTrue((self.vault / "Topics/Duplicate/Duplicate.md").exists())
        self.assertIn("] restore | Topics/Duplicate/Duplicate.md\n- restored from `", self.wiki("log.md"))

    def test_review_reject_changes_nothing(self):
        self.seed_for_review()
        reviews = Reviews(Server(self.config))
        reviews.run()
        self.assertIn("0 action(s) applied, 2 rejected", reviews.decide(reject_all=True))
        self.assertTrue((self.server_home / "wiki/Topics/Duplicate/Duplicate.md").exists())
        self.assertEqual(self.wiki(".memex/refusals.jsonl").count("\n"), 2)
        self.assertEqual(reviews.get(reviews.all()[0]["id"])["status"], "rejected")

    # ── backup and rebuild ────────────────────────────────────────────────
    def test_backup_then_rebuild_on_an_empty_server(self):
        self.two_projects_sharing_an_entity()
        archive = serverops.backup(self.config)
        expected = self.wiki("Projects/Solitaire/Solitaire.md")
        commits = git(self.server_home / "wiki", "rev-list", "--count", "main")
        new_home = self.root / "new-server"
        self.set(MEMEX_HOME=str(new_home), MEMEX_VAULT_DIR=str(self.root / "new-vault"))
        self.assertIn("restored", serverops.restore_backup(self.config, archive))
        self.assertEqual((new_home / "wiki/Projects/Solitaire/Solitaire.md").read_text(encoding="utf-8"), expected)
        self.assertEqual(git(new_home / "wiki", "rev-list", "--count", "main"), commits)
        self.assertEqual(Server(self.config).run(), 0)
        self.assertTrue((self.root / "new-vault/Projects/Solitaire/Solitaire.md").exists())
        # the host plugs into the new server without losing anything
        self.assertEqual(pull(self.config).error, "")
        self.assertTrue((self.data / "wiki/Projects/Atlas/Atlas.md").exists())

    def test_rebuild_from_hosts_alone(self):
        """Server lost without a backup: the hosts send their segments again and the wiki is rebuilt."""
        self.two_projects_sharing_an_entity()
        self.set(MEMEX_HOME=str(self.root / "fresh-server"), MEMEX_VAULT_DIR="")
        self.assertEqual(resend_all(self.config), 2)
        self.assertEqual(ship(self.config).sent, 2)
        self.assertEqual(Server(self.config).run(), 0)
        self.assertTrue((self.root / "fresh-server/wiki/Projects/Solitaire/Solitaire.md").exists())
        self.assertTrue((self.root / "fresh-server/wiki/Projects/Atlas/Atlas.md").exists())

    def test_offline_host_keeps_segments_until_a_server_exists(self):
        self.set(MEMEX_SERVER_SSH="")
        self.solitaire_session()
        Sweeper(self.config).run()
        self.assertEqual(ship(self.config).pending, 1)
        self.set(MEMEX_SERVER_SSH="local")
        self.assertEqual(ship(self.config).sent, 1)


class EndToEndSandboxed(Sandbox):
    """The nominal path, with the agents really locked inside bubblewrap."""

    overrides = {"MEMEX_SANDBOX": "bwrap"}

    def test_pipeline_works_inside_the_sandbox(self):
        from memex.agent import Agent

        if "active" not in Agent(self.config).sandbox_state():
            self.skipTest("bubblewrap unavailable here")
        repo = self.make_repo("solitaire", "git@github.com:me/solitaire.git")
        self.claude_session(repo, [
            ("user", LONG),
            ("assistant", "DECISION: Server-side scores | Scores are stored on the server. | Leaderboard shared."),
        ])
        self.assertEqual(sync(self.config, quiet=True), 0)
        self.assertEqual(Server(self.config).run(), 0)
        self.assertIn("Server-side scores", (self.server_home / "wiki/Projects/Solitaire/Solitaire.md").read_text(encoding="utf-8"))
        self.assertTrue((self.vault / RECORD).exists())


if __name__ == "__main__":
    unittest.main()
