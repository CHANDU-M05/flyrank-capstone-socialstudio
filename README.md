# Social Media Studio — FlyRank Capstone 05

One blog post in, platform-specific drafts out, a human decides, and the database — not a
browser tab — remembers what still has to be published.

```text
paste ─┐                        ┌─ X        (mock)      ┐
       ├─► source (stored) ─► variants ─► review ─► scheduled_jobs ─► publisher ─► history
URL ───┘   SSRF-guarded        per-platform  draft →   durable rows      Telegram   append-only
           fetch               constraints   approved   claimed by a      or a mock
                                                     separate worker
```

The interesting problem is not CRUD. It is that **a scheduled post must not be lost or sent
twice**: a timer lives inside one process, a row lives in the database, and only one of those
survives a restart.

| | |
|---|---|
| Stack | Python 3.11+ (verified on 3.14.8), FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic, SQLite, httpx, pytest |
| Entrypoint | `app.main:app` (factory `create_app()`) |
| Database | SQLite file — no Docker, no Redis, no external service |
| Scheduler | the `scheduled_jobs` table + `scripts/run_worker.py` (no APScheduler, no in-memory timers) |
| Tests | **168 passing**, fully offline (injected transport, no network, no credentials) |
| Live checks | **37 passing** against a real uvicorn over TCP (`scripts/smoke_test.py`) |

Everything below was executed on this machine. Verbatim transcripts are in
[EVIDENCE.md](EVIDENCE.md), the build history including every bug found and fixed is in
[BUILDLOG.md](BUILDLOG.md), and the pre-implementation design is in [DESIGN.md](DESIGN.md).

---

## 1. Scope

**In scope:** source ingestion (paste + SSRF-guarded URL fetch), deterministic variant
generation from a *stored* source, per-platform constraint validation that never truncates
silently, a guarded review workflow, three publisher adapters (real Telegram + two
deterministic mocks), durable DB-backed scheduling with bounded retries and crash recovery,
idempotent scheduling, append-only publication history, and a documented REST/OpenAPI surface.

**Deliberately out of scope:** a UI (API only), an LLM (templates only — the composer is an
injectable seam), real X/LinkedIn APIs, multi-tenant accounts, Docker/Redis/Celery, media
uploads, multi-slot campaigns per variant (see §10).

## 2. Architecture and component responsibilities

```
app/
├── main.py            create_app(settings, registry): lifespan, /, error handlers, OpenAPI tags
├── config.py          pydantic-settings; public_summary() is the only loggable shape
├── api/
│   ├── deps.py        session, settings, registry, pagination, ops-token guard (hmac.compare_digest)
│   ├── errors.py      one error envelope + handlers; a 500 never echoes the exception
│   └── routes.py      HTTP only: read request → call service → shape response
├── services/
│   ├── ingest.py      SSRF-guarded fetch: scheme/host/port checks, manual redirects, size cap
│   ├── sources.py     store pasted or fetched content; nothing is derived here
│   ├── variants.py    deterministic composer (hook + summary + CTA + hashtags) and edits
│   ├── profiles.py    platform constraints as *data*, one validate() implementation
│   ├── review.py      the only state machine: draft/approved/rejected/scheduled/published
│   ├── scheduling.py  idempotency key, slot normalisation, schedule_variant()
│   ├── publishing.py  the only caller of SocialPublisher.publish(); writes every attempt
│   └── sanitize.py    redact() removes tokens and auth pairs before persistence
├── publishers/        base.py (Protocol + typed errors), mock.py, telegram.py, registry.py
├── scheduler/worker.py recover stale claims → claim due jobs → publish → record outcome
├── models/entities.py SQLAlchemy 2.x typed models + pinned constraint names
└── db/                engine/session (PRAGMA foreign_keys=ON) and UTCDateTime (rejects naive)
```

Layering rule, enforced by review rather than by imports: **handlers** do auth, validation and
serialisation; every state change goes through a **service**; **adapters** never touch the
database; the **worker** is the only writer of publication attempts. Adapters return domain
errors, never HTTP codes — retry policy is decided in one place.

## 3. Prerequisites

* Python **3.11 or newer** (built and verified on CPython 3.14.8).
* No database server, no Docker, no Node.
* Optional: a Telegram bot token and a chat id, only if you want to verify real delivery
  (see §8). Everything else runs without credentials.

## 4. Installation

