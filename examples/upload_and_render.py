"""Minimal API client: upload a demo, wait for the analysis, print the scoreboard, render the best highlight.

    uv run python examples/upload_and_render.py path/to/match.dem [http://127.0.0.1:8000] [api-key]

Only depends on httpx. Shows the three things every integration needs: upload, long-poll a job, read results.
"""

from __future__ import annotations

import sys

import httpx


def wait(client: httpx.Client, job_id: str) -> dict:
    """Long-poll a job until it finishes (the server holds each request for up to 30 s)."""
    while True:
        job = client.get(f"/api/v1/jobs/{job_id}", params={"wait": 30}).raise_for_status().json()
        print(f"  job {job['type']}: {job['status']} {job['progress']:.0%} {job.get('message') or ''}")
        if job["done"]:
            if job["status"] != "succeeded":
                raise SystemExit(f"job failed: {job.get('error')}")
            return job


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    path = sys.argv[1]
    base = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8000"
    headers = {"X-API-Key": sys.argv[3]} if len(sys.argv) > 3 else {}

    with httpx.Client(base_url=base, headers=headers, timeout=httpx.Timeout(60, read=600, write=600)) as client:
        with open(path, "rb") as f:
            up = client.post("/api/v1/matches", files={"file": (path, f)}, params={"auto_render_top": 0})
        up.raise_for_status()
        match = up.json()["match"]
        print(f"match {match['id']} ({'new' if up.json()['created'] else 'already uploaded'})")
        if up.json()["job"]:
            wait(client, up.json()["job"]["id"])

        board = client.get(f"/api/v1/matches/{match['id']}/scoreboard").raise_for_status().json()
        for team in board["teams"]:
            print(f"\n{team['name']}  {team['score']}")
            for p in team["players"]:
                print(f"  {p['name'][:22]:22} {p['kills']:>2}/{p['deaths']:>2}/{p['assists']:>2}  "
                      f"ADR {p['adr']:>5}  rating {p['rating']}")

        highlights = client.get(f"/api/v1/matches/{match['id']}/highlights", params={"limit": 5}).json()
        if not highlights:
            print("\nno highlights found")
            return
        print("\ntop highlights:")
        for h in highlights:
            print(f"  {h['score']:>6}  {h['title']}")

        best = highlights[0]
        print(f"\nrendering: {best['title']}")
        accepted = client.post(f"/api/v1/highlights/{best['id']}/renders", json={"renderer": "auto"})
        accepted.raise_for_status()
        wait(client, accepted.json()["job"]["id"])
        render = client.get(f"/api/v1/renders/{accepted.json()['render']['id']}").json()
        print(f"video: {render['video_url']}")


if __name__ == "__main__":
    main()
