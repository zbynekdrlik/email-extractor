---
paths:
  - "email-extractor/app/**"
  - "email-extractor/tests/**"
  - "email-extractor/config.yaml"
  - "docs/autopilot-log.md"
---

# GitHub `mergeable_state: "dirty"` on a PR that merges CLEANLY locally — criss-cross
# merge-base false positive (integration round A, #273/#275/#255, 2026-08-13)

This project's fleet workflow (`agents/autopilot-worker.md`'s WORKTREE AWARENESS —
several `autopilot-worker`s build independent branches in parallel worktrees, then a
supervisor/integration-round session merges them into `dev` and opens ONE `dev`→`main`
PR) routinely produces a **criss-cross merge history**: `dev` and `main` each have
their own long streak of "Merge pull request #N from zbynekdrlik/dev" commits from
PRIOR integration rounds, and a worktree branch built off an EARLIER `dev` snapshot can
end up sharing history with `main` via a DIFFERENT path than the one the CURRENT `dev`
tip shares — `git merge-base --all dev origin/main` genuinely returns **two** distinct
merge-base commits, not one.

**Symptom:** `gh pr create`/`gh api .../pulls/<N>` reports `mergeable: false`,
`mergeable_state: "dirty"` — persistently, for 10+ minutes, surviving a close/reopen —
even though `git merge --no-ff origin/main` (while on `dev`) or a merge test in a
**completely fresh `git clone`** comes back 100% clean with zero conflict markers.
`rebaseable` also reports `false`. Actually attempting the merge via
`gh api .../pulls/<N>/merge -X PUT` returns `{"message":"Pull Request has merge
conflicts"...}` (HTTP 405) — GitHub genuinely believes this, it is not a stale-cache
display glitch.

**Root cause, confirmed via `git merge-tree --write-tree --merge-base=<base> dev
origin/main` run against EACH of the two merge-bases separately:** using the MORE
RECENT base (`de0eddd` in this incident) merges clean; forcing the OLDER base
(`bf2530f`) produces a real textual conflict in the version-bump files
(`app/__init__.py`, `config.yaml`) — both branches bumped the version along their own
path since that older point, so a merge relative to it sees two edits to the same
version-string line. Git's own `ort` merge strategy (the default for a plain `git
merge`) automatically computes a **virtual merge base** by recursively merging the
multiple bases together first, so it resolves this cleanly and silently — but GitHub's
own server-side mergeable pre-check appears to pick (or behave as if it picked) the
OLDER of the two bases, hence the false "dirty".

**Diagnosis recipe — run this BEFORE assuming a real conflict, or wasting more than a
couple of `sleep`+recheck cycles on a persistently "dirty" PR that a local test says is
clean:**

```bash
git merge-base --all dev origin/main        # more than one line printed = criss-cross
git merge-tree --write-tree --merge-base=<older-base> dev origin/main   # reveals the
                                                                          # real conflict
```

**Fix — sync `origin/main` back into `dev` BEFORE relying on the PR's own merge
button**, collapsing the criss-cross to a single, unambiguous base (main's own tip):

```bash
git merge --no-ff origin/main -m "merge: sync main back into dev (collapse criss-cross merge-base ambiguity before PR #N)"
git push origin dev
```

This updates the open PR automatically (no need to close/reopen). `mergeable_state`
settles to `"blocked"` (waiting on the freshly-triggered CI for the new commit, normal)
then `"clean"` once CI passes — verified end-to-end on PR #292. **This is NOT the
"merge despite a real conflict" shortcut `autonomous-quality-discipline.md` bans** — it
is proven, twice over (a local merge test AND a from-scratch clone test), to be a
GitHub-side tooling false-positive on a criss-cross topology; the sync-back is the
same content a normal PR merge would have produced anyway, just performed in a way
that also fixes the ambiguous ancestry for the NEXT integration round.

**Prevention for a future round:** if this recurs at the START of an integration round
(before opening the PR at all), the same `git merge --no-ff origin/main` sync-back done
FIRST (right after `git fetch origin` / `git checkout dev`, before the version bump)
avoids ever hitting the false-positive in the first place.
