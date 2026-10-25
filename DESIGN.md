# DESIGN — Social Media Studio (Capstone 05)

Written before implementation, then used as the implementation plan. Every decision below
is what the code actually does; where a requirement is only partially achievable, the
limitation is stated here rather than discovered later.

---

## 1. Problem and non-goals

**Problem.** A marketing operator has a blog post (a URL or pasted Markdown) and needs to
turn it into platform-appropriate social drafts (X, LinkedIn, Telegram), have a human
review them, schedule the approved ones, and get a durable, auditable record of what was
actually published — without the process depending on an in-memory timer or a browser tab.

**In scope:** source ingestion (paste + URL with SSRF protection), deterministic variant
generation from *stored* sources, per-platform constraint validation, a guarded review
workflow, three publisher adapters (real Telegram + two deterministic mocks), durable
DB-backed scheduling with retries and crash recovery, idempotent publishing, publication
history, and a documented REST API.

**Explicit non-goals:** no frontend (API only), no external LLM (templates only; an AI
provider can be plugged into `services/variants.py` later), no real X/LinkedIn APIs, no
multi-tenant accounts/RBAC, no exactly-once guarantee across a third-party API, no
Docker/Redis/Celery, no browser automation, no attachment/media uploads.

---

## 2. Architecture

```
                 ┌──────────────────────── FastAPI (app/api) ───────────────────────┐
 client ────────►│ sources · variants · review · jobs · publications · publishers    │
                 └───────────────┬───────────────────────────────┬──────────────────┘
                                 │                               │
                    ┌────────────▼────────────┐      ┌───────────▼────────────┐
                    │ services/               │      │ publishers/            │
                    │  ingest (SSRF-guarded)  │      │  base (SocialPublisher)│
                    │  variants (templates)   │      │  mock_x                │
                    │  profiles (constraints) │      │  mock_linkedin         │
                    │  review (state machine) │      │  telegram (real API)   │
                    │  publishing (dispatch)  │      └───────────┬────────────┘
                    └────────────┬────────────┘                  │
                                 │                               │
                    ┌────────────▼───────────────────────────────▼────────────┐
                    │ scheduler/  claim → publish → record → retry/terminal   │
                    │             (durable state in the database)             │
                    └────────────┬────────────────────────────────────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │ db/  SQLAlchemy 2.x + SQLite + Alembic │
                    └─────────────────────────┘
```

Layering rule: API handlers do auth/validation/serialisation only; every state change goes
through a service; adapters never touch the database; the worker is the only writer of
publication attempts.

### Key decisions

| Decision | Rationale | Rejected alternative |
|---|---|---|
| **No APScheduler.** A DB-backed queue (`scheduled_jobs`) plus `scripts/run_worker.py` is the scheduler. | Job state must survive restarts; APScheduler's SQLAlchemy job store would duplicate the same facts in a second table and still need custom claiming. The brief permits this ("if the chosen scheduling library is not durable by itself, persist job state in the application database"). | APScheduler + SQLAlchemyJobStore (two sources of truth), Celery (needs Redis) |
| **Single owner, no tenancy** | Capstone 05's brief describes one operator's studio; tenancy was a Capstone 04 requirement. Adding unused tenant columns would be speculative. | Adding `tenant_id` everywhere and an auth system the brief does not ask for |
| **Store source content once** | Variants reference `source_id`; no duplicated canonical text. Generation always reads the persisted source. | Copying body into each variant row |
| **Deterministic templates, injectable AI hook** | Runs free, offline, and reproducibly; `generate_variant()` takes a composer so an LLM composer can be swapped in. | Requiring an API key to run the project |
| **Money-free project** — no pricing concerns | Not applicable here; noted so nobody looks for one. | — |

---

## 3. Data model (all timestamps UTC, ISO-8601 on the wire)

```
sources ──1:N──► variants ──1:N──► scheduled_jobs ──1:N──► publication_attempts
```

