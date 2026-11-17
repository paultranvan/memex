---
type: meta
title: Wiki conventions
---

# Wiki conventions

This wiki is the lasting memory of my sessions with agents (Claude Code, Codex, Hermes…).
It is written by agents, and read by them and by me in Obsidian. This file sets its rules.
I may edit it by hand; agents may not.

## What it is for

- Finding out where a project or a topic stands, and **why**.
- Not reopening a question that has already been settled, or going back down a dead end.

It is neither an exhaustive journal nor a copy of the code: what can be read directly in a project's
repository does not belong here.

## Structure

| Location | Content |
|---|---|
| `Projects/<Name>/<Name>.md` | Living page of a code project |
| `Topics/<Name>/<Name>.md` | Living page of a non-code topic (for example "Retirement savings") |
| `<folder>/Decisions/<YYYY-MM-DD> <title>.md` | Decision records |
| `<folder>/<Other page>.md` | Supplementary page (comparison, synthesis) when the living page gets too long |
| `Concepts/<Name>.md` | Cross-cutting pages: a tool, a technique or a theme shared by several folders |
| `Journal/`, `index.md`, `log.md` | Kept automatically: do not write to them |

A folder is always a directory that bears the name of its living page.

## Page header

```yaml
type: project | topic | decision | concept | synthesis
title: Readable title
summary: One sentence, copied as is into the index.
entities:
  - Lasting tools, services, techniques or themes that another folder might mention (a dozen at most)
```

A decision record adds `date` (YYYY-MM-DD), `status` (`active`, `superseded` or
`abandoned`) and, where relevant, `supersedes` / `superseded_by` (a link, in quotes).
The fields `sources` (the last five sessions that touched the page) and `updated` are filled in
automatically. The full provenance is in `.memex/ledger.jsonl` and in the git history.

## Living page

It says what is true **today**. It is rewritten when reality changes; history does not pile up
in it. Recommended sections, to be left out if they would be empty:

- `## In brief` — the goal and the current state, in three to six lines.
- `## Key points` — for a project: architecture and structural choices; for a topic: where things
  stand. Each structural choice points to its decision record.
- `## Decisions` — the list of records, most recent first: link, then one line.
- `## Dead ends` — what was tried or considered and then abandoned, with the reason.
- `## Known problems` — the non-obvious pitfalls, with their cause and their solution.
- `## Open questions` — what remains to be settled or done.

## Decision record

A record describes a choice that shapes what comes next. It is **never rewritten**. If the decision
changes, a new record is created; the old one stays, with `status: superseded` and `superseded_by`.

```markdown
## Context
## Options considered
## Decision
## Why
## Consequences
```

A small choice with nothing at stake does not deserve a record: one line in the living page is enough.

## Where to file what has no project

- A working preference that applies everywhere → `Topics/Working preferences/`.
- A fact about my machines, my tools or my services → `Topics/Environment/`.
- Everything else → the closest existing topic; failing that, a new topic with a short, stable name.

## Writing

- In English, in full sentences, in a factual tone. Never "the user", never "in this session": the
  reader did not see the session.
- Concise: each piece of information in one place only. Before adding, check whether it is already
  written, and add to it rather than repeat it.
- Absolute dates (`2026-11-12`), never "yesterday" or "last week".
- No volatile detail: no commit identifier, no line number, no temporary branch name.
- Simple file names: no `:`, `/`, `?`, `*`, `"`, `<`, `>`, `|`, `#` (some devices reject them).
- Obsidian links `[[Folder/Page|label]]` to the pages mentioned, only once per page for a given
  target.
- Never a secret (password, key, token) or sensitive personal data.
- What is not known is not written: no extrapolation.

## The three roles

- **The archivist** integrates what the sessions bring. It adds and updates; it deletes nothing.
- **The weaver** links: links between pages, pages in `Concepts/`. It does not change the text of other pages.
- **The curator** tightens: duplicates, stale content, overlong pages. Its changes are applied only
  after my approval.
