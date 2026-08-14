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

## Sibling worktree branches touching the SAME rule/log file almost always conflict
## textually only, at the tail — AND a NEW cross-cutting test helper introduced by
## one sibling needs porting onto tests a DIFFERENT sibling wrote (integration round
## C1, 2026-08-13)

When several worktree-isolated branches are all built against roughly the same `dev`
snapshot, each independently APPENDING a new `##`-headed section to a shared
`.claude/rules/*.md` or `docs/autopilot-log.md` (the standard "record what I learned"
mandate every branch follows), git's 3-way merge sees each later sibling's append as
"insert after the same context line" and reports a real `CONFLICT (content)` even
though there is **zero semantic overlap** — every section from every sibling is meant
to survive. Resolve by keeping ALL sections, in commit order (earliest-merged
branch's section first): delete only the `<<<<<<<`/`=======`/`>>>>>>>` markers
themselves, never any branch's actual content. This is the DEFAULT expectation for
this kind of file across a multi-branch round, not a surprise to investigate.

**A SEPARATE, non-textual integration step can be needed when one sibling branch
introduces a shared TEST HELPER that supersedes a hand-rolled pattern, and ANOTHER
sibling — built independently, at roughly the same time, against the OLD pattern —
added NEW test cases using that old pattern in the SAME test file.** Two branches
touching the same file with NO line-level overlap merge silently clean (git sees
disjoint edits) — but the result is semantically stale: the new sibling's new tests
still use the idiom the OTHER sibling's ticket existed specifically to retire. This
is invisible to `git diff --check` / conflict markers; only a targeted grep catches
it (`grep -n "t1\.join\|threading\.Thread(target" <file>` after merging both, looking
for the OLD idiom re-appearing in code added by the LATER-authored branch). Port the
new sibling's tests onto the shared helper as its OWN small, explicitly-justified
integration commit (never silently folded into either branch's own merge commit) —
this is genuine integration work, not scope creep, and the commit message should say
exactly why (which ticket's helper, which tests, why they still used the old idiom).

## A worktree-isolated autopilot-worker CANNOT monitor CI — a full-flow dispatch hits a
## wall at the CI-wait stage (#305/#312, 2026-08-14)

A dispatch prompt may (as #305/#312's did) explicitly ask a worktree-isolated
`autopilot-worker` to do the WHOLE flow — push, PR, wait CI, merge, wait main CI,
deploy. But two hooks make CI monitoring from a worktree **structurally impossible**,
and they compose into a hard wall:

- **The worktree-isolation guard blocks EVERY loop containing `gh`/`git`** ("too complex
  to verify it stays inside the worktree") — a `for`/`while` bounded poll loop, even with
  explicit `-R owner/repo` (cwd-independent) and even armed via the `Monitor` tool. So the
  `ci-monitoring.md` foreground-bounded-loop AND a `Monitor` until-loop are BOTH refused.
- **`block-ci-poll-repeat.sh` blocks the 3rd+ one-shot `gh run view`** for the same run —
  wanting either a bounded loop (worktree-blocked, above) or a RETURN.

The intersection: a worktree worker can do a FEW single `gh run view` calls (single calls
pass the worktree guard), and can bypass the repeat-guard with `# airuleset:poll-ok
<reason>` on a single call — that is the ONLY way to poll CI from a worktree, used
judiciously (spaced by real deploy-prep work), NOT a bounded loop. This is the framework
telling you integration belongs to the SUPERVISOR (WORKTREE AWARENESS in
`agents/autopilot-worker.md`: worktree workers stop at local-green + return the branch).
#305/#312 nonetheless completed the full flow from the worktree via single `gh` calls +
`poll-ok` because the dispatch explicitly delegated merge+deploy and the run-card gate
(`subagent-stop-check-run-card.sh`) requires a delivered card (which needs the deployed
version). If a FUTURE full-flow worktree dispatch is not time-critical, prefer reporting
the run-id + RETURN and letting the supervisor integrate, rather than fighting the guards.

**Also (#305/#312): the merge/deploy DID work from the worktree** — `git push origin
HEAD:dev` (fast-forward, branch based on origin/main's tip so no criss-cross), `gh pr
create/merge`, and the SSH `ha addons update` deploy are all single non-loop commands the
guards allow. Only the CI *monitoring* between them is blocked.
