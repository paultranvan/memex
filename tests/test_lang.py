"""The language layer: which language a wiki gets, the two prompt sets, the strings, the command line."""
import contextlib
import io
import json
import os
import re
import string
import subprocess
import sys
import unittest
from unittest import mock

from helpers import ROOT, Sandbox, git, write_pages

from memex import __version__, cli, frontmatter, i18n
from memex.agent import Agent, AgentResult
from memex.pipeline import ITEM_KINDS, RESULT_FILE, Server
from memex.review import ACTION_KINDS, Reviews
from memex.segment import Message, Segment
from memex.util import now

PACKAGE = ROOT / "src" / "memex"
# What each prompt is given by the code.
PLACEHOLDERS = {
    "extract": {"meta", "transcript"},
    "integrate": {"conventions", "meta", "folder", "items", "index", "max_chars"},
    "weave": {"conventions", "report", "index"},
    "curate-diagnose": {"conventions", "report", "changed", "index", "refused", "max_actions"},
    "curate-apply": {"conventions", "action"},
}


def template(lang, name):
    return (PACKAGE / "prompts" / lang / f"{name}.md").read_text(encoding="utf-8")


def example(lang, name):
    """The JSON object a prompt shows as the expected reply or result: its last `json` block."""
    return json.loads(re.findall(r"```json\n(.*?)\n```", template(lang, name), re.S)[-1])


def fields(text):
    return sorted({field for _literal, field, _spec, _conversion in string.Formatter().parse(text) if field})


class Detection(Sandbox):
    init_server = False

    def test_record_first_then_folders_then_memex_lang(self):
        root = self.root / "some-wiki"
        self.assertEqual(i18n.detect(root), "en")                       # nothing there, nothing asked for
        self.assertEqual(i18n.detect(root, "fr"), "fr")                 # a wiki still to be created takes MEMEX_LANG
        (root / "Projets").mkdir(parents=True)
        self.assertEqual(i18n.detect(root, "en"), "fr")                 # French folders, whatever MEMEX_LANG says
        i18n.record(root, "en")
        self.assertEqual(json.loads((root / ".memex/wiki.json").read_text()), {"lang": "en"})
        self.assertEqual(i18n.detect(root, "fr"), "en")                 # what the wiki recorded wins over everything
        (root / ".memex/wiki.json").write_text("{ not json")
        self.assertEqual(i18n.detect(root, "en"), "fr")                 # unreadable record: the folders again
        self.assertEqual(i18n.use_for(root, "en").lang, "fr")
        self.assertEqual(i18n.current().lang, "fr")
        topics_only = self.root / "topics-only"
        (topics_only / "Sujets").mkdir(parents=True)
        self.assertEqual(i18n.detect(topics_only, "en"), "fr")
        english = self.root / "english"
        (english / "Topics").mkdir(parents=True)
        self.assertEqual(i18n.detect(english, "fr"), "en")
        with self.assertRaises(i18n.UnsupportedLanguage):
            i18n.detect(self.root / "nothing-yet", "de")
        with self.assertRaises(i18n.UnsupportedLanguage):
            i18n.record(root, "de")

    def test_existing_french_wiki_without_a_record_is_french_even_when_memex_lang_says_english(self):
        (self.server_home / "wiki/Projets").mkdir(parents=True)
        self.set(MEMEX_LANG="en")
        self.assertEqual(Server(self.config).locale.lang, "fr")
        self.assertEqual(i18n.current().lang, "fr")

    def test_recorded_language_wins_over_memex_lang(self):
        self.assertTrue(Server(self.config).init())                     # MEMEX_LANG left alone: English
        wiki = self.server_home / "wiki"
        self.assertEqual(json.loads((wiki / ".memex/wiki.json").read_text()), {"lang": "en"})
        self.set(MEMEX_LANG="fr")
        server = Server(self.config)
        self.assertEqual(server.locale.lang, "en")
        self.assertFalse(server.init())
        self.assertEqual((server.locale.lang, i18n.current().lang), ("en", "en"))
        self.assertEqual(json.loads((wiki / ".memex/wiki.json").read_text()), {"lang": "en"})

    def test_new_wiki_takes_memex_lang(self):
        for lang in ("fr", "en"):
            with self.subTest(lang=lang):
                home = self.root / f"server-{lang}"
                self.set(MEMEX_HOME=str(home), MEMEX_LANG=lang)
                server = Server(self.config)
                self.assertTrue(server.init())
                loc = i18n.LOCALES[lang]
                self.assertIs(server.locale, loc)
                wiki = home / "wiki"
                self.assertEqual(json.loads((wiki / ".memex/wiki.json").read_text()), {"lang": lang})
                self.assertEqual((wiki / "log.md").read_text(encoding="utf-8"), loc.t("log.header"))
                self.assertTrue((wiki / "index.md").read_text(encoding="utf-8").startswith(loc.t("index.header")))
                self.assertEqual((wiki / "AGENTS.md").read_bytes(), (PACKAGE / "wiki_template" / lang / "AGENTS.md").read_bytes())
                # exactly the files the code creates, and commits in English whatever the language
                self.assertEqual(git(wiki, "ls-files").splitlines(), [
                    ".gitignore", ".memex/ledger.jsonl", ".memex/projects.json", ".memex/refusals.jsonl", ".memex/wiki.json",
                    "AGENTS.md", "index.md", "log.md"])
                self.assertEqual(git(wiki, "log", "--format=%an|%s").splitlines(), ["memex|initial index", "memex|wiki initialised"])
                self.assertEqual((wiki / ".memex/projects.json").read_text(), "{}\n")
        self.assertNotEqual(i18n.FR.t("log.header"), i18n.EN.t("log.header"))

    def test_unsupported_language_stops_the_command_with_one_line(self):
        self.set(MEMEX_LANG="de")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--config", str(self.env_file), "server", "init"])
        self.assertEqual(code, 2)
        self.assertEqual(err.getvalue(), "memex: unsupported wiki language 'de' (MEMEX_LANG): choose one of en, fr\n")
        self.assertEqual(out.getvalue(), "")
        self.assertFalse((self.server_home / "wiki").exists())
        # the same through the real entry point: an exit status and a message, no traceback
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        done = subprocess.run([sys.executable, "-m", "memex", "server", "status"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, done.stdout), (2, ""))
        self.assertEqual(done.stderr, "memex: unsupported wiki language 'de' (MEMEX_LANG): choose one of en, fr\n")