**`sources`** — `id` (uuid str PK), `source_type` (`url`|`paste`), `origin_url` (nullable),
`title` (nullable), `content` (Text, canonical), `content_type` (`text/markdown`|`text/plain`|
`text/html`), `created_at`, `updated_at`.

**`variants`** — `id`, `source_id` FK → sources (ON DELETE CASCADE), `platform`
(`x`|`linkedin`|`telegram`), `text` (Text), `hashtags` (JSON list), `validation` (JSON:
`{"valid": bool, "errors": [...], "warnings": [...]}`), `status`
(`draft`|`approved`|`rejected`|`scheduled`|`published`), `created_at`, `updated_at`,
`status_changed_at`.

**`scheduled_jobs`** — `id`, `variant_id` FK → variants, `platform`, `scheduled_for`
(UTC), `status` (`pending`|`processing`|`completed`|`failed`), `attempts`,
`max_attempts`, `next_attempt_at`, `last_error` (sanitized), `idempotency_key`
**UNIQUE**, `claimed_at`, `claim_token`, `external_id`, `created_at`, `updated_at`,
`completed_at`. Index on `(status, next_attempt_at)`.

**`publication_attempts`** — `id`, `job_id` FK, `variant_id`, `platform`, `adapter`,
`attempt_number`, `started_at`, `finished_at`, `success`, `retryable`, `dry_run`,
`external_id`, `error` (sanitized), `retry_after_seconds`.

Integrity, and the constraint name it is declared with (names are pinned so the Alembic
revision and the models can never disagree):

| Constraint | Table | Purpose |
|---|---|---|
| `uq_variants_source_platform` | `variants` | one variant per (source, platform) — regenerating a source cannot fork the review history |
| `uq_jobs_idempotency_key` | `scheduled_jobs` | the real duplicate-publish guarantee |
| `uq_attempt_job_number` | `publication_attempts` | one row per attempt number, so history is auditable |
| `ix_jobs_due` (`status`, `next_attempt_at`) | `scheduled_jobs` | the due-job scan |
| `ix_variants_status`, `ix_variants_source` | `variants` | list filters |
| `ix_attempts_variant` (`variant_id`, `started_at`) | `publication_attempts` | history per variant |

Foreign keys are enforced per connection (`PRAGMA foreign_keys=ON`) and `ON DELETE CASCADE`
is declared on every child table, so deleting a source removes its variants, their jobs and
their attempts — verified by a test that asserts the rows are actually gone.

### The single-slot rule

Because `uq_variants_source_platform` gives a source exactly one variant per platform, a
variant is the unit of publication and it publishes **once**. `POST /variants/{id}/schedule`
is only legal from `approved`; once the variant is `scheduled` or `published`, asking for a
different slot is a `409 variant_not_approved` rather than a second job. Rescheduling the
*same* slot is not an error — it returns the existing job with `deduplicated: true`.

This is a deliberate scope limit, not an accident: the brief's workflow is
`draft → approved → scheduled → published`, and a variant that could be scheduled twice would
need a campaign-level identity the data model does not have. To publish again, regenerate or
reopen the variant (only possible while nothing was published) and schedule it as a new
variant. Stated again in README §10 so no reader assumes multi-slot campaigns are supported.

---

## 4. API contract

