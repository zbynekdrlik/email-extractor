---
paths:
  - "email-extractor/app/**"
  - "email-extractor/tests/**"
  - "email-extractor/config.yaml"
---

# `git commit -F <scratch-file>` — verify the file's content immediately, every time

The design-gate hook (`hooks/block-commit-without-design.sh`) can block a WHOLE
compound Bash command atomically — including a `cat > msg.txt <<'EOF' ... EOF`
heredoc write that appears earlier in the SAME command — whenever the commit
references a ticket with no valid design-comment marker yet. When that happens, the
heredoc write never runs, and the scratch path at `msg.txt` is left exactly as it
was BEFORE this call: either genuinely absent, or — on THIS project, because the
scratchpad directory is derived from the session id and autopilot-worker sessions on
this repo reuse the same session id across separate dispatches — a **leftover file
from an entirely unrelated EARLIER ticket's commit message**.

airuleset ships a fix for this class of bug (a "stale-msgfile quarantine" that is
supposed to move the leftover file aside so a later bare `git commit -F` retry fails
loud instead of silently reading it) — but a real incident on this project
(2026-08-13, working #277) showed the quarantine NOT firing on an ordinary-looking
blocked compound, and a later bare retry DID silently commit the wrong, stale
message onto the current ticket's commit. Filed upstream as
`zbynekdrlik/airuleset#431` — don't assume the quarantine protects you.

**The reliable local discipline, regardless of whether the upstream fix is working
this session:**

1. Write the commit-message file in its OWN dedicated Bash call — never chained with
   the `git commit -F` that consumes it (this alone is `gh-cli-recipes.md`'s existing
   global rule; keep following it even though it didn't fully save this incident).
2. **Immediately before running `git commit -F <path>`, `cat`/`Read` that exact path
   and confirm the content is what THIS commit should say** — especially any time an
   earlier attempt in the same session got blocked by ANY hook. This is the one
   step that would have caught the incident: the stale content was visibly wrong
   the moment it was read.
3. **Immediately after `git commit`, run `git log -1 --format=%B` and confirm the
   landed message matches what you intended.** If it doesn't: `git reset --soft
   HEAD~1` (never `--amend`, per `commit-conventions.md`), rewrite the message file
   fresh (in its own call, with a NEW filename to be extra sure — e.g.
   `commit-<issue>-<step>.txt` — rather than reusing a name a stale file might share),
   and recommit.

This cost one wrong-then-recovered commit on #277 (`git reset --soft HEAD~1`,
re-committed correctly as `ccf1148`) — cheap because it was caught immediately by
step 3 above. Skipping steps 2/3 is what would let a wrong commit message actually
ship.

## The `#N`-in-commit-message design gate ALSO checks the ISSUE COMMENT's own SHAPE
## for a NON-TRIVIAL ticket — a prose "Triage: non-trivial" paragraph is not enough
## on its own (#291, 2026-08-13)

`orders-corpus.md` already documents that the design-gate hook's `_CAUSE_RE`
classifier needs the literal word "Príčina:" on the commit-message-referenced
ISSUE COMMENT. There is a SEPARATE, additional classifier
(`design_gate.classify_triage_and_approaches`/`classify_architecture_section`) that
fires whenever that same comment's own `Triage:` line names the ticket non-trivial
(matches words like "non-trivial"/"cross-cutting"/"architektonick*"/"komplexn*") —
and it needs a MECHANICALLY specific shape, not just a longer prose writeup:

- At least **2 DISTINCT** literal `Approach N`/`Option N`/`Variant N`/`Prístup N`/
  `Možnosť N` markers (N = 1-3) — "Approach chosen: X" plus a separate prose list of
  "rejected alternatives" does **NOT** satisfy this; the numbered word itself must
  precede the number for EACH candidate, including the one you chose (`Approach 1
  (CHOSEN): ...`, `Approach 2 (REJECTED): ...`).
- Explicit trade-off language somewhere in the comment (`trade-off`, `kompromis`,
  `výhod`/`nevýhod`, `pros`/`cons`, "on the other hand"/"na druhej strane").
- An `Architektúra:` (or `Architecture:`) section HEADER, containing a
  structure/topology word (`štruktúra`/`structure`/`topológia`/`topology`) AND
  either a framework/library word (`framework`, `rámec`, `knižnica`/`library`) OR an
  evidenced "no existing framework fits" justification.

A first design comment written as normal, thorough prose (root cause + "the chosen
approach" + a numbered "alternatives considered and rejected" list, no
`Architektúra:` header at all) was REJECTED by the hook on the first commit attempt
of a non-trivial ticket, even though the actual engineering content was already
complete and correct — the gate wants the MECHANICAL markers, not just the
substance. Fix: re-post as a SECOND comment restructured with the literal headers
above (`Approach 1 (CHOSEN): ...` / `Approach 2 (REJECTED): ...` / `Approach 3
(REJECTED): ...` / `Approach 4 (REJECTED): ...`, then an `Architektúra:` paragraph
naming the new module's structure + confirming no existing internal framework
covers the narrow concern) — the retried commit then went through immediately. For
a **TRIVIAL** ticket (`Triage: trivial`) none of this applies — one honest
paragraph is still sufficient, per `autonomous-batch-issue-development.md`'s own
"depth scales with the problem" principle. Write the non-trivial shape correctly
the FIRST time by drafting the `Approach N (CHOSEN/REJECTED):` headers and the
`Architektúra:` section from the start, rather than writing normal prose and
discovering the mechanical requirement only after a rejection.

## ANY PreToolUse hook — not just the design gate — can block a compound
## `cat > msg.txt <<'EOF' ... EOF && git commit -F msg.txt` atomically (integration
## round C1, 2026-08-13)

The design-gate section above documents this failure mode specifically for
`block-commit-without-design.sh`, but it is a property of PreToolUse hooks in
general, not that one hook. Live incident: a merge commit's `cat > msg.txt <<'EOF'
... EOF && git commit -F msg.txt` compound got blocked in ONE call by
`block-sensitive-staging.sh` (a real-looking-but-synthetic test-token literal
staged in `tests/test_httpapi_waitress.py`) — the heredoc write never ran, so the
retry's bare `git commit -F msg.txt` (with the bypass comment appended) failed with
`fatal: could not read log file ... No such file or directory`, because the file
genuinely never existed yet. The fix is the SAME discipline `gh-cli-recipes.md`
already states generally: write the scratch file in its OWN Bash call, THEN commit
in a SEPARATE call — this makes the failure mode impossible regardless of WHICH
hook (if any) blocks the write, not just the design gate. If a compound command
that combines a heredoc write with its consuming command ever errors, do not assume
the write happened — check the file exists (`ls`/`cat`) before retrying the bare
consuming command.
