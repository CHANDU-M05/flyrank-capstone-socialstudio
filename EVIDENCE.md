# EVIDENCE — Social Media Studio (Capstone 05)

Every block below is a **verbatim transcript** produced on this machine during the build.
Nothing is hand-written to look like output: where a value appears, it came from a real run.
Where a requirement could not be verified, the section says so and explains why (§18).

| # | Required evidence | Status |
|---|---|---|
| 1 | Installation and startup | ✅ captured |
| 2 | API / OpenAPI access | ✅ captured |
| 3 | Pasted-content ingestion | ✅ captured |
| 4 | URL ingestion and failure handling | ✅ captured |
| 5 | Variant generation | ✅ captured |
| 6 | Constraint validation | ✅ captured |
| 7 | Review workflow | ✅ captured |
| 8 | Rejection of unapproved scheduling | ✅ captured |
| 9 | Mock publishing | ✅ captured |
| 10 | Telegram configuration status | ✅ captured |
| 11 | Idempotency | ✅ captured |
| 12 | Scheduler execution + restart recovery | ✅ captured |
| 13 | Retry / terminal failure | ✅ captured |
| 14 | Dry-run safety mode | ✅ captured |
| 15 | Publication history | ✅ captured |
| 16 | Automated test results | ✅ captured |
| 17 | Real Telegram delivery | ❌ **not verified** — no credentials (§18) |
| 18 | Secret scan | ✅ captured |

---

## 1. Environment

| Item | Value |
|---|---|
| OS | Linux (Arch), kernel provided by the host |
| Python | 3.14.8 (`.venv`; project requires 3.11+) |
| Database | SQLite file created by Alembic migrations |
| Telegram | adapter present, **no token or chat id available** |
| Network | outbound HTTPS available (used for one real URL ingestion, §5) |

Pinned dependency install (`pip list` inside `.venv`):

```text
alembic           1.20.1
fastapi           0.162.0
httpx             0.28.1
pydantic          2.14.1
pydantic-settings 2.16.1
pytest            9.1.1
PyYAML            6.0.3
SQLAlchemy        2.1.6
uvicorn           0.54.0
```

## 2. Installation, migration and startup

```console
$ python3 -m venv .venv && source .venv/bin/activate
$ pip install -r requirements.txt -r requirements-dev.txt
$ export DATABASE_URL="sqlite:///./.scratch/readme.db" OPS_TOKEN="readme-ops-token"
$ alembic upgrade head
INFO  [alembic.runtime.migration] Context impl SQLiteImpl.
INFO  [alembic.runtime.migration] Will assume non-transactional DDL.
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, Initial schema: sources, variants, scheduled jobs, publication attempts.
```

Tables created (read back from the file, not assumed):

```console
$ python -c "import sqlite3; print(sorted(r[0] for r in sqlite3.connect('.scratch/readme.db').execute(\"select name from sqlite_master where type='table'\")))"
['alembic_version', 'publication_attempts', 'scheduled_jobs', 'sources', 'variants']
```

Drift check — the migration and the models agree:

```console
$ alembic check
No new upgrade operations detected.
```

Startup:

```console
$ uvicorn app.main:app --host 127.0.0.1 --port 8030
INFO:     Started server process [620265]
INFO:     Waiting for application startup.
2026-10-26 01:32:22,500 INFO     app.main: Social Media Studio 1.0.0 starting
2026-10-26 01:32:22,500 INFO     app.main: configuration: {'environment': 'development', 'database': 'sqlite', 'scheduler': {'max_attempts': 3, 'poll_seconds': 5, 'retry_base_seconds': 30, 'retry_max_seconds': 900, 'stale_claim_seconds': 900}, 'ingest': {'timeout_seconds': 5.0, 'max_bytes': 2000000, 'max_redirects': 3}, 'telegram': {'enabled': False, 'configured': False, 'dry_run': False}, 'ops_endpoint_enabled': True}
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8030 (Press CTRL+C to quit)
```

