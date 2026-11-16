# memex

Mes sessions avec des agents (Claude Code, Codex, Hermes) deviennent un wiki markdown structuré,
lisible dans Obsidian et consulté par ces mêmes agents au démarrage de chaque session.

- Chaque **projet** a sa page vivante et ses fiches de décision datées.
- Chaque **sujet** hors code (un PEA, une configuration) a la sienne.
- Trois agents l'entretiennent : l'**archiviste** intègre, le **tisseur** relie, le **vérificateur**
  resserre — ce dernier seulement après mon accord.

```
 machines de travail                        serveur
 balayeur (sans LLM) ──── condensés ────►  archiviste → tisseur → wiki (git)
 copie du wiki       ◄──── git fetch ────  vérificateur (sur accord)
 hook de démarrage                         vault ◄─► LiveSync ◄─► Obsidian
```

## Démarrer

Essai sur une seule machine, sans serveur ni root :

```bash
server/install.sh --user-mode --agent claude
host/install.sh --server local --server-home ~/.local/share/memex-server --no-backfill
memex note "Décision : essayer memex, pour garder la mémoire de mes sessions."
memex server run && memex server status
```

Installation réelle :

```bash
server/deploy.sh root@mon-vps --agent hermes --deny /var/www     # le serveur
host/install.sh --server memex@mon-vps                           # chaque machine de travail
```

La suite (connecter l'agent, autoriser les machines, vault, réseau privé) est dans le
[guide d'exploitation](docs/RUNBOOK.md).

## Documentation

| | |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Comment c'est fait, et chaque décision avec sa raison |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | Installer, brancher, exploiter, reconstruire, dépanner |
| [config/memex.env.example](config/memex.env.example) | Toutes les variables de configuration |
| [server/livesync/README.md](server/livesync/README.md) | Vault Obsidian sur le serveur |
| [server/tailscale/README.md](server/tailscale/README.md) | Réseau privé, sens unique |

## Dépôt

```
src/memex/            le programme (Python, bibliothèque standard seulement)
  adapters/           lecture des sessions : Claude Code, Codex, Hermes
  prompts/            les invites des agents
  wiki_template/      le wiki de départ, avec ses conventions (AGENTS.md)
host/                 installation d'une machine de travail
server/               installation du serveur, unités systemd, skill Hermes
config/               modèle de configuration
tests/                74 tests, dont les scénarios de bout en bout
```

## Tests

```bash
make test
```

Aucune dépendance : Python 3.9 ou plus récent, et git. Les tests du bac à sable sont ignorés sur
une machine sans `bubblewrap`.