class Prompts(Sandbox):
    init_server = False

    def server_in(self, lang):
        self.set(MEMEX_HOME=str(self.root / f"server-{lang}"), MEMEX_LANG=lang, MEMEX_VAULT_DIR="")
        server = Server(self.config)
        server.init()
        self.assertEqual(i18n.current().lang, lang)
        return server

    def test_both_prompt_sets_are_complete_and_fully_substituted(self):
        self.assertEqual(sorted(p.name for p in (PACKAGE / "prompts").iterdir()), ["en", "fr"])     # no prompt left outside a language
        self.assertEqual(sorted(p.name for p in (PACKAGE / "wiki_template").iterdir()), ["en", "fr"])
        for lang in sorted(i18n.LOCALES):
            server = self.server_in(lang)
            self.assertEqual(sorted(p.name for p in (PACKAGE / "prompts" / lang).iterdir()), sorted(f"{name}.md" for name in PLACEHOLDERS))
            self.assertEqual([p.name for p in (PACKAGE / "wiki_template" / lang).iterdir()], ["AGENTS.md"])
            for name, expected in PLACEHOLDERS.items():
                with self.subTest(lang=lang, prompt=name):
                    self.assertEqual(set(re.findall(r"\{\{(\w+)\}\}", template(lang, name))), expected)
                    text = server.prompt(name, **{field: f"<the {field}>" for field in expected})
                    self.assertEqual(re.findall(r"\{\{.*?\}\}", text), [])
                    for field in expected:
                        self.assertIn(f"<the {field}>", text)
        self.assertNotEqual(template("en", "extract"), template("fr", "extract"))

    def test_custom_prompts_are_looked_up_by_language_first(self):
        server = self.server_in("fr")
        custom = self.root / "my-prompts"
        write_pages(custom, {"extract.md": "for every language: {{meta}}"})
        self.set(MEMEX_PROMPTS_DIR=str(custom))
        server = Server(self.config)
        self.assertEqual(server.prompt("extract", meta="M"), "for every language: M")
        write_pages(custom, {"fr/extract.md": "for a French wiki: {{meta}}", "en/extract.md": "for an English wiki: {{meta}}"})
        self.assertEqual(server.prompt("extract", meta="M"), "for a French wiki: M")
        self.assertEqual(server.prompt("curate-apply"), template("fr", "curate-apply"))        # not customised: the packaged one

    def test_reply_formats_shown_in_the_prompts_are_what_the_code_reads(self):
        for lang in sorted(i18n.LOCALES):
            server = self.server_in(lang)
            loc = i18n.current()

            with self.subTest(lang=lang, protocol="extraction reply"):
                shown = example(lang, "extract")
                self.assertEqual(list(shown), ["summary", "topic", "items"])
                self.assertEqual(set(re.findall(r"(?m)^- `(\w+)`", template(lang, "extract"))), ITEM_KINDS)
                seg = Segment("claude-code", "laptop", "s", 1, [Message("user", "x")], now(), now())
                reply = AgentResult("```json\n" + json.dumps(shown, ensure_ascii=False) + "\n```", 0, 0.0)
                with mock.patch.object(Agent, "run", return_value=reply) as run:
                    extracted = server.extract(seg)
                self.assertEqual(run.call_args.kwargs["role"], "extract")
                self.assertEqual((extracted["summary"], extracted["topic"]), (shown["summary"], shown["topic"][:80]))
                [item], [wanted] = extracted["items"], shown["items"]
                self.assertEqual(item, {**wanted, "entities": [entity[:60] for entity in wanted["entities"]]})
                self.assertEqual(set(item), {"kind", "title", "content", "reason", "entities", "explicit"})

            with self.subTest(lang=lang, protocol="curator diagnosis"):
                shown = example(lang, "curate-diagnose")
                self.assertEqual(list(shown), ["actions", "questions"])
                [action], [question] = shown["actions"], shown["questions"]
                self.assertEqual(list(action), ["kind", "title", "pages", "reason", "instruction"])
                self.assertEqual(list(question), ["title", "detail"])
                kinds = action["kind"].split(" | ")
                self.assertEqual(set(kinds), ACTION_KINDS)
                self.assertEqual(set(re.findall(r"(?m)^- `(\w+)`", template(lang, "curate-diagnose"))), ACTION_KINDS)
                page = f"{loc.topics}/A/A.md"
                write_pages(server.repo.dir, {page: f"---\ntype: {loc.type('topic')}\n{loc.k_title}: A\n{loc.k_summary}: A.\n---\n\nText.\n"})
                server.repo.commit_all(server.repo.dir, "a page", "human")
                plan = {"actions": [{**action, "kind": kind, "pages": [page]} for kind in kinds], "questions": shown["questions"]}
                roles = []

                def run(kind, prompt, cwd, label="", timeout=None, role=""):
                    roles.append(role)
                    return AgentResult(json.dumps(plan, ensure_ascii=False) if role == "diagnose" else "", 0, 0.0)

                reviews = Reviews(server)
                with mock.patch.object(Agent, "run", side_effect=run), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(reviews.run(), 0)
                self.assertEqual(roles, ["diagnose"] + ["apply"] * len(kinds))
                [review] = reviews.all()
                self.assertEqual([a["kind"] for a in review["actions"]], kinds)
                for stored in review["actions"]:
                    self.assertEqual([stored[key] for key in ("title", "pages", "reason", "instruction")],
                                     [action["title"], [page], action["reason"], action["instruction"]])
                self.assertEqual(review["questions"], shown["questions"])

            with self.subTest(lang=lang, protocol="result files"):
                self.assertIn(f"`{RESULT_FILE}`", template(lang, "integrate"))
                self.assertIn(f"`{RESULT_FILE}`", template(lang, "curate-apply"))
                shown = example(lang, "integrate")
                self.assertEqual(list(shown), ["folder", "summary", "skipped"])
                self.assertIn(f"{loc.projects}/", shown["folder"])
                self.assertIn(f"{loc.topics}/", shown["folder"])
                self.assertEqual(list(example(lang, "curate-apply")), ["done", "summary"])

            with self.subTest(lang=lang, protocol="report given to the weaver"):
                self.assertEqual(re.findall(r"(?m)^- `(\w+)`", template(lang, "weave")), ["unlinked_mentions", "concept_candidates", "broken_links"])

    def test_prompts_and_conventions_use_the_names_of_the_wiki_s_language(self):
        for lang, loc in sorted(i18n.LOCALES.items()):
            expected = {
                "integrate": [
                    f"/{loc.decisions}/`", f"`{loc.k_status}: {loc.status('superseded')}`", f"`{loc.k_superseded_by}`",
                    f"(`type`, `{loc.k_title}`, `{loc.k_summary}`, `{loc.k_entities}`)", f"`type: {loc.type('synthesis')}`",
                    f"`{loc.topics}/", f"`{loc.concepts}/`", f"`{loc.journal}/`",
                ],
                "weave": [
                    f"`{loc.concepts}/<", f"`type: {loc.type('concept')}`", f"`{loc.k_title}`", f"`{loc.k_summary}`", "`aliases`",
                    f"`## {loc.see_also}`", f"`{loc.decisions}/`", f"`{loc.journal}/`",
                ],
                "curate-diagnose": [f"`{loc.decisions}/`", f"`{loc.journal}/`"],
                "curate-apply": [f"`{loc.k_summary}`", f"`{loc.decisions}/`", f"`{loc.journal}/`"],
            }
            for name, snippets in expected.items():
                for snippet in snippets:
                    with self.subTest(lang=lang, prompt=name, snippet=snippet):
                        self.assertIn(snippet, template(lang, name))
            conventions = (PACKAGE / "wiki_template" / lang / "AGENTS.md").read_text(encoding="utf-8")
            meta, body = frontmatter.parse(conventions)
            self.assertEqual((meta["type"], bool(meta[loc.k_title])), (loc.type("meta"), True))
            types = " | ".join(loc.type(name) for name in ("project", "topic", "decision", "concept", "synthesis"))
            for snippet in (
                f"`{loc.projects}/<", f"`{loc.topics}/<", f"/{loc.decisions}/<", f"`{loc.concepts}/<", f"`{loc.journal}/`",
                f"\ntype: {types}\n{loc.k_title}: ", f"\n{loc.k_summary}: ", f"\n{loc.k_entities}:\n",
                f"`{loc.k_status}`", f"`{loc.status('active')}`", f"`{loc.status('superseded')}`", f"`{loc.status('abandoned')}`",
                f"`{loc.k_supersedes}` / `{loc.k_superseded_by}`", "`sources`", f"`{loc.k_updated}`",
            ):
                with self.subTest(lang=lang, conventions=snippet):
                    self.assertIn(snippet, body)


