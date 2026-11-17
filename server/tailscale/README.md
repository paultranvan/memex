# Private network with Tailscale

memex does not need Tailscale: `MEMEX_SERVER_SSH` accepts any SSH address.
Tailscale lets you stop exposing the server's SSH to the Internet, and guarantees that the server
can never open a connection to your machines.

## Set-up

On the server and on each work machine:

```bash
# Debian / Ubuntu
curl -fsSL https://tailscale.com/install.sh | sh
# Arch
sudo pacman -S tailscale && sudo systemctl enable --now tailscaled

sudo tailscale up
```

On the server, give it the `tag:vps` tag (admin console, or
`sudo tailscale up --advertise-tags=tag:vps`).

On each work machine, refuse every incoming connection over Tailscale:

```bash
sudo tailscale set --shields-up
```

## Access rules

Replace the default rule with the one in [`acl.example.jsonc`](acl.example.jsonc): your devices
can reach the server's SSH, and nothing can originate from the server.

## Closing public SSH

Once `ssh memex@<tailscale-name-of-the-server>` has been checked from one machine, you can restrict
port 22 of the server to the Tailscale interface in its firewall. Keep your hosting provider's
console as a way back in before you do.

## On the memex side

```
MEMEX_SERVER_SSH=memex@<tailscale-name-of-the-server>
```