```bash
git clone <this-repo> flyrank-capstone-social-studio
cd flyrank-capstone-social-studio

python3 -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -r requirements.txt     # runtime only
pip install -r requirements-dev.txt # + pytest, pyyaml
```

## 5. Configuration

All settings come from the environment or a local `.env`; every one has a safe default, so
`.env` is optional. `.env.example` documents every variable.

```bash
cp .env.example .env
```

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./studio.db` | Any SQLAlchemy URL; SQLite by default |
| `OPS_TOKEN` | *(empty)* | Shared secret for `POST /ops/run-due`. **Empty disables `/ops` with 503** |
| `MAX_ATTEMPTS` | `3` | Retry ceiling per job |
| `RETRY_BASE_SECONDS` / `RETRY_MAX_SECONDS` | `30` / `900` | Exponential backoff base and cap |
| `STALE_CLAIM_SECONDS` | `900` | A claim older than this is treated as a crashed worker |
| `SCHEDULER_POLL_SECONDS` | `5` | Worker sleep between ticks |
| `INGEST_TIMEOUT_SECONDS` / `INGEST_MAX_BYTES` / `INGEST_MAX_REDIRECTS` | `5` / `2000000` / `3` | URL-fetch limits |
| `TELEGRAM_ENABLED` | `false` | Turns the real adapter on (requires the two values below) |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | *(empty)* | Real credentials; never logged |
| `TELEGRAM_DRY_RUN` | `false` | Build the request, then abort **before** the network call |
| `TELEGRAM_API_BASE` | `https://api.telegram.org` | Overridable so a test can point at a closed port |

Invalid combinations fail at startup rather than at publish time: `TELEGRAM_ENABLED=true`
without a token and chat id is a configuration error, `RETRY_MAX < RETRY_BASE` is rejected,
and an `OPS_TOKEN` shorter than 8 characters is rejected. `GET /config` returns
`public_summary()` — booleans and non-secret settings, never a token:

```json
{"environment":"development","database":"sqlite",
 "scheduler":{"max_attempts":3,"poll_seconds":5,"retry_base_seconds":0,"retry_max_seconds":0,"stale_claim_seconds":900},
 "ingest":{"timeout_seconds":5.0,"max_bytes":2000000,"max_redirects":3},
 "telegram":{"enabled":true,"configured":true,"dry_run":false},
 "ops_endpoint_enabled":true}
```

## 6. Database initialisation

Migrations only — application code never calls `create_all()`.

```bash
alembic upgrade head
```

```text
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, Initial schema: sources, variants, scheduled jobs, publication attempts.
```

Four tables plus `alembic_version`: `sources`, `variants`, `scheduled_jobs`,
`publication_attempts`. To confirm the migration still matches the models:

```bash
alembic check        # No new upgrade operations detected.
```

`/health` distinguishes a missing schema from an unreachable file, so a forgotten migration
is visible instead of mysterious:

```json
{"status":"ok","database":"ready","version":"1.0.0"}
```

## 7. Local development

```bash
# terminal 1 — the API
uvicorn app.main:app --host 127.0.0.1 --port 8020

# terminal 2 — the scheduler (publishes; the API never does, except via /ops/run-due)
python scripts/run_worker.py --loop

# terminal 2, alternative — a single tick, useful in scripts
python scripts/run_worker.py --once
```

`scripts/seed.py` creates one realistic source, three validated drafts and approves them; it
is idempotent:

```bash
python scripts/seed.py                 # source + 3 variants + approvals
python scripts/seed.py --publish-now   # also schedules the Telegram variant, due immediately
```

## 8. API

Base URL `http://127.0.0.1:8020`. Interactive docs at `/docs`, schema at `/openapi.json`
(19 documented paths). Every error has one shape:

```json
{"error": {"code": "machine_readable_code", "message": "human readable explanation", "details": {}}}
```

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/` | none | Banner: name, version, docs link |
| GET | `/health` | none | Liveness + database/schema state |
| GET | `/config` | none | Non-secret configuration |
| GET | `/publishers` | none | Adapter status: configured/enabled + reason, no secrets |
| POST | `/sources` | none | Store pasted Markdown/text → 201 |
| POST | `/sources/ingest` | none | Fetch and store a URL → 201, or 422 with a reason |
| GET | `/sources` | none | List (`limit`, `offset`) |
| GET | `/sources/{source_id}` | none | Retrieve one, including content |
| POST | `/sources/{source_id}/variants` | none | Generate drafts (optional `platforms`) → 201 |
| GET | `/variants` | none | List (`status`, `platform`, `source_id`) |
| GET | `/variants/{variant_id}` | none | Retrieve one |
| PATCH | `/variants/{variant_id}` | none | Edit a draft's text/hashtags → revalidated |
| POST | `/variants/{variant_id}/validate` | none | Re-run constraint checks |
| POST | `/variants/{variant_id}/approve` | none | `draft` → `approved`; refuses invalid content |
| POST | `/variants/{variant_id}/reject` | none | `draft` → `rejected` |
| POST | `/variants/{variant_id}/reopen` | none | `rejected`/`approved` → `draft` |
| POST | `/variants/{variant_id}/schedule` | none | `approved` + valid → 201 job (200 if the slot exists) |
| GET | `/jobs` | none | List (`status`, `variant_id`) |
| GET | `/jobs/{job_id}` | none | Job detail |
| GET | `/publications` | none | Publication history (`variant_id`, `job_id`) |
| POST | `/ops/run-due` | `X-Ops-Token` | Run one scheduler tick now |

Status codes: 200 read/transition/deduplicated, 201 created, 404 unknown id, 409 invalid state
transition, 422 validation/ingestion/unknown field, 403 missing or wrong ops token, 503 `/ops`
disabled or schema not migrated.

### Store a source from a URL

```bash
curl -s -X POST http://127.0.0.1:8020/sources/ingest \
  -H 'Content-Type: application/json' -d '{"url":"https://example.com"}'
```

```json
{"id":"fa0e00a09bfb479da1a7eca98fc6b7de","source_type":"url","origin_url":"https://example.com/",
 "title":"Example Domain","content_type":"text/html","content":"Example Domain\n\nThis domain is for use in illustrative examples …"}
```

Ingestion refuses anything that could reach the host's own network — the checks happen before
a socket is opened, and every redirect is re-validated:

```bash
curl -s -X POST .../sources/ingest -d '{"url":"file:///etc/passwd"}'
# 422 {"error":{"code":"url_scheme_not_allowed","message":"Unsupported URL scheme 'file'. Only http and https are allowed."}}

curl -s -X POST .../sources/ingest -d '{"url":"http://169.254.169.254/latest/meta-data/"}'
# 422 {"error":{"code":"url_target_not_public","message":"Refusing to fetch the non-public address '169.254.169.254'."}}

curl -s -X POST .../sources/ingest -d '{"url":"https://this-host-does-not-exist-flyrank.invalid/post"}'
# 422 {"error":{"code":"url_dns_failed","message":"Could not resolve host '…': gaierror."}}
```

A failed fetch creates **no** source row.

### Generate variants

```bash
curl -s -X POST http://127.0.0.1:8020/sources/$SID/variants \
  -H 'Content-Type: application/json' -d '{}'
```

Three drafts, each validated against its own profile. Real output for the seeded document:

```text
x         valid=True warnings=[] chars=113
linkedin  valid=True warnings=[] chars=461
telegram  valid=True warnings=[] chars=490
```

```text
*How a durable scheduler works*

A timer lives inside one process; a row lives in the database. When publishing has to survive a
restart, the database has to own the schedule. Same clock everywhere. Every timestamp is UTC and
slots are truncated to whole seconds, which is also the granularity of the idempotency key.
Failures are expected. Transient failures get exponential backoff. Rate limits get the delay the
platform asks for. Permanent failures stop immediately.

More updates soon.
```

Generation is deterministic: the same source plus the same profile produces byte-identical
text, and regenerating never overwrites a variant that has already been reviewed.

### Constraint validation never truncates

Editing a draft revalidates it and reports why it is unacceptable; approval then refuses:

```bash
curl -s -X PATCH .../variants/$XID -d '{"text":"too short"}'
# 200 {"status":"draft","validation":{"valid":false,"errors":["text is too short: 9 characters; X needs at least 40"]}}

curl -s -X POST .../variants/$XID/approve -d '{}'
# 422 {"error":{"code":"variant_invalid","message":"The variant does not satisfy the X profile: text is too short: 9 characters; X needs at least 40"}}
```

There is no override flag: text a platform would reject cannot be approved, and nothing is
silently cut to fit.

### Review

```bash
curl -s -X POST .../variants/$LID/approve -d '{"reason":"copy reads well"}'
# 200 {"status":"approved","validation":{"valid":true,"warnings":["approved: copy reads well"]}}