Note what the logged configuration does **not** contain: the ops token appears only as
`ops_endpoint_enabled: True`, and Telegram only as booleans.

## 3. API and OpenAPI access

```console
$ curl -s http://127.0.0.1:8030/health
{"status":"ok","database":"ready","version":"1.0.0"}

$ curl -s http://127.0.0.1:8030/openapi.json | jq '{title:.info.title,version:.info.version,paths:(.paths|keys|length),tags:[.tags[].name]}'
{
  "title": "Social Media Studio",
  "version": "1.0.0",
  "paths": 19,
  "tags": ["meta","sources","variants","review","scheduling","history","publishers","ops"]
}
```

`/docs` serves the interactive UI from the same document. `tests/test_capstone_yaml.py`
additionally asserts that `capstone.yaml` advertises exactly these 19 paths and no others.

## 4. Pasted-content ingestion

```console
$ curl -s -X POST http://127.0.0.1:8030/sources -H 'Content-Type: application/json' \
    -d '{"title":"How a durable scheduler works","content":"# How a durable scheduler works\n\nA timer lives inside one process; a row lives in the database. …"}'
{
  "id": "5506fb58a94345ec9ada889dfc187425",
  "title": "How a durable scheduler works",
  "source_type": "paste",
  "content_type": "text/markdown",
  "chars": 478
}
```

Blank content is refused (no row is written):

```console
$ curl -s -X POST http://127.0.0.1:8030/sources -d '{"content":"    "}'
422 {"error":{"code":"empty_source","message":"Source content must not be empty."}}
```

## 5. URL ingestion and failure handling

**Success** — a real fetch, against the public internet:

```console
$ curl -s -X POST http://127.0.0.1:8030/sources/ingest -H 'Content-Type: application/json' -d '{"url":"https://example.com"}'
201 {"id":"fa0e00a09bfb479da1a7eca98fc6b7de","source_type":"url","origin_url":"https://example.com/","title":"Example Domain","chars":171}
```

**Failures** — each refused with a stable code, and each leaving the store untouched:

```console
$ curl -s -X POST .../sources/ingest -d '{"url":"file:///etc/passwd"}'
422 {"error":{"code":"url_scheme_not_allowed","message":"Unsupported URL scheme 'file'. Only http and https are allowed."}}

$ curl -s -X POST .../sources/ingest -d '{"url":"http://169.254.169.254/latest/meta-data/"}'
422 {"error":{"code":"url_target_not_public","message":"Refusing to fetch the non-public address '169.254.169.254'."}}

$ curl -s -X POST .../sources/ingest -d '{"url":"http://127.0.0.1:8030/health"}'
422 {"error":{"code":"url_target_not_public","message":"Refusing to fetch the non-public address '127.0.0.1'."}}

$ curl -s -X POST .../sources/ingest -d '{"url":"https://this-host-does-not-exist-flyrank.invalid/post"}'
422 {"error":{"code":"url_dns_failed","message":"Could not resolve host 'this-host-does-not-exist-flyrank.invalid': gaierror."}}
```

Proof that the three rejected URLs wrote nothing — only the paste and the one real fetch exist:

```console
$ curl -s http://127.0.0.1:8030/sources | jq -c '{total,items:[.items[]|{id:.id,title:.title}]}'
{"total":2,"items":[{"id":"fa0e00a09bfb479da1a7eca98fc6b7de","title":"Example Domain"},{"id":"5506fb58a94345ec9ada889dfc187425","title":"How a durable scheduler works"}]}
```

The redirect, size, content-type and timeout paths are covered by
`tests/test_ingest.py` (20 tests) with an injected transport, including a public URL that
redirects into `127.0.0.1` and is refused on the second hop.

## 6. Variant generation

