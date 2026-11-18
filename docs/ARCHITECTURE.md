# memex — architecture and decisions

memex turns your sessions with coding agents (Claude Code, Codex, Hermes) into a structured
markdown wiki, readable in Obsidian and consulted by those same agents. This document describes
the system as it is built in this repository, then lists every decision with its reason and what
was ruled out.

- [1. Overview](#1-overview)
- [2. The path of a piece of information](#2-the-path-of-a-piece-of-information)
- [3. Host side](#3-host-side)
- [4. Server side](#4-server-side)
- [5. The wiki](#5-the-wiki)
- [6. The three agents](#6-the-three-agents)
- [7. Security](#7-security)
- [8. Rebuilding](#8-rebuilding)
- [9. Decision register](#9-decision-register)
- [10. What is verified and what is not](#10-what-is-verified-and-what-is-not)
- [11. Known limits and possible next steps](#11-known-limits-and-possible-next-steps)

## 1. Overview

```
 WORK MACHINES (hosts)                          SERVER (VPS, always on)
┌──────────────────────────────┐              ┌──────────────────────────────────────┐
│ Claude Code, Codex           │              │ SSH gate (memex-gate)                │
│   │ local transcripts        │   segments   │   │                                  │
│   ▼                          │ ───────────► │   ▼                                  │
│ sweeper (no LLM)             │     SSH      │ raw/inbox ──► archivist ──► weaver   │
│   redacts secrets            │              │                 │                    │
│                              │              │                 ▼                    │
│ wiki copy (read-only)        │ ◄─────────── │ wiki (git repository) ◄── curator    │
│   ▲            │             │  git fetch   │   │        ▲           (on approval) │
│   │            ▼             │              │   ▼        │                         │
│ session-start hook: project  │              │ vault ◄──► LiveSync ◄──► Obsidian    │
│ page enters the context      │              │                                      │
└──────────────────────────────┘              │ Hermes: writes, notifies, converses  │
                                              └──────────────────────────────────────┘
```

Two rules hold the whole thing together:

- **Connections always start from the host.** The server never contacts a work machine.
- **The model writes, the code keeps the books.** An agent writes nothing but pages, in a
  disposable copy. The index, the journal, provenance, commits and checks are deterministic.

## 2. The path of a piece of information

1. You work with Claude Code or Codex. The session is written to a local transcript.
2. At the end of the session (Claude Code's `SessionEnd` hook), or after ten minutes of
   inactivity, the **sweeper** extracts a **segment** from it: your messages and the agent's final
   answers, without tool output, with secrets redacted.
3. The segment goes to the server over SSH. Its arrival wakes the pipeline.
4. The **archivist** extracts what is durable (decisions, dead ends, problems, facts, preferences,
   state), then integrates it into the wiki in a working copy. The result is checked, committed,
   merged.
5. The **weaver** links the pages that were touched and maintains the cross-cutting pages.
6. The wiki is exported to the vault folder; LiveSync pushes it to your devices.
7. The next time a session starts in that project, the **session-start hook** injects the
   project's page into the agent's context, followed by the list of the pages linked with it.

A few minutes pass between the end of a session and the update of the wiki.

## 3. Host side

A single program, `memex`, in Python with no dependencies. Installed by `host/install.sh`.

### The sweeper

| Source | Location read | What is kept |
|---|---|---|
| Claude Code | `~/.claude/projects/<project>/<session>.jsonl` | Human turns (`origin.kind = human`), the agent's text |
| Codex | `~/.codex/sessions/**/rollout-*.jsonl` | `item_completed` events: `UserMessage`, `AgentMessage` |
| Hermes | `~/.hermes/state.db` (SQLite, read-only) | `user` and `assistant` messages |

- **Incremental.** One cursor per session (bytes read, or last message). Measured on the author's
  machine: a second sweep over the whole history takes ten milliseconds.
- **Immutable segments.** Each sweep of a session produces a new, numbered file that contains only
  what came after the previous one. A segment is never rewritten: it can therefore be sent,
  archived and replayed without ambiguity.
- **What is left out.** Tool output, internal reasoning, sub-agents, messages injected by the
  harness (system reminders, compaction summaries, notifications), and short intermediate
  narration ("let me look at the file…"). Final answers are always kept.
- **Redaction.** API keys, tokens, passwords in a URL or an assignment, private keys, IBANs, card
  numbers. By known patterns: better to redact a little too much.
- **Exclusions.** By path, by remote repository, by a `.memex-ignore` file, or by writing
  `#memex-off` in a session.
- **Attachment to a project.** By the remote URL of the git repository. A worktree, even one that
  has since been deleted, resolves to the same project.

### The link

`memex sync` sweeps, ships, then updates the local copy of the wiki. It is started by:

- Claude Code's `SessionEnd` hook, for the session that has just ended;
- the `SessionStart` hook (Claude Code and Codex), in the background, without making the session
  wait;
- a systemd user timer, every ten minutes.

Without a network, segments wait in `outbox/`. Once sent, they are kept in `sent/`: that is the
backup from which the server can be rebuilt.

### How the agents read the wiki

- **A pointer** in `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`: where the wiki is, when to
  consult it, never to write to it.
- **An injection at start-up**: if the session opens in a project the wiki knows, its living page
  is printed by the hook and enters the context (8,000 characters at most; a living page within
  its limit of 5,000 is printed whole). Under it, the hook
  lists the pages elsewhere in the wiki that are linked with the project, in either direction:
  their path and their one-line summary, not their content. The agent reads the ones it needs.
- **A read rule** in Claude Code's permissions, for the wiki folder.

The agents on the hosts only read. What they learn comes back through the segments. To record
something without waiting: `memex note "…"`.

## 4. Server side

Installed by `server/install.sh`, under a dedicated system account, `memex`, whose home directory
is `MEMEX_HOME` (`/srv/memex` by default).

```
/srv/memex/
  raw/inbox/<device>/       segments received, waiting to be processed
  raw/archive/<device>/     segments processed (the sources the pages cite)
  raw/quarantine/           segments that failed repeatedly
  raw/extracts/             result of the extraction, kept so it is not done again
  wiki/                     git repository: the source of truth for the wiki
  work/                     the agents' disposable working copies
  vault/                    folder synchronised by LiveSync (optional)
  state/  logs/  backups/
/opt/memex/app/             the program, owned by root
/etc/memex/memex.env        the configuration
```

### The gate

Each machine has its own SSH key, registered with a forced command:

```
restrict,command="/usr/local/bin/memex-gate --forced --device laptop" ssh-ed25519 AAAA…
```

That key allows only three things: dropping segments into the inbox of **its own** device, cloning
the wiki read-only, fetching the latest backup. No shell, no other repository, no writing to the
wiki. The device name comes from the key, never from what the client says.

### The pipeline

`memex server run` is started by systemd: when a segment arrives (`memex-run.path`), and every
fifteen minutes as a safety net (`memex-run.timer`). A lock guarantees a single writer.

For each segment, in chronological order:

1. **Triage without a model.** A session in which you wrote fewer than 80 characters costs no
   call: one line in the journal.
2. **Extraction.** A call without tools, which returns JSON: a summary and a list of typed items.
   Empty list: one line in the journal, and that is all.
3. **Integration.** A call that can read and write files, in a git working copy created for the
   occasion. The agent receives the conventions, the items, the list of pages and the folder to
   file them in.
4. **Check.** The diff is checked against the scope of the role (see §7). If it is rejected, a
   second attempt receives the list of faults. After three failed runs, the segment goes to
   quarantine and you are notified. If it is the agent itself that no longer answers (quota
   reached, network down), the run stops without counting a failure: see D39.
5. **Bookkeeping.** The code adds the source and the date to the front matter of the pages
   touched, writes the journal line and the ledger of processed segments, regenerates the index,
   completes `log.md`.
6. **Commit and merge.** One commit per segment, fast-forward merged into `main`.
7. **Export** to the vault.

Then the weaver runs, if anything changed.

### The agent harness

The pipeline depends on no particular harness: it writes a prompt to a file and runs a shell
command. Three presets (`MEMEX_AGENT=hermes|claude|codex`), three permission levels:

| Level | Used for | Hermes | Claude Code | Codex |
|---|---|---|---|---|
| `extract` | Extraction | `-t todo -z` | `-p --tools ""` | `exec --sandbox read-only` |
| `read` | The curator's diagnosis | `-t file -z` | `-p --tools "Read,Glob,Grep"` | `exec --sandbox read-only` |
| `edit` | Integration, weaver, actions | `-t file --yolo -z` | `-p --tools "Read,Write,Edit,Glob,Grep" --permission-mode acceptEdits` | `exec --sandbox workspace-write` |

Any other command will do (`MEMEX_AGENT_*_CMD`).

### The vault

`vault/` is an ordinary folder, with nothing from git and nothing hidden in it. A manifest keeps
the hash of each file at the last exchange, which tells who changed what:

- the vault differs from the manifest: you edited it in Obsidian; the change is imported and
  committed under the `human` author;
- the wiki differs from the vault: an agent changed it; the change is exported;
- both changed: your version wins.

If the manifest is lost, the wiki is authoritative: an old vault cannot overwrite it.

### Hermes

Hermes has three roles, all replaceable:

- **writer**: it is the default harness for the agents;
- **messenger**: `MEMEX_NOTIFY_CMD='hermes send --to telegram …'`;
- **interlocutor**: a skill (`server/hermes/skills/memex`) teaches it to answer reviews
  ("ok except 2"), to read the wiki, to restore a page, to record a note.

Its own conversations (messaging) are captured on the server like any other session.

## 5. The wiki

```
Projects/<Name>/<Name>.md                     living page of a code project
Projects/<Name>/Decisions/<date> <title>.md   decision records
Topics/<Name>/<Name>.md                       living page of a topic outside code
Concepts/<Name>.md                            cross-cutting pages, kept by the weaver
Journal/<year>/<date>.md                      one line per session               (code)
index.md   log.md                             catalogue, operations log          (code)
AGENTS.md                                     the conventions (yours alone to edit)
.memex/                                       ledger, project → folder mapping, refusals, language
```

- **Living page.** It says what is true today: in brief, the broad outline, decisions, dead ends,
  known problems, open questions. It is rewritten; nothing is piled onto it.
- **Decision record.** Context, options, decision, why, consequences. Never rewritten. A reversal
  creates a new record; the old one changes to `status: superseded`.
- **Provenance.** Each page carries in its front matter the list of segments that fed it.
- **Conventions.** `AGENTS.md` is injected into every prompt. Changing it changes the behaviour of
  the three agents.
- **Language.** The layout above is that of an English wiki. A wiki is written in one language
  (`en` by default, `fr` available), recorded in `.memex/wiki.json`. The language sets the folder
  names, the front-matter keys and values, and the generated text: where an English wiki has
  `Projects/Solitaire/Solitaire.md`, a French one has `Projets/Solitaire/Solitaire.md`. See D41.

## 6. The three agents

| | Archivist | Weaver | Curator |
|---|---|---|---|
| Role | Integrates what the sessions bring | Links the pages, maintains `Concepts/` | Tightens (duplicates, stale content, pages over their limit) and checks the links |
| When | On every segment | After the archivist, if there are leads | Every week |
| Risk | Additive | Additive | Destructive |
| Applied | Directly | Directly | **After your approval** |
| May | Add, complete | Wrap in `[[…]]`, add a "See also" section, write concept pages | Modify, merge, delete |
| May not | Delete, rename, touch an existing record, write in `Concepts/` | Change a word of the text, touch a record | Touch a decision record |

**The weaver** starts from a check without a model: page titles mentioned without a link, entities
mentioned in at least two folders without a page of their own, broken links. The model settles the
homonyms and writes the cross-cutting page. It does not wake up while the wiki has fewer than
twelve pages, nor if the leads are the same as on the previous run.

**Page sizes.** A living page is loaded whole at the start of every session, so it has a limit of
its own (`MEMEX_LIVING_PAGE_CHARS`, 5,000 characters); every other page has a higher one
(`MEMEX_MAX_PAGE_CHARS`, 12,000). At 80 % of its limit a page is flagged to the archivist, which
then puts new detail in a supplementary page of the folder, by theme, and leaves one line with the
link. Beyond the limit the check without a model reports the page, and the curator proposes to
condense it the same way. The curator is told the size and the limit of each page it condenses,
and gets one more pass if a living page is still over its limit. See D43.

**The curator** has two jobs. It tightens: duplicates, stale content, pages over their limit. And
it checks the links of a limited number of folders on each run (`MEMEX_CURATOR_LINK_FOLDERS`, 10),
first the ones due for a check (never checked, or changed since the last run), then the ones
checked longest ago, so that every folder comes round: a link that leads to the wrong page, two
pages on the same matter that ignore each other, a broken link whose target can be identified. Each folder is a call of its own, in which the agent reads the folder's
pages and the pages they are linked with, says what it found, and may propose one `link` action,
in addition to the actions of the diagnosis. Only the folders it reports on count as checked, and
a folder that the same review already reworks waits for the next run. A `link` action may change
links and the "See also" section, and the guardrails reject it if a single other word differs.
A folder due for a check that the limit leaves out is remembered and comes first on the next run,
and the review carries a warning, which is notified with it: the limit may be too low. The same
goes for a folder the agent failed on. See D45.
See D44.

It makes a read-only diagnosis, then applies each action in a separate commit, on a
review branch. You receive the list; you answer "ok", "ok except 2" or "no". Approving carries the
chosen commits over to `main`. A rejected action is recorded and is not proposed again. A review
left unanswered expires after ten days. A deleted page stays in the history:
`memex server restore "Path/Page.md"`.

## 7. Security

| Threat | Countermeasure |
|---|---|
| A secret leaves in a segment | Redaction on the host, before anything is sent. Files in mode 600 |
| A session from your job is captured | Exclusions by path and by repository; `.memex-ignore`; `#memex-off` |
| A host key is stolen | It gives the gate and nothing else: dropping segments, reading the wiki |
| A forged segment targets another folder | The device is imposed by the inbox; no field of the header forms a path |
| The server is compromised | It cannot open any connection to your machines (one-way, Tailscale rules) |
| A segment contains a malicious instruction | Extraction has no tools. Integration writes only in its working copy. The diff is checked |
| An agent writes somewhere other than intended | Per-call sandbox: read-only system, `~/.ssh` hidden, writes limited to the working copy and the harness's own state |
| An agent adds an SSH key for itself | `authorized_keys` belongs to root |
| An agent modifies the pipeline | The code is in `/opt/memex`, owned by root |
| An agent reaches the other services on the server | Account without sudo; hardened systemd unit; `--deny /var/www` makes those folders invisible |
| An agent destroys content | Per-role guardrails on the diff; git history; daily backups |
| The server reads your personal notes | Separate vault for the wiki, with its own passphrase |

What stays out of reach of these countermeasures: an agent can **read** everything the `memex`
account can read (segments, wiki, its own configuration) and has the network to reach its model. A
successful injection could therefore get wiki content out. That is the reason not to put secrets
in the wiki, and to keep your personal notes in another vault.

## 8. Rebuilding

Nothing exists in a single copy:

| Data | Where else it is |
|---|---|
| The program | This repository, on every machine where it is cloned |
| The wiki and its history | Full clone on every host; daily backup |
| The segments | `sent/` on the host that produced them; daily backup |
| The backups | Pulled every day by the hosts |

Three paths, described step by step in the [runbook](RUNBOOK.md):

- **New work machine**: clone this repository, `host/install.sh --server …`, authorise the key.
- **New server, with a backup**: `server/deploy.sh`, then `memex server restore-backup`.
- **New server, with nothing**: `server/deploy.sh`, then `memex resend && memex ship` on each
  host. The wiki is rebuilt from the segments.

No path, machine name or address is written in the code: everything is in `memex.env`.

## 9. Decision register

Each entry: the decision, why, and what was ruled out.

### Foundations

**D1 — A wiki maintained by agents, on the model of Karpathy's
["LLM Wiki"](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).**
Three layers: immutable raw sources, a wiki written by the model, a conventions file.
*Why:* knowledge is compiled once and kept up to date, instead of being rediscovered at every
question. *Ruled out:* a vector database queried on demand.

**D2 — The archive and the wiki are two different things.**
Every session becomes segments; the wiki retains only a small part of them.
*Why:* most sessions contain nothing durable, and a wiki that grows with every session stops
being read.

**D3 — Markdown, read in Obsidian; no database.**
*Ruled out:* claude-mem (SQLite and a vector database per machine, unreadable in Obsidian);
basic-memory (writes only when the agent decides to take a note); llmwiki as a whole (generic
structure, designed for a single machine).

**D4 — Python, standard library only.**
*Why:* it installs anywhere there is Python 3.9 and git, with no virtual environment and no
package.

### Capture

**D5 — A sweeper without a model on each machine, rather than an agent at the end of each session.**
*Why:* the same mechanism for every harness, interrupted sessions are caught up, the existing
history is backfilled, no cost. The hooks only serve to trigger it sooner.

**D6 — Immutable segments, one cursor per session.**
*Why:* a file that is never modified can be transported, deduplicated and replayed without risk.
*Ruled out:* one segment per session, rewritten on every sweep, which forced the server to work
out what had changed.

**D7 — Keep your messages and the final answers; leave out tools and narration.**
*Measured on the author's machine:* 181 MB of transcripts give 660 KB of segments.

**D8 — Redact secrets on the machine, by patterns.**
*Why:* nothing sensitive must leave the host, whatever happens afterwards.
*Ruled out:* an external tool (gitleaks) as a mandatory dependency.

**D9 — Explicit exclusions; no session from your job by default in the example.**
By path, by repository, by marker file, by marker in the session.

**D10 — The identity of a project is its remote repository.**
*Why:* stable from one machine to the next and across worktrees. Falls back to the folder name.

**D11 — `--no-backfill` at installation.**
*Why:* backfilling the whole history costs one call per session; on a new machine, you may want to
capture only what comes next.

### Transport and server

**D12 — The server is the centre; no GitHub.**
*Why:* the wiki contains personal conversations; on a server of your own, it stays in your hands.
The clones on the hosts serve as backups. *Ruled out:* a private GitHub repository as the hub,
and scheduled tasks in the cloud.

**D13 — One-way: the host calls, the server never calls back.**
*Why:* an exposed server must not have a path to your workstation.
*Ruled out:* letting Hermes drive agents on your machine.

**D14 — An SSH gate with a forced command, rather than `rsync`.**
A change from what was first planned (`rsync`). An archive piped to a forced command is better on
four counts: a key limited to three verbs, no tool to install, atomic delivery, and the pipeline
woken straight away.

**D15 — Git for the wiki only.**
*Why:* to undo a whole run, to propose before applying (the curator's branch), to serve the hosts
when Obsidian is closed. LiveSync has a history, but it is per file and does not survive a rebuild
of its database. *Ruled out:* git for the segments, and an `_archive/` folder (the history is
enough; `log.md` keeps track of deletions).

**D16 — Scheduling by systemd, not by Hermes's scheduler.**
A change from what was first planned. *Why:* the pipeline keeps running if Hermes's gateway is
stopped, and it depends on no harness. Hermes remains the writer and the interlocutor.

**D17 — The harness is a configurable shell command.**
*Why:* to depend on neither Hermes, nor Claude, nor Codex. Hermes by default, which in the
author's setup means a Codex subscription, with no Claude token on the server.

**D18 — A single configuration, in variables.**
Same file and same format on the host and on the server. Installers that can be re-run. A mode
without root, and an "everything on one machine" mode (`MEMEX_SERVER_SSH=local`) to try it without
a server.

### Pipeline

**D19 — Agents never work in the live wiki.**
A disposable git working copy, a check, then a fast-forward merge. *Why:* a half-written state
never reaches Obsidian, and a failed run is thrown away without a trace.

**D20 — The model writes, the code keeps the books.**
Index, journal, ledger, provenance and `log.md` are generated. *Why:* this is what a model does
least reliably, and what a program does without error. When the code completes the front matter of
a page, it replaces only its own fields: the rest, written by hand, is kept as it is.

**D21 — The archivist works in two steps.**
Extraction without tools, then integration. *Why:* most sessions stop at extraction, which is
cheap; integration receives a short list instead of a transcript; and text that came from outside
never meets an agent that has tools.

**D22 — Guardrails are checked on the diff.**
Fix automatically what is cosmetic, undo what is out of scope, reject what is destructive. A
second attempt receives the faults; then quarantine. Any internal error while a segment is being
processed counts as a failure: one segment cannot block the ones that follow.

**D39 — An agent outage costs no segment.**
When a call to the agent fails, a probe asks it for one word. If it answers, the failure is down
to the segment and counts. If it does not, the run stops: no failure is counted, the queue stays
intact, you are notified once, and the next run tries again. Likewise, an agent that hands back
control with neither a page nor a report is a failure, not a session "with nothing to keep".
*Why:* without this, a quota reached during a backfill sent the whole queue to quarantine in three
runs, that is, 45 minutes. Found by re-reading the code before the first real backfill, not in
production.

**D40 — Three adjustments drawn from the first real backfill (66 sessions, 2026-11-16).**
- *The front matter of a page keeps only its last five sources.* A living page touched by 27
  sessions was dragging 27 lines of provenance along; the full list remains in the ledger and in
  git.
- *The weaver receives five candidate concepts, the most widely shared first, and must read their
  pages.* With eight candidates in alphabetical order and the instruction "doing nothing is
  acceptable", it had created only one cross-cutting page for 16 candidates, without reading the
  pages cited. A candidate it dismisses after reading is proposed again only if it gains a folder.
- *A living page that is already long stops growing.* The list of pages flags it to the archivist,
  which files the detail in a companion page per theme. One catch-all page had reached 30,000
  characters.

**D43 — Pages stay condensed; a living page fits in what a session receives.**
On the author's wiki, four of six project pages were longer than what the hook injects, so every
session started with a page cut in the middle. The limit that existed (24,000 characters, flagged
at 60 %) was a ceiling against runaway pages, not a target. There are now two limits: 5,000
characters for a living page, 12,000 for any other, both flagged at 80 %; the hook injects 8,000.
The conventions ask for one or two lines per point, and for the detail (a diagnosis, a comparison,
a procedure) to live in supplementary pages by theme, linked from the living page.
*Why two limits:* the living page is paid for by every session, a supplementary page only by the
session that opens it.
*Why the archivist does not condense on its own:* moving text out of a page is where facts get
lost. The archivist stops adding; cutting an existing page down is a `condense` action of the
curator, which you approve.
*What the code does for the model:* it measures. On a first real run, three condensed pages out of
six were still over their limit, because nothing told the model how far it was from it. Each
`condense` action now receives the size and the limit of its pages, and one more pass when a
living page is still over.

**D44 — The curator checks the links, with your approval.**
The weaver works from textual leads: a title mentioned without a link, an entity shared by several
folders. Nothing looked at whether an existing link still leads to the right page, or at two pages
on the same matter that never name each other. That judgement needs reading, so it goes to an
agent, and to the one whose changes you approve: the curator reads a few folders per run with the
pages they are linked with, and proposes `link` actions.
*Why one call per folder:* asked to check six folders along with the rest of its diagnosis, the
model answered in 27 seconds and said itself that it had judged the links from the summaries.
*Why not the weaver:* the weaver may not remove or redirect a link, and that restriction is what
makes it safe to run unattended.
*Why in rotation:* a wiki cannot be reread whole every week. Changed folders come first, then the
ones checked longest ago (`state/curator.json`).
*What the code guarantees:* a `link` action that changes a word of the text, creates a page or
deletes one is rejected. A folder counts as checked only if the curator's reply says what it read
and found there.

**D45 — A folder left unchecked is remembered, and you are warned.**
The limit on the folders checked per run started at 4, chosen for the cost without a measure. A
check takes 8 to 17 seconds, and in the one wiki measured the sessions of a week touched 1 to 3
folders four weeks out of five, and 12 the other week: the default is now 10. A limit can still be
too low without anyone noticing, so the code keeps the list of the folders due for a check, the
ones never checked or changed since the last run (`links_due` in `state/curator.json`, shown by
`memex server status`). The folders the limit leaves out come first on the next run, before the
ones that have just changed, and the review says how many waited and which, with the name of the
setting to raise. The warning is sent even when the curator has nothing else to propose.
*What counts as left out:* a folder due for a check. A folder checked before and unchanged since
is not: it comes round in rotation, when the limit leaves room.
*Also warned about:* a folder on which the agent failed or reported nothing, which is tried again
on the next run. That is not a matter of limit, and the message says so.
*An unchanged wiki* gets no new diagnosis, but the folders still due get their check.

**D23 — One writer at a time.**
A single lock covers the archivist, the weaver, the curator and the approvals.

### Wiki structure

**D24 — Folders: projects, topics, concepts.**
One living page per folder, always in a directory that bears its name. *Ruled out:* a hierarchy of
themes, which complicates filing without bringing anything the concepts do not.

**D25 — Decision records are immutable.**
*Why:* it is the history that explains why things are where they are.

**D26 — Three agents.**
The archivist adds, the weaver links, the curator tightens and checks: three risks, three cadences,
three scopes. The weaver is separate because it needs an overall view that the archivist does not have.

**D27 — The curator proposes; you approve.**
By messaging, action by action. Refusals are remembered.

**D28 — In French.** *(Superseded by D41.)*
Content, conventions and prompts. The folder names follow.

### Reading and synchronisation

**D29 — Agents read the files; no Obsidian search, no MCP.**
*Why:* Obsidian's CLI and the MCP servers require the application to be open. `qmd` remains
possible later.

**D30 — Instant session-start hook, synchronisation in the background.**
*Why:* opening a session must never wait for the network.

**D42 — The hook lists the pages linked with the project, without loading them.**
The project's page says which pages it points to, but not which pages point to it: a concept page
that describes how three projects use a tool was invisible from each of them. The hook now lists
every page outside the project's folder that is linked with it in either direction, the most
linked first, with its summary (`MEMEX_HOOK_RELATED`, 8 by default, a quarter of the injected text
at most). Journal pages are left out: they link to every project by construction.
*Why a list and not the content:* loading the neighbours would multiply what every session pays
for, whatever it is about. One line is enough for the agent to know the page exists and to decide.
*Why links and not mentions:* a link was checked by an agent when it was written. A bare mention
of the project's name may be about something else, and the ones still unlinked after the weaver
has run are mostly the ones it turned down.
*Cost:* the list is computed from the local copy when the session starts, with no network and no
LLM: under 0.1 s for a wiki of 80 pages, about 0.5 s for 2,000 pages (measured 2026-11-17).

**D31 — The vault is an ordinary folder, tied to the repository by a manifest.**
*Why:* no git file must go through LiveSync, and your edits in Obsidian must come back. In a
conflict, you win.

**D32 — A separate Obsidian vault for the wiki (recommended; the choice remains open).**
*Why:* the synchronisation is end-to-end encrypted; giving the server the passphrase of your main
vault would open all your notes to it. *Cost:* no direct link to them. memex imposes nothing: it
writes to `MEMEX_VAULT_DIR`, whichever vault that folder serves.

### Security and operations

**D33 — A sandbox for each agent call.**
Added along the way: during a trial on the author's machine, Hermes wrote to the home directory
instead of its working folder. Bubblewrap, with a fallback if it is unavailable
(`MEMEX_SANDBOX=bwrap` to require it).

**D34 — Dedicated account, code and keys owned by root, hardened units.**

**D35 — Daily backup, pulled by the hosts.**

**D36 — The pipeline's own sessions do not capture themselves.**
A marker at the top of every prompt, and the "no persistence" options of Claude Code and Codex.

**D37 — ChatGPT and claude.ai import: provided, experimental.**
Written from the known structure of the exports, without a real export at hand. The MCP connector
for writing to the wiki from those applications is not built.

**D38 — The name: memex.**
Vannevar Bush's "memex" (1945), which Karpathy's
["LLM Wiki"](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) cites: a personal
memory in which the links matter as much as the documents.

### Language

**D41 — The project is in English; a wiki has a language.**
Code, documentation, the default prompt set, command output and logs are in English. A wiki is
written in one language (`en` by default, `fr` available), chosen when it is created with
`MEMEX_LANG` or `server/install.sh --lang`, and recorded in the wiki itself (`.memex/wiki.json`)
so that the server, every host and a rebuilt server agree. The language decides the folder names,
the front-matter keys and values, the generated text (index, journal, log) and which prompt set
and conventions file are used (`prompts/<lang>/`, `wiki_template/<lang>/AGENTS.md`). The machine
protocol between prompts and code (JSON keys, kinds, statuses) is English in every language.
*Why:* the notes belong to their author and stay in the author's language, while the tool stays
usable and maintainable by anyone; recording the language in the wiki keeps a host and a server
from disagreeing. Adding a language takes one `Locale` in `src/memex/i18n.py`, a prompt folder
and a conventions file.

## 10. What is verified and what is not

**Verified on the author's machine (2026-11-16):**

- 127 automated tests, including 29 end-to-end scenarios (nominal path, session continuations,
  guardrails, quarantine, agent outage, review, restore, rebuild, forged segment), on an English wiki
  and on a French one.
- An independent review of the code by a second agent, which reproduced five defects (including a
  forged segment able to write outside its inbox). All are fixed and covered by a test.
- The sweeper on the author's real history: 66 segments from 69 sessions, in 1.2 seconds.
- The full pipeline with a real model (Claude, via `claude -p`) on six real sessions: six
  integrations, no failure, four concept pages created by the weaver.
- The same pipeline with Hermes as the writer (its default model), in the sandbox, on four real
  sessions: three integrations, one session judged to hold nothing to keep, no failure.
- The curator with a real model, in the sandbox: two actions proposed, one approved and applied,
  one rejected and remembered.
- A full backfill of the author's history (2026-11-16): 66 segments processed in 31 minutes with
  Claude as the agent, inside the sandbox, under the systemd user units: no failure, nothing
  quarantined, 71 pages. Then the weaver and the curator on that wiki (five actions proposed).
- The list of related pages printed by the session-start hook, on that wiki (2026-11-17): three to
  seven pages per project, 0.09 s added to the start of a session. Whether agents make good use
  of the list has not been measured.
- Condensed pages and the link check (2026-11-18), with a real model on a copy of that wiki, in
  French. The archivist filed the detail of a note in a supplementary page and left two lines with
  the link in the living page. The curator brought the six living pages over their limit under it,
  one `condense` action each (the longest from 30,000 characters to 2,800); the total text of the
  pages concerned grew by 0 to 23 %, which suggests detail was moved rather than dropped, but this
  was not checked fact by fact. The links of six folders were checked, one call each: two `link`
  actions proposed, both accepted by the guardrails. The warning about folders left unchecked
  (D45) came later the same day and is covered by the automated tests only.
- The three harnesses (Hermes, Claude Code, Codex), at the three permission levels, in the sandbox.
- The exclusion of the pipeline's own sessions, on Hermes's real database.
- The SSH gate against a real `sshd`: shell, arbitrary command, other repository and writing all
  refused.
- Both installers, in a throwaway home directory; the units with `systemd-analyze`.

**Not verified, for lack of access:**

- The real installation on the server: system mode, hardened units, and above all **the sandbox
  inside the systemd unit**. `memex server status` says whether it is active.
- The headless LiveSync client, Tailscale, Hermes's messaging gateway and its skill.
- The full pipeline with Codex as the writer (only its individual calls were).
- The English versions of the prompts changed or added on 2026-11-18 (`integrate`, `curate-diagnose`,
  `curate-apply`, `curate-links`) with a real model: they have only run against the fake agent.
- The injection of the `SessionStart` hook in Codex (a bug report exists for one version).
- The path without systemd (cron), macOS, and the real formats of the ChatGPT and claude.ai
  exports.

**Prompt sets (a wiki has a language, see D41):**

- French prompt set: exercised on the 66-segment backfill of 2026-11-16, and again on 2026-11-17
  after the machine protocol moved to English keys.
- English prompt set: exercised on 2026-11-17.
- Both runs of 2026-11-17 used the same four real sessions, with Claude as the agent: four
  integrations, concept pages created by the weaver, a curator review proposed and approved.
  In the French run, one extraction reply held two JSON objects (the model corrected itself): it
  failed, was retried on the next run and succeeded. The parser now takes the last object.

## 11. Known limits and possible next steps

- **Cloud sessions** (claude.ai/code started from the web): no local transcript, so no capture.
  Sessions driven remotely but executed on a machine are captured.
- **ChatGPT and Claude on the web**: by manual export only.
- **A very long session** is split into several segments, processed separately: an item that
  straddles two pieces can be seen twice. The archivist looks for what already exists before
  writing.
- **Cost**: one to two calls per non-trivial session. Backfilling a long history is done in
  batches of 25.
- **Next steps**: an MCP connector to the wiki for Claude on the web; semantic search (`qmd`) once
  the wiki exceeds a few hundred pages; an architecture page bootstrapped from the repository of
  each project.