curl -s -X POST .../variants/$LID/approve -d '{}'
# 409 {"error":{"code":"invalid_transition","message":"Cannot approve a variant in status 'approved'. From 'approved' the allowed review actions are: reopen."}}

curl -s -X POST .../variants/$XID/schedule -d "{\"scheduled_for\":\"$(date -u +%FT%TZ)\"}"
# 409 {"error":{"code":"variant_not_approved","message":"Only approved variants can be scheduled; this one is 'rejected'. …","details":{"status":"rejected"}}}
```

### Schedule, then publish

```bash
SLOT=$(date -u -d '-5 seconds' +%Y-%m-%dT%H:%M:%SZ)
curl -s -X POST .../variants/$LID/schedule -d "{\"scheduled_for\":\"$SLOT\"}"
# 201 {"job":{"id":"4b5aff45…","status":"pending","scheduled_for":"…","attempts":0,"max_attempts":3,
#      "idempotency_key":"a242a3a80120f58bc4c7ea491ec7fbba", …},"deduplicated":false}

curl -s -X POST .../variants/$LID/schedule -d "{\"scheduled_for\":\"$SLOT\"}"
# 200 {"job":{…same id…},"deduplicated":true}

curl -s -X POST .../ops/run-due -H 'Content-Type: application/json' \
  -H "X-Ops-Token: $OPS_TOKEN" -d '{"limit":5}'
# {"claimed":1,"completed":1,"retried":0,"failed":0,"skipped":0,"recovered":0,"jobs":[{"status":"completed",
#  "external_id":"mock_linkedin-dc1315219c89","attempts":1,…}]}
```

Same slot twice → one job. A *different* slot for a variant that is already scheduled or
published → `409`, because a variant publishes once (§10).

### History

```bash
curl -s "http://127.0.0.1:8020/publications?variant_id=$LID"
# {"total":1,"items":[{"attempt_number":1,"success":true,"adapter":"mock_linkedin","dry_run":false,
#   "external_id":"mock_linkedin-dc1315219c89","scheduled_for":"2026-10-25T20:01:37Z"}]}
```

## 9. End-to-end workflow

```bash
# 1. source
SID=$(curl -s -X POST localhost:8020/sources -H 'Content-Type: application/json' \
  -d '{"title":"My post","content":"# My post\n\n…"}' | jq -r .id)

# 2. drafts
curl -s -X POST localhost:8020/sources/$SID/variants -H 'Content-Type: application/json' -d '{}' > v.json
LID=$(jq -r '.[]|select(.platform=="linkedin")|.id' v.json)

# 3. human decision
curl -s -X POST localhost:8020/variants/$LID/approve -H 'Content-Type: application/json' -d '{}' > /dev/null

# 4. durable slot (UTC, whole seconds, at most 365 days out)
curl -s -X POST localhost:8020/variants/$LID/schedule -H 'Content-Type: application/json' \
  -d '{"scheduled_for":"2026-11-01T09:00:00Z"}'

# 5. let the worker run (or trigger one tick)
python scripts/run_worker.py --once

# 6. audit
curl -s "localhost:8020/publications?variant_id=$LID" | jq
```

`scripts/smoke_test.py` performs exactly this sequence over TCP, plus the negative paths
(unapproved scheduling, SSRF, duplicate slots, token guard, unconfigured Telegram):

```bash
python scripts/smoke_test.py --base-url http://127.0.0.1:8020 --ops-token "$OPS_TOKEN"
```

## 10. Scheduler operation and crash recovery

The database is the queue. There is no timer to lose.

```text
schedule ─► pending(attempts=0, next_attempt_at=slot)
                │ due
                ▼
           processing(claim_token, claimed_at) ──success──► completed → variant published
                │            ├──retryable──► pending(attempts+1, next_attempt_at=now+delay)
                │            └──permanent──► failed (terminal) → variant back to approved
                └──worker died──► recovered once claimed_at < now − STALE_CLAIM_SECONDS
