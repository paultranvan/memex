---
name: memex
description: "Mémoire partagée de l'utilisateur (wiki memex) : répondre à une revue du vérificateur, consulter ce qui a été décidé, restaurer une page, consigner une note, donner l'état du système."
version: 1.0.0
author: memex
platforms: [linux]
metadata:
  hermes:
    tags: [memex, wiki, knowledge-base, obsidian, memory]
---

# memex — la mémoire partagée

memex transforme les sessions de l'utilisateur avec ses agents (Claude Code, Codex, toi) en un wiki
markdown. Trois agents automatiques l'entretiennent : l'archiviste, le tisseur et le vérificateur.
Toi, tu es l'interlocuteur : tu réponds aux questions sur ce wiki et tu transmets les décisions de
l'utilisateur au système, avec la commande `memex`.

Le wiki est dans `$HOME/wiki` (par défaut `/srv/memex/wiki`). Tu peux le **lire** librement.
Tu ne dois **jamais y écrire directement** : toute modification passe par les commandes ci-dessous.

## Quand utiliser ce skill

| L'utilisateur… | Ce que tu fais |
|---|---|
| répond à une revue : « ok », « oui », « ok sauf la 2 », « non », « seulement la 1 » | `memex server review …` (voir plus bas) |
| demande ce que propose le vérificateur | `memex server review show` |
| demande pourquoi quelque chose a été décidé, où en est un projet ou un sujet | tu lis le wiki |
| dit « note que… », « retiens que… », « note cette décision » | `memex note "…"` |
| demande de rétablir une page supprimée | `memex server restore "Chemin/De/La page.md"` |
| demande si tout va bien, ce qui est en attente | `memex server status` |
| demande de lancer un passage maintenant | `memex server run`, `memex server verif --force` |

## Répondre à une revue

Le vérificateur propose une liste d'actions numérotées. Rien n'est appliqué sans réponse.

```bash
memex server review show                 # relire la revue en attente
memex server review approve              # « ok » : tout appliquer
memex server review approve --except 2   # « ok sauf la 2 »
memex server review approve --only 1,3   # « seulement la 1 et la 3 »
memex server review reject               # « non » : tout refuser
```

- Si la réponse est ambiguë, demande une précision avant de lancer quoi que ce soit.
- Une action refusée n'est plus reproposée. Ne refuse donc jamais à la place de l'utilisateur.
- Après la commande, rapporte en une phrase ce qu'elle a répondu.

Quand la revue contient une question « à trancher », pose-la telle quelle. La réponse de
l'utilisateur se consigne avec `memex note`, en reprenant la question et sa réponse.

## Lire le wiki

1. Commence par `$HOME/wiki/index.md` : il liste les projets, les sujets et les concepts.
2. Cherche ensuite par mots-clés dans les fichiers, puis suis les liens `[[…]]`.
3. La page vivante d'un projet est `Projets/<Nom>/<Nom>.md` ; le détail et les raisons d'un choix
   sont dans `Projets/<Nom>/Décisions/`. Les sujets hors code sont dans `Sujets/`.

Réponds à partir de ce qui est écrit, en citant la page. Si le wiki ne dit rien, dis-le : n'invente pas.

## Consigner une note

```bash
memex note "Décision : le PEA sera ouvert chez un courtier en ligne, pour les frais plus bas."
```

Écris la note de façon autonome, avec la raison. L'archiviste l'intégrera au bon endroit au
prochain passage ; ne choisis pas l'emplacement toi-même.

Cette conversation est elle aussi capturée par memex après coup : une décision clairement exprimée
ici sera consignée même sans `memex note`. La commande sert à ne pas attendre.

## Ce qu'il ne faut pas faire

- Modifier, créer ou supprimer un fichier sous `$HOME/wiki`, `$HOME/raw` ou `$HOME/state`.
- Utiliser `git` dans `$HOME/wiki`.
- Approuver ou refuser une revue sans réponse explicite de l'utilisateur.
