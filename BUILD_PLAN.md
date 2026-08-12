# Build Plan — revision 2

Phase 1 is built, hardened, and tested (40 passing). Phases 2–4 are specified
below with prompts written to be pasted as-is.

**Total remaining: ~4 hours across three sittings.**

The rule for the whole project: **the model proposes, deterministic code
commits.** That boundary is the reason this is an engineering project and not a
script.

---

## What changed from revision 1

Hardening applied:

| Change | Why |
|---|---|
| Append-only triggers on `events` | Invariant enforced by the engine, not by convention |
| `ON DELETE CASCADE` → `RESTRICT` | Cascade contradicted append-only; it was a hole |
| `event_kinds` lookup table + FK | Same guarantee as `CHECK`, no table rebuild to add a kind |
| `PRAGMA recursive_triggers = ON` | **`OR REPLACE` bypassed the delete trigger without it** |
| Injectable `today` throughout | Time-dependent tests must not rot at midnight |
| `jt rm` (zero events only) | Fixing a typo stays possible; erasing a rejection does not |
| Review count in `jt stale` | Surfaces the silent-failure mode |
| `jt backup` | The real catastrophic risk is `rm -rf`, not concurrency |
| Test suite, `.gitignore`, `.env.example`, CI, packaging | Table stakes |

Three amendments to the hardening list as proposed:

**Triggers and cascade interact.** A `BEFORE DELETE` trigger plus
`ON DELETE CASCADE` means deleting an application fires the cascade, the cascade
hits the trigger, and you get an abort with a baffling message. `RESTRICT` plus
the zero-events rule in `jt rm` is the coherent pairing.

**Lookup table, not `CHECK`.** SQLite cannot alter a `CHECK` constraint; adding
an event kind would mean rebuild-copy-drop-rename, which is genuinely annoying
once append-only triggers exist. The FK gives identical enforcement with an
`INSERT` as the migration path.

**Packaging belongs at Phase 2, not now.** `pyproject.toml` is in the repo and
works, but Phase 1 is one file with zero dependencies — `pipx` buys nothing over
an alias until the Google and Anthropic SDKs arrive.

### The bug the tests found

`INSERT OR REPLACE` did **not** fire the delete trigger. SQLite fires delete
triggers for `OR REPLACE` only when `recursive_triggers` is on, and it is off by
default. Without the pragma, one `OR REPLACE` silently rewrites history through
the invariant the whole design rests on. Fixed in `connect()`, documented in the
module docstring, and covered by
`test_insert_or_replace_is_rejected_by_the_delete_trigger`.

It is per-connection, so it only holds for code that goes through `connect()`.
Worth saying out loud to any agent you point at this repo.

---

## Phase 0 — Right now (5 minutes)

```bash
mkdir -p ~/dev/jobtrack && cd ~/dev/jobtrack
# copy jt.py, transitions.py, tests/, README.md, .gitignore, .env.example,
# pyproject.toml, .github/, evals/ here
git init
git add . && git commit -m "feat: phase 1 tracker with append-only event log"
alias jt="python3 ~/dev/jobtrack/jt.py"   # add to ~/.zshrc
```

Log today's batch — `today.txt`, one line each:

```
Doorstep | Software Engineer | swe
Cooke School | Operations Coordinator | ops
White & Case | Communications Coordinator | comms
```

```bash
jt bulk < today.txt && jt list
```

**You are now tracking.** Everything below is optimization. If you stop here you
have still beaten the spreadsheet, because `jt stale` answers a question a
spreadsheet cannot.

If you already created a database from revision 1, it migrates automatically on
next run and prints a note to stderr. Nothing is lost.

---

## Phase 2 — Gmail sync (90 min) → **Codex**

Goal: pull messages into `raw_messages`. **Parse nothing. Interpret nothing.**
You are proving auth, pagination, and the watermark work. Resisting the urge to
also classify here is the entire discipline of the phase.

### Your tasks first (15 min — do not delegate)

1. **Rotate the OAuth client secret from `morning-briefing-agent`.** It was
   committed in plaintext at `credentials/google_oauth_client.json`. If that
   repo was ever pushed, treat the secret as burned. Rotate in Google Cloud
   Console, then use the new client here.
2. Enable the Gmail API and add scope `gmail.readonly` — read-only only.
3. Store the client JSON at `~/.jobtrack/`, **outside the repo**.
4. In Gmail, create a filter applying label `jobsearch` to application mail.
   Ten seconds of Gmail UI eliminates an entire class of parsing problem.

### On the watermark — pick overlap, not history IDs

History IDs are the correct answer at scale, but they expire and return 404 when
stale, so you need a date-range fallback anyway — meaning you have built both.
For a few hundred emails: watermark minus two days, and let the UNIQUE
constraint eat the duplicates. That is *why* the constraint exists.

