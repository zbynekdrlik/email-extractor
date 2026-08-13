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
