#!/usr/bin/env python3
"""Incrementally push new local commits to GitHub via the Git Data API.

For environments where git-over-HTTPS to github.com is blocked but
api.github.com is reachable (gh CLI authenticated).  Replays each new
local commit (blobs -> trees -> commits) on top of the current remote
main head, then fast-forwards the ref.

Remote/local alignment is automatic: the remote head is matched against
local history by (first message line, author date) — unique per commit —
so only genuinely new local commits are replayed.

Usage:
    python scripts/push_via_github_api.py
"""
from __future__ import annotations

import base64
import json
import subprocess
import time
from datetime import datetime

REPO = "suehongt/clinical-trial-monitor-public"
IDENT = {"name": "ct-monitor", "email": "ct-monitor@local"}


def _instant(iso_date: str) -> float:
    """Parse an ISO-8601 timestamp ('Z' or numeric offset) to epoch seconds."""
    return datetime.fromisoformat(iso_date.replace("Z", "+00:00")).timestamp()


def gh(endpoint, method="GET", payload=None):
    cmd = ["gh", "api", f"repos/{REPO}/{endpoint}"]
    if method != "GET":
        cmd += ["-X", method]
    last_err = None
    for _ in range(3):
        r = subprocess.run(cmd + (["--input", "-"] if payload is not None else []),
                           input=json.dumps(payload) if payload is not None else None,
                           capture_output=True, text=True)
        if r.returncode == 0:
            out = r.stdout.strip()
            return json.loads(out) if out else None
        last_err = r.stderr.strip()
        time.sleep(3)
    raise RuntimeError(f"{method} {endpoint}: {last_err}")


def run(*args, binary=False):
    r = subprocess.run(args, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"{args}: {r.stderr.decode()}")
    return r.stdout if binary else r.stdout.decode()


def local_commit_info(sha):
    msg = run("git", "log", "-1", "--format=%B", sha).strip()
    return msg, run("git", "log", "-1", "--format=%aI", sha).strip(), \
        run("git", "log", "-1", "--format=%cI", sha).strip()


def main():
    remote_head = gh("git/ref/heads/main")["object"]["sha"]
    remote_info = gh(f"commits/{remote_head}")
    rmsg = remote_info["commit"]["message"].strip().split("\n")[0]
    rdate = remote_info["commit"]["author"]["date"]
    print(f"remote main head: {remote_head[:10]}  ({rmsg[:50]})")

    # auto-align: find the local commit matching the remote head
    # (dates compared as instants — local %aI uses +08:00, GitHub uses Z)
    base_local = None
    for line in run("git", "log", "--reverse", "--format=%H|%aI", "main").strip().split("\n"):
        sha, adate = line.split("|")
        msg, a_date, _ = local_commit_info(sha)
        if msg.split("\n")[0] == rmsg and _instant(a_date) == _instant(rdate):
            base_local = sha
    if base_local is None:
        raise SystemExit("remote head not found in local history — cannot align")
    print(f"aligned to local {base_local[:10]}")

    new_commits = [l.split("|") for l in
                   run("git", "log", "--reverse", "--format=%H|%aI|%cI",
                       f"{base_local}..main").strip().split("\n") if l]
    if not new_commits:
        print("nothing to push.")
        return
    print(f"replaying {len(new_commits)} new commit(s)")

    prev_commit = remote_head
    # CRITICAL: base new trees on the REMOTE tree, otherwise the first
    # replayed commit's tree only contains its own changed files.
    prev_tree = remote_info["commit"]["tree"]["sha"]

    for sha, adate, cdate in new_commits:
        entries = []
        for line in run("git", "diff-tree", "-r", sha).strip().split("\n"):
            if not line.startswith(":"):
                continue
            meta, path = line.split("\t", 1)
            parts = meta.strip().lstrip(":").split()
            mode, status = parts[1], parts[4][0]
            if status == "D":
                entries.append({"path": path, "sha": None, "mode": "100644", "type": "blob"})
                continue
            content = run("git", "show", f"{sha}:{path}", binary=True)
            entries.append({"path": path, "mode": mode, "type": "blob",
                            "sha": gh("git/blobs", "POST",
                                      {"content": base64.b64encode(content).decode(),
                                       "encoding": "base64"})["sha"]})
        tree = gh("git/trees", "POST", {"base_tree": prev_tree, "tree": entries})
        msg, adate, cdate = local_commit_info(sha)
        commit = gh("git/commits", "POST", {
            "message": msg, "tree": tree["sha"], "parents": [prev_commit],
            "author": {**IDENT, "date": adate}, "committer": {**IDENT, "date": cdate},
        })
        prev_commit, prev_tree = commit["sha"], tree["sha"]
        print(f"  {sha[:7]} -> {commit['sha'][:7]}  {msg.splitlines()[0][:50]}")

    gh("git/refs/heads/main", "PATCH", {"sha": prev_commit, "force": True})
    print(f"main -> {prev_commit[:10]}")

    total = gh("git/trees/main?recursive=1")
    n_remote = sum(1 for e in total["tree"] if e["type"] == "blob")
    n_local = len(run("git", "ls-files").strip().split("\n"))
    print(f"verified: remote {n_remote} blobs / local {n_local} files"
          + ("  ✓" if n_remote == n_local else "  ⚠ MISMATCH"))


if __name__ == "__main__":
    main()
