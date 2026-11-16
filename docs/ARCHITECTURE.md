# memex — architecture et décisions

memex transforme mes sessions avec des agents (Claude Code, Codex, Hermes) en un wiki markdown
structuré, lisible dans Obsidian et consulté par ces mêmes agents. Ce document décrit le système
tel qu'il est construit dans ce dépôt, puis liste chaque décision avec sa raison et ce qui a été
écarté.

- [1. Vue d'ensemble](#1-vue-densemble)
- [2. Le trajet d'une information](#2-le-trajet-dune-information)
- [3. Côté hôte](#3-côté-hôte)
- [4. Côté serveur](#4-côté-serveur)
- [5. Le wiki](#5-le-wiki)
- [6. Les trois agents](#6-les-trois-agents)
- [7. Sécurité](#7-sécurité)
- [8. Reconstruction](#8-reconstruction)
- [9. Registre des décisions](#9-registre-des-décisions)
- [10. Ce qui est vérifié, ce qui ne l'est pas](#10-ce-qui-est-vérifié-ce-qui-ne-lest-pas)
- [11. Limites connues et suites possibles](#11-limites-connues-et-suites-possibles)

## 1. Vue d'ensemble

```
 MACHINES DE TRAVAIL (hôtes)                    SERVEUR (VPS, toujours allumé)
┌──────────────────────────────┐              ┌──────────────────────────────────────┐
│ Claude Code, Codex           │              │ porte SSH (memex-gate)               │
│   │ transcripts locaux       │   condensés  │   │                                  │
│   ▼                          │ ───────────► │   ▼                                  │
│ balayeur (sans LLM)          │     SSH      │ raw/inbox ──► archiviste ──► tisseur │
│   masque les secrets         │              │                 │                    │
│                              │              │                 ▼                    │
│ copie du wiki (lecture)      │ ◄─────────── │ wiki (dépôt git) ◄── vérificateur    │
│   ▲            │             │  git fetch   │   │        ▲          (sur accord)   │
│   │            ▼             │              │   ▼        │                         │
│ hook de démarrage : la page  │              │ vault ◄──► LiveSync ◄──► Obsidian    │
│ du projet entre en contexte  │              │                                      │
└──────────────────────────────┘              │ Hermes : rédige, notifie, converse   │
                                              └──────────────────────────────────────┘
```

Deux règles tiennent l'ensemble :

- **Les connexions partent toujours de l'hôte.** Le serveur ne contacte jamais une machine de travail.
- **Le modèle rédige, le code tient les comptes.** Un agent n'écrit que des pages, dans une copie
  jetable. L'index, le journal, la provenance, les commits et les contrôles sont déterministes.

## 2. Le trajet d'une information

1. Je travaille avec Claude Code ou Codex. La session s'écrit dans un transcript local.
2. À la fin de la session (hook `SessionEnd` de Claude Code), ou après dix minutes d'inactivité, le
   **balayeur** en extrait un **condensé** : mes messages et les réponses finales de l'agent, sans
   les sorties d'outils, secrets masqués.
3. Le condensé part au serveur par SSH. Son arrivée réveille le pipeline.
4. L'**archiviste** en extrait ce qui est durable (décisions, pistes abandonnées, problèmes, faits,
   préférences, état), puis l'intègre au wiki dans une copie de travail. Le résultat est contrôlé,
   commité, fusionné.
5. Le **tisseur** relie les pages touchées et tient les pages transverses.
6. Le wiki est exporté dans le dossier du vault ; LiveSync le pousse vers mes appareils.
7. Au prochain démarrage d'une session dans ce projet, le **hook de démarrage** injecte la page du
   projet dans le contexte de l'agent.

Entre la fin d'une session et la mise à jour du wiki, il s'écoule quelques minutes.

## 3. Côté hôte

Un seul programme, `memex`, en Python sans dépendance. Installé par `host/install.sh`.

### Le balayeur

| Source | Emplacement lu | Ce qui est gardé |
|---|---|---|
| Claude Code | `~/.claude/projects/<projet>/<session>.jsonl` | Paroles humaines (`origin.kind = human`), textes de l'agent |
| Codex | `~/.codex/sessions/**/rollout-*.jsonl` | Événements `item_completed` : `UserMessage`, `AgentMessage` |
| Hermes | `~/.hermes/state.db` (SQLite, lecture seule) | Messages `user` et `assistant` |

- **Incrémental.** Un curseur par session (octets lus, ou dernier message). Un second passage sur
  tout mon historique prend dix millisecondes.
- **Segments immuables.** Chaque passage sur une session produit un nouveau fichier, numéroté, qui
  ne contient que la suite. Un condensé n'est jamais réécrit : il peut donc être envoyé, archivé et
  rejoué sans ambiguïté.
- **Ce qui est écarté.** Sorties d'outils, réflexion interne, sous-agents, messages injectés par le
  harnais (rappels système, résumés de compaction, notifications), et la narration intermédiaire
  courte (« je regarde le fichier… »). Les réponses finales sont toujours gardées.
- **Masquage.** Clés d'API, jetons, mots de passe dans une URL ou une affectation, clés privées,
  IBAN, numéros de carte. Par motifs connus : mieux vaut masquer un peu trop.
- **Exclusions.** Par chemin, par dépôt distant, par fichier `.memex-ignore`, ou en écrivant
  `#memex-off` dans une session.
- **Rattachement au projet.** Par l'adresse distante du dépôt git. Un worktree, même supprimé
  depuis, retombe sur le même projet.

### La liaison

`memex sync` balaye, envoie, puis met à jour la copie locale du wiki. Il est lancé par :

- le hook `SessionEnd` de Claude Code, pour la session qui vient de finir ;
- le hook `SessionStart` (Claude Code et Codex), en arrière-plan, sans faire attendre la session ;
- une minuterie systemd utilisateur, toutes les dix minutes.

Sans réseau, les condensés attendent dans `outbox/`. Une fois envoyés, ils sont conservés dans
`sent/` : c'est la sauvegarde qui permet de reconstruire le serveur.

### La lecture par les agents

- **Un pointeur** dans `~/.claude/CLAUDE.md` et `~/.codex/AGENTS.md` : où est le wiki, quand le
  consulter, ne jamais y écrire.
- **Une injection au démarrage** : si la session s'ouvre dans un projet connu du wiki, sa page
  vivante est imprimée par le hook et entre dans le contexte (6 000 caractères au plus).
- **Une règle de lecture** dans les permissions de Claude Code, pour le dossier du wiki.

Les agents des hôtes ne font que lire. Ce qu'ils apprennent revient par les condensés. Pour
consigner quelque chose sans attendre : `memex note "…"`.

## 4. Côté serveur

Installé par `server/install.sh`, sous un compte système dédié, `memex`, dont le répertoire
personnel est `MEMEX_HOME` (par défaut `/srv/memex`).

```
/srv/memex/
  raw/inbox/<appareil>/     condensés reçus, à traiter
  raw/archive/<appareil>/   condensés traités (les sources que citent les pages)
  raw/quarantine/           condensés en échec répété
  raw/extracts/             résultat de l'extraction, gardé pour ne pas la refaire
  wiki/                     dépôt git : la vérité du wiki
  work/                     copies de travail jetables des agents
  vault/                    dossier synchronisé par LiveSync (facultatif)
  state/  logs/  backups/
/opt/memex/app/             le programme, propriété de root
/etc/memex/memex.env        la configuration
```

### La porte

Chaque machine a sa clé SSH, enregistrée avec une commande forcée :

```
restrict,command="/usr/local/bin/memex-gate --forced --device portable" ssh-ed25519 AAAA…
```

Cette clé ne permet que trois choses : déposer des condensés dans la boîte de **son** appareil,
cloner le wiki en lecture, récupérer la dernière sauvegarde. Pas de shell, pas d'autre dépôt, pas
d'écriture dans le wiki. Le nom de l'appareil vient de la clé, jamais de ce que dit le client.

### Le pipeline

`memex server run` est lancé par systemd : à l'arrivée d'un condensé (`memex-run.path`), et toutes
les quinze minutes par sécurité (`memex-run.timer`). Un verrou garantit un seul rédacteur.

Pour chaque condensé, dans l'ordre chronologique :

1. **Tri sans modèle.** Une session où j'ai écrit moins de 80 caractères ne coûte aucun appel : une
   ligne au journal.
2. **Extraction.** Un appel sans outil, qui renvoie du JSON : un résumé et une liste d'éléments
   typés. Liste vide : une ligne au journal, et c'est tout.
3. **Intégration.** Un appel avec lecture et écriture de fichiers, dans une copie de travail git
   créée pour l'occasion. L'agent reçoit les conventions, les éléments, la liste des pages et le
   dossier où ranger.
4. **Contrôle.** Le diff est vérifié contre le périmètre du rôle (voir §7). En cas de refus, un
   second essai reçoit la liste des fautes. Après trois passages en échec, le condensé part en
   quarantaine et je suis prévenu. Si c'est l'agent lui-même qui ne répond plus (quota atteint,
   réseau coupé), le passage s'arrête sans compter d'échec : voir D39.
5. **Comptabilité.** Le code ajoute la source et la date dans l'en-tête des pages touchées, écrit
   la ligne du journal, le registre des condensés traités, régénère l'index, complète `log.md`.
6. **Commit et fusion.** Un commit par condensé, fusionné en avance rapide dans `main`.
7. **Export** vers le vault.

Puis le tisseur passe, si quelque chose a changé.

### Le harnais des agents

Le pipeline ne dépend d'aucun harnais : il écrit une invite dans un fichier et lance une commande
shell. Trois préréglages (`MEMEX_AGENT=hermes|claude|codex`), trois niveaux de droits :

| Niveau | Sert à | Hermes | Claude Code | Codex |
|---|---|---|---|---|
| `extract` | Extraction | `-t todo -z` | `-p --tools ""` | `exec --sandbox read-only` |
| `read` | Diagnostic du vérificateur | `-t file -z` | `-p --tools "Read,Glob,Grep"` | `exec --sandbox read-only` |
| `edit` | Intégration, tisseur, actions | `-t file --yolo -z` | `-p --tools "Read,Write,Edit,Glob,Grep" --permission-mode acceptEdits` | `exec --sandbox workspace-write` |

N'importe quelle autre commande convient (`MEMEX_AGENT_*_CMD`).

### Le vault

`vault/` est un dossier ordinaire, sans rien de git ni de caché. Un manifeste garde l'empreinte de
chaque fichier au dernier échange, ce qui dit qui a changé quoi :

- le vault diffère du manifeste : je l'ai modifié dans Obsidian, c'est importé et commité sous
  l'auteur « humain » ;
- le wiki diffère du vault : un agent l'a modifié, c'est exporté ;
- les deux ont changé : ma version gagne.

Si le manifeste est perdu, le wiki fait foi : un vieux vault ne peut pas l'écraser.

### Hermes

Hermes a trois rôles, tous remplaçables :

- **rédacteur** : c'est le harnais par défaut des agents ;
- **messager** : `MEMEX_NOTIFY_CMD='hermes send --to telegram …'` ;
- **interlocuteur** : un skill (`server/hermes/skills/memex`) lui apprend à répondre aux revues
  (« ok sauf la 2 »), à lire le wiki, à restaurer une page, à consigner une note.

Ses propres discussions (messagerie) sont capturées sur le serveur comme n'importe quelle session.

## 5. Le wiki

```
Projets/<Nom>/<Nom>.md                    page vivante d'un projet de code
Projets/<Nom>/Décisions/<date> <titre>.md fiches de décision
Sujets/<Nom>/<Nom>.md                     page vivante d'un sujet hors code
Concepts/<Nom>.md                         pages transverses, tenues par le tisseur
Journal/<année>/<date>.md                 une ligne par session        (code)
index.md   log.md                         catalogue, journal des opérations (code)
AGENTS.md                                 les conventions (moi seul)
.memex/                                   registre, correspondance projet → dossier, refus
```

- **Page vivante.** Elle dit ce qui est vrai aujourd'hui : en bref, grandes lignes, décisions,
  pistes écartées, problèmes connus, questions ouvertes. On la réécrit ; on n'y empile pas.
- **Fiche de décision.** Contexte, options, décision, pourquoi, conséquences. Jamais réécrite. Un
  revirement crée une nouvelle fiche ; l'ancienne passe à `statut: remplacee`.
- **Provenance.** Chaque page porte dans son en-tête la liste des condensés qui l'ont nourrie.
- **Conventions.** `AGENTS.md` est injecté dans chaque invite. Le modifier, c'est changer le
  comportement des trois agents.

## 6. Les trois agents

| | Archiviste | Tisseur | Vérificateur |
|---|---|---|---|
| Rôle | Intègre ce qu'apportent les sessions | Relie les pages, tient `Concepts/` | Resserre : doublons, périmé, pages trop longues |
| Quand | À chaque condensé | Après l'archiviste, si des pistes existent | Chaque semaine |
| Risque | Additif | Additif | Destructif |
| Application | Directe | Directe | **Après mon accord** |
| Peut | Ajouter, compléter | Entourer de `[[…]]`, section « Voir aussi », pages de concept | Modifier, fusionner, supprimer |
| Ne peut pas | Supprimer, renommer, toucher une fiche existante, écrire dans `Concepts/` | Changer un mot du texte, toucher une fiche | Toucher une fiche de décision |

**Le tisseur** part d'un contrôle sans modèle : titres de pages cités sans lien, entités citées
dans au moins deux dossiers sans page à elles, liens cassés. Le modèle tranche les homonymes et
rédige la page transverse. Il ne se réveille pas tant que le wiki a moins de douze pages, ni si
les pistes sont les mêmes qu'au passage précédent.

**Le vérificateur** fait un diagnostic en lecture seule, puis applique chaque action dans un commit
séparé, sur une branche de revue. Je reçois la liste ; je réponds « ok », « ok sauf la 2 » ou
« non ». Approuver reporte les commits choisis sur `main`. Une action refusée est notée et n'est
plus reproposée. Une revue sans réponse expire au bout de dix jours. Une page supprimée reste dans
l'historique : `memex server restore "Chemin/Page.md"`.

## 7. Sécurité

| Menace | Parade |
|---|---|
| Un secret part dans un condensé | Masquage sur l'hôte, avant tout envoi. Fichiers en mode 600 |
| Une session de travail est capturée | Exclusions par chemin et par dépôt ; `.memex-ignore` ; `#memex-off` |
| Une clé d'hôte est volée | Elle ne donne que la porte : dépôt de condensés, lecture du wiki |
| Un condensé forgé vise un autre dossier | L'appareil est imposé par la boîte de dépôt ; aucun champ de l'en-tête ne forme un chemin |
| Le serveur est compromis | Il ne peut ouvrir aucune connexion vers mes machines (sens unique, règles Tailscale) |
| Un condensé contient une instruction malveillante | L'extraction n'a aucun outil. L'intégration n'écrit que dans sa copie de travail. Le diff est contrôlé |
| Un agent écrit ailleurs que prévu | Bac à sable par appel : système en lecture seule, `~/.ssh` masqué, écriture limitée à la copie de travail et à l'état du harnais |
| Un agent s'ajoute une clé SSH | `authorized_keys` appartient à root |
| Un agent modifie le pipeline | Le code est dans `/opt/memex`, propriété de root |
| Un agent atteint les autres services du serveur | Compte sans sudo ; unité systemd cloisonnée ; `--deny /var/www` rend ces dossiers invisibles |
| Un agent détruit du contenu | Garde-fous par rôle sur le diff ; historique git ; sauvegardes quotidiennes |
| Le serveur lit mes notes personnelles | Vault séparé pour le wiki, avec sa propre phrase secrète |

Ce qui reste hors de portée de ces parades : un agent peut **lire** tout ce que le compte `memex`
peut lire (condensés, wiki, sa propre configuration) et dispose du réseau pour joindre son modèle.
Une injection réussie pourrait donc faire sortir du contenu du wiki. C'est la raison de ne pas y
mettre de secrets, et de garder mes notes personnelles dans un autre vault.

## 8. Reconstruction

Rien n'existe en un seul exemplaire :

| Donnée | Où elle est aussi |
|---|---|
| Le programme | Ce dépôt, sur chaque machine où il est cloné |
| Le wiki et son historique | Clone complet sur chaque hôte ; sauvegarde quotidienne |
| Les condensés | `sent/` sur l'hôte qui les a produits ; sauvegarde quotidienne |
| Les sauvegardes | Rapatriées chaque jour par les hôtes |

Trois chemins, décrits pas à pas dans le [guide d'exploitation](RUNBOOK.md) :

- **Nouvelle machine de travail** : cloner ce dépôt, `host/install.sh --server …`, autoriser la clé.
- **Nouveau serveur, avec une sauvegarde** : `server/deploy.sh`, puis `memex server restore-backup`.
- **Nouveau serveur, sans rien** : `server/deploy.sh`, puis `memex resend && memex ship` sur chaque
  hôte. Le wiki est reconstruit à partir des condensés.

Aucun chemin, nom de machine ou adresse n'est écrit dans le code : tout est dans `memex.env`.

## 9. Registre des décisions

Chaque entrée : la décision, pourquoi, et ce qui a été écarté.

### Fondations

**D1 — Un wiki entretenu par des agents, sur le modèle « LLM wiki » de Karpathy.**
Trois couches : sources brutes immuables, wiki écrit par le modèle, fichier de conventions.
*Pourquoi :* la connaissance est compilée une fois et tenue à jour, au lieu d'être redécouverte à
chaque question. *Écarté :* une base vectorielle interrogée à la demande.

**D2 — L'archive et le wiki sont deux choses.**
Toutes les sessions deviennent des condensés ; le wiki n'en retient qu'une petite part.
*Pourquoi :* la plupart des sessions ne contiennent rien de durable, et un wiki qui grossit à
chaque session cesse d'être lu.

**D3 — Du markdown, lu dans Obsidian ; pas de base de données.**
*Écarté :* claude-mem (SQLite et base vectorielle par machine, illisible dans Obsidian) ;
basic-memory (n'écrit que quand l'agent décide de noter) ; llmwiki en entier (structure générique,
pensé pour une seule machine).

**D4 — Python, bibliothèque standard seulement.**
*Pourquoi :* s'installe partout où il y a Python 3.9 et git, sans environnement virtuel ni paquet.

### Capture

**D5 — Un balayeur sans modèle sur chaque machine, plutôt qu'un agent à la fin de chaque session.**
*Pourquoi :* même mécanisme pour tous les harnais, rattrapage des sessions interrompues, reprise
de l'historique existant, aucun coût. Les hooks ne servent qu'à le déclencher plus tôt.

**D6 — Des segments immuables, un curseur par session.**
*Pourquoi :* un fichier jamais modifié se transporte, se déduplique et se rejoue sans risque.
*Écarté :* un condensé par session, réécrit à chaque passage, qui obligeait le serveur à calculer
ce qui avait changé.

**D7 — Garder mes messages et les réponses finales ; écarter outils et narration.**
*Mesuré :* mes 181 Mo de transcripts donnent 660 Ko de condensés.

**D8 — Masquer les secrets sur la machine, par motifs.**
*Pourquoi :* rien de sensible ne doit quitter l'hôte, quoi qu'il arrive ensuite.
*Écarté :* un outil externe (gitleaks) comme dépendance obligatoire.

**D9 — Exclusions explicites ; aucune session de travail par défaut dans l'exemple.**
Par chemin, par dépôt, par fichier marqueur, par marqueur dans la session.

**D10 — L'identité d'un projet est son dépôt distant.**
*Pourquoi :* stable d'une machine à l'autre et à travers les worktrees. Repli sur le nom du dossier.

**D11 — `--no-backfill` à l'installation.**
*Pourquoi :* reprendre tout l'historique coûte un appel par session ; sur une nouvelle machine, on
peut vouloir ne capturer que la suite.

### Transport et serveur

**D12 — Le serveur est le centre ; pas de GitHub.**
*Pourquoi :* le wiki contient des discussions personnelles ; avec un serveur à moi, il ne sort pas
de chez moi. Les clones des hôtes servent de sauvegarde. *Écarté :* un dépôt privé GitHub comme
pivot, et les tâches planifiées dans le cloud.

**D13 — Sens unique : l'hôte appelle, le serveur ne rappelle jamais.**
*Pourquoi :* un serveur exposé ne doit pas avoir de chemin vers mon poste.
*Écarté :* qu'Hermes pilote des agents sur ma machine.

**D14 — Une porte SSH à commande forcée, plutôt que `rsync`.**
Changement par rapport à ce qui était envisagé (`rsync`). Une archive passée à une commande forcée
est meilleure sur quatre points : clé limitée à trois verbes, aucun outil à installer, dépôt
atomique, et pipeline réveillé dans la foulée.

**D15 — Git pour le wiki seulement.**
*Pourquoi :* annuler un passage entier, proposer avant d'appliquer (branche du vérificateur),
servir les hôtes quand Obsidian est fermé. L'historique de LiveSync existe, mais il est par
fichier et ne survit pas à une reconstruction de sa base. *Écarté :* git pour les condensés, et le
dossier `_archive/` (l'historique suffit ; `log.md` garde la trace des suppressions).

**D16 — Planification par systemd, pas par le planificateur d'Hermes.**
Changement par rapport à ce qui était envisagé. *Pourquoi :* le pipeline continue de tourner si la
passerelle d'Hermes est arrêtée, et il ne dépend d'aucun harnais. Hermes reste le rédacteur et
l'interlocuteur.

**D17 — Le harnais est une commande shell configurable.**
*Pourquoi :* ne dépendre ni d'Hermes, ni de Claude, ni de Codex. Par défaut Hermes, donc mon
abonnement Codex, sans jeton Claude sur le serveur.

**D18 — Une configuration unique, en variables.**
Même fichier et même format sur l'hôte et le serveur. Installeurs rejouables. Mode sans root, et
mode « tout sur une machine » (`MEMEX_SERVER_SSH=local`) pour essayer sans serveur.

### Pipeline

**D19 — Les agents ne travaillent jamais dans le wiki vivant.**
Copie de travail git jetable, contrôle, puis fusion en avance rapide. *Pourquoi :* un état à moitié
écrit n'atteint jamais Obsidian, et un passage raté se jette sans trace.

**D20 — Le modèle rédige, le code tient les comptes.**
Index, journal, registre, provenance et `log.md` sont générés. *Pourquoi :* c'est ce qu'un modèle
fait le moins fiablement, et ce qu'un programme fait sans erreur. Quand le code complète l'en-tête
d'une page, il ne remplace que ses propres champs : le reste, écrit à la main, est conservé tel quel.

**D21 — L'archiviste en deux temps.**
Extraction sans outil, puis intégration. *Pourquoi :* la plupart des sessions s'arrêtent à
l'extraction, qui est bon marché ; l'intégration reçoit une liste courte au lieu d'un transcript ;
et le texte venu de l'extérieur ne rencontre jamais un agent doté d'outils.

**D22 — Les garde-fous sont vérifiés sur le diff.**
Corriger d'office ce qui est cosmétique, annuler ce qui est hors périmètre, refuser ce qui est
destructif. Un second essai reçoit les fautes ; puis quarantaine. Toute erreur interne pendant le
traitement d'un condensé compte comme un échec : un condensé ne peut pas bloquer les suivants.

**D39 — Une panne de l'agent ne coûte aucun condensé.**
Quand un appel à l'agent échoue, une sonde lui demande un mot. S'il répond, l'échec tient au
condensé et compte. S'il ne répond pas, le passage s'arrête : aucun échec n'est compté, la file
reste intacte, je suis prévenu une fois, et le passage suivant réessaie. De même, un agent qui
rend la main sans page ni compte rendu est un échec, pas une session « sans suite ».
*Pourquoi :* sans cela, un quota atteint pendant une reprise d'historique envoyait toute la file
en quarantaine en trois passages, soit 45 minutes. Trouvé en relisant le code avant la première
reprise réelle, pas en production.

**D40 — Trois réglages tirés de la première reprise réelle (66 sessions, 2026-11-16).**
- *L'en-tête d'une page ne garde que ses cinq dernières sources.* Une page vivante touchée par 27
  sessions traînait 27 lignes de provenance ; la liste complète reste dans le registre et dans git.
- *Le tisseur reçoit cinq concepts candidats, les plus partagés d'abord, et doit lire leurs pages.*
  Avec huit candidats par ordre alphabétique et la consigne « ne rien faire est acceptable », il
  n'avait créé qu'une page transverse pour 16 candidats, sans lire les pages citées.
- *Une page vivante déjà longue ne grossit plus.* La liste des pages la signale à l'archiviste, qui
  range le détail dans une page complémentaire par thème. Une page fourre-tout avait atteint
  30 000 caractères.

**D23 — Un rédacteur à la fois.**
Un verrou unique couvre l'archiviste, le tisseur, le vérificateur et les approbations.

### Structure du wiki

**D24 — Des dossiers : projets, sujets, concepts.**
Une page vivante par dossier, toujours dans un répertoire à son nom. *Écarté :* une hiérarchie de
thèmes, qui complique le rangement sans rien apporter que les concepts ne fassent.

**D25 — Les fiches de décision sont immuables.**
*Pourquoi :* c'est l'historique qui explique pourquoi on en est là.

**D26 — Trois agents.**
L'archiviste ajoute, le tisseur relie, le vérificateur resserre : trois risques, trois cadences,
trois périmètres. Le tisseur est séparé parce qu'il lui faut une vue d'ensemble que l'archiviste
n'a pas.

**D27 — Le vérificateur propose ; j'approuve.**
Par messagerie, action par action. Les refus sont mémorisés.

**D28 — En français.**
Contenu, conventions et invites. Les noms de dossiers suivent.

### Lecture et synchronisation

**D29 — Les agents lisent les fichiers ; pas de recherche Obsidian ni de MCP.**
*Pourquoi :* la CLI d'Obsidian et les serveurs MCP exigent l'application ouverte. `qmd` reste
possible plus tard.

**D30 — Hook de démarrage instantané, synchronisation en arrière-plan.**
*Pourquoi :* ouvrir une session ne doit jamais attendre le réseau.

**D31 — Le vault est un dossier ordinaire, relié au dépôt par un manifeste.**
*Pourquoi :* aucun fichier git ne doit passer par LiveSync, et mes modifications dans Obsidian
doivent revenir. En cas de conflit, je gagne.

**D32 — Un vault Obsidian séparé pour le wiki (recommandé ; le choix reste ouvert).**
*Pourquoi :* ma synchronisation est chiffrée de bout en bout ; donner la phrase secrète du vault
principal au serveur lui ouvrirait toutes mes notes. *Coût :* pas de lien direct vers elles.
memex n'impose rien : il écrit dans `MEMEX_VAULT_DIR`, quel que soit le vault que ce dossier sert.

### Sécurité et exploitation

**D33 — Un bac à sable par appel d'agent.**
Ajouté en cours de route : à l'essai, Hermes a écrit dans mon répertoire personnel au lieu de son
dossier de travail. Bubblewrap, avec repli si indisponible (`MEMEX_SANDBOX=bwrap` pour l'exiger).

**D34 — Compte dédié, code et clés à root, unités cloisonnées.**

**D35 — Sauvegarde quotidienne, rapatriée par les hôtes.**

**D36 — Les sessions du pipeline ne se capturent pas elles-mêmes.**
Marqueur en tête de chaque invite, et options « sans persistance » de Claude Code et Codex.

**D37 — Import ChatGPT et claude.ai : fourni, expérimental.**
Écrit d'après la structure connue des exports, sans export réel sous la main. Le connecteur MCP
pour écrire dans le wiki depuis ces applications n'est pas construit.

**D38 — Le nom : memex.**
Le « memex » de Vannevar Bush (1945), que cite Karpathy : une mémoire personnelle dont les liens
comptent autant que les documents.

## 10. Ce qui est vérifié, ce qui ne l'est pas

**Vérifié sur cette machine (2026-11-16) :**

- 73 tests automatiques, dont 24 scénarios de bout en bout (trajet nominal, reprises, garde-fous,
  quarantaine, panne de l'agent, revue, restauration, reconstruction, condensé forgé).
- Une relecture indépendante du code par un second agent, qui a reproduit cinq défauts (dont un
  condensé forgé capable d'écrire hors de sa boîte). Tous sont corrigés et couverts par un test.
- Le balayeur sur mon historique réel : 66 condensés depuis 69 sessions, en 1,2 seconde.
- Le pipeline complet avec un vrai modèle (Claude, via `claude -p`) sur six sessions réelles : six
  intégrations, aucun échec, quatre pages de concept créées par le tisseur.
- Le même pipeline avec Hermes comme rédacteur (son modèle par défaut), dans le bac à sable, sur
  quatre sessions réelles : trois intégrations, une session jugée sans suite, aucun échec.
- Le vérificateur avec un vrai modèle, dans le bac à sable : deux actions proposées, une approuvée
  et appliquée, une refusée et mémorisée.
- Les trois harnais (Hermes, Claude Code, Codex), aux trois niveaux de droits, dans le bac à sable.
- L'exclusion des sessions du pipeline, sur la base réelle d'Hermes.
- La porte SSH face à un vrai `sshd` : shell, commande arbitraire, autre dépôt et écriture refusés.
- Les deux installeurs, dans un répertoire personnel jetable ; les unités avec `systemd-analyze`.

**Non vérifié, faute d'accès :**

- L'installation réelle sur le serveur : mode système, unités cloisonnées, et surtout **le bac à
  sable à l'intérieur de l'unité systemd**. `memex server status` dit s'il est actif.
- Le client LiveSync sans écran, Tailscale, la passerelle de messagerie d'Hermes et son skill.
- Le pipeline complet avec Codex comme rédacteur (seuls ses appels unitaires l'ont été).
- L'injection du hook `SessionStart` dans Codex (un rapport de bogue existe sur une version).
- Le chemin sans systemd (cron), macOS, et les formats réels des exports ChatGPT et claude.ai.

## 11. Limites connues et suites possibles

- **Sessions dans le cloud** (claude.ai/code lancé depuis le web) : pas de transcript local, donc
  pas de capture. Les sessions pilotées à distance mais exécutées sur une machine le sont.
- **ChatGPT et Claude web** : par export manuel seulement.
- **Une session très longue** est découpée en plusieurs condensés, traités séparément : un élément
  à cheval sur deux morceaux peut être vu deux fois. L'archiviste cherche l'existant avant d'écrire.
- **Coût** : un à deux appels par session non triviale. La reprise d'un long historique se fait par
  lots de 25.
- **Suites** : connecteur MCP du wiki pour Claude web ; recherche sémantique (`qmd`) quand le wiki
  dépassera quelques centaines de pages ; page d'architecture amorcée depuis le dépôt de chaque projet.
