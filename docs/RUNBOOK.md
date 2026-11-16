# memex — guide d'exploitation

Tout ce qu'il faut pour installer, brancher, exploiter et reconstruire. Les raisons des choix sont
dans [ARCHITECTURE.md](ARCHITECTURE.md).

- [Essayer sans serveur](#essayer-sans-serveur)
- [Installer le serveur](#installer-le-serveur)
- [Brancher une machine de travail](#brancher-une-machine-de-travail)
- [Au quotidien](#au-quotidien)
- [Répondre au vérificateur](#répondre-au-vérificateur)
- [Régler le comportement](#régler-le-comportement)
- [Reconstruire](#reconstruire)
- [Dépannage](#dépannage)
- [Mettre à jour, désinstaller](#mettre-à-jour-désinstaller)

## Essayer sans serveur

Tout peut tourner sur une seule machine, sans root : le « serveur » est alors un dossier local.

```bash
server/install.sh --user-mode --agent claude        # ou hermes, codex
host/install.sh --server local --server-home ~/.local/share/memex-server
memex sync            # balaye tes sessions et les dépose
memex server run      # l'archiviste les intègre
memex server status
```

Le wiki est dans `~/.local/share/memex-server/wiki`, et sa copie de lecture dans
`~/.local/share/memex/wiki`. Pour ne pas reprendre tout ton historique d'un coup, ajoute
`--no-backfill` à `host/install.sh`.

## Installer le serveur

Prérequis sur le serveur : Linux avec systemd, Python 3.9 ou plus, git, un accès root par SSH.
Recommandé : `bubblewrap` (`apt install bubblewrap`), pour le bac à sable des agents.

### 1. Déployer

Depuis une machine où ce dépôt est cloné :

```bash
server/deploy.sh root@mon-vps --agent hermes --vault /srv/memex/vault --deny /var/www
```

- `--deny` rend un dossier invisible aux agents. Répète-le pour chaque service du serveur que
  memex n'a pas à voir.
- `--vault` peut être omis si tu ne veux pas de vault Obsidian sur le serveur.
- La commande est rejouable : c'est aussi celle des mises à jour.

Elle crée le compte `memex` (sans mot de passe ni sudo), installe le programme dans `/opt/memex`,
écrit `/etc/memex/memex.env`, crée le dépôt du wiki et active trois minuteries.

### 2. Connecter l'agent

Le pipeline lance l'agent sous le compte `memex`. Il faut donc l'y installer et l'y connecter, une
fois.

**Hermes** (défaut) :

```bash
sudo -iu memex
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
hermes setup                 # choisir le fournisseur et se connecter
hermes -z "Réponds OK"       # doit répondre sans rien demander
```

**Claude Code** ou **Codex** : installe-le sous ce compte, connecte-le (`claude` puis `/login`, ou
`claude setup-token` ; `codex login`), et mets `MEMEX_AGENT=claude` ou `codex` dans
`/etc/memex/memex.env`.

Vérifie ensuite :

```bash
sudo -u memex memex server status     # la ligne « bac_a_sable » doit dire « actif »
sudo systemctl start memex-run.service && journalctl -u memex-run.service -n 20
```

Si le bac à sable est indiqué « indisponible », le pipeline tourne quand même, protégé par le
cloisonnement de l'unité systemd. Pour l'exiger : `MEMEX_SANDBOX=bwrap`.

### 3. Messagerie et notifications (facultatif)

```bash
sudo -iu memex
hermes gateway setup         # connecter Telegram, Signal, Discord…
hermes gateway install       # passerelle en service, démarrée avec le serveur
hermes send --to telegram "essai"
```

N'autorise que ton propre compte sur chaque messagerie. Puis, dans `/etc/memex/memex.env` :

```
MEMEX_NOTIFY_CMD='hermes send --to telegram --subject "$MEMEX_SUBJECT" --file -'
```

Le skill `memex` est déjà installé pour Hermes : tu peux lui écrire « où en est la revue ? »,
« ok sauf la 2 », « note que… », « restaure la page … ».

### 4. Vault Obsidian et réseau privé (facultatif)

- [Vault synchronisé par LiveSync](../server/livesync/README.md)
- [Réseau privé avec Tailscale](../server/tailscale/README.md)

## Brancher une machine de travail

Sur chaque machine où tu utilises Claude Code ou Codex :

```bash
git clone <ce dépôt> memex && cd memex
host/install.sh --server memex@mon-vps --exclude-remotes '*mon-employeur*'
```

L'installeur :

1. installe la commande `memex` dans `~/.local/bin` ;
2. écrit `~/.config/memex/memex.env` ;
3. crée une clé SSH dédiée, `~/.ssh/memex_ed25519` ;
4. branche Claude Code (hooks de début et de fin de session, pointeur dans `CLAUDE.md`, droit de
   lecture sur le wiki) et Codex (hook de début de session, pointeur dans `AGENTS.md`) ;
5. active une minuterie de synchronisation toutes les dix minutes.

Il affiche pour finir la commande à lancer **sur le serveur** pour autoriser cette machine :

```bash
sudo memex server device add portable "ssh-ed25519 AAAA… memex@portable"
```

Puis, sur la machine : `memex doctor && memex sync`.

Options utiles :

| Option | Effet |
|---|---|
| `--no-backfill` | Ne reprend pas l'historique : seules les sessions à venir sont capturées |
| `--exclude-paths ~/Work` | Ne capture jamais ces répertoires |
| `--exclude-remotes '*entreprise*'` | Ne capture jamais ces dépôts |
| `--sources claude-code,codex,hermes` | Ajoute Hermes si tu l'utilises sur cette machine |
| `--device NOM` | Nom de la machine dans le wiki (défaut : son nom d'hôte) |

Au prochain lancement, Codex demandera d'approuver le nouveau hook : c'est attendu.

## Au quotidien

Rien à faire : la capture, l'archivage et la mise à jour du wiki sont automatiques.

| Besoin | Commande |
|---|---|
| Consigner quelque chose tout de suite | `memex note "Décision : … parce que …"` |
| Forcer une synchronisation | `memex sync` |
| Voir l'état de la machine | `memex status`, `memex doctor` |
| Exclure une session | écrire `#memex-off` dedans |
| Exclure un projet | créer un fichier vide `.memex-ignore` à sa racine |
| Importer un export ChatGPT ou claude.ai | `memex import chatgpt export.zip --since 2026-01-01 --dry-run` |

Sur le serveur (sous le compte `memex`, ou avec `sudo -u memex`) :

| Besoin | Commande |
|---|---|
| État général | `memex server status` |
| Lancer un passage | `memex server run` |
| Contrôle sans modèle | `memex server lint` |
| Lancer le tisseur, le vérificateur | `memex server tisseur --force`, `memex server verif --force` |
| Rétablir une page supprimée | `memex server restore "Sujets/X/Page.md"` |
| Sauvegarder maintenant | `memex server backup` |
| Lister, retirer une machine | `sudo memex server device list`, `sudo memex server device remove NOM` |

## Répondre au vérificateur

Chaque semaine, s'il a quelque chose à proposer, tu reçois une liste numérotée :

```
1. [fusion] Fusionner Courtiers.md dans PEA.md
   Pourquoi : les deux comparent les mêmes courtiers.
2. [condensation] Condenser Solitaire.md
? À trancher : le déploiement d'Atlas est-il manuel ou scripté ?
```

Réponds à Hermes « ok », « ok sauf la 2 », « seulement la 1 » ou « non ». Ou en ligne de commande :

```bash
memex server review show
memex server review approve --except 2
memex server review reject
```

Rien ne change avant ta réponse. Une action refusée n'est plus reproposée. Sans réponse, la revue
expire au bout de dix jours et le vérificateur repart de zéro.

## Régler le comportement

| Ce que tu veux changer | Où |
|---|---|
| La structure et le ton du wiki | `AGENTS.md` à la racine du wiki, modifiable depuis Obsidian |
| Ce que l'archiviste retient | `MEMEX_PROMPTS_DIR` : copie `src/memex/prompts/` et adapte `extract.md` |
| Le seuil des sessions ignorées | `MEMEX_MIN_USER_CHARS` |
| La sensibilité du tisseur | `MEMEX_TISSEUR_MIN_PAGES`, `MEMEX_TISSEUR_MIN_DOSSIERS`, `MEMEX_TISSEUR=0` |
| Le nombre d'actions par revue | `MEMEX_VERIF_MAX_ACTIONS` |
| Le jour du vérificateur | `server/install.sh --verif "Sun *-*-* 05:00:00"` |
| Le modèle | `MEMEX_AGENT_MODEL` |
| Le harnais | `MEMEX_AGENT`, ou `MEMEX_AGENT_EXTRACT_CMD` / `_READ_CMD` / `_EDIT_CMD` |
| Le bruit des notifications | `MEMEX_NOTIFY_LEVEL` : `never`, `failures`, `changes`, `always` |

Toutes les variables sont décrites dans [`config/memex.env.example`](../config/memex.env.example).

## Reconstruire

### Une nouvelle machine de travail

Voir [Brancher une machine de travail](#brancher-une-machine-de-travail). Rien d'autre.

### Un nouveau serveur, avec une sauvegarde

Les sauvegardes sont dans `/srv/memex/backups` sur le serveur, et rapatriées chaque jour dans
`~/.local/share/memex/backups` sur chaque hôte.

```bash
server/deploy.sh root@nouveau-vps --agent hermes
scp ~/.local/share/memex/backups/memex-AAAAMMJJ.tar.gz root@nouveau-vps:/tmp/
ssh root@nouveau-vps 'sudo -u memex memex server restore-backup /tmp/memex-AAAAMMJJ.tar.gz --force'
```

Puis reconnecte l'agent, ré-autorise chaque machine (`sudo memex server device add …`) et, sur
chaque hôte, mets `MEMEX_SERVER_SSH` à jour si l'adresse a changé.

### Un nouveau serveur, sans sauvegarde

Chaque hôte garde ses condensés envoyés. Le wiki se reconstruit en les rejouant :

```bash
server/deploy.sh root@nouveau-vps --agent hermes     # puis agent et clés, comme ci-dessus
# sur chaque hôte :
memex resend && memex ship
```

Le résultat n'est pas identique au wiki perdu (il est réécrit par le modèle), et tes modifications
faites à la main dans Obsidian ne reviennent pas. Si un hôte a encore sa copie du wiki, tu peux
aussi repartir d'elle : c'est un dépôt git complet, dans `~/.local/share/memex/wiki`.

### Changer de machine de travail

Avant d'abandonner une machine, lance `memex sync`. Ses condensés sont déjà sur le serveur ;
`~/.local/share/memex/sent` n'en est que la copie.

## Dépannage

| Symptôme | À regarder |
|---|---|
| Rien n'arrive dans le wiki | `memex status` sur l'hôte : des condensés « en attente » ? Puis `memex doctor` |
| `memex doctor` : serveur injoignable | La clé est-elle autorisée ? `ssh -i ~/.ssh/memex_ed25519 memex@mon-vps memex-gate ping` |
| Les condensés arrivent, pas les pages | Sur le serveur : `memex server status`, puis `journalctl -u memex-run.service -n 50` |
| Un condensé est en quarantaine | `logs/agent/` pour la raison ; pour réessayer, remets le fichier de `raw/quarantine/<appareil>/` dans `raw/inbox/<appareil>/` |
| L'agent ne répond pas | `memex server status` affiche `agent_indisponible` avec la date et l'erreur. Rien n'est perdu : la file attend et chaque passage réessaie. Si la connexion a expiré : `sudo -iu memex` puis `hermes -z "OK"` |
| Une page a été abîmée | `git -C /srv/memex/wiki log -- "Chemin/Page.md"`, puis `memex server restore "Chemin/Page.md" --rev <commit>` |
| Le hook ne s'affiche pas | `memex hook session-start < /dev/null` dans le dossier du projet |
| Le wiki local est en retard | `memex pull` |

Où sont les journaux :

- hôte : `systemctl --user status memex-sync.timer`, `journalctl --user -u memex-sync.service` ;
- serveur : `journalctl -u memex-run.service`, et dans `/srv/memex/logs/` : `agent/` (une trace par
  appel, avec l'invite et la réponse) et `notifications.log`.

## Mettre à jour, désinstaller

```bash
git pull
server/deploy.sh root@mon-vps      # serveur
host/install.sh                    # chaque machine de travail
```

Les installeurs ne touchent pas à une configuration existante.

```bash
host/uninstall.sh                  # retire hooks, pointeurs, minuterie et programme
host/uninstall.sh --purge          # … et les données locales
```

Sur le serveur :

```bash
sudo server/uninstall.sh           # retire minuteries, unités et programme ; garde le wiki
sudo server/uninstall.sh --purge   # … et supprime wiki, condensés, sauvegardes et configuration
server/uninstall.sh --user-mode    # pour une installation faite sans root
```

Le compte `memex` et le dossier du vault ne sont jamais supprimés par le script.