```console
$ curl -s -X POST http://127.0.0.1:8030/sources/5506fb58a94345ec9ada889dfc187425/variants \
    -H 'Content-Type: application/json' -d '{}'
x         valid=True warnings=[] chars=113
linkedin  valid=True warnings=[] chars=461
telegram  valid=True warnings=[] chars=490
```

The Telegram draft, verbatim from the response (`jq -r`):

```text
*How a durable scheduler works*

A timer lives inside one process; a row lives in the database. When publishing has to survive a
restart, the database has to own the schedule. Same clock everywhere. Every timestamp is UTC and
slots are truncated to whole seconds, which is also the granularity of the idempotency key.
Failures are expected. Transient failures get exponential backoff. Rate limits get the delay the
platform asks for. Permanent failures stop immediately.

More updates soon.
```

Hashtags: `["#failures","#lives","#database","#durable","#scheduler","#works"]`. Regenerating
the same source produces the same bytes (`test_generation_is_deterministic`), and a variant that
has already been approved is never overwritten (`test_generation_never_overwrites_a_reviewed_variant`).

## 7. Constraint validation

Editing a draft revalidates it immediately:

```console
$ curl -s -X PATCH http://127.0.0.1:8030/variants/645178b7028a430a98af802a53f178d5 \
    -H 'Content-Type: application/json' -d '{"text":"too short"}'
200 {"status":"draft","validation":{"valid":false,"errors":["text is too short: 9 characters; X needs at least 40"]}}
```

Approval refuses the same variant — there is no override:

```console
$ curl -s -X POST .../variants/645178b7028a430a98af802a53f178d5/approve -d '{}'
422 {"error":{"code":"variant_invalid","message":"The variant does not satisfy the X profile: text is too short: 9 characters; X needs at least 40","details":{"errors":["text is too short: 9 characters; X needs at least 40"],"warnings":[]}}}
```

Over-length behaves the same way — an error naming the limit, never a silent cut
(`test_profiles_validation.py`, 17 tests, including "too long is rejected, not truncated",
"exactly at the limit is accepted", and the markdown rules per platform).

## 8. Review workflow

```console
$ curl -s -X POST .../variants/13764ac2526f4a44a3800b1de33a7db7/approve -d '{"reason":"copy reads well"}'
200 {"id":"13764ac2526f4a44a3800b1de33a7db7","status":"approved","warnings":["approved: copy reads well"]}

$ curl -s -X POST .../variants/13764ac2526f4a44a3800b1de33a7db7/approve -d '{}'
409 {"error":{"code":"invalid_transition","message":"Cannot approve a variant in status 'approved'. From 'approved' the allowed review actions are: reopen.","details":{"from":"approved","to":"approved","allowed":["draft"]}}}

$ curl -s -X POST .../variants/645178b7028a430a98af802a53f178d5/reject -d '{"reason":"too thin for X"}'
200 {"status":"rejected","warnings":["rejected: too thin for X"]}

$ curl -s -X POST .../variants/645178b7028a430a98af802a53f178d5/reopen -d '{}'
200 {"status":"draft"}
```

A published variant is terminal:

```console
$ curl -s -X POST .../variants/13764ac2526f4a44a3800b1de33a7db7/reopen -d '{}'
409 {"error":{"code":"invalid_transition","message":"Cannot reopen a variant in status 'published'. From 'published' the allowed review actions are: none.","details":{"from":"published","to":"draft","allowed":[]}}}
```

## 9. Rejection of unapproved scheduling

A rejected variant cannot be scheduled, and the failed attempt creates **no** job:

```console
$ curl -s http://127.0.0.1:8030/jobs | jq .total
1

$ curl -s -X POST .../variants/645178b7028a430a98af802a53f178d5/schedule -d '{"scheduled_for":"2026-10-25T20:01:42Z"}'
409 {"error":{"code":"variant_not_approved","message":"Only approved variants can be scheduled; this one is 'rejected'. Approve it first (or reopen and edit it, then approve).","details":{"status":"rejected"}}}

$ curl -s http://127.0.0.1:8030/jobs | jq .total
1
```