Base: `http://127.0.0.1:8020`. All errors: `{"error": {"code", "message", "details"?}}`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Banner: service name, version, docs link (no state) |
| GET | `/health` | Liveness + DB check (`ready` / `schema_missing` / `unreachable`) |
| GET | `/config` | `public_summary()` — non-secret settings only |
| POST | `/sources` | Create source from pasted Markdown/text → 201 |
| POST | `/sources/ingest` | Fetch and store a URL → 201, or 422 with a reason |
| GET | `/sources` | List (limit/offset) |
| GET | `/sources/{id}` | Retrieve one |
| POST | `/sources/{id}/variants` | Generate variants (optional `platforms` list) → 201 |
| GET | `/variants` | List (`status`, `platform`, `source_id` filters) |
| GET | `/variants/{id}` | Retrieve one |
| PATCH | `/variants/{id}` | Edit draft text/hashtags → revalidated |
| POST | `/variants/{id}/validate` | Re-run constraint checks |
| POST | `/variants/{id}/approve` | `draft` → `approved` (rejects invalid) |
| POST | `/variants/{id}/reject` | `draft` → `rejected` |
| POST | `/variants/{id}/reopen` | `rejected` → `draft` |
| POST | `/variants/{id}/schedule` | `approved` + valid only → 201 job |
| GET | `/jobs` | List (`status` filter) |
| GET | `/jobs/{id}` | Job detail incl. attempts |
| GET | `/publications` | Durable publication history (`variant_id`, `job_id` filters) |
| GET | `/publishers` | Adapter status/capabilities, no secrets |
| POST | `/ops/run-due` | Protected trigger for due jobs (needs `X-Ops-Token`) |
| GET | `/docs`, `/openapi.json` | Generated OpenAPI |

Status codes: 200 read/transition (and deduplicated schedule), 201 create, 404 unknown id,
409 invalid state transition, 422 validation / ingestion / unknown field, 403 missing or wrong
ops token, 503 ops endpoint disabled (`OPS_TOKEN` unset) or schema not migrated, 500 for an
unexpected fault — with the exception text never echoed back.

Stable machine-readable `code` values — they are part of the contract and the tests assert on
them, grouped by the boundary that raises them:

| Area | Codes |
|---|---|
| Generic | `not_found`, `method_not_allowed`, `request_invalid`, `http_error`, `internal_error` |
| Sources | `source_not_found`, `empty_source`, `no_platforms`, `variant_not_editable`, `variant_not_found` |
| URL ingestion | `url_missing`, `url_invalid`, `url_too_long`, `url_scheme_not_allowed`, `url_credentials_not_allowed`, `url_dns_failed`, `url_target_not_public`, `redirect_invalid`, `redirect_loop`, `too_many_redirects`, `unsupported_content_type`, `response_too_large`, `fetch_timeout`, `fetch_failed`, `upstream_http_error`, `no_readable_content` |
| Review / scheduling | `variant_invalid`, `invalid_transition`, `variant_not_approved`, `naive_datetime`, `schedule_too_far`, `job_not_found` |
| Ops | `ops_disabled`, `ops_forbidden`, `unauthorized` |

---

## 5. Constraint profiles

| Platform | Max chars | Hashtags | Tone / formatting |
|---|---|---|---|
| `x` | 280 | 1–3 | Punchy, single line, no markdown, link allowed |
| `linkedin` | 3000 | 1–5 | Professional, short paragraphs, no markdown emphasis |
| `telegram` | 4096 | 1–10 | Channel voice, blank-line separated, markdown allowed |

Each profile also declares `min_chars`. Validation returns **errors** (blocking: too long,
too short, too many/few hashtags, forbidden markdown for the platform, empty text) and
**warnings** (non-blocking: no call-to-action, hashtag not lowercase, text near the limit).
Approval is refused while errors exist; warnings are recorded and surfaced but do not block
a human decision. Content is **never silently truncated** — over-length is a 422.

---

## 6. Review state machine

```
                 ┌──────────── approve (revalidates) ────────────┐
                 │                                               ▼
   draft ────────┴───────────────────────────────────────────► approved
     │  ▲                                                          │
     │  └──── reopen ──── rejected ◄──── reject ────────────┐      │ schedule
     │                                                      │      ▼
     └──── reopen ──────────────────────────────────────────┘   scheduled
                                                                  │    ▲
                                        successful delivery       │    │ terminal failure
                                                                  ▼    │ (variant → approved)
                                                              published
```

