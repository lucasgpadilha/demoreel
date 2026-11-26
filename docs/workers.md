# Remote workers

A demoreel server can hand its heavy work to other machines. The server keeps the queue, the database and the
finished videos; a **remote worker** - typically a Windows PC with CS2 and a GPU - asks for work, does it on
its own disk and sends back only the result.

```
server (small, always on)                         worker (has the game, works when it can)
  queue, database, site, API        <── claim ──    demos, analysis
  finished videos                   <── upload ─    recording with CS2
  brand kits, match sources                         editing; the raw footage stays here
```

Every connection is opened by the worker, over HTTPS. That machine needs no open port and no fixed address,
and when it is off the site keeps working: jobs simply wait in the queue.

This is the alternative to [`demoreel publish`](hosting.md), where the recording machine owns everything and
pushes a copy to a read-only mirror. Do not mix the two on one server: `publish` replaces the mirror's
database, including its list of workers.

> **Status.** The server side of the protocol (this page) is implemented and tested. The program that runs
> on the worker machine and speaks it - `demoreel worker --server <url>` - is the next step; until it lands,
> workers on the same machine as the server (`demoreel worker`, `demoreel serve --workers N`) work as before.

## What a worker may do

| Capability | Jobs | Needs |
|---|---|---|
| `cpu` | analyse a demo, render a 2D replay, edit footage that is already recorded | ffmpeg |
| `ingame` | record first-person footage with the real CS2 client | Windows, CS2, a GPU - see [ingame-recording.md](ingame-recording.md) |

The organiser decides what each worker is allowed to take. The worker says, every time it asks for work, what
it can take *right now*: a machine that also hosts game servers leaves `ingame` out while people are playing.

## Registering a worker

On the server:

```bash
demoreel workers add recording-pc -c cpu -c ingame   # prints the token, once
demoreel workers list                                # who is online, what each is doing
demoreel workers remove recording-pc                 # the token stops working at once
```

The same through the API, with the organiser's key: `POST /api/v1/workers`, `GET /api/v1/workers`,
`DELETE /api/v1/workers/{name}`. Agents can read the list with the MCP tool `list_workers`; tokens are never
handed out through MCP.

Once a worker with `ingame` is registered, the server offers first-person videos (`GET /api/v1/capabilities`
reports the `ingame` renderer as available) even though nothing records on the server itself.

## Authentication

- **One token per worker**, created by the organiser. The server stores only its SHA-256; the token cannot be
  shown again. If it leaks, remove the worker and register it anew.
- The worker sends it as `Authorization: Bearer <token>` on every request, **over HTTPS only**.
- A worker token opens `/api/v1/worker/*` and nothing else. It carries none of the organiser's rights, and the
  organiser's key (`DEMOREEL_ADMIN_KEY`) is not accepted as a worker token.
- A worker can only touch **the jobs it holds**: read their inputs, report their progress, send their result.
  A job that was re-queued or cancelled answers `409` from then on.
- The server trusts nothing it receives: each file must match the SHA-256 the worker announces, only the few
  file names a job type produces are accepted, sizes are capped, a video must be an MP4 and an analysis must
  follow the schema. Where a file is stored is decided by the server.
- A job is **data** - a match, tick ranges, options. It never carries a command for the worker to run.

## The protocol

All routes are under `/api/v1/worker`. Errors are JSON `{"detail": "..."}` written to be read by a person.

| Step | Request | Answer |
|---|---|---|
| Test the token | `GET /me` | the worker as the server sees it |
| Take a job | `POST /claim` `{"capabilities": ["cpu", "ingame"], "info": {"protocol": 2, "version": "…"}}` | `200` the job, or `204` nothing to do |
| Read inputs | `GET /jobs/{id}/files/demo.dem`, `…/analysis.json` | the file, or `404` saying where else to look |
| Read the brand kit | `GET /jobs/{id}/brand`, `GET /jobs/{id}/brand/{path}` | file list with sizes and hashes; one file |
| Stay alive | `POST /jobs/{id}/heartbeat` `{"progress": 0.4, "message": "Recording"}` | `{"cancel_requested": false}` |
| Send a result file | `PUT /jobs/{id}/files/{name}`, raw body, header `X-Content-SHA256` | name, size and hash as received |
| Close the job | `POST /jobs/{id}/finish` `{"status": "succeeded", "result": {…}}` | the job |
| Report footage | `PUT /masters` `{"masters": [ … ]}` | how many recordings the server now knows |

**Claim.** The answer is everything the job needs, as data:

```jsonc
{
  "job": {"id": "j_…", "type": "render", "attempt": 1, "payload": {"render_id": "r_…"}},
  "heartbeat_seconds": 30, "stale_after_seconds": 180,
  "match": {"id": "m_…", "filename": "match.dem", "sha256": "…", "size_bytes": 137177993, "map": "de_nuke",
            "tickrate": 64, "total_ticks": 201344, "source_id": null, "remote_id": null},
  "inputs": {"demo.dem": false, "analysis.json": true},     // what the server can send for this job
  "brand": "mixdavila",
  "defaults": {"render_width": 1280, "render_height": 720, "render_fps": 30,
               "ingame_capture_width": 1920, "ingame_capture_height": 1080, "ingame_capture_fps": 30},
  "uploads": {"video.mp4": 1073741824, "thumbnail.jpg": 8388608},   // files it may send back, with size limits
  "render": {"id": "r_…", "kind": "highlight", "renderer": "ingame", "title": "…", "options": {"format": "9:16"}},
  "clips": [{"highlight_id": "h_…", "title": "…", "subtitle": "…", "version": 3, "action_ticks": [],
             "segments": [{"start_tick": 85423, "end_tick": 86222, "pov_steamid": "7656…", "speed": 1.0,
                           "caption": null, "role": "play"}]}]
}
```

`inputs.demo.dem` is `false` when the server does not keep the demo (a server fed by a recording machine
never did). The worker then looks on its own disk, by the SHA-256 or the file name in `match`.

**Protocol version.** A worker says in `info.protocol` which version of this protocol it understands; saying
nothing means 1. Version 2 adds segments filmed from a **fixed camera** instead of a player: their
`pov_steamid` is `"cam:x,y,z,pitch,yaw"` and `camera` holds `{x, y, z, pitch, yaw}` (the numbers CS2's `spec_pos`
prints; `null` for player segments). A job with such a segment is only handed to a worker that says
`"protocol": 2` or more; for the others it stays in the queue, in its place, with a message saying why, and
they get the next job. The server speaks version 2.

**Heartbeat.** At least every `heartbeat_seconds`. A job with no heartbeat for `stale_after_seconds` goes back
to the queue (it fails after two attempts). When the answer says `cancel_requested`, stop and finish the job
as `canceled`.

**Finish.** `status` is one of:

| Status | Meaning |
|---|---|
| `succeeded` | the result files were sent; `result` holds the facts below |
| `failed` | `error` says why; with `"retry": true` the job is queued again (up to two attempts) |
| `canceled` | the worker stopped because the server asked it to |
| `released` | given back untouched - players joined before recording started; it does not count as an attempt |

A result the server cannot accept (a file missing, a field out of range) is answered `400` with the reason and
the job **stays with the worker**: send what was missing and finish again.

| Job type | Files to send first | `result` |
|---|---|---|
| `analyze_match` | `analysis.json` (the `MatchAnalysis` of the demo) | - |
| `render` | `video.mp4`, optionally `thumbnail.jpg` | `duration_s`, `width`, `height`, `fps`; optional `body_offset_s`, `kill_marks` (`[{"tick", "t"}]`, video time), `warnings`, `composition`, `masters` |
| `capture_match` | none: the footage stays on the worker | `masters`: the description of every recording made (the `.json` stored next to the footage) |

## Raw footage stays on the worker

Recording is the expensive step, so the footage is kept as a *master* and every later edit is cut from it
([how](ingame-recording.md)). With remote workers the masters live on the machine that recorded them - they
are large, about 0.6 GB per match - and the server keeps only their description. That is enough for it to
know which plays can be edited again without the game, and to **address that job to the worker that keeps the
footage**; other workers never see it.

- After a `capture_match`, the videos of the recorded plays are queued for that same worker.
- `PUT /masters` replaces the server's list with what the worker really has (call it at start-up: footage
  deleted on the worker to free disk space then stops being counted on). Footage draws the edits of its match
  to whoever keeps it, so the server keeps only footage of matches that worker took a job of, or reported
  before; the rest of the report is ignored. The `masters` of a job's result must be of that job's match, or
  the result is refused (`400`, the job stays with the worker).
- Removing a worker fails the queued edits that only it could do, with the reason. Asking for those videos
  again records the plays once more.

## Settings

| Setting | Default | |
|---|---|---|
| `DEMOREEL_WORKER_MAX_FILE_MB` | `1024` | largest video a worker may send back |
| `DEMOREEL_WORKERS` | `1` | workers started *on the server itself*; `0` on a server that only hands work out |

## Stopping a worker

`demoreel serve` asks the workers it started to stop when it stops, and gives them 30 s before ending them.
A worker started with `demoreel worker` stops the same way on Ctrl+C, SIGTERM or Ctrl+Break: the job it runs
is cancelled at its next check - a recording closes the game and puts the CS2 folder back - and goes back to
the queue, where the next worker takes it; it is not counted as an attempt. A second signal stops it at once.
Should a worker die anyway (closed window, crash), the game it started dies with it, and the CS2 folder is put
back the next time `serve`, `worker` or `doctor` starts there ([details](ingame-recording.md#how-it-works)).
