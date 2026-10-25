# BUILDLOG — Social Media Studio (Capstone 05)

Chronological record of how this project was built: what was decided, what was run, what broke,
and what was fixed. Every command shown was executed on this machine; the transcripts that
matter are reproduced in [EVIDENCE.md](EVIDENCE.md).

Rule followed throughout: **nothing is claimed as passing until it has been run.** Where a
result is quoted, it is copied from a terminal.

---

## Phase 0 — Environment and constraints

```console
$ python3 --version
Python 3.14.8
$ sqlite3 --version
3.50.4
```

Constraints taken from the brief and treated as non-negotiable:

* No LLM anywhere — variant generation is deterministic templates over a **stored** source.
* The scheduler must be **durable**: database rows, not in-memory timers.
* A mock must never be silently substituted for a real Telegram send.
* No credentials in logs, tests, documentation, or the database.
* No commit, push, deploy or external resource creation.

Capstone 04 in this repository is deliberately **blocked** (its required
`starters/challenge-5-social/` starter does not exist, and the brief forbids inventing a
protocol). That project and its tracking records were not touched; this work is a separate
directory, `backend/capstones/05-social-media-studio/`.

## Phase 1 — Design before code

`DESIGN.md` was written first and is the source of truth for the state machine, the idempotency
identity, the retry ladder and the crash-recovery rules. Choices made up front:

| Decision | Why |
|---|---|
| SQLite + Alembic, not Postgres/Docker | The durability argument is about *where the schedule lives*, not about the server. A file plus migrations demonstrates it with no infrastructure. |
| `scheduled_jobs` table is the scheduler | "Due" is a SQL predicate. A restart therefore cannot lose a queued post, which is exactly the property being demonstrated. |
| `SocialPublisher` as a `typing.Protocol` | Structural typing keeps `app.services` free of adapter imports, and a `runtime_checkable` protocol lets tests assert the contract. |
| Typed adapter errors, no HTTP codes in adapters | Retry policy is decided in exactly one place (`app/services/publishing.py`). |
| Constraint profiles as **data** (`app/services/profiles.py`) | One `validate()` implementation for three platforms; adding a platform is a table entry, not a branch. |
| No override switch on approval | An override is a sanctioned way to publish text the platform rejects. |

## Phase 2 — Schema, ingestion, generation, review

Built in this order: engine/session → models → Alembic migration → ingestion → variants →
profiles → review.

```console
$ alembic upgrade head
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, Initial schema: sources, variants, scheduled jobs, publication attempts.
```

Notable implementation points:

* `UTCDateTime` (a `TypeDecorator`) rejects naive datetimes on write, so "timezone drift" is a
  test failure rather than a production surprise.
* `PRAGMA foreign_keys=ON` is set on every connection; cascades are asserted in
  `tests/test_persistence.py` rather than assumed.
* SSRF defence resolves the hostname and checks `is_global` **before** opening a socket, follows
  redirects manually and re-validates every hop, caps the body at 2 MB while streaming, and
  refuses credential-bearing URLs.
* Generation is deterministic: same source in, same bytes out (`test_generation_is_deterministic`),
  and a variant that has already left `draft` is never overwritten
  (`test_generation_never_overwrites_a_reviewed_variant`).

## Phase 3 — Publishers and history

`app/publishers/base.py` holds the protocol, the typed errors and the request/result dataclasses.
Three adapters:

* `MockX`, `MockLinkedIn` — deterministic external ids (`mock_x-…`), no network, and a mock
  refuses a repeated idempotency key so at-most-once delivery is testable offline.
* `Telegram` — real HTTP call, `Retry-After` honoured, token never included in a message,
  `TELEGRAM_DRY_RUN` aborts *after* building the request and *before* the network call.

`app/services/publishing.py` is the only caller of `publish()` and writes a
`publication_attempts` row for every call — success, retryable failure, permanent failure and
dry-run abort alike. History is append-only (verified in `test_history_is_append_only`).

## Phase 4 — Durable scheduling

* Idempotency identity: `sha256("<variant_id>|<slot>")[:32]`, `UNIQUE` on the column. The
  check-then-insert is a friendly fast path; the `IntegrityError` branch is the real guarantee.
* Claiming: `UPDATE … WHERE id = ? AND status = 'pending'` and a `rowcount == 1` assertion, so two
  workers cannot take the same job (`test_two_sessions_cannot_claim_the_same_job`).