The five statuses are the only values the column can hold; `scheduled` and `published` have
no outgoing review action, which is what makes a published variant immutable.

Rules enforced in `services/review.py`:
* New variants are `draft`.
* `approve` requires zero validation errors (422 `variant_invalid` otherwise).
* `reject` is allowed from `draft` only.
* `reopen` moves `rejected` → `draft` **and** `approved` → `draft` (text preserved, review
  restarts). Reopening is refused from `scheduled` and `published`.
* `approve`/`reject` accept an optional `reason`. It is recorded as an extra entry in the
  variant's `validation.warnings` (`"approved: …"` / `"rejected: …"`, truncated to 200
  characters) so the audit trail explains *why* — it is never an override and cannot make
  invalid content approvable.
* `schedule` requires `approved` **and** a valid variant; anything else is 409/422 and
  creates **no** job.
* Rejected variants can never be scheduled directly (no transition exists).
* `published` is terminal for ordinary transitions — repeat scheduling of a published
  variant is a 409, so a second publish cannot be triggered by state edits.
* Edits are allowed only in `draft`; editing cleared the previous approval by construction.

---

## 7. Publisher interface

```python
class SocialPublisher(Protocol):
    name: str          # "mock_x" | "mock_linkedin" | "telegram"
    platform: str      # "x" | "linkedin" | "telegram"
    def capabilities(self) -> PublisherCapabilities: ...
    def status(self) -> PublisherStatus: ...         # configured / enabled / reason, no secrets
    def publish(self, req: PublishRequest) -> PublishResult: ...
```

`PublishRequest` carries `text`, `hashtags`, `idempotency_key`, `variant_id`, `scheduled_for`.
`PublishResult` carries `ok`, `external_id`, `dry_run`, `detail`.

Failures raise a typed hierarchy: `PublisherRetryableError(retry_after_seconds)`,
`PublisherPermanentError`, `PublisherNotConfigured`, `PublisherDryRunAborted`. The scheduler
maps these to retry / terminal failure; adapters never decide policy.

Adding a platform = new adapter class + a profile entry + a registry line. The campaign
business logic (`services/publishing.py`, `scheduler/worker.py`) is unchanged — asserted by
a test that registers a fake adapter and runs it through the same path.

### Adapters

* **`MockXPublisher` / `MockLinkedInPublisher`** — no network, deterministic external id
  `mock-<platform>-<sha256(idempotency_key)[:12]>`. They keep an in-process registry so a
  repeated key returns the *same* id and is reported as `deduplicated`; the registry is
  explicitly documented as non-durable demo state, while the real duplicate guarantee comes
  from the DB unique key. Deterministic failure modes are injectable
  (`fail_mode="retryable"|"permanent"`, `retry_after`) for tests.
* **`TelegramPublisher`** — official Bot API `POST {base}/bot{token}/sendMessage` with
  `chat_id`, `text`. Configured only when `TELEGRAM_ENABLED=true` **and** a bot token and
  chat id are present. Unconfigured → `PublisherNotConfigured` (job fails visibly; it is
  never swapped for a mock). `TELEGRAM_DRY_RUN=true` builds and logs the request shape but
  raises `PublisherDryRunAborted` **before** the network call: the attempt is recorded as a
  failure with `dry_run=true`, so no run can ever claim a delivery that did not happen.
  HTTP 429 / 5xx / timeouts → retryable (honouring `Retry-After`); 400/401/403 → permanent.
  The token is never logged; error text is redacted before persistence.

---

## 8. Scheduling lifecycle

```
schedule → pending(attempts=0, next_attempt_at=scheduled_for)
   │ due
   ▼
processing(claimed_at, claim_token) ──success──► completed (variant → published)
   │                         └──retryable──► pending(attempts+1, next_attempt_at=now+delay)
   │                         └──permanent──► failed (terminal, last_error recorded)
   └──crash (row stuck in processing) — recovered when claimed_at < now - STALE_AFTER
```

