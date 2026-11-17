#!/usr/bin/env python3
"""Deterministic fake agent for the tests: it plays every role of the pipeline without an LLM.

The pipeline says what it expects through the environment: MEMEX_ROLE (extract,
integrate, weave, diagnose, apply or probe) and MEMEX_LANG (the wiki language).
The prompt is read from $MEMEX_PROMPT_FILE and the work happens in the current
directory. Nothing here is tied to one language: folder names and header keys
come from `memex.i18n`, and the places where the prompt is searched come from
the prompt templates (`prompts/<lang>/`) and from the i18n strings.

FAKE_AGENT_MODE simulates the misbehaviours the guardrails must stop:
fail, fail_work, garbage, silent, delete, delete_once, edit_decision, outside,
rewrite, no_concepts. FAKE_AGENT_CALLS names a file that gets one JSON line per call.

In the test transcripts, a line `DECISION: title | content | reason` (or FACT:,
DEAD_END:) declares an item to extract, `@Name` an entity, and `TOPIC: name`
the theme of a session that has no project.
"""
import json
import os
import re
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from memex import i18n  # noqa: E402
from memex.agent import PROBE_TOKEN  # noqa: E402

ROLE = os.environ.get("MEMEX_ROLE", "")
MODE = os.environ.get("FAKE_AGENT_MODE", "ok")
PROMPTS = Path(i18n.__file__).resolve().parent / "prompts"
RETRY = {"integrate": "retry.archivist", "weave": "retry.weaver"}
prompt = Path(os.environ["MEMEX_PROMPT_FILE"]).read_text(encoding="utf-8")
loc = i18n.get(os.environ["MEMEX_LANG"])


def pattern(key: str, **fields: str) -> str:
    """The i18n string `key` as a regular expression: each `{field}` becomes a group of that name."""
    out, seen = "", set()
    for literal, field, _spec, _conversion in string.Formatter().parse(loc.strings[key]):
        out += re.escape(literal)
        if field:
            out += f"(?P={field})" if field in seen else f"(?P<{field}>{fields[field]})"
            seen.add(field)
    return out


def section(name: str, placeholder: str) -> str:
    """What the prompt holds under the heading where the template `name` puts `{{placeholder}}`."""
    template = (PROMPTS / loc.lang / f"{name}.md").read_text(encoding="utf-8")
    before = template.split("{{" + placeholder + "}}", 1)[0]
    heading = [line for line in before.splitlines() if line.startswith("## ")][-1]
    return re.split(r"(?m)^## ", prompt.split(f"\n{heading}\n", 1)[1], maxsplit=1)[0]


def json_block(name: str, placeholder: str):
    return json.loads(re.search(r"```json\n(.*?)\n```", section(name, placeholder), re.S).group(1))


def page(kind: str, title: str, summary: str, body: str, extra: str = "") -> str:
    head = f"{i18n.K_TYPE}: {loc.type(kind)}\n{loc.k_title}: {title}\n{loc.k_summary}: {summary}\n{extra}"
    return f"---\n{head}---\n\n{body}\n"


# Whether the prompt carries the feedback of a first attempt rejected by the guardrails.
retried = ROLE in RETRY and re.search(pattern(RETRY[ROLE], feedback="(?s:.+?)"), prompt) is not None
calls = os.environ.get("FAKE_AGENT_CALLS")
if calls:
    with open(calls, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "role": ROLE, "lang": loc.lang, "retry": retried, "placeholders": re.findall(r"\{\{\w+\}\}", prompt),
        }) + "\n")

if MODE == "fail":           # agent out of service: even the probe fails
    sys.exit(1)
if ROLE == "probe":
    expected = f"Reply with this single word and nothing else: {PROBE_TOKEN}"
    print(PROBE_TOKEN if prompt.splitlines()[-1] == expected else "this is not the probe I know")
    sys.exit(0)
if MODE == "fail_work":      # the agent answers the probe but fails on the work it is given
    sys.exit(1)


def extract() -> None:
    if MODE == "garbage":
        print("Sorry, I cannot answer that.")
        return
    transcript = prompt.split("<segment>", 1)[1].rsplit("</segment>", 1)[0]
    items = []
    for line in transcript.splitlines():
        match = re.match(r"^(DECISION|FACT|DEAD_END): (.*)$", line.strip())
        if match:
            parts = [p.strip() for p in match.group(2).split("|")] + ["", ""]
            items.append({
                "kind": match.group(1).lower(), "title": parts[0], "content": parts[1] or parts[0],
                "reason": parts[2], "entities": re.findall(r"@(\w+)", line), "explicit": False,
            })
    topic = re.search(r"TOPIC: (.*)", transcript)
    print("Here is the result:\n```json\n" + json.dumps({
        "summary": "Test session.", "topic": topic.group(1).strip() if topic else "", "items": items,
    }, ensure_ascii=False) + "\n```")


def target_folder() -> str:
    where = section("integrate", "folder")
    for key in ("where.new", "where.known"):
        match = re.search(pattern(key, folder="[^`]+", name="[^`/]+"), where)
        if match:
            return match.group("folder")
    hint = re.search(pattern("where.hint", topic=".+?"), where)
    return f"{loc.topics}/" + (hint.group("topic").strip() if hint else "Misc")


