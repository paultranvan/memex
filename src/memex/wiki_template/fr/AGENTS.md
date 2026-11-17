---
type: meta
titre: Conventions du wiki
---

# Conventions du wiki

Ce wiki est la mémoire durable de mes sessions avec des agents (Claude Code, Codex, Hermes…).
Il est écrit par des agents, lu par eux et par moi dans Obsidian. Ce fichier en fixe les règles.
Je peux le modifier à la main ; les agents, non.

## À quoi il sert

- Retrouver l'état d'un projet ou d'un sujet, et **pourquoi** on en est là.
- Ne pas rouvrir une question déjà tranchée, ni retenter une piste déjà abandonnée.

Ce n'est ni un journal exhaustif, ni une copie du code : ce qui se lit directement dans le dépôt
d'un projet n'a pas sa place ici.

## Structure

| Emplacement | Contenu |
|---|---|
| `Projets/<Nom>/<Nom>.md` | Page vivante d'un projet de code |
| `Sujets/<Nom>/<Nom>.md` | Page vivante d'un sujet hors code (par exemple « PEA ») |
| `<dossier>/Décisions/<AAAA-MM-JJ> <titre>.md` | Fiches de décision |
| `<dossier>/<Autre page>.md` | Page complémentaire (comparatif, synthèse) quand la page vivante devient trop longue |
| `Concepts/<Nom>.md` | Pages transverses : un outil, une technique, un thème commun à plusieurs dossiers |
| `Journal/`, `index.md`, `log.md` | Tenus automatiquement : ne pas y écrire |

Un dossier est toujours un répertoire qui porte le nom de sa page vivante.

## En-tête de page

```yaml
type: projet | sujet | decision | concept | synthese
titre: Titre lisible
resume: Une phrase, reprise telle quelle dans l'index.
entites:
  - Outils, services, techniques ou thèmes durables qu'un autre dossier pourrait citer (une douzaine au plus)
```

Pour une fiche de décision, s'ajoutent `date` (AAAA-MM-JJ), `statut` (`active`, `remplacee` ou
`abandonnee`) et, le cas échéant, `remplace` / `remplacee_par` (un lien entre guillemets).
Les champs `sources` (les cinq dernières sessions qui ont touché la page) et `maj` sont remplis
automatiquement. La provenance complète est dans `.memex/ledger.jsonl` et dans l'historique git.

## Page vivante

Elle dit ce qui est vrai **aujourd'hui**. On la réécrit quand la réalité change ; on n'y empile pas
l'historique. Sections recommandées, à omettre si elles seraient vides :

- `## En bref` — le but et l'état actuel, en trois à six lignes.
- `## Grandes lignes` — pour un projet : architecture et choix structurants ; pour un sujet : où on en
  est. Chaque choix structurant renvoie à sa fiche de décision.
- `## Décisions` — la liste des fiches, la plus récente en premier : lien, puis une ligne.
- `## Pistes écartées` — ce qui a été essayé ou envisagé puis abandonné, avec la raison.
- `## Problèmes connus` — les pièges non évidents, avec leur cause et leur solution.
- `## Questions ouvertes` — ce qui reste à trancher ou à faire.

## Fiche de décision

Une fiche décrit un choix qui engage la suite. Elle n'est **jamais réécrite**. Si la décision change,
on crée une nouvelle fiche ; l'ancienne reste, avec `statut: remplacee` et `remplacee_par`.

```markdown
## Contexte
## Options envisagées
## Décision
## Pourquoi
## Conséquences
```

Un petit choix sans enjeu ne mérite pas de fiche : une ligne dans la page vivante suffit.

## Où ranger ce qui n'a pas de projet

- Une préférence de travail valable partout → `Sujets/Préférences de travail/`.
- Un fait sur mes machines, mes outils ou mes services → `Sujets/Environnement/`.
- Tout le reste → le sujet existant le plus proche ; à défaut, un nouveau sujet au nom court et stable.

## Écriture

- En français, en phrases complètes, sur un ton factuel. Jamais « l'utilisateur », jamais « dans cette
  session » : le lecteur n'a pas vu la session.
- Concis : une information à un seul endroit. Avant d'ajouter, chercher si c'est déjà écrit, et
  compléter plutôt que répéter.
- Dates absolues (`2026-11-12`), jamais « hier » ou « la semaine dernière ».
- Pas de détail volatil : ni identifiant de commit, ni numéro de ligne, ni nom de branche temporaire.
- Noms de fichiers simples : pas de `:`, `/`, `?`, `*`, `"`, `<`, `>`, `|`, `#` (certains appareils les refusent).
- Liens Obsidian `[[Dossier/Page|libellé]]` vers les pages citées, une seule fois par page pour une
  même cible.
- Jamais de secret (mot de passe, clé, jeton) ni de donnée personnelle sensible.
- Ce qu'on ne sait pas ne s'écrit pas : aucune extrapolation.

## Les trois rôles

- **L'archiviste** intègre ce que les sessions apportent. Il ajoute et met à jour ; il ne supprime rien.
- **Le tisseur** relie : liens entre pages, pages de `Concepts/`. Il ne change pas le texte des autres pages.
- **Le vérificateur** resserre : doublons, contenu périmé, pages trop longues. Ses changements ne
  s'appliquent qu'après mon accord.