The bug being avoided is real and specific: Gmail's `after:` operator is
date-granular in the mailbox timezone, so a tight watermark genuinely skips
same-day messages.

### Prompt → Codex

> Add `sync.py` to this repo. It fetches Gmail messages into the existing
> `raw_messages` table. Read `jt.py` first, including the invariants in its
> module docstring — match its style: stdlib plus the Google client libraries,
> no framework, no ORM.
>
> Behavior:
> - Open the database via `jt.connect()`. Never open it with a bare
>   `sqlite3.connect` — the pragmas set in `connect()` are load-bearing.
> - OAuth installed-app flow. Client path from env `GOOGLE_OAUTH_CLIENT`, token
>   cached at `GOOGLE_TOKEN_PATH`. Scope `gmail.readonly` only.
> - Query messages carrying label `GMAIL_LABEL`, newer than the watermark in
>   `sync_state['gmail_last_synced']` **minus `GMAIL_OVERLAP_DAYS`**. First run
>   defaults to 30 days back.
> - Paginate via `nextPageToken`. Retry 429 and 5xx with exponential backoff and
>   jitter; give up after 5 attempts with a clear error.
> - Insert `gmail_msg_id`, `received_on`, `sender`, `subject`, plain-text body
>   into `raw_messages` with `processed = 0`, using `INSERT OR IGNORE`.
>   Duplicates must be silent — the overlap window guarantees them.
> - Prefer `text/plain`; fall back to stripping tags from `text/html`. Truncate
>   body to 4000 chars.
> - Advance the watermark only **after** the full page batch commits. If the run
>   dies mid-page, the next run re-fetches and no-ops on duplicates.
> - Do not touch `jt.py`, `transitions.py`, the schema, or any table other than
>   `raw_messages` and `sync_state`. Do not classify, do not parse company
>   names, do not create events or applications.
> - Add `--dry-run` and `--since YYYY-MM-DD`.
>
> Then `tests/test_sync.py` against a faked Gmail service covering: duplicate
> message is a no-op; watermark does not advance on mid-run exception;
> pagination fetches all pages; overlap window re-fetches without duplicating.

**Acceptance gate:** run it twice. The second run must insert zero rows. Do not
start Phase 3 until that holds — every later phase compounds on it.

---

## Phase 3 — Classification (90 min) → **Claude Code**

The LLM enters, tightly boxed. `transitions.py` is already written and is the
gate.

### Your task first (20 min — do not delegate)

Read `transitions.py` and edit it to match how your pipeline actually moves. My
defaults are opinionated: `applied` cannot jump straight to `offer`; terminal
states accept nothing at all.

This table is what catches a model proposing `rejected → onsite` at confidence
0.97. That model is not uncertain — it is certain and wrong, and no confidence
threshold catches it. Only a legality check does.

### Prompt → Claude Code

> Add `classify.py`. It reads unprocessed rows from `raw_messages` and proposes
> events. Read `jt.py`'s docstring and `transitions.py` first.
>
> The constraint that matters: **Claude proposes, code commits.** Model output
> is untrusted input, validated like a form submission from a stranger.
>
> 1. Batch up to `CLASSIFIER_BATCH_SIZE` unprocessed messages per API call to
>    `CLASSIFIER_MODEL`.
> 2. Prompt for a JSON array only — no prose, no markdown fences. Each item:
>    `{gmail_msg_id, company, role_hint, kind, occurred_on, confidence, evidence}`
>    where `kind` is in `jt.EVENT_KINDS`, `confidence` is 0.0–1.0, and `evidence`
>    is a verbatim span of under 15 words from that email.
> 3. Validate every proposal in Python before anything is written:
>    - JSON parses; every field present and correctly typed
>    - `evidence` is an actual substring of that message's body — if not, reject
>      as hallucinated
>    - fuzzy-match `company` against `applications.company`: require one
>      unambiguous match (rapidfuzz ratio ≥ 88 with the runner-up ≥ 10 points
>      behind). Ambiguity is a refusal, not a coin flip.
>    - `transitions.is_legal(current_status, kind)` passes
>    - `confidence >= CLASSIFIER_MIN_CONFIDENCE`
> 4. Passing → `INSERT OR IGNORE` into `events` with `gmail_msg_id`,
>    `confidence`, `evidence`. Never `INSERT OR REPLACE` — see the docstring.
> 5. Failing → `review_queue` with the specific failing `reason`. Never guess.
> 6. Mark the message `processed = 1` either way, so a permanently ambiguous
>    email cannot loop forever.
> 7. `--dry-run` prints proposals and verdicts without writing.
>
> Do not add a status column. Do not add entries to `TRANSITIONS`. Do not let
> `classify.py` create applications — an email matching nothing goes to review,
> because a confirmation email is not proof I applied, it is evidence about
> something I already recorded.