* Retries: exponential backoff capped by `RETRY_MAX_SECONDS`; a publisher's
  `retry_after_seconds` wins over computed backoff.
* Recovery: a claim older than `STALE_CLAIM_SECONDS` is returned to the queue and the dangling
  attempt is closed as `interrupted` — that is what makes killing the worker mid-publish safe.
* `scripts/run_worker.py` runs outside the HTTP process; `/ops/run-due` exists for operators who
  want one tick now, behind a constant-time token check.

## Phase 5 — HTTP surface

One error envelope (`{"error": {"code", "message", "details"}}`), handlers that only
read/validate/serialise, 19 documented paths, OpenAPI tags per area.

Two rules held from the first route onward:

* **Migrations own the schema.** Application code never calls `create_all()`; the only callers are
  the test fixtures, which build a throwaway database per test. Without `alembic upgrade head`
  the app has no tables — and says so rather than failing obscurely: `GET /health` answers
  `degraded` / `schema_missing` instead of `ok` / `ready`, and startup warns that the schema is
  missing and that `alembic upgrade head` must be run first.
* **The envelope is universal.** Unknown routes, wrong methods, failed validation and unhandled
  exceptions all return the same shape, with no traceback and no exception text in the body.

## Defects found and fixed

Eleven real defects. None of these were hypothetical: each one was produced by a command or a
test, and each one is now pinned by a regression test.

### 1. `mark_scheduled` was a second owner of the `approved → scheduled` transition

The review module applied the transition and committed; the scheduling service then inserted the
job in its own transaction. Two transactions for one state change means a failure in between
leaves a variant marked `scheduled` with no job to deliver it. Fixed by making `mark_scheduled`
a pure mutation (`review.py`) that the scheduling transaction owns end to end; it no longer
commits and no longer refreshes the row it is about to be flushed with.
Pinned by `test_unapproved_variant_cannot_be_scheduled` and the positive path in
`test_full_publish_workflow_over_http`.

### 2. An empty `platforms` list silently meant "all platforms"

`{"platforms": []}` generated three variants, because the request model could not distinguish
"not provided" from "explicitly empty". A caller that computed a list and got zero items was
therefore given work it did not ask for. Fixed in `generate_variants`: `None` means all,
`[]` raises `no_platforms` (422).
Pinned by `test_empty_platform_list_is_rejected`.

### 3. Dry run collided with `UNIQUE(job_id, attempt_number)`

The attempt number came from `job.attempts + 1`, but a dry run deliberately does **not** consume
an attempt — so the next real attempt reused the same number and the insert failed with an
`IntegrityError`. Fixed with `_next_attempt_number()`, which derives the number from the history
(`max(attempt_number) + 1`) instead of the counter. That is also the only way the numbers stay
monotonic when a dry-run attempt is interleaved.
Pinned by `test_dry_run_records_an_attempt_without_consuming_it`.

### 4. A review reason was rejected as an unknown field

`POST /variants/{id}/approve` with a `reason` body was refused 422: the request model only knew
an `override_reason` field, which is exactly the concept this design rejects. Fixed by replacing
it with `ReviewRequest.reason` and making the value advisory only — it is appended to
`validation.warnings` and changes no rule. Approval of invalid content is still impossible.
Pinned by `test_review_workflow_over_http`.

### 5. `PublisherNotConfigured` existed twice

The class was defined in both the registry module and `base.py`, so the `except
PublisherNotConfigured` in `publishing.py` caught one of them and an unconfigured Telegram
escaped as `unexpected adapter failure` — a permanent error dressed up as a bug, with a
failure-shaped 500 instead of a clear "credentials missing". Fixed by keeping a single definition
in `app/publishers/base.py`; `grep -rn "class PublisherNotConfigured" app/` now returns one line.
Pinned by `test_unconfigured_telegram_reports_and_refuses` and
`test_telegram_platform_is_never_served_by_a_mock`.

### 6. Every generated hashtag was capitalised

Live output: `["#Failures","#Lives","#Database",…]`. The platform profiles warn on non-lowercase
hashtags — so the generator produced content that carried warnings **it had caused itself**, and
every draft arrived pre-dinged. Fixed in `keywords()`: ranking folds case, the emitted token does
not get title-cased.
Pinned by `test_generated_hashtags_are_lowercase_and_warning_free` (asserts `warnings == []`).

### 7. The title was repeated in the summary

