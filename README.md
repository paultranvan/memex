# memex

memex turns your sessions with coding agents (Claude Code, Codex, Hermes) into a structured markdown
wiki. You read it in Obsidian, and the same agents read it at the start of every session.

- Each **project** gets a living page that says what is true today, and dated decision records that
  say why.
- Each **topic** outside code (a savings plan, a machine set-up) gets the same.
- Three agent roles maintain it: the **archivist** integrates what sessions bring, the **weaver** links
  pages and writes cross-cutting concept pages, and the **curator** tightens the wiki. The curator's
  changes are applied only after you approve them.

```
 work machines                              server
 sweeper (no LLM) ──── segments ────────►  archivist → weaver → wiki (git)
 copy of the wiki ◄──── git fetch ───────  curator (on approval)
 session-start hook                        vault ◄─► LiveSync ◄─► Obsidian
```

Nothing has to be running on your work machine for the wiki to grow, and nothing in your sessions
changes: capture is automatic and happens after the fact.

## Getting started

Try it on a single machine, without a server and without root:

```bash
server/install.sh --user-mode --agent claude
host/install.sh --server local --server-home ~/.local/share/memex-server --no-backfill
memex note "Decision: try memex, to keep a memory of my agent sessions."
memex server run && memex server status
```

A real installation has one server and any number of work machines:

```bash
server/deploy.sh root@my-server --agent hermes --deny /var/www     # the server
host/install.sh --server memex@my-server                           # each work machine
```

The rest (signing the agent in, authorising machines, the vault, a private network) is in the
[runbook](docs/RUNBOOK.md).

### Wiki language

memex is in English, and your wiki is in your language. A wiki is created in English by default;
pass `--lang fr` to `server/install.sh` for a French one. The language sets the folder names, the page
headers and the prompts the agents receive, and the wiki remembers it. Adding a language takes one
entry in [`src/memex/i18n.py`](src/memex/i18n.py), a prompt folder and a conventions file.

## Inspiration and credits

memex is an implementation of the **LLM Wiki** pattern described by Andrej Karpathy in
[this gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f): instead of retrieving
from raw documents at question time, an LLM builds and maintains a persistent, interlinked wiki of
markdown files, and the knowledge compounds. The three layers of memex (immutable raw sources, a wiki
written by the LLM, a conventions file that tells it how), the `index.md` and `log.md` files, and the
use of Obsidian and git all come from that text.

The name comes from Vannevar Bush's Memex (1945), a personal knowledge store with associative trails
between documents, which the gist cites as the same idea in spirit.

## How memex differs from the LLM Wiki

The gist calls itself "an idea file" and says it "describes the idea, not a specific implementation".
memex is one implementation, for one kind of source, and it makes different choices where running
unattended requires them.

| | LLM Wiki (the pattern) | memex |
|---|---|---|
| Sources | Documents you curate and drop into a raw folder: articles, papers, images, data | Your own agent sessions, captured automatically, plus explicit notes (`memex note`) |
| Ingestion | You ask the LLM to process a source, in a session you supervise | Unattended: a session ends, its segment is shipped, the server integrates it within minutes |
| Where it runs | Described as one agent working alongside you | Any number of work machines feeding one server |
| Who maintains | A single LLM agent | Three roles with separate permissions: archivist, weaver, curator |
| What the LLM may change | Guided by the conventions file; raw sources are read-only | Enforced by code on the diff of each run: out-of-scope changes are reverted, destructive ones rejected |
| Clean-up ("lint") | You periodically ask the LLM for a health check | Deterministic checks, then proposals you approve or reject one by one; refusals are remembered |
| Bookkeeping | The LLM updates `index.md` and `log.md` | Code generates the index, log, journal and provenance; the LLM only writes pages |
| History of choices | Not covered | Immutable, dated decision records; a changed decision gets a new record |
| Secrets | Not covered | Known secret patterns are redacted on the work machine, before anything leaves it |
| Reading it back | You query the wiki through the LLM | Your coding agents get the project's page at session start and consult the wiki themselves |

What the pattern covers and memex does not:

- **General documents.** memex does not ingest articles, papers or web clippings. Its sources are
  sessions, notes, and (experimentally) ChatGPT and claude.ai exports.
- **Query as an operation.** There is no `memex query`, and answers are not filed back into the wiki
  as new pages. You ask your usual agent, which reads the wiki.
- **Search tooling.** memex relies on `index.md`, plain file search and Obsidian. It does not bundle a
  search engine such as the one the gist suggests.

## Status

memex was built and first run in November 2026, on one machine. What has and has not been verified
is listed precisely in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#10-what-is-verified-and-what-is-not).
In short:

- **Verified:** capture of Claude Code, Codex and Hermes sessions; the full pipeline on 66 real
  sessions with Claude as the agent and a French wiki; both prompt sets (English and French) on
  real sessions, curator review included; the three harnesses inside the sandbox; the SSH gate
  against a real `sshd`; both installers; the test suite.
- **Not verified:** a real server installed as root with its own account; the headless LiveSync
  client; the Tailscale set-up; Hermes messaging; the session-start hook under Codex.

## Documentation

| | |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it works, and every decision with its reason |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | Install, connect, operate, rebuild, troubleshoot |
| [config/memex.env.example](config/memex.env.example) | Every configuration variable |
| [server/livesync/README.md](server/livesync/README.md) | The Obsidian vault on the server |
| [server/tailscale/README.md](server/tailscale/README.md) | A private, one-way network |

## Repository

```
src/memex/            the program (Python, standard library only)
  adapters/           session readers: Claude Code, Codex, Hermes
  prompts/<lang>/     the agents' prompts, one set per wiki language
  wiki_template/      the conventions file a new wiki starts with (AGENTS.md)
  i18n.py             what changes with the wiki's language
host/                 installer for a work machine
server/               installer for the server, systemd units, Hermes skill
config/               configuration template
tests/                unit tests and end-to-end scenarios driven by a fake agent
```

## Tests

```bash
make test
```

No dependency beyond Python 3.9 or later and git. The sandbox tests are skipped on a machine without
`bubblewrap`.
