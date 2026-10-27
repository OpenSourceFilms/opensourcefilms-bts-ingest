# Versioned worker-lease fencing: opt-in core and rollout contract

**Status: candidate library API, not deployed or approved as a complete fix.**
Tracked by [issue #2](https://github.com/OpenSourceFilms/opensourcefilms-bts-ingest/issues/2).
The public repository contains the SQLite registry but not the actual deployed HTTP
worker endpoints, worker processes, release controls or live database migrations.
Read-only inspection of the BTS development source identified owner-authenticated
`/api/worker/lease`, `/api/worker/heartbeat`, `/api/worker/finish` and
`/api/worker/defer` calls. The worker currently constructs a fresh
UUID-suffixed owner per acquisition, mitigating the specific same-owner
reuse condition in that implementation, but it is not an enforced,
future-proof lease credential. **Do not copy/replace this public registry
over the separate private development worktree** or live service:
those implementations have independently diverged and the private tree has
other uncommitted work. Review and forward-port the minimal changes only
after the service/worker owner signs off.

## The safety invariant

Every new fenced lease has a fresh unpredictable 256-bit bearer token, generated
within the acquiring registry transaction. Only its SHA-256 is stored as
`jobs.lease_token_sha256`. The token is returned **once** as
`lease_token` by `lease_job_fenced(owner, lease_s)`, and must be treated as
sensitive authentication material, separate from the ordinary worker owner label.

For a **fenced** job, all subsequent mutators must authenticate the current
`job_id`, `owner`, `lease_token`, status `leased` and unexpired deadline
in the same SQLite writer transaction. The registry exposes optional
`lease_token=` keyword arguments on `heartbeat`, `finish_job` and
`defer_job` to support a versioned v2 HTTP contract without changing the
existing callers' positional argument lists. Requests without a token only
match **unfenced v1 leases**. On expiry/requeue/quarantine, finish or deferral,
the stored token hash is cleared. A new acquisition always gets a fresh token,
even when the process owner and numeric `attempt` are reused. In particular,
`defer_job` deliberately refunds an attempt, so attempt number alone is not a
safe fencing credential.

A v2 client cannot safely take a token returned by the registry and then
call the existing owner-only HTTP heartbeat/finish/defer routes. **The API
server must explicitly forward the token**. It must also authenticate the
service caller independently: a token is a lease-scoped capability, not a
replacement for a worker's service/API authentication.

## Compatibility and explicit limitations

The additive SQLite column is created for fresh databases and migrated
idempotently when opening an older database. The presence check and ALTER
hold SQLite's `BEGIN IMMEDIATE` writer lock so concurrent startup attempts
cannot race the column addition. Historical rows receive NULL.
Old-style `lease_job(owner, lease_s)` and existing positional mutators still
operate on legacy v1 leases. That is intentional for offline compatibility but
means **the same-owner stale-worker race remains open for v1 jobs**. This branch
does not pretend the globally deployed race is fixed and issue #2 must remain
open.

**Critical mixed-version hazard:** old registry code knows nothing about
`lease_token_sha256`, so rolling back only the registry implementation while
v2 jobs are still leased would re-enable unfenced mutations. A supported
rollback must stop issuing new v2 leases, drain/quarantine existing v2 leases,
verify no fenced jobs remain, and only then revert the registry/API together.

## Opt-in accepted-derivative reference contract

The draft public package now includes
[`bts_ingest/derivative_generation.py`](../bts_ingest/derivative_generation.py)
and [synthetic regression cases](../tests/offline_derivative_generation.py).
A fenced worker can derive a public, non-credential `generation_prefix`
using its private `lease_token`. Each derivative must be written under
`<validated original derived prefix>/_generations/<generation-id>/<safe filename>`
rather than to a shared latest-object key.

For a successful fenced lease **that reports derivative-publication fields**,
the registry validates `publication_protocol="generation-scoped-v1"`,
validates each derivative's B2 key against this generation's prefix,
requires `sha256`, `version_id` and `bytes`, and rejects reported
`published_path`/`publish_state` before committing `result_json`.
The accepted `result_json` then includes the independently recomputed,
non-secret `generation_prefix`. A stale lease cannot replace that row or
claim another generation's derivative list. The isolated memory-only test
models B's acceptance and an even later upload by A; B's accepted key and
pinned version remain unchanged.

**This is not sufficient production acceptance:**
- The published registry does not yet require this protocol for successful
  fenced jobs **without** derivative fields, so v2 API integration must
  explicitly fail closed on unscoped output-producing successes.
- The source/API must authenticate the worker and independently verify every
  reported B2 object, version and digest. A syntactically valid worker
  manifest is not remote integrity evidence.
- Readers must retrieve the **accepted** manifest from the registry and
  GET B2 objects by their recorded **version IDs**. Fetching a latest object
  by a static key, or using existing local canonical directories, bypasses
  the acceptance pointer.
- The development worker currently emits `published_path` from
  `publish_local` before finish. It will be intentionally incompatible
  with the fenced derivative publication contract until local output
  becomes generation-scoped and reader promotion is redesigned.
- Existing owner-only v1 calls still work, including their existing
  limitations; no v2 live service or worker has been deployed.

## Actual BTS service/worker contract, read-only source inspection

The current private BTS development snapshot has these authenticated
FastAPI handlers: `POST /api/worker/lease`, `/heartbeat`, `/finish` and
`/defer` in `bts_ingest/app.py`, and calls them from `Api.call`,
`Heartbeat.run`, `handle` and `main` in `bts_ingest/worker.py`.
The shared worker API bearer token is separate from its currently
UUID-suffixed `owner` field. Existing HTTP request bodies do not carry
a claim-generation credential.

**Recommended additive v2 contract (not deployed):**

| v2 route | Request payload | Response / invariant |
|---|---|---|
| `POST /api/worker/v2/lease` | `owner` | No job: 204. Leased: existing fields plus opaque `lease_token`, delivered only on acquisition. |
| `POST /api/worker/v2/heartbeat` | `job_id`, `owner`, `lease_token` | 200 if current unexpired token matched atomically; 409 otherwise. |
| `POST /api/worker/v2/defer` | `job_id`, `owner`, `lease_token`, `delay_s`, `reason` | Release/refund only this generation; 409 on lost claim. |
| `POST /api/worker/v2/finish` | `job_id`, `owner`, `lease_token`, `ok`, result/error fields | Terminal result accepted only for this generation. This does not authorize premature public derivative promotion. |

Keep the existing service bearer authentication on every route. Use a
distinct v2 request model requiring `lease_token`; do not make the field
optional in a v2 handler or silently retry a failed v2 call against v1.
The worker must retain the generation token only in memory for the lifetime
of its claim, pass it from `main` through `handle` into its heartbeat
thread and terminal/defer calls, and never include it in logs or error JSON.
On 409, stop publication and discard its staging generation.

### Publication fence, separate from the SQLite fence

**Release blocker:** In the inspected development worker,
`process()` currently uploads to a **shared, deterministic B2 key**
(`key=f"{prefix}/{Path(path).name}"`) and calls
`publish_local(prefix, ...)` before `handle()` calls
`/api/worker/finish`. The `hb.check()` calls are local checks of an
asynchronously renewed lease, not atomic checks coupled to an external
write. Lease expiry and reassignment during a transfer can therefore
allow a stale worker to overwrite B2 derivatives or publish a local
directory *even when the later registry finish is correctly rejected*.
This remains possible even if every current worker uses a unique owner.

A safe redesign must ensure that **uncommitted generations never write
the canonical keys/directories or become visible as the accepted result**.
Prefer content/claim-generation-scoped immutable B2 staging keys and
local staging directories. The server must verify the claim token and
approve one canonical generation before the readers follow a pointer to
it, or a single controlled publisher must promote only already accepted
generations with a separate idempotent promotion protocol. If older
readers require fixed B2 keys or local paths, qualify the migration and
atomicity explicitly; appending a token to a filename by itself does not
make the canonical publication path safe.

Required red tests before release: worker A's B2 upload begins, its claim
expires, worker B leases and publishes, then worker A's transfer completes.
Assert that A's output is invisible to canonical readers and B remains
authoritative. Repeat for local `publish_local` between its two renames,
for a process crash during promotion, for a retried finish response and
for restarted workers discovering obsolete staging generations.

## Proposed deployment sequence (requires the worker/API owner)

1. **Inventory consumers and backups.** Locate every worker route and caller;
   confirm actual request and response schemas, retries, idempotency rules,
   restart behaviour and SQLite migration strategy. Back up the database.
2. **Stage additive schema.** First deploy software that knows the new column
   but does not issue fenced leases. Old workers can keep operating on v1
   while version-skew acceptance is tested against disposable replicas.
3. **Introduce authenticated v2 routes.** Return the token only to the
   authorised worker holding the newly acquired job. Require and forward it
   on every v2 heartbeat, finish and defer. Never expose the token in HTTP
   access logs, traces, event JSON, public status responses or exception
   messages. Keep token storage restricted and avoid retry leakage.
4. **Upgrade all workers and pause v1 issuance.** After every worker is
   confirmed to use v2 and no v1 worker jobs remain, reject new v1 lease
   requests at the HTTP boundary and retire legacy mutation endpoints. Do
   **not** silently fall back from an invalid/missing v2 token to v1.
5. **Exercise races end to end.** Same owner on attempts N/N+1; two different
   owners; lease expiry before reacquire; defer/refund and reacquire with the
   same numeric attempt; delayed heartbeat/finish; process crash; rejected
   malformed tokens; quarantine and attempt limit; B2 side effects and worker
   retries. Assert that stale results cannot alter the new attempt. Crucially,
   registry fencing of `finish_job` **does not by itself prevent a stale
   worker from writing derivative objects to B2 before its completion call**.
   Fence or version-scope each external publication side effect as well,
   and ensure a late worker cannot overwrite or promote another attempt's
   derivatives.
6. **Accept rollout only with real evidence.** Record exact API/worker/registry
   revisions, migration and rollback procedure, success/error rates, and
   deployment state. Keep production assets and credentials out of GitHub.

The offline test `tests/offline_fenced_leases.py` covers only the registry
contract on disposable SQLite, **not** HTTP authentication or production
correctness. Nothing in this patch executes worker jobs or contacts B2.
