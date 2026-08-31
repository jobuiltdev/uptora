# Background scheduling (Milestone 6A)

Enabled monitors run automatically at their configured interval through Celery
with a Redis broker. The database is the source of truth for what is due;
Celery Beat carries exactly one periodic entry, the dispatcher.

## Running it locally

### Redis

Docker (any platform):

```
docker run -d --name uptora-redis -p 6379:6379 redis:7-alpine
```

Native Linux/macOS: `redis-server`. On Windows without Docker, use WSL and run
`redis-server` inside it -- there is no maintained native Windows build.

Point Uptora at it with `CELERY_BROKER_URL` in `.env`; it defaults to
`redis://localhost:6379/0`.

### Workers and beat

Two queues, so the number of concurrent Chromium processes is capped by how you
start the browser worker rather than by anything hard-coded in the product.

Linux/macOS (and production):

```
celery -A config worker -Q http --concurrency=8
celery -A config worker -Q browser --concurrency=2
celery -A config worker -Q notifications --concurrency=4
celery -A config beat
```

Native Windows development -- Celery's prefork pool does not work there, so use
the solo pool. Production is assumed to be Linux; nothing in the architecture
is shaped around this:

```
celery -A config worker -Q http --pool=solo
celery -A config worker -Q browser --pool=solo
celery -A config worker -Q notifications --pool=solo
celery -A config beat
```

The concurrency numbers are examples. A browser check holds a whole Chromium
process, so keep that worker low -- 1 or 2 on a development machine.

## How scheduling works

`Monitor.next_check_at` is when a monitor is next due. Null means "not
scheduled", which is exactly what a disabled monitor is, so disabling removes a
monitor from the dispatcher's queryset by data rather than by a flag anyone has
to remember to check.

The rules, all applied by `Monitor.apply_schedule()` on save:

| Event | `next_check_at` becomes |
| --- | --- |
| Created, enabled | now -- a user sees a result shortly after creating a monitor |
| Created, disabled | null |
| Disabled | null |
| Re-enabled | now |
| `interval_seconds` changed | now + the new interval |
| Enabled but never scheduled | now |
| Any other save | unchanged |

Creating a monitor never runs a check synchronously; it only makes it due.

### The dispatcher

`dispatch_due_monitors` runs every 30 seconds (`UPTORA_DISPATCH_INTERVAL_SECONDS`).
It first rescues abandoned runs (see Stale lease recovery), then calls
`claim_due_monitors()`, which in one transaction:

1. selects up to 100 enabled monitors with `next_check_at <= now`, using
   `select_for_update(skip_locked=True)` so a second dispatcher sees a
   different set instead of blocking or double-claiming;
2. creates one `MonitorRun` per monitor at `scheduled_for = next_check_at`;
3. advances `next_check_at` past now.

The dispatcher sends the messages only after that transaction has committed, so
a message can only ever exist for work that is already durably recorded and no
broker call is made with a transaction open.

`MonitorRun` has a unique constraint on `(monitor, scheduled_for)`. That is the
real guarantee: even if two dispatchers somehow reached the same slot, the
database permits only one run.

### Coalescing

If Uptora is down for hours, monitors do not come back to a backlog. When a
monitor is overdue, the current slot produces **one** run and `next_check_at`
jumps past every missed slot in a single arithmetic step, staying on the
original cadence. A monitor 24 hours overdue on a 60-second interval yields one
run, not 1440.

## Execution

`execute_monitor_run(run_id)` is delivered at least once, so `MonitorRun` -- not
the message -- is the unit of work.

```
PENDING ──claim──> RUNNING ──> COMPLETED
   │                  │
   │                  └──> FAILED_INTERNAL   (retries exhausted)
   └──> CANCELLED                            (monitor disabled before it ran)
```

A worker may act only while it holds the current `claim_token` and the lease
has not expired. A duplicate delivery arriving mid-execution stands down. An
expired lease may be reclaimed, which is what stops a dead worker stranding a
run forever.

Lease duration is `timeout_seconds * 4 + 90s`, comfortably longer than any
legitimate check including browser launch.

### Network vs. incident retries

The Milestone 4 durable boundary is preserved. Once a `CheckResult` is linked to
the run, no later delivery makes another request -- it resumes at incident
processing. A database hiccup during incident processing therefore never
re-probes a customer's site or writes a second observation.

