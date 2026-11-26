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

The other way round - the server owns the queue and the database, and machines with the game take work from
it - is described in [workers.md](workers.md). Pick one model per server.

## What `demoreel publish` copies

| Copied | Never copied |
|---|---|
| a snapshot of the database: matches, highlights, finished videos | jobs, webhooks and remote workers (they carry secrets), unfinished renders |
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

The mirror's `DEMOREEL_ADMIN_KEY` must have at least 16 characters (`openssl rand -hex 24` gives 48): a
shorter key, or an example value such as `change-me`, stops the server at start with a message naming the
variable. Check it before updating the code of an existing mirror.

## Keys, other sites and what visitors see

Without any key (the default of `demoreel serve` on 127.0.0.1) everyone who reaches the server is the
organiser. That is fine on your own machine and nowhere else. `DEMOREEL_ADMIN_KEY` makes it a public viewer:
anyone reads, only the organiser's key changes things. `DEMOREEL_API_KEY` closes reading too. Keys need 16
characters or more. `docker compose up` publishes the port on 127.0.0.1 only; its comments say how to open it
on purpose. `demoreel serve` itself refuses to listen beyond this machine (`--host 0.0.0.0` or a LAN address)
while neither key is set; `--i-know-it-is-open` or the environment variable `DEMOREEL_ALLOW_OPEN=1` (read from
the process environment, not from `.env`) starts it anyway, which the Docker image does because its port is
only published on 127.0.0.1.

What the server refuses, so that a web page open in someone's browser cannot use it:

| Rule | Applies | To allow more |
|---|---|---|
| A request must name one of the server's hosts: `localhost`, `127.0.0.1`, `[::1]`, `DEMOREEL_HOST`, the host of `DEMOREEL_PUBLIC_URL` (stops DNS rebinding) | while no key is set (`/mcp` always) | `DEMOREEL_ALLOWED_HOSTS`, e.g. `["192.168.0.10"]`; `["*"]` turns it off |
| Pages of other sites change nothing: `POST`/`PUT`/`PATCH`/`DELETE` with a foreign `Origin` get 403 | always | `DEMOREEL_CORS_ORIGINS` |
| Browsers let other sites read the answers (CORS) only for this server's own names (any port), the kits' `ui.embed.parents` and `DEMOREEL_CORS_ORIGINS`; while a key is set, any site may read (`GET`) | always | `DEMOREEL_CORS_ORIGINS` |
| Only this server, the kits' `ui.embed.parents` and `DEMOREEL_FRAME_ANCESTORS` may frame the UI (CSP `frame-ancestors`; a proxy may replace it, as the mirror's Caddyfile does) | always | `DEMOREEL_FRAME_ANCESTORS`; `["*"]` allows any site |
| A request under `/api/v1` without the key it needs is refused before its body is read; bodies are limited to `DEMOREEL_MAX_JSON_KB` (1024) for JSON and `DEMOREEL_MAX_UPLOAD_MB` for an upload; a zip may list 1000 files | always | the two settings |
| Webhooks may not point to private, loopback or link-local addresses | while a key is set | `DEMOREEL_WEBHOOKS_ALLOW_PRIVATE=1` |

A mirror behind a proxy that passes on its own host name (another site forwarding `/demoreel/*` to it) keeps
working: with a key set the host name is not checked.

What a visitor (no organiser key, while one is set) gets differs from the organiser in a few places:

- **Chat.** Free chat text is never sent to visitors: people paste phone numbers and addresses into it. In
  `GET /matches/{id}/analysis` a line keeps its text only when it is short banter with no digits or links
  (`analysis/chat.py`, `shareable`); the others keep their classification with an empty `text` and
  `redacted: true`. The lines attached to highlights and the chat rankings use the same rule, with slurs
  masked.
- **Errors and machine details.** The `error` of jobs, matches and videos is null, and `/capabilities` says
  which renderers exist and whether they are available, without the reason or details: those texts carry the
  folders of the recording machine (CS2, HLAE), worker names and the end of the game console.
- **Webhooks and workers** are listed only to the organiser (a webhook address works like a password).

Names and SteamIDs of players are public by design: they are what the scoreboards, highlights, rivalries and
player pages are made of, on the server and on its mirror. A community that does not want them public should
not publish its matches.

## Sizes (measured on 20 matches)

About 0.45 GB of videos per match (first-person 16:9 and 9:16 for the featured plays, 2D replays for all of
them), 0.4 MB of analysis, a 5 MB database. The mirror's process uses about 110 MB of memory at rest.
