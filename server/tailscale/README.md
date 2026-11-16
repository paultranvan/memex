# Réseau privé avec Tailscale

memex n'a pas besoin de Tailscale : `MEMEX_SERVER_SSH` accepte n'importe quelle adresse SSH.
Tailscale sert à ne plus exposer le SSH du serveur sur Internet, et à garantir que le serveur ne
peut jamais ouvrir de connexion vers tes machines.

## Mise en place

Sur le serveur et sur chaque machine de travail :

```bash
# Debian / Ubuntu
curl -fsSL https://tailscale.com/install.sh | sh
# Arch
sudo pacman -S tailscale && sudo systemctl enable --now tailscaled

sudo tailscale up
```

Sur le serveur, donne-lui l'étiquette `tag:vps` (console d'administration, ou
`sudo tailscale up --advertise-tags=tag:vps`).

Sur chaque machine de travail, refuse toute connexion entrante par Tailscale :

```bash
sudo tailscale set --shields-up
```

## Règles d'accès

Remplace la règle par défaut par celle de [`acl.example.jsonc`](acl.example.jsonc) : tes appareils
peuvent joindre le SSH du serveur, et rien ne peut partir du serveur.

## Fermer le SSH public

Une fois `ssh memex@<nom-tailscale-du-vps>` vérifié depuis une machine, tu peux limiter le port 22
du serveur à l'interface Tailscale dans son pare-feu. Garde la console de ton hébergeur comme issue
de secours avant de le faire.

## Côté memex

```
MEMEX_SERVER_SSH=memex@<nom-tailscale-du-vps>
```