Naive datetimes are refused too, because a slot without a timezone is ambiguous:

```console
$ curl -s -X POST .../variants/.../schedule -d '{"scheduled_for":"2026-12-01T10:00:00"}'
422 {"error":{"code":"naive_datetime","message":"scheduled_for must be timezone-aware (e.g. 2026-11-01T09:00:00Z)."}}
```

## 10. Mock publishing

```console
$ curl -s -X POST http://127.0.0.1:8030/ops/run-due -H 'Content-Type: application/json' \
    -H 'X-Ops-Token: readme-ops-token' -d '{"limit":5}'
{"claimed":1,"completed":1,"retried":0,"failed":0,"skipped":0,"recovered":0,
 "jobs":[{"id":"4b5aff45db3742e39659049bddb8f4d9","variant_id":"13764ac2526f4a44a3800b1de33a7db7",
          "platform":"linkedin","status":"completed","scheduled_for":"2026-10-25T20:01:37Z",
          "next_attempt_at":"2026-10-25T20:01:37Z","attempts":1,"max_attempts":3,"last_error":null,
          "idempotency_key":"a242a3a80120f58bc4c7ea491ec7fbba",
          "external_id":"mock_linkedin-dc1315219c89",
          "created_at":"2026-10-25T20:01:42.050857Z","updated_at":"2026-10-25T20:01:42.163925Z",
          "completed_at":"2026-10-25T20:01:42.147887Z"}]}

$ curl -s http://127.0.0.1:8030/variants/13764ac2526f4a44a3800b1de33a7db7 | jq -c '{platform,status}'
{"platform":"linkedin","status":"published"}
```

The external id is `mock_linkedin-<sha256(idempotency_key)[:12]>`, so the same job always
produces the same id. No network call is made by a mock (`test_mock_status_reports_capabilities`,
`test_mock_external_id_is_deterministic`).

## 11. Telegram configuration status

Three adapters are registered, and the unconfigured one says so instead of pretending:

```console
$ curl -s http://127.0.0.1:8030/publishers | jq -c '.publishers[]|{name,platform,configured,enabled,detail}'
{"name":"mock_linkedin","platform":"linkedin","configured":true,"enabled":true,"detail":"Deterministic local mock; no network calls are made."}
{"name":"telegram","platform":"telegram","configured":false,"enabled":false,"detail":"Disabled: TELEGRAM_ENABLED is false, so no message can be sent."}
{"name":"mock_x","platform":"x","configured":true,"enabled":true,"detail":"Deterministic local mock; no network calls are made."}
```

A job for an unconfigured adapter fails **visibly**, is never handed to a mock, and the variant
returns to a human-decidable state:

```console
$ curl -s -X POST .../ops/run-due -H 'X-Ops-Token: readme-ops-token' -d '{"limit":5}'
{"claimed":1,"completed":0,"failed":1,"skipped":0}

$ curl -s ".../jobs?variant_id=$TID" | jq -c '.items[0]|{status,attempts,last_error}'
{"status":"failed","attempts":1,"last_error":"Telegram adapter is not configured (TELEGRAM_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID). Nothing was sent."}

$ curl -s .../variants/$TID | jq -c '{status}'
{"status":"approved"}
```

`test_telegram_platform_is_never_served_by_a_mock` asserts the routing invariant directly.

## 12. Idempotency

Same variant + same slot, twice:

```console
$ SLOT=$(date -u -d '-5 seconds' +%Y-%m-%dT%H:%M:%SZ)
$ curl -s -X POST .../variants/13764ac2526f4a44a3800b1de33a7db7/schedule -d "{\"scheduled_for\":\"$SLOT\"}"
201 {"job":{"id":"4b5aff45db3742e39659049bddb8f4d9","status":"pending","scheduled_for":"2026-10-25T20:01:37Z",
      "idempotency_key":"a242a3a80120f58bc4c7ea491ec7fbba","attempts":0,"max_attempts":3,…},"deduplicated":false}

$ curl -s -X POST .../variants/13764ac2526f4a44a3800b1de33a7db7/schedule -d "{\"scheduled_for\":\"$SLOT\"}"
200 {"job":{"id":"4b5aff45db3742e39659049bddb8f4d9",…},"deduplicated":true}

$ curl -s http://127.0.0.1:8030/jobs | jq .total
1
```

The guarantee is the database, not a Python dictionary: `scheduled_jobs.idempotency_key` is
`UNIQUE`, and `test_database_rejects_a_duplicate_idempotency_key` inserts the same key twice and
asserts the `IntegrityError`. `test_two_sessions_cannot_claim_the_same_job` covers the claim race.

A second, *different* slot for the same variant is a conflict by design:

```console
$ curl -s -X POST .../variants/13764ac2526f4a44a3800b1de33a7db7/schedule -d '{"scheduled_for":"…+1h…"}'
409 {"error":{"code":"variant_not_approved","message":"Only approved variants can be scheduled; this one is 'scheduled'. …","details":{"status":"scheduled"}}}
```

## 13. Scheduler execution and crash recovery

Nothing due → nothing claimed, even after a full process restart:

```console
$ curl -s -X POST .../ops/run-due -H 'X-Ops-Token: …' -d '{"limit":5}'
{"claimed":0,"completed":0,"retried":0,"failed":0,"skipped":0,"recovered":0}
```

**Restart recovery.** The server was killed (confirmed dead: `curl /health` → `000`), restarted
against the same file, and asked for its state:

```console
$ kill 609678 && curl -s -o /dev/null -w '%{http_code}\n' --max-time 2 http://127.0.0.1:8030/health
000
$ uvicorn app.main:app --host 127.0.0.1 --port 8030 &
$ for p in sources variants jobs publications; do printf "%-13s %s\n" $p "$(curl -s http://127.0.0.1:8030/$p | jq .total)"; done
sources       2
variants      3
jobs          2
publications  2
$ curl -s .../publications | jq -c '{total,items:[.items[]|{attempt_number,success,adapter,external_id}]}'
{"total":2,"items":[{"attempt_number":1,"success":false,"adapter":"telegram","external_id":null},
                    {"attempt_number":1,"success":true,"adapter":"mock_linkedin","external_id":"mock_linkedin-dc1315219c89"}]}
```

**Crash mid-publish.** A job was forced into `processing` with a two-hour-old claim and an
attempt that never finished — the state a killed worker leaves behind:

```console
job before tick: ('processing', '05c0b78b3daa42f3ad9922709080c454', '2026-10-25 18:02:52.370033', 0)
attempt before tick: (1, '2026-10-25 18:02:52.370033', None, None)

$ curl -s -X POST .../ops/run-due -H 'X-Ops-Token: …' -d '{"limit":5}'
{"recovered":1,"claimed":1,"completed":1,"failed":0}

$ curl -s .../jobs/2defd1198cb54f94b0246c4d947bd291 | jq -c '{status,attempts,external_id,completed_at}'
{"status":"completed","attempts":2,"external_id":"mock_x-26e5b140735f","completed_at":"2026-10-25T20:02:52.403441Z"}

$ curl -s ".../publications?job_id=2defd1198cb54f94b0246c4d947bd291" | jq -c '{total,items:[.items[]|{attempt_number,success,adapter,error}]}'
{"total":2,"items":[{"attempt_number":2,"success":true,"adapter":"mock_x","error":null},
                    {"attempt_number":1,"success":false,"adapter":"mock_x",
                     "error":"interrupted: worker claim expired before an outcome was recorded"}]}

$ curl -s .../variants/$XID | jq -c '{status}'
{"status":"published"}
```