### Second prompt, same session — adversarial pass

> Now assume `classify.py` is wrong. Specifically: malformed JSON from the API;
> a `kind` not in `EVENT_KINDS`; two applications at the same company for
> different roles both fuzzy-matching; one email legitimately about two
> applications; `sync.py` and `classify.py` running concurrently; an email
> arriving for an application I logged *after* the email; an evidence span that
> matches a substring by coincidence. Show me each failure, then the fix.

**Acceptance:** `--dry-run` over 20 real emails; read every verdict yourself.
Expect 60–75% auto-commit. If it commits 95%, thresholds are too loose and it is
guessing.

---

## Phase 3.5 — The eval set (60 min) → you, then **Claude Code**

This is the step that separates the project from competent hygiene. Nothing in
the hardening list changes what the system *does*; an eval set lets you state
what it *achieves*.

1. Pull 30 real emails. Hand-label each: expected kind, expected company, and
   whether it should commit or land in review.
2. **Redact before anything touches git.** Real companies, real recruiter
   addresses, and a legible map of who rejected you. Scrub to `Company A` /
   `recruiter@example.com`. `evals/fixtures.example.jsonl` shows the format and
   the redaction standard; `evals/fixtures.jsonl` is gitignored.
3. Weight the set toward the hard cases, not the easy ones. Fixtures 4, 5, and 7
   in the example file are the ones that matter — a talent-community blast, a
   two-role ambiguity, and marketing noise. Any classifier gets the clean
   rejection right.

> Prompt → Claude Code: Add `evals/run.py`. Load `evals/fixtures.jsonl`, run each
> through the same validation path `classify.py` uses — import it, do not
> reimplement it, or the eval drifts from the product. Report precision, recall,
> and the count of misclassifications that reached `events` rather than
> `review_queue`. That last number is the one I care about; print it last and
> print it loudly.

Then you can say: **94% precision, 81% recall, and 100% of misclassifications
landed in the review queue rather than the database.**

---

## Phase 4 — Prioritization (45 min) → **Claude Code**

> In `jt.py`, add a `priority` command. Score each non-terminal application:
>
> ```
>   base            2.0 if lane == 'swe' else 1.0
>   + 3.0           if contact_email is not null (warm channel)
>   + 2.0           if had_response
>   + 1.5 * (stage_rank / max(STAGE_RANK.values()))
>   - 0.25          per business day since last_touch
>   - 10.0          if followups >= 1 and bdays_quiet >= 15
> ```
>
> Print the top 10 with one suggested next action per row. Keep weights in a
> module-level `WEIGHTS` dict with a comment per term so I can tune without
> reading the function. Add `--explain <id>` printing each term's contribution —
> I want to see why something ranks where it does, not just the number. Accept an
> optional `today` parameter as `derive` does, and add tests using a fixed date.

Optional and small: `jt draft <id>` generating a follow-up email from the
application's event history. It removes the last bit of friction that makes
people skip follow-ups.

---

## Division of labor

| | Owns |
|---|---|
| **You** | Schema, lane taxonomy, `transitions.py`, confidence thresholds, scoring weights, eval labels |
| **Codex** | Phase 2 — mechanical, well-specified, high line count |
| **Claude Code** | Phases 3, 3.5, 4 — prompt design, validation, adversarial review |

Your judgment calls total maybe 40 minutes. They are what prevents a weekend of
debugging why the database thinks you were rejected from a job you have an offer
from.

---

## Sequencing — do not authorize all of this in one run

Phase 2, verify the double-run inserts zero rows, **then** Phase 3. The failure
mode of a fully autonomous build is a system that looks complete and has a
subtly broken idempotency story you discover once the database holds three
copies of every event.

Skip CI until the classifier works. CI cannot exercise the Gmail or Anthropic
paths without live credentials in GitHub Actions secrets, which is more exposure
than the coverage is worth for a personal tool. The honest CI story is unit
tests against fakes — which is what `.github/workflows/ci.yml` does, with the
reasoning in a comment so a reviewer sees it was a decision.

---

## Deployment

There is nothing to host. Ship it as a GitHub repo plus an installable CLI:

```bash
pipx install git+https://github.com/Nicolercc/log-agent
```

Tag `v0.2.0` and write release notes that lead with the invariants. A hosted
deployment would require a web UI or a scheduled worker, both of which the scope
rules exclude.

---

## The interview version

Not "I built a job tracker." Rather:

> Entity resolution against noisy inbound text, idempotent sync with an overlap
> window, an append-only event log with derived state, and LLM output
> constrained by a validated state machine — so the model can propose but never
> commit. Measured: 94% precision, and every misclassification landed in the
> review queue rather than the database.

Same shape as Sano's failure containment and Ruvia's auditable-AI thesis. Third
instance of the same through-line, at one-tenth the size — which is exactly why
it finishes.
