---
name: memex
description: "The user's shared memory (memex wiki): answer a curator review, look up what was decided, restore a page, record a note, report the state of the system."
version: 1.0.0
author: memex
platforms: [linux]
metadata:
  hermes:
    tags: [memex, wiki, knowledge-base, obsidian, memory]
---

# memex — the shared memory

memex turns the user's sessions with their agents (Claude Code, Codex, you) into a markdown wiki.
Three automatic agents maintain it: the archivist, the weaver and the curator.
You are the point of contact: you answer questions about this wiki and you pass the user's
decisions on to the system, with the `memex` command.

The wiki is in `$HOME/wiki` (by default `/srv/memex/wiki`). You may **read** it freely.
You must **never write to it directly**: every change goes through the commands below.

## When to use this skill

| The user… | What you do |
|---|---|
| answers a review: "ok", "yes", "ok except 2", "no", "only 1" | `memex server review …` (see below) |
| asks what the curator is proposing | `memex server review show` |
| asks why something was decided, or where a project or a topic stands | you read the wiki |
| says "note that…", "remember that…", "note this decision" | `memex note "…"` |
| asks to bring back a deleted page | `memex server restore "Path/To/The page.md"` |
| asks whether everything is fine, or what is pending | `memex server status` |
| asks to start a run now | `memex server run`, `memex server curate --force` |

## Answering a review

The curator proposes a numbered list of actions. Nothing is applied without an answer.

```bash
memex server review show                 # reread the pending review
memex server review approve              # "ok": apply everything
memex server review approve --except 2   # "ok except 2"
memex server review approve --only 1,3   # "only 1 and 3"
memex server review reject               # "no": reject everything
```

- If the answer is ambiguous, ask for clarification before running anything.
- A rejected action is never proposed again. So never reject on the user's behalf.
- After the command, report in one sentence what it replied.

When the review contains a question for the user to settle, ask it as it stands. The user's answer
is recorded with `memex note`, restating both the question and the answer.

## Reading the wiki

1. Start with `$HOME/wiki/index.md`: it lists the projects, the topics and the concepts.
2. Then search the files by keyword, and follow the `[[…]]` links.
3. A project's living page is `Projects/<Name>/<Name>.md`; the detail of a choice and the reasons
   for it are in `Projects/<Name>/Decisions/`. Non-code topics are in `Topics/`. A wiki written in
   another language uses its own folder names: `index.md` shows the actual paths.

Answer from what is written, citing the page. If the wiki says nothing, say so: do not make anything up.

## Recording a note

```bash
memex note "Decision: the retirement account will be opened with an online broker, for the lower fees."
```

Write the note so that it stands on its own, with the reason. The archivist will integrate it in the
right place on its next run; do not choose the location yourself.

This conversation is itself captured by memex afterwards: a decision clearly stated here will be
recorded even without `memex note`. The command is there so that you do not have to wait.

## What not to do

- Modify, create or delete a file under `$HOME/wiki`, `$HOME/raw` or `$HOME/state`.
- Use `git` in `$HOME/wiki`.
- Approve or reject a review without an explicit answer from the user.