The abandoned attempt is closed with an explicit error rather than left dangling, and the retry
reused the same idempotency key.

## 14. Bounded retries and terminal failure

Telegram pointed at a **closed port** (`TELEGRAM_API_BASE=http://127.0.0.1:9`), `MAX_ATTEMPTS=3`,
`RETRY_BASE_SECONDS=0` so the three ticks can happen back to back:

```console
$ curl -s .../publishers | jq -c '.publishers[]|select(.name=="telegram")|{configured,detail}'
{"configured":true,"detail":"Configured: real Telegram delivery is enabled."}

tick 1: {"claimed":1,"completed":0,"retried":1,"failed":0}
tick 2: {"claimed":1,"completed":0,"retried":1,"failed":0}
tick 3: {"claimed":1,"completed":0,"retried":0,"failed":1}
tick 4: {"claimed":0,"completed":0,"retried":0,"failed":0}

$ curl -s .../jobs/$RJID | jq -c '{status,attempts,max_attempts,last_error}'
{"status":"failed","attempts":3,"max_attempts":3,"last_error":"Telegram transport error: [Errno 111] Connection refused"}

$ curl -s ".../publications?job_id=$RJID" | jq -c '{total,items:[.items[]|{attempt_number,success,retryable,error}]}'
{"total":3,"items":[{"attempt_number":3,"success":false,"retryable":true,"error":"Telegram transport error: [Errno 111] Connection refused"},
                    {"attempt_number":2,"success":false,"retryable":true,"error":"Telegram transport error: [Errno 111] Connection refused"},
                    {"attempt_number":1,"success":false,"retryable":true,"error":"Telegram transport error: [Errno 111] Connection refused"}]}

$ curl -s .../variants/$RVID | jq -c '{status}'
{"status":"approved"}
```

Three attempts, then a terminal failure that stops — and a variant handed back to a human. The
`Retry-After` path (a publisher saying "not before N seconds") is covered by
`test_retry_after_from_the_publisher_wins_over_backoff` and `test_rate_limit_reports_telegrams_retry_after`.

## 15. Dry-run safety mode

Echoing §14's setup but with `TELEGRAM_DRY_RUN=true` and the same closed port:

```console
$ curl -s -X POST .../ops/run-due -H 'X-Ops-Token: dryrun-ops-token' -d '{"limit":5}'
{"claimed":1,"completed":0,"retried":0,"failed":0,"skipped":1}

$ curl -s .../jobs/$DJID | jq -c '{status,attempts,last_error}'
{"status":"pending","attempts":0,"last_error":"TELEGRAM_DRY_RUN=true: request validated and aborted before sending; no message was delivered."}

$ curl -s ".../publications?job_id=$DJID" | jq -c '{total,items:[.items[]|{attempt_number,success,dry_run,error}]}'
{"total":1,"items":[{"attempt_number":1,"success":false,"dry_run":true,"error":"TELEGRAM_DRY_RUN=true: request validated and aborted before sending; no message was delivered."}]}

$ curl -s .../variants/$DVID | jq -c '{status}'
{"status":"scheduled"}

# a second dry-run tick
{"claimed":1,"skipped":1,"failed":0}
```

The job stayed `pending` with `attempts: 0`, so the dry run consumed **no** retry budget — and
because the endpoint was a closed port, the absence of a transport error proves the request was
never sent. This is the safe test mode the brief asks for.

## 16. Publication history

```console
$ curl -s "http://127.0.0.1:8030/publications?variant_id=$LID" | jq .
{
  "items": [
    {
      "id": "d1ebebf8e60b48f7983c60370e12d777",
      "job_id": "4b5aff45db3742e39659049bddb8f4d9",
      "variant_id": "13764ac2526f4a44a3800b1de33a7db7",
      "attempt_number": 1,
      "adapter": "mock_linkedin",
      "platform": "linkedin",
      "scheduled_for": "2026-10-25T20:01:37Z",
      "started_at": "2026-10-25T20:01:42.147887Z",
      "finished_at": "2026-10-25T20:01:42.147887Z",
      "success": true,
      "retryable": false,
      "dry_run": false,
      "external_id": "mock_linkedin-dc1315219c89",
      "error": null,
      "retry_after_seconds": null
    }
  ],
  "total": 1,
  "limit": 20,
  "offset": 0
}
```