* **Claiming:** a single `UPDATE … WHERE id IN (SELECT id … WHERE status='pending' AND
  next_attempt_at<=now …) AND status='pending'` sets `status='processing'` and a fresh
  `claim_token`; the worker then selects only rows with that token. SQLite serialises
  writers, and a duplicate publication is additionally impossible because the idempotency
  key is unique and completed jobs are never re-run — so a double-claim degrades to a
  no-op rather than a double post.
* **Recovery:** at the start of every tick, `processing` rows older than
  `STALE_CLAIM_SECONDS` (default 900) return to `pending` with `attempts` preserved and a
  `recovered` log line. This is the crash-recovery path.
* **Retries:** bounded by `max_attempts` (default 3). Retryable failure → exponential
  backoff `min(BASE * 2**(attempts-1), MAX)` unless the adapter supplied `retry_after`, in
  which case `now + retry_after` wins (never earlier). Attempts are always incremented, so a
  permanent failure ends in `failed` after the first attempt and a retryable one ends in
  `failed` after `max_attempts`.
* **Dry run is not an attempt.** `PublisherDryRunAborted` writes an attempt row with
  `dry_run=true`, returns the job to `pending` **without** incrementing `attempts` and without
  moving `next_attempt_at`, and the tick counts it as `skipped` rather than `completed` or
  `failed`. So a dry-run loop can never burn the retry budget, and the job is still there to
  be delivered for real once `TELEGRAM_DRY_RUN=false`. Attempt numbers are derived from
  `max(attempt_number)` in the history rather than from `job.attempts`, so the dry-run row can
  coexist with the later real attempt without violating `uq_attempt_job_number`.
* **Terminal failure returns the variant to `approved`**, not to a dead state: the job is
  `failed` (terminal, visible in history) and a human decides whether to fix, reopen or
  reschedule. Nothing retries a failed job automatically.
* **Clock injection:** every function takes `now: datetime` and the worker accepts a
  `sleep`/`clock` seam, so tests advance time instead of sleeping.
* **Isolation:** one failing job never aborts the tick; the loop continues and the failure
  is persisted.
* **Time zone:** all stored times are UTC (`datetime.now(timezone.utc)`); the API accepts
  ISO-8601 with an offset and normalises to UTC. Naive datetimes are rejected with 422.

---

## 9. Idempotency

`idempotency_key = sha256(f"{variant_id}|{scheduled_for:%Y-%m-%dT%H:%M:%SZ}")[:32]`,
persisted with a UNIQUE constraint.

* Same variant + same slot requested twice → one job; the second call returns the existing
  job with `deduplicated: true` and 200 (not an error — the request is idempotent).
* Concurrent identical requests → one insert wins, the loser catches `IntegrityError`,
  re-reads and returns the winner.