class Strings(unittest.TestCase):
    def setUp(self):
        self.addCleanup(i18n.use, "en")

    def test_english_schema_is_the_documented_one(self):
        en = i18n.EN
        self.assertEqual((en.projects, en.topics, en.concepts, en.journal, en.decisions), ("Projects", "Topics", "Concepts", "Journal", "Decisions"))
        self.assertEqual(en.content_dirs, ("Projects", "Topics", "Concepts"))
        self.assertEqual(
            (i18n.K_TYPE, en.k_title, en.k_summary, en.k_entities, i18n.K_ALIASES, i18n.K_DATE, en.k_status, en.k_supersedes,
             en.k_superseded_by, i18n.K_SOURCES, en.k_updated),
            ("type", "title", "summary", "entities", "aliases", "date", "status", "supersedes", "superseded_by", "sources", "updated"))
        self.assertEqual(list(en.types.values()), ["project", "topic", "decision", "concept", "synthesis", "journal", "meta"])
        self.assertEqual(list(en.statuses.values()), ["active", "superseded", "abandoned"])
        self.assertEqual(en.see_also, "See also")
        self.assertEqual(i18n.DEFAULT, "en")

    def test_both_languages_define_the_same_strings_with_the_same_fields(self):
        en, fr = i18n.EN, i18n.FR
        self.assertEqual(i18n.LOCALES, {"en": en, "fr": fr})
        self.assertEqual(sorted(en.strings), sorted(fr.strings))
        for key in en.strings:
            self.assertEqual(fields(en.strings[key]), fields(fr.strings[key]), key)
        for table in ("types", "statuses", "roles", "kinds"):
            self.assertEqual(list(getattr(en, table)), list(getattr(fr, table)), table)
        self.assertEqual(set(en.kinds), ACTION_KINDS)
        self.assertEqual(set(en.roles), {"archivist", "weaver", "curator", "restore"})

    def test_every_string_used_in_the_source_exists(self):
        patterns = {
            "strings": r"\.t\(\s*[\"']([a-z_.]+)[\"']", "types": r"\.type\(\s*[\"'](\w+)[\"']",
            "statuses": r"\.status\(\s*[\"'](\w+)[\"']", "roles": r"\broles\[\s*[\"'](\w+)[\"']\s*\]|\brole=[\"'](\w+)[\"']",
        }
        used = set()
        for path in sorted(PACKAGE.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for table, pattern in patterns.items():
                for found in re.findall(pattern, text):
                    name = found if isinstance(found, str) else next(part for part in found if part)
                    if table == "roles" and name in ("extract", "integrate", "weave", "diagnose", "apply", "probe"):
                        continue                       # `role=` is also how the pipeline names the agent's task
                    used.add((table, name, path.name))
        self.assertGreater(len({name for table, name, _ in used if table == "strings"}), 30)      # the scan does see the calls
        self.assertEqual({name for table, name, _ in used if table == "roles"}, {"archivist", "weaver", "curator", "restore"})
        for lang, loc in i18n.LOCALES.items():
            for table, name, where in sorted(used):
                self.assertIn(name, getattr(loc, table), f"{lang}: {table}[{name!r}], used in {where}")

    def test_language_lookup(self):
        self.assertIs(i18n.get("fr"), i18n.FR)
        self.assertIs(i18n.get(" FR "), i18n.FR)
        self.assertIs(i18n.get(""), i18n.EN)
        with self.assertRaises(i18n.UnsupportedLanguage) as caught:
            i18n.get("de")
        self.assertIsInstance(caught.exception, ValueError)
        self.assertEqual(str(caught.exception), "unsupported wiki language 'de' (MEMEX_LANG): choose one of en, fr")
        self.assertIs(i18n.use("fr"), i18n.current())
        self.assertEqual(i18n.current().t("journal.title", day="2026-11-12"), "Journal du 2026-11-12")
        self.assertEqual(i18n.use("en").t("journal.title", day="2026-11-12"), "Journal for 2026-11-12")


class PackageDirectory(unittest.TestCase):
    def test_cli_runs_from_inside_the_package_directory(self):
        """Started from `src/memex`, that directory comes first on the path: a module there must not hide a standard one."""
        package = ROOT / "src" / "memex"
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "MEMEX_CONFIG": os.devnull}
        done = subprocess.run([sys.executable, "-m", "memex", "--version"], cwd=package, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, f"memex {__version__}\n", ""))
        ours = {p.stem for p in package.glob("*.py")} | {p.name for p in package.iterdir() if p.is_dir()}
        standard = set(getattr(sys, "stdlib_module_names", ()))                # Python 3.10 and later
        self.assertEqual({name for name in ours & standard if not name.startswith("__")}, set())


if __name__ == "__main__":
    unittest.main()
