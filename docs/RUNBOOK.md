# memex — runbook

Everything you need to install, connect, operate and rebuild. The reasons behind the choices are
in [ARCHITECTURE.md](ARCHITECTURE.md).

- [Trying it without a server](#trying-it-without-a-server)
- [Installing the server](#installing-the-server)
- [Connecting a work machine](#connecting-a-work-machine)
- [Day to day](#day-to-day)
- [Answering the curator](#answering-the-curator)
- [Tuning the behaviour](#tuning-the-behaviour)
- [Rebuilding](#rebuilding)
- [Troubleshooting](#troubleshooting)
- [Updating, uninstalling](#updating-uninstalling)

## Trying it without a server

Everything can run on a single machine, without root: the "server" is then a local folder.

```bash
server/install.sh --user-mode --agent claude        # or hermes, codex
host/install.sh --server local --server-home ~/.local/share/memex-server
memex sync            # sweeps your sessions and drops them off
memex server run      # the archivist integrates them
memex server status
```

The wiki is in `~/.local/share/memex-server/wiki`, and its read-only copy in
`~/.local/share/memex/wiki`. To avoid backfilling your whole history in one go, add
`--no-backfill` to `host/install.sh`.

## Installing the server

Requirements on the server: Linux with systemd, Python 3.9 or later, git, root access over SSH.
Recommended: `bubblewrap` (`apt install bubblewrap`), for the agents' sandbox.

### 1. Deploy

From a machine where this repository is cloned:

```bash
server/deploy.sh root@my-server --agent hermes --vault /srv/memex/vault --deny /var/www
```

- `--deny` makes a folder invisible to the agents. Repeat it for every service on the server that
  memex has no business seeing.
- `--vault` can be left out if you do not want an Obsidian vault on the server.
- `--lang en|fr` sets the language of a **new** wiki (`en` by default; it is written to the
  configuration as `MEMEX_LANG`). The wiki records its language when it is created, in
  `.memex/wiki.json`: changing the variable afterwards does not change the language of an existing
  wiki.
- The command can be re-run: it is also the one used for updates.

It creates the `memex` account (no password, no sudo), installs the program in `/opt/memex`,
writes `/etc/memex/memex.env`, creates the wiki repository and enables three timers.

### 2. Connect the agent

The pipeline runs the agent under the `memex` account. The agent therefore has to be installed
and signed in there, once.

**Hermes** (default):

```bash
sudo -iu memex
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
hermes setup                 # choose the provider and sign in
hermes -z "Reply OK"         # must answer without asking anything
```

**Claude Code** or **Codex**: install it under that account, sign it in (`claude` then `/login`, or
`claude setup-token`; `codex login`), and set `MEMEX_AGENT=claude` or `codex` in
`/etc/memex/memex.env`.

Then check:

```bash
sudo -u memex memex server status     # the `sandbox` line must report it as active
sudo systemctl start memex-run.service && journalctl -u memex-run.service -n 20
```

If the sandbox is reported as unavailable, the pipeline runs all the same, protected by the
hardening of the systemd unit. To require it: `MEMEX_SANDBOX=bwrap`.

### 3. Messaging and notifications (optional)

```bash
sudo -iu memex
hermes gateway setup         # connect Telegram, Signal, Discord…
hermes gateway install       # gateway as a service, started with the server
hermes send --to telegram "test"
```

Allow only your own account on each messaging service. Then, in `/etc/memex/memex.env`:

```
MEMEX_NOTIFY_CMD='hermes send --to telegram --subject "$MEMEX_SUBJECT" --file -'
```

The `memex` skill is already installed for Hermes: you can write to it "where is the review at?",
"ok except 2", "note that…", "restore the page …".

### 4. Obsidian vault and private network (optional)

- [Vault synchronised by LiveSync](../server/livesync/README.md)
- [Private network with Tailscale](../server/tailscale/README.md)

## Connecting a work machine

On each machine where you use Claude Code or Codex:

```bash
git clone <this repository> memex && cd memex
host/install.sh --server memex@my-server --exclude-remotes '*my-employer*'
```

The installer:

1. installs the `memex` command in `~/.local/bin`;
2. writes `~/.config/memex/memex.env`;
3. creates a dedicated SSH key, `~/.ssh/memex_ed25519`;
4. hooks into Claude Code (session start and session end hooks, pointer in `CLAUDE.md`, read
   permission on the wiki) and Codex (session start hook, pointer in `AGENTS.md`);
5. enables a synchronisation timer, every ten minutes.

It ends by printing the command to run **on the server** to authorise this machine:

```bash
sudo memex server device add laptop "ssh-ed25519 AAAA… memex@laptop"
```

Then, on the machine: `memex doctor && memex sync`.

Useful options:

| Option | Effect |
|---|---|
| `--no-backfill` | Does not backfill the history: only the sessions to come are captured |
| `--exclude-paths ~/Work` | Never captures these directories |
| `--exclude-remotes '*company*'` | Never captures these repositories |
| `--sources claude-code,codex,hermes` | Adds Hermes if you use it on this machine |
| `--device NAME` | Name of the machine in the wiki (default: its hostname) |

The next time it starts, Codex will ask you to approve the new hook: this is expected.

## Day to day

Nothing to do: capture, archiving and the update of the wiki are automatic.

| Need | Command |
|---|---|
| Record something right away | `memex note "Decision: … because …"` |
| Force a synchronisation | `memex sync` |
| See the state of the machine | `memex status`, `memex doctor` |
| Exclude a session | write `#memex-off` in it |
| Exclude a project | create an empty `.memex-ignore` file at its root |
| Import a ChatGPT or claude.ai export | `memex import chatgpt export.zip --since 2026-01-01 --dry-run` |

On the server (under the `memex` account, or with `sudo -u memex`):

| Need | Command |
|---|---|
| Overall state | `memex server status` |
| Start a run | `memex server run` |
| Check without a model | `memex server lint` |
| Run the weaver, the curator | `memex server weave --force`, `memex server curate --force` |
| Restore a deleted page | `memex server restore "Topics/X/Page.md"` |
| Back up now | `memex server backup` |
| List, remove a machine | `sudo memex server device list`, `sudo memex server device remove NAME` |

## Answering the curator

Every week, if it has something to propose, you receive a numbered list:

```
1. [merge] Merge Brokers.md into Savings plan.md
   Why: both compare the same brokers.
2. [condense] Condense Solitaire.md
? To decide: is the deployment of Atlas manual or scripted?
```

Reply to Hermes "ok", "ok except 2", "only 1" or "no". Or on the command line:

```bash
memex server review show
memex server review approve --except 2
memex server review reject
```

Nothing changes before you answer. A rejected action is not proposed again. Without an answer, the
review expires after ten days and the curator starts again from scratch.

## Tuning the behaviour

| What you want to change | Where |
|---|---|
| The structure and the tone of the wiki | `AGENTS.md` at the root of the wiki, editable from Obsidian |
| What the archivist retains | `MEMEX_PROMPTS_DIR`: copy `src/memex/prompts/<lang>/` and adapt `extract.md` |
| The threshold below which sessions are ignored | `MEMEX_MIN_USER_CHARS` |
| The sensitivity of the weaver | `MEMEX_WEAVER_MIN_PAGES`, `MEMEX_WEAVER_MIN_FOLDERS`, `MEMEX_WEAVER=0` |
| The number of actions per review | `MEMEX_CURATOR_MAX_ACTIONS` |
| The curator's day | `server/install.sh --curate "Sun *-*-* 05:00:00"` |
| The model | `MEMEX_AGENT_MODEL` |
| The harness | `MEMEX_AGENT`, or `MEMEX_AGENT_EXTRACT_CMD` / `_READ_CMD` / `_EDIT_CMD` |
| How noisy the notifications are | `MEMEX_NOTIFY_LEVEL`: `never`, `failures`, `changes`, `always` |

Every variable is described in [`config/memex.env.example`](../config/memex.env.example).

## Rebuilding

### A new work machine

See [Connecting a work machine](#connecting-a-work-machine). Nothing else.

### A new server, with a backup

The backups are in `/srv/memex/backups` on the server, and pulled every day into
`~/.local/share/memex/backups` on each host.

```bash
server/deploy.sh root@new-server --agent hermes
scp ~/.local/share/memex/backups/memex-YYYYMMDDTHHMMSSZ.tar.gz root@new-server:/tmp/
ssh root@new-server 'sudo -u memex memex server restore-backup /tmp/memex-YYYYMMDDTHHMMSSZ.tar.gz --force'
```

Then sign the agent in again, re-authorise each machine (`sudo memex server device add …`) and, on
each host, update `MEMEX_SERVER_SSH` if the address has changed.

### A new server, without a backup

Each host keeps the segments it has sent. The wiki is rebuilt by replaying them:

```bash
server/deploy.sh root@new-server --agent hermes     # then the agent and the keys, as above
# on each host:
memex resend && memex ship
```

The new server starts with an empty wiki, created in English unless you say otherwise: if the lost
wiki was in French, add `--lang fr` to the `server/deploy.sh` command.

The result is not identical to the lost wiki (it is rewritten by the model), and the edits you
made by hand in Obsidian do not come back. If a host still has its copy of the wiki, you can also
start again from that: it is a complete git repository, in `~/.local/share/memex/wiki`.

### Changing work machine

Before you give up a machine, run `memex sync`. Its segments are already on the server;
`~/.local/share/memex/sent` is only the copy of them.

## Troubleshooting

| Symptom | Where to look |
|---|---|
| Nothing arrives in the wiki | `memex status` on the host: are there segments pending? Then `memex doctor` |
| `memex doctor`: server unreachable | Is the key authorised? `ssh -i ~/.ssh/memex_ed25519 memex@my-server memex-gate ping` |
| The segments arrive, the pages do not | On the server: `memex server status`, then `journalctl -u memex-run.service -n 50` |
| A segment is in quarantine | `logs/agent/` for the reason; to try again, move the file from `raw/quarantine/<device>/` back to `raw/inbox/<device>/` |
| The agent does not answer | `memex server status` shows `agent_unavailable` with the date and the error. Nothing is lost: the queue waits and every run tries again. If the sign-in has expired: `sudo -iu memex` then `hermes -z "OK"` |
| A page has been damaged | `git -C /srv/memex/wiki log -- "Path/Page.md"`, then `memex server restore "Path/Page.md" --rev <commit>` |
| The hook shows nothing | `memex hook session-start < /dev/null` in the project folder |
| The local wiki is behind | `memex pull` |

Where the logs are:

- host: `systemctl --user status memex-sync.timer`, `journalctl --user -u memex-sync.service`;
- server: `journalctl -u memex-run.service`, and in `/srv/memex/logs/`: `agent/` (one trace per
  call, with the prompt and the reply) and `notifications.log`.

## Updating, uninstalling

```bash
git pull
server/deploy.sh root@my-server    # server
host/install.sh                    # each work machine
```

The installers do not touch an existing configuration.

```bash
host/uninstall.sh                  # removes hooks, pointers, timer and program
host/uninstall.sh --purge          # … and the local data
```

On the server:

```bash
sudo server/uninstall.sh           # removes timers, units and program; keeps the wiki
sudo server/uninstall.sh --purge   # … and deletes wiki, segments, backups and configuration
server/uninstall.sh --user-mode    # for an installation done without root
```

The `memex` account and the vault folder are never deleted by the script.
