# Evidence package — Capstone 05

This directory is the entry point for the evidence that goes with this capstone. It exists so a
reviewer can tell, in one screen, **what was proved, how to re-run it, and what was not proved.**

## Where the evidence actually is

Everything is text. There are no screenshots to trust and no recordings to play: the artifacts
are terminal transcripts, HTTP bodies and test output, all of which are reproducible on any
machine with Python 3.11+.

| Artifact | Location | What it proves |
|---|---|---|
| Required-evidence checklist (15 items) + status table | [`../EVIDENCE.md`](../EVIDENCE.md) | One row per requirement, each pointing at a verbatim transcript |
| Verbatim transcripts §1–§19 | [`../EVIDENCE.md`](../EVIDENCE.md) | Installation, ingestion, generation, validation, review, scheduling, idempotency, retries, crash recovery, dry run, history, secret scan |
| What could **not** be verified | [`../EVIDENCE.md`](../EVIDENCE.md) §18 | Real Telegram delivery — no bot token or chat id in this environment |
| Build history, eleven defects found and fixed | [`../BUILDLOG.md`](../BUILDLOG.md) | How the code got to its current shape, including the failures that drove it |
| Design and its trade-offs | [`../DESIGN.md`](../DESIGN.md) | State machine, idempotency identity, retry ladder, security boundaries |
| API manifest | [`../capstone.yaml`](../capstone.yaml) | Machine-checked against the served OpenAPI document |

No binary artifacts are stored here on purpose: a screenshot of a terminal is weaker evidence than
the command that produced it, and it cannot be re-run.

## How to reproduce

Offline, no credentials, no network:

```bash
cd backend/capstones/05-social-media-studio
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
alembic upgrade head                 # against a fresh DATABASE_URL
pytest -q                            # 168 tests, all offline
```

Live, over real TCP (this is what produces the transcripts in `EVIDENCE.md`):

```bash
export DATABASE_URL="sqlite:///./evidence-run.db" OPS_TOKEN="evidence-ops-token"
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8020 &
python scripts/smoke_test.py --base-url http://127.0.0.1:8020 --ops-token "$OPS_TOKEN"
```

The scheduler runs as a separate process, which is the whole point of the design:

```bash
python scripts/run_worker.py --once      # one tick
python scripts/run_worker.py --loop      # poll until Ctrl+C
```

Killing that worker between claim and delivery, then restarting it, is the crash-recovery probe
recorded in `EVIDENCE.md` §13.

## How to read the transcripts

* Blocks marked `console` or `text` are copied from a terminal. `$` lines are the commands; the
  rest is what came back.
* Bodies are shown as returned. Where a body was trimmed for length, the block says so.
* Two entries in `EVIDENCE.md` were **replaced after review** during the build, because the first
  version had been assembled from a projection rather than the raw response, and one `pytest`
  transcript contained invented progress percentages. Both were re-captured and are now verbatim;
  `BUILDLOG.md` (Phase 6) records this deliberately rather than hiding it.

## Status of the unverifiable item

| Requirement | Status | Reason | What stands in for it |
|---|---|---|---|
| Deliver a real message to Telegram | **not verified** | No `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` available in this environment | The adapter is exercised with an injected transport (`tests/test_publishers.py`), against a closed port to prove the retry ladder (EVIDENCE §14), and in dry-run mode which builds the exact request and aborts before the socket (EVIDENCE §15). `GET /publishers` reports the adapter as present but unconfigured, and a job for an unconfigured Telegram fails cleanly — it is never handed to a mock. |