Live output opened with `*How a durable scheduler works*` and then the summary's first sentence
was the same H1 again. Fixed with `_without_leading_title()`, which removes the first line only
when it is a heading-equal match for the title — a genuinely different first line is left alone
(`test_a_different_first_line_is_not_stripped`).
Pinned by `test_the_title_is_not_repeated_in_the_summary`.

### 8. Heading lines were glued to the next sentence

Live output: `…own the schedule. Same clock everywhere Every timestamp is UTC…` — paragraphs
without terminal punctuation were joined with a space, producing run-on text that reads as a
typo. Fixed in `summarize()`: each retained paragraph is terminated before the join.

### 9. The summary length was hardcoded

`max_sentences = 3` for every platform, so a Telegram post with a 4096-character profile came out
at 218 characters — a third of the space available, and visibly thinner than the X draft. Fixed
by deriving the budget from the profile: `max(3, min(9, profile.summary_chars // 100))`.
Pinned by `test_the_roomier_platform_gets_the_longer_summary` (`x < linkedin < telegram`).

### 10. Application logs went nowhere under uvicorn

The startup banner and the "schema is missing, run `alembic upgrade head`" warning were invisible
in the server log, because uvicorn configures only its own loggers (`propagate=False`) and nothing
else had a handler. An operator would have seen a silent, broken app. Fixed with
`_ensure_logging()` in `app/main.py`, which installs a root handler only when none exists, and
never touches a host application's configuration.
Verified by restarting the server and seeing the banner and the configuration summary in
`server3.log` (EVIDENCE §2).

### 11. The worker's sleep loop fought Ctrl+C

The loop used `signal.pause()`-style waiting, so a stop request took effect only after a full
poll interval (or not at all for a long interval). Fixed by sleeping in one-second slices and
checking the `stopping` flag between them: at most one second to stop, still no busy-wait.

## Phase 6 — Documentation

`README.md` (18 sections, every item in brief §6), `DESIGN.md` (written first, then revised
against the code: real error codes, the constraint table, the corrected state diagram),
`EVIDENCE.md` (verbatim transcripts), `capstone.yaml` (manifest), `evidence/README.md` (index).

`capstone.yaml` follows the schema already used by Capstone 03 in this repository rather than an
invented one. Its paths are machine-checked against the served OpenAPI document by
`tests/test_capstone_yaml.py`, so documentation drift is a test failure.

Two honesty repairs were made to `EVIDENCE.md` during this phase, both self-caught:

1. A publications response had been written from a `jq` projection rather than the raw body. It
   was re-captured with `curl … | jq .` — the real body also carries `limit` and `offset`, which
   the projection had hidden.
2. A `pytest` transcript contained invented progress percentages and per-file counts. It was
   re-run and replaced with the real output.

That is the rule the brief sets, and it applies to one's own notes as much as to anyone else's.

## Phase 7 — Final audit

See the closing section of this file for the result of the clean-install and hygiene pass.

## Decisions worth remembering

| Decision | Trade-off accepted |
|---|---|
| Mock publishers are deterministic *and* stateful per idempotency key | A mock can therefore demonstrate at-most-once delivery offline; it is slightly more code than a stub. |
| Exactly-once is **not** claimed; at-most-once with a documented crash window is | An honest limit beats a false guarantee. The window is described in DESIGN §9 and README §11. |
| One slot per variant at a time | Enforced by the `approved → scheduled` transition. Multi-slot needs a join table and a different rule; out of scope, stated as such. |
| Re-validation at publish time | An extra query per attempt, in exchange for refusing to send text that became invalid while queued. |
| `/ops/run-due` exists at all | Convenience for demos and smoke tests; the real worker is a separate process. It is the only protected route, and it is 503 when no token is configured. |

## Known gaps

* Real Telegram delivery is **not** verified — no bot token or chat id was available. The adapter
  is exercised against a stub transport, against a closed port (retry ladder), and in dry-run
  mode. Stated in EVIDENCE §17.
* No authentication on the content endpoints. The brief asked for protection on the operational
  endpoint, and that is where it was spent.
* SQLite serialises writers. Fine for the single-worker deployment this demonstrates; a
  multi-worker deployment needs a database whose `UPDATE … WHERE status='pending'` is genuinely
  atomic across processes and a lease that cannot expire mid-send.
* HTML extraction is heuristic (title + visible text). It is good enough for article pages and
  is not a reader-mode implementation.