def integrate() -> None:
    if MODE == "silent":         # the agent hands back without writing anything, not even its report
        return
    items = json_block("integrate", "items")
    existing = sorted(p for p in Path(".").rglob("*.md") if p.parts[0] in (loc.projects, loc.topics))
    folder = target_folder()
    name = folder.split("/")[-1]
    stamp = pattern("meta.date", value=r"(?P<day>\d{4}-\d{2}-\d{2}) \d{2}:\d{2}")
    date = re.search(f"(?m)^{stamp}$", section("integrate", "meta")).group("day")
    main = Path(folder) / f"{name}.md"
    main.parent.mkdir(parents=True, exist_ok=True)
    if not main.exists():
        kind = "project" if folder.split("/")[0] == loc.projects else "topic"
        main.write_text(page(kind, name, f"Page of {name}.", f"# {name}\n\n## In brief\n\nTest folder."), encoding="utf-8")
    text = main.read_text(encoding="utf-8")
    for item in items:
        if item["kind"] == "decision":
            record = Path(folder) / loc.decisions / f"{date} {item['title']}.md"
            record.parent.mkdir(parents=True, exist_ok=True)
            entities = "".join(f"  - {e}\n" for e in item["entities"])
            record.write_text(page(
                "decision", item["title"], item["content"],
                f"## Decision\n\n{item['content']}\n\n## Why\n\n{item['reason']}",
                f"{i18n.K_DATE}: {date}\n{loc.k_status}: {loc.status('active')}\n"
                + (f"{loc.k_entities}:\n{entities}" if entities else ""),
            ), encoding="utf-8")
            link = f"[[{folder}/{loc.decisions}/{date} {item['title']}|{item['title']}]]"
            text += f"\n## {loc.decisions}\n\n- {link} — {item['content']}\n"
        else:
            text += f"\n- {item['title']}: {item['content']}\n"
    if any(item["entities"] for item in items) and f"{loc.k_entities}:" not in text:
        entities = sorted({e for item in items for e in item["entities"]})
        text = text.replace("---\n\n", f"{loc.k_entities}:\n" + "".join(f"  - {e}\n" for e in entities) + "---\n\n", 1)
    main.write_text(text, encoding="utf-8")
    if MODE == "delete_once" and not retried or MODE == "delete":
        victims = [p for p in existing if p != main and p.exists()]
        (victims[0] if victims else main).unlink()
    if MODE == "edit_decision":
        for record in Path(".").rglob(f"{loc.decisions}/*.md"):
            if date not in record.name:
                record.write_text(record.read_text(encoding="utf-8") + "\nForbidden rewrite.\n", encoding="utf-8")
    if MODE == "outside":
        Path("index.md").write_text("overwritten", encoding="utf-8")
        Path("Loose notes.md").write_text("outside any folder", encoding="utf-8")
        Path(".memex/ledger.jsonl").write_text("forged\n", encoding="utf-8")
    Path(".memex-result.json").write_text(json.dumps({"folder": folder, "summary": "Test integration."}), encoding="utf-8")


def weave() -> None:
    report = json_block("weave", "report")
    assert set(report) == {"unlinked_mentions", "concept_candidates", "broken_links"}, sorted(report)
    for item in report["unlinked_mentions"]:
        target = Path(item["page"])
        text = target.read_text(encoding="utf-8")
        head, body = text.split("---\n\n", 1) if text.startswith("---") else ("", text)
        link = f"[[{item['target'][:-3]}|{item['term']}]]"
        body = re.sub(r"(?<![\w\[|])" + re.escape(item["term"]) + r"(?![\w\]|])", lambda _m: link, body, count=1)
        if MODE == "rewrite":
            body += "\nSentence added by the weaver.\n"
        target.write_text((head + "---\n\n" if head else "") + body, encoding="utf-8")
    for item in report["concept_candidates"] if MODE != "no_concepts" else []:
        links = "\n".join(f"- [[{p[:-3]}]]" for p in item["pages"])
        Path(loc.concepts).mkdir(exist_ok=True)
        Path(loc.concepts, f"{item['entity']}.md").write_text(
            page("concept", item["entity"], f"Notion shared by {len(item['folders'])} folders.", f"# {item['entity']}\n\n{links}"),
            encoding="utf-8",
        )


def diagnose() -> None:
    report = json_block("curate-diagnose", "report")
    assert set(report) == {"problems", "orphans", "stats"}, sorted(report)
    actions = []
    for candidate in sorted(Path(".").rglob("*Duplicate*.md")):
        actions.append({
            "kind": "delete", "title": f"Delete {candidate.name}", "pages": [candidate.as_posix()],
            "reason": "Page entirely covered elsewhere.", "instruction": f"Delete {candidate.as_posix()}.",
        })
    for candidate in sorted(Path(".").rglob("*Long*.md")):
        actions.append({
            "kind": "condense", "title": f"Condense {candidate.name}", "pages": [candidate.as_posix()],
            "reason": "Repetitive page.", "instruction": "Remove the repetitions.",
        })
    questions = [{"title": "Manual or scripted deployment?", "detail": "Two pages contradict each other."}]
    print(json.dumps({"actions": actions, "questions": questions}, ensure_ascii=False))


def apply() -> None:
    action = json_block("curate-apply", "action")
    for name in action["pages"]:
        target = Path(name)
        if action["kind"] == "delete":
            target.unlink()
        else:
            text = target.read_text(encoding="utf-8")
            target.write_text(text.replace("\nRepetition.", "") + "\nCondensed.\n", encoding="utf-8")
    Path(".memex-result.json").write_text(json.dumps({"done": True, "summary": "Test action applied."}), encoding="utf-8")


ROLES = {"extract": extract, "integrate": integrate, "weave": weave, "diagnose": diagnose, "apply": apply}
if ROLE not in ROLES:
    sys.exit(3)
ROLES[ROLE]()