Every attempt is recorded — success, retry, terminal failure and dry-run alike (see §14, §15,
§13) — and the rows are append-only: no code path updates an existing attempt.
`tests/test_persistence.py` re-opens the same database in a fresh session and asserts the
history is still there.

## 17. Automated tests

```console
$ pytest -q
........................................................................ [ 42%]
........................................................................ [ 85%]
........................                                                 [100%]
168 passed, 1 warning in 3.35s
```

The single warning is Starlette's own `httpx`-vs-`httpx2` deprecation notice emitted from
`fastapi/testclient.py` — third-party, not project code.

Per file (collected counts, so parametrised cases are included):

```text
tests/test_api_e2e.py              31
tests/test_capstone_yaml.py         4
tests/test_config.py                7
tests/test_ingest.py               28
tests/test_persistence.py           9
tests/test_profiles_validation.py  19
tests/test_publishers.py           20
tests/test_review.py               10
tests/test_schedule_worker.py      23
tests/test_variants.py             17
                              ------
                                 168
```

The suite never touches the network: ingestion tests inject a resolver and an
`httpx.MockTransport`, Telegram tests point at a closed port, and every test uses its own
temporary SQLite file.

## 18. What could NOT be verified

**Real Telegram delivery.** No bot token, chat id or Telegram account exists in this
environment, so no message was ever sent to a real chat. What *is* verified:

* the adapter builds the documented Bot API request (`POST {base}/bot{token}/sendMessage` with
  `chat_id` and `text`) — asserted by `test_successful_send_returns_the_message_id` against an
  injected transport that inspects the URL and body;
* the error mapping: 429 → retryable with `Retry-After`, 5xx → retryable, timeout → retryable,
  400/401/403 → permanent (`test_rate_limit_reports_telegrams_retry_after`,
  `test_server_error_is_retryable`, `test_timeout_is_retryable`,
  `test_client_error_is_permanent_and_redacted`);
* the unconfigured path fails visibly (§11) and the dry-run path sends nothing (§15);
* the transport-failure path retries and then fails terminally against a real closed socket (§14).

To close this gap: set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, run with
`TELEGRAM_DRY_RUN=true` first, then `false`, and repeat §14 — the only difference should be a
real message arriving and `external_id` holding Telegram's `message_id`.

## 19. Secret scan

The live probes used a fake token (`123456:FAKE-TOKEN-FOR-TRANSPORT-TEST`) against a closed
port. It must not appear in the database, the logs or the tree:

```console
$ grep -c "FAKE-TOKEN-FOR-TRANSPORT-TEST" .scratch/retry.db
0
$ grep -c "FAKE-TOKEN-FOR-TRANSPORT-TEST" .scratch/retry.log
0
$ grep -rn "FAKE-TOKEN-FOR-TRANSPORT-TEST" --exclude-dir=.scratch --exclude-dir=.venv . | wc -l
0
$ curl -s http://127.0.0.1:8031/config
{"environment":"development","database":"sqlite","scheduler":{"max_attempts":3,"poll_seconds":5,"retry_base_seconds":0,"retry_max_seconds":0,"stale_claim_seconds":900},"ingest":{"timeout_seconds":5.0,"max_bytes":2000000,"max_redirects":3},"telegram":{"enabled":true,"configured":true,"dry_run":false},"ops_endpoint_enabled":true}
```

The stored error text (§14) is the adapter's sanitised message: no URL, no token, no chat id.
`.env` is git-ignored and `.env.example` contains placeholders only; no credential exists in
the repository.