```

* **Ticking.** The API cannot publish; `scripts/run_worker.py` is the publisher. `POST
  /ops/run-due` exists for operators and scripts and always re-checks `next_attempt_at`, so a
  manual tick can never publish a job that is not due yet.
* **Claiming.** `UPDATE … SET status='processing', claim_token=? WHERE id=? AND status='pending'`
  returns `rowcount == 1` for exactly one worker. SQLite serialises writers, so the loser's
  update matches nothing and is skipped instead of publishing.
* **Retries.** Bounded by `MAX_ATTEMPTS`. A retryable failure backs off
  `min(BASE × 2^(attempts−1), MAX)`; if the adapter supplied `Retry-After`, that delay wins.
  Attempts are persisted, so a restart resumes the count instead of starting over.
* **Crash recovery.** Every tick first returns `processing` rows whose claim is older than
  `STALE_CLAIM_SECONDS` to `pending` and closes any attempt that never finished with
  `interrupted: worker claim expired before an outcome was recorded`. Verified live: a job
  stranded with a two-hour-old claim came back as `{"recovered":1,"claimed":1,"completed":1}`,
  attempt 1 recorded as interrupted, attempt 2 succeeded, variant published (EVIDENCE §12).
* **Dry run is not a retry.** With `TELEGRAM_DRY_RUN=true` the attempt row is written with
  `dry_run=true`, the job returns to `pending`, `attempts` stays at 0 and the tick counts
  `skipped`. A dry-run loop cannot burn the retry budget (EVIDENCE §14).
* **Single writer.** One worker process per database is the supported deployment. Two workers
  are safe for correctness (the claim is atomic) but contend on the SQLite write lock.

## 11. Telegram setup and safe test mode

Telegram is the one real publisher. It is never silently replaced by a mock.

```bash
# 1. talk to @BotFather, create a bot, copy the token
# 2. get a chat id: message the bot, then read https://api.telegram.org/bot<token>/getUpdates
# 3. put both in .env (never in the shell history if you can avoid it)
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=123456789:AA…real…token
TELEGRAM_CHAT_ID=-1001234567890
TELEGRAM_DRY_RUN=true   # start here
```

`GET /publishers` always answers honestly:

```json
{"name":"telegram","configured":false,"enabled":false,
 "detail":"Disabled: TELEGRAM_ENABLED is false, so no message can be sent."}
