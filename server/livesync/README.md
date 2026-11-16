# Vault Obsidian sur le serveur (LiveSync sans écran)

memex écrit le wiki dans un dossier ordinaire, `MEMEX_VAULT_DIR`. Pour que ce dossier apparaisse
dans Obsidian sur tes appareils, un client LiveSync en ligne de commande le synchronise avec ta
base CouchDB, comme le ferait le plugin.

> **Non testé.** Cette notice a été écrite sans accès au serveur ni à la base CouchDB. Les commandes
> ci-dessous viennent de la documentation officielle du client
> ([`src/apps/cli/README.md`](https://github.com/vrtmrz/obsidian-livesync/blob/main/src/apps/cli/README.md)),
> lue le 2026-11-16. Vérifie-les sur ta version avant de t'y fier.

## Avant de commencer : un vault séparé

Ta synchronisation LiveSync est chiffrée de bout en bout. Le client du serveur a besoin de la
phrase secrète pour lire et écrire. Avec celle de ton vault principal, le serveur pourrait
déchiffrer toutes tes notes personnelles.

**Recommandation : un vault Obsidian dédié au wiki**, avec sa propre base CouchDB et sa propre
phrase secrète. Sur tes appareils, tu l'ouvres comme un second vault.

1. Crée une base CouchDB vide (par exemple `memex-wiki`) et un utilisateur qui n'a accès qu'à elle.
2. Sur ton poste, crée un vault Obsidian vide, installe le plugin Self-hosted LiveSync, configure-le
   sur cette base avec une **nouvelle** phrase secrète, puis copie son « Setup URI ».

## Installer le client sur le serveur

Le client demande Node.js 22.22 ou plus récent. Debian 12 fournit une version plus ancienne :
installe Node depuis NodeSource, ou utilise l'image Docker.

### Voie 1 : installeur officiel, sous le compte `memex`

```bash
sudo loginctl enable-linger memex          # ses services tournent sans session ouverte
sudo -iu memex
git clone --depth 1 https://github.com/vrtmrz/obsidian-livesync.git ~/livesync-src
cd ~/livesync-src
bash src/apps/cli/deploy/install.sh --user --vault /srv/memex/vault
```

L'installeur compile le client, crée la commande `~/.local/bin/livesync-cli` et un service
utilisateur `livesync-cli.service` qui surveille le dossier et la base en continu.

### Voie 2 : Docker

L'image est `ghcr.io/vrtmrz/livesync-cli`. La base locale du client se monte sur `/data`, le vault
sur `/vault`. Lance le conteneur sous l'identifiant du compte `memex` (`--user "$(id -u memex)"`),
sinon les fichiers écrits ne lui appartiendront pas. La commande exacte du mode continu est dans la
documentation du client, section « Docker ».

## Configurer la connexion

Le client lit `.livesync/settings.json` dans son dossier de base. Deux façons de le remplir :

- appliquer le Setup URI copié depuis ton poste (commande `setup` du client) ;
- ou écrire le fichier à la main, à partir de [`settings.example.json`](settings.example.json).

Protège ce fichier : il contient le mot de passe CouchDB et la phrase secrète.

```bash
chmod 600 /srv/memex/vault/.livesync/settings.json
```

## Brancher memex

Dans `/etc/memex/memex.env` :

```
MEMEX_VAULT_DIR=/srv/memex/vault
```

Puis `sudo systemctl start memex-run.service`. memex exporte le wiki dans le dossier ; le client
l'envoie à CouchDB ; tes appareils le reçoivent.

## Ce qu'il faut savoir

- **Fichiers cachés.** Le client ignore les entrées cachées, donc `.livesync/` et `.obsidian/` ne
  voyagent pas. memex n'écrit rien de caché dans le vault : ni `.git`, ni `.memex`.
- **Modifications depuis Obsidian.** Elles arrivent dans le dossier du vault, et memex les reprend
  dans le wiki à son passage suivant (toutes les 15 minutes au plus), sous l'auteur « humain ».
  Si un agent a modifié la même page entre-temps, c'est ta version qui gagne.
- **`index.md` et `log.md`** sont régénérés par memex : une modification à la main y est écrasée.
- **Sans LiveSync**, memex fonctionne quand même. Tu lis alors le wiki par la copie de tes hôtes
  (`~/.local/share/memex/wiki`), que tu peux ouvrir comme vault Obsidian en lecture.