On failure the lease is expired rather than the state rolled back, so the retry
picks the run up and continues from wherever it got to.

### Stale lease recovery

A lease expiring makes a run *reclaimable*, but nothing reclaims it on its own.
If the worker holding it died, its Celery message died with it, and without a
proactive scan the run would sit RUNNING forever waiting for a redelivery that
may never come.

Every dispatcher pass therefore starts with `recover_stale_runs()`, which finds
runs that are `RUNNING` with `lease_expires_at` in the past and re-enqueues the
**existing** run id. It creates no new run, no new slot, no observation, and
leaves `Monitor.next_check_at` exactly where the original dispatch put it.
Recovery runs before ordinary dispatch: a run whose worker died is already late.

Concurrent scans take row locks with `skip_locked`, so two dispatchers see
disjoint sets. A `recovery_enqueued_at` stamp debounces *successive* passes for
two minutes, so a run whose message is still queued is not re-sent every 30
seconds. That stamp is deliberately **not** the lease -- pushing
`lease_expires_at` forward would make the recovered worker see a live lease and
stand down, stalling the very recovery it was meant to help.

`FAILED_INTERNAL` is left alone. It is terminal under current semantics, and
reviving it is a different decision from rescuing an interrupted run.

### After a worker dies

The recovered run goes through the ordinary execute task, which reclaims the
expired lease and resumes from whatever durable state it finds:

* **No CheckResult linked.** Nothing recorded that the target was contacted, so
  the network stage runs. This is the at-least-once boundary below.
* **A CheckResult already linked.** The network stage is skipped entirely and
  the run resumes at incident processing, completing the same run against the
  same observation. A worker crash during incident processing therefore costs a
  retry of some bookkeeping, never a second request to the customer's site and
  never a second row in their history.

Recovery deliberately makes no decision about which of these applies. It only
hands the run back; the durable state decides, which is what keeps behaviour
identical whether a run was recovered by this scan or redelivered by Celery.

### Broker outages

Sends happen after the scan's transaction has committed, and a failed send is
logged and skipped rather than raised. A run whose message could not be sent is
left exactly as it was and is picked up on a later pass once the debounce
window passes. Nothing writes a `CheckResult`: Redis being unavailable is
Uptora's problem, and recording it as the customer's site failing would be a
lie.

### attempt_count

Incremented only when a worker actually claims the run -- never by the recovery
scan observing it. A run showing `attempt_count = 4` has genuinely been picked
up four times, which is a real signal that workers are dying on it rather than
that a dispatcher noticed it repeatedly.

### Crash boundaries

| Died... | Consequence |
| --- | --- |
| A. before the request | Lease expires, run reclaimed, no harm |
| B. after the request, before it was stored | **The request is made again on reclaim** |
| C. between storing the result and linking it | Impossible -- one transaction |
| D. during incident processing | Result is linked; retry only reprocesses |
| E. after incident processing, before COMPLETED | Retry reprocesses (idempotent), then completes |

**B is not solvable.** An external side effect and a local commit are not one
atomic act. The window is one function call wide -- between `observe()`
returning and the transaction that stores its result committing -- but it is
real, and stale lease recovery makes it reachable sooner rather than removing
it: a run abandoned in that window has no `CheckResult`, so the recovered
worker legitimately makes the request again.

The honest statement is that **network execution for scheduled checks is
at-least-once, not exactly-once.** What is bounded is the cost: the duplicate
window is one function call wide, and the moment an observation is committed no
recovery, redelivery or retry will ever repeat the request for that run.

### Internal failures are not outages

A broker outage, a database error or a bug in Uptora marks the run
`FAILED_INTERNAL` and writes no `CheckResult`. Nothing reaches the incident
engine, so Uptora breaking never opens an incident against a customer's site.
Only things the *target* does -- timeouts, DNS failures, error statuses, missing
selectors, blocked destinations -- become observations.

## Manual runs

`POST /api/monitors/{id}/run/` is unchanged: still synchronous, still throttled
at 10/minute, still returns the `CheckResult` directly. It is a separate
execution path from the scheduler and does not create a `MonitorRun`. Both paths
produce identical `CheckResult` and incident behaviour because both go through
the same two stages.

## What is not logged

Task arguments are ids and nothing else. No URL, selector or configured flow
value is ever placed in a broker payload or a log line: Redis payloads are
readable by anyone with access to the broker, and a contact form's fields may
hold a real person's name and address.