```

With `TELEGRAM_DRY_RUN=true`, a due job builds and validates the request, logs its shape,
writes an attempt with `dry_run=true` and **aborts before any network call**:

```json
{"claimed":1,"completed":0,"retried":0,"failed":0,"skipped":1}
{"status":"pending","attempts":0,"last_error":"TELEGRAM_DRY_RUN=true: request validated and aborted before sending; no message was delivered."}
```

Nothing claims a delivery that did not happen. Turn the flag off to publish for real: 429 and
5xx are retryable (honouring `Retry-After`), 400/401/403 are permanent.

> **This build did not send a real Telegram message.** No bot token or chat id was available in
> this environment, so real delivery is *unverified* — see EVIDENCE §9 and §17.

## 12. Mock publisher behaviour

`mock_x` and `mock_linkedin` make no network calls and are deterministic:

* external id = `<adapter>-<first 12 hex of sha256(idempotency_key)>`
  (`mock_linkedin-dc1315219c89`), so the same job always yields the same id;
* a repeated key is reported as deduplicated by the adapter;
* failure modes are injectable for tests (`fail_mode="retryable"|"permanent"`, `retry_after`),
  which is how the retry path is exercised without touching a real API;
* the in-process dedupe registry is demo state and is **not** durable — the actual duplicate
  guarantee is `scheduled_jobs.idempotency_key UNIQUE` in the database.

## 13. Security considerations

* **SSRF.** Only `http`/`https`; credentials in the URL are rejected; the hostname is resolved
  and every resolved address must be globally routable (private, loopback, link-local,
  reserved, multicast and unspecified are refused) *before* connecting; redirects are followed
  manually with full re-validation per hop; 2 MB response cap; content-type allowlist.
* **Secrets.** Config comes from the environment; `.env` is git-ignored; `/config` and
  `/publishers` expose booleans only; every adapter message passes through `redact()` before
  it is stored, and a 500 response never echoes the exception text.
* **Ops endpoint.** `POST /ops/run-due` requires `X-Ops-Token` compared with
  `hmac.compare_digest`. With `OPS_TOKEN` unset the route returns 503 and is unusable rather
  than open.
* **Input handling.** Pydantic models use `extra="forbid"`; text has length ceilings; there is
  no `eval`, no pickle, no shell invocation anywhere in the request path.
* **Authorisation scope.** There is no user auth: the brief describes a single-operator studio,
  so the API is a trusted-localhost tool. Do not expose it publicly as-is.

## 14. Known limitations (honest scope)

* **Exactly-once external delivery is impossible in general.** A platform can accept a message
  while the response is lost. What this project guarantees is: at most one *recorded* attempt
  set per (variant, slot), retries reusing the same idempotency key, and a job that never
  re-runs once `completed`.
* **A variant publishes once.** Because `variants` is unique per (source, platform), scheduling
  a second slot for an already scheduled/published variant is a `409`. Multi-slot campaigns
  would need a campaign entity this data model does not have.
* **No user accounts or RBAC**; single-tenant by design.
* **SQLite** is the default: one writer at a time, so this is a correctness-first reference, not
  a throughput benchmark (the schema and queries are portable to Postgres).
* **Variant generation is templated, not generative**: the summary is the first sentences of the
  source, so it is accurate but plain. The composer is a seam for an LLM, not an LLM.
* **HTML extraction is heuristic** (tag stripping + entity unescaping), not a full readability
  pipeline; article pages with heavy navigation may keep some noise.
* **DNS rebinding** is not fully prevented: resolution and connection are separate steps, and a
  hostile resolver could answer differently for each. A production deployment would pin the
  resolved IP or use an egress proxy.
* **Real Telegram delivery is unverified** in this environment (no credentials). The adapter,
  its error mapping, the dry-run path and the retry path are all verified.
* **No rate limiter on the public API**; it is a trusted-localhost tool.

## 15. Tests

```bash
pytest -q                      # 168 tests, ~3 s, no network
pytest -q tests/test_schedule_worker.py -v
```

| File | What it pins down |
|---|---|
| `test_config.py` | defaults, invalid combinations rejected, `public_summary()` never leaks a token |
| `test_profiles_validation.py` | per-platform limits, no silent truncation, warning vs error, hashtag rules |
| `test_ingest.py` | SSRF (scheme, credentials, private/loopback/link-local), redirect re-validation, size/type caps, no row on failure |
| `test_variants.py` | determinism, profile fit, reviewed variants never overwritten, title not repeated, lowercase tags |
| `test_review.py` | every legal transition and every illegal one, approval refuses invalid content |
| `test_publishers.py` | mock determinism, Telegram not-configured/dry-run/429/5xx/permanent mapping, no token in errors |
| `test_schedule_worker.py` | idempotency under concurrency (`IntegrityError`), claim race, backoff, `Retry-After`, stale-claim recovery, isolation |
| `test_persistence.py` | UTC round-trip, naive datetime rejected, FK cascade actually deletes |
| `test_api_e2e.py` | the whole workflow over HTTP, error envelope, ops guard, pagination bounds |
| `test_capstone_yaml.py` | `capstone.yaml` parses **and** matches the served OpenAPI paths exactly |

The suite is offline by construction: `httpx.MockTransport` and an injected resolver replace
the network, and each test gets its own temporary SQLite file.

## 16. Evidence

| Artifact | Contents |
|---|---|
| [EVIDENCE.md](EVIDENCE.md) | the 15 required proof points with verbatim transcripts, plus the retry/dry-run probes and the secret scan |
| [BUILDLOG.md](BUILDLOG.md) | chronological build log: decisions, commands, and the eleven real defects found and fixed |
| [evidence/](evidence/README.md) | where to put screenshots or captured output if you add any |
| `tests/`, `scripts/smoke_test.py` | re-runnable proofs: `pytest -q` and the live TCP walkthrough |

## 17. Repository layout

```text
README.md  DESIGN.md  BUILDLOG.md  EVIDENCE.md  capstone.yaml
.env.example  .gitignore  LICENSE  pyproject.toml  requirements.txt  requirements-dev.txt
alembic.ini  alembic/{env.py,script.py.mako,versions/0001_initial_schema.py}
app/  tests/  scripts/{seed.py,run_worker.py,smoke_test.py}  evidence/README.md
```

`capstone.yaml` records the run/worker/seed/test commands, the base URL, the adapters and the
endpoint list; a test compares it with the served OpenAPI document so it cannot drift.

## 18. License

MIT — see [LICENSE](LICENSE).