* Retries reuse the job (and therefore the adapter's idempotency key), so the adapter can
  recognise a repeat.
* A job already `completed` is never re-run (`publish_job` short-circuits and returns the
  stored `external_id`).
* **Documented limitation:** exactly-once *external* delivery is impossible in general — a
  remote API can accept a message and the client can lose the response. Mocks deduplicate
  by key, and Telegram has no such facility, so the guarantee we actually provide is
  "at most one *recorded* attempt set per slot, with retries reusing the same key". This is
  stated in README and EVIDENCE rather than claimed away.

---

## 10. Security boundaries

* **SSRF:** only `http`/`https`; hostnames resolved with `socket.getaddrinfo` and every
  resolved address rejected if private/loopback/link-local/reserved/multicast/unspecified;
  redirects followed manually (max 3) with re-validation of each hop; 2 MB response cap;
  content-type allowlist; connect/read timeouts. The resolver and transport are injectable
  so the tests do not touch the network.
* **Secrets:** config comes from the environment via `pydantic-settings`; `.env` is
  git-ignored; adapter status endpoints expose only booleans and a reason string; exception
  text is redacted (`token=…`, `bot\d+:` patterns) before it is stored or returned.
* **Operational endpoint:** `POST /ops/run-due` requires `X-Ops-Token` compared with
  `hmac.compare_digest`; if `OPS_TOKEN` is unset the endpoint returns 503 and is unusable
  rather than open. The CLI is the primary mechanism.
* **Input handling:** Pydantic models with `extra="forbid"` at the API boundary; text
  length ceilings; no `eval`, no pickle, no shell exec; errors never leak stack traces.

---

## 11. Failure scenarios and trade-offs

| Scenario | Behaviour |
|---|---|
| URL fetch fails / times out / oversized / wrong type | 422 with a stable error code; **no** source row is created |
| URL resolves to a private address | 422 `url_target_not_public` (before any connection) |
| Variant exceeds platform limit | 422 with the exact limit; nothing is truncated |
| Approve an invalid variant | 422 `variant_invalid` |
| Schedule a non-approved variant | 409 `variant_not_approved`; no job row |
| Schedule a second slot for a scheduled/published variant | 409 `variant_not_approved`; the first job is untouched |
| Duplicate schedule of the same slot | 200 + existing job + `deduplicated: true` |
| Telegram unconfigured | Job `failed` with the `PublisherNotConfigured` message; history shows it; never mocked silently |
| Telegram dry-run enabled | Attempt recorded with `dry_run=true`; job back to `pending`, attempts unchanged; nothing is sent and nothing claims success |
| 429 from a publisher | Retry not before `now + Retry-After`; persisted in `next_attempt_at` |
| Worker dies mid-publish | Job returns to `pending` after `STALE_CLAIM_SECONDS` and reuses the same idempotency key |
| Permanent publisher error | Terminal `failed` after one attempt; visible in history |
| SQLite write contention | Single writer, short transactions; the worker is best-effort single-instance (documented) |

**Trade-offs accepted:** SQLite limits multi-worker throughput; the mock registry is
in-process; there is no UI; SSRF defence is allowlist-by-IP-resolution (not a full egress
proxy); publication history is append-only and never edited.

---

## 12. Migrations and how the design was verified against the code

The schema is created by Alembic, never by `create_all()` in application code
(`create_all` appears only in the test fixture, where it is the right tool).
`alembic/versions/0001_initial_schema.py` is hand-written rather than autogenerated so every
constraint keeps the exact name the models declare — a renamed constraint would make a future
diff look like a schema change.

```
alembic upgrade head    # creates sources, variants, scheduled_jobs, publication_attempts
#   + alembic_version
```

Drift check, run after the models were final:

```
$ alembic check
No new upgrade operations detected.
```

That is the evidence that the migration and the model metadata agree; without it, "the
models match the database" would only be an assumption. The five statuses and four job
statuses are *not* enforced by CHECK constraints — they are enforced in the service layer,
which is the only writer. A CHECK constraint would have to be edited every time the
lifecycle grows, and the service-level errors carry the useful message (`409` with `from`,
`to` and `allowed`).

Other design claims and where they are actually checked:

| Claim | Where it is checked |
|---|---|
| Migrations match the models | `alembic check` (manual, reproduced in EVIDENCE §2) |
| Duplicate slot → one job | `tests/test_schedule_worker.py` (DB `IntegrityError` path) |
| Crash recovery closes the dangling attempt | `tests/test_schedule_worker.py` + live probe in EVIDENCE §12 |
| Retries stop at `max_attempts` | `tests/test_schedule_worker.py`, live probe in EVIDENCE §13 |
| No secret reaches the database or the API | `tests/test_config.py`, `tests/test_publishers.py`, secret scan in EVIDENCE §16 |
| `capstone.yaml` matches the routes | `tests/test_capstone_yaml.py` parses the manifest and compares it with `app.routes` |
