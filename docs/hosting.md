# Hosting: a recording machine and a public mirror

First-person videos need the game, so they are made on a desktop with CS2 and a GPU. The public site does
not: a 1 vCPU / 1 GB server is enough to serve the interface, the API and the videos. demoreel keeps the two
apart on purpose.

```
recording machine (CS2, GPU)                      public mirror (small Linux server)
  demoreel serve: import, analyze,   demoreel       demoreel serve --workers 0
  record, compose, curate            publish  ───>  Caddy: HTTPS, videos from disk
  data/: demos, masters, videos       (SSH)         data/: database, analyses, videos, brand kits
```

The mirror is read-only by design: it has no workers, `DEMOREEL_ADMIN_KEY` is set, and every `publish`
replaces its database with a snapshot of the recording machine's. Curate (feature, edit, delete) on the
recording machine and publish; changes made directly on the mirror are lost at the next publish.

## What `demoreel publish` copies

| Copied | Never copied |
|---|---|
| a snapshot of the database: matches, highlights, finished videos | jobs, webhooks (they carry secrets), unfinished renders |
| `matches/*/analysis.json` | demos (`demos/`) |
| the videos and posters the snapshot lists (`renders/`) | masters (`masters/`), caches, orphaned files |
| brand kits: `brand.json` and images | anything else in a kit (music, notes) |

Files are compared by size and sent as a tar stream over SSH; an interrupted run is simply run again. The
order is files, then the database, then removal of videos the mirror no longer lists - so the mirror never
shows a video it does not have. A video the database still lists is never removed from the mirror, also when
its file was deleted here to free disk space: the mirror's copy may be the only one left. It needs the `ssh`
client here and `find`, `tar`, `mv`, `rm` there.

```bash
demoreel publish --target demoreel@mirror.example.org:/srv/demoreel/data \
                 --restart "sudo systemctl restart demoreel"
demoreel publish --dry-run          # only say what would be sent and removed
```

`DEMOREEL_PUBLISH_RENDERERS` limits the videos sent to some renderers, e.g. `["ingame"]` for a community that
shows only first-person videos: the others stay on the recording machine and are removed from the mirror at
the next publish.

`DEMOREEL_PUBLISH_TARGET` and `DEMOREEL_PUBLISH_RESTART` hold the two options so the command can be run bare
(or from a scheduled task after each batch). The restart matters: the running mirror keeps the previous
database open until then.

## Setting up the mirror (Ubuntu / Debian)

1. A user and folders: `useradd --system --create-home demoreel`; `/opt/demoreel/app` (code),
   `/srv/demoreel/data` (data), both owned by it. Give it your SSH public key.
2. Code and dependencies, as that user: copy `src/`, `pyproject.toml`, `uv.lock`, `README.md`, `LICENSE` into
   `/opt/demoreel/app`, then `uv sync --frozen --no-dev --python 3.13`. System libraries Skia wants:
   `apt install libgl1 libegl1 libfontconfig1`.
3. Settings: [`deploy/demoreel.env.example`](../deploy/demoreel.env.example) as `/etc/demoreel/demoreel.env`
   (mode 640, group `demoreel`), with `DEMOREEL_PUBLIC_URL` and a fresh `DEMOREEL_ADMIN_KEY`.
4. Service: [`deploy/demoreel.service`](../deploy/demoreel.service) as `/etc/systemd/system/demoreel.service`;
   `systemctl enable --now demoreel`. To let `publish` restart it, allow exactly that:
   `demoreel ALL=(root) NOPASSWD: /usr/bin/systemctl restart demoreel` in `/etc/sudoers.d/`.
5. Web server: [`deploy/Caddyfile`](../deploy/Caddyfile) as `/etc/caddy/Caddyfile`, with `SITE_ADDRESS` and
   `FRAME_ANCESTORS` in `/etc/caddy/site.env` and `EnvironmentFile=-/etc/caddy/site.env` in a drop-in of the
   caddy service. Open ports 80 and 443 (on Oracle Cloud images: in iptables as well as in the security list).

Embedding the mirror in the community's site is described in [embedding.md](embedding.md): the `FRAME_ANCESTORS`
variable is the `frame-ancestors` policy that page asks for.

## Sizes (measured on 20 matches)

About 0.45 GB of videos per match (first-person 16:9 and 9:16 for the featured plays, 2D replays for all of
them), 0.4 MB of analysis, a 5 MB database. The mirror's process uses about 110 MB of memory at rest.
