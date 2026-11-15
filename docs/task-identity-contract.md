# Shared production task identity (v1)

This change is a data contract carried by existing records, not a new scheduler,
job database, approval, or cache authority. Studio still owns its request and
checkpoint; Batch still owns its queue row; V2 still owns its media-stage state.

## What is now bound

`task_id` identifies one submission owner and episode. `spec_sha256` binds its
versioned contract: request digest, authored-input digest, implementation digest,
effective-settings/environment digest, ordered stages and the exact budget path
and limits. The ledger's mutable spending is deliberately **not** part of this
identity. `attempt_id` is new for each execution, including an explicit resume.
An execution reference contains all three IDs, so a prior attempt's result is
not a result for the current attempt, even when the episode is unchanged.

Only digests of configuration/request material are stored in the contract, not
credentials or the full script. Hashes are integrity checks, not signatures:
an administrator able to rewrite all local evidence is outside this contract's
trust boundary. Existing media, approval and provider-receipt gates remain in
force and are not replaced by a matching task ID.

## Integration

- **New Studio submissions:** `request.json` declares `task_schema=1`;
  `task.json` is created once. `_spawn` writes a new `execution.json` and stamps
  the checkpoint/process record. The child validates the original submission,
  current inputs, implementation and budget before starting work. Resume keeps
  the same request, task and ledger, archives the previous execution, and uses
  a new attempt. A voice-only task keeps its narration-specific input identity;
  editing ordinary visual planning does not substitute the full video identity.
- **Durable Batch:** the first eligible dispatch pins a task on that episode's
  existing row. Rechecks occur before spawn and inside the child after taking
  the episode lease. A revised request, admission input, code or settings cannot
  silently repin a retry. Submit a new explicit task after reviewing such a
  change; `retry_episode` authorizes attempts, not a different task or more money.
  Child PID, creation time, owner and token are verified before execution. Both
  live completion and supervisor-crash recovery validate the result's task and
  attempt reference. Malformed/foreign results remain diagnosable and do not
  prevent other episodes being scanned. A current approval can finish a versioned
  row only when its execution reference, candidate path and candidate bytes match.
- **V2:** the worker's contract passes through the existing execution context.
  V2 checks episode/stage scope and writes the same reference to the result,
  stage progress and run record. ProviderBudget checks the ledger binding both
  at construction and reserve, including already-created budget objects.
- **Direct CLI/API V2 calls:** each invocation gets a new direct task/attempt;
  it is not silently treated as another job's resume. Existing explicit/default
  budget IDs and provider caches are unchanged. Verify-only stays read-only for
  the saved V2 run, while returning the invocation's identity in its result.

The implementation digest now also includes the Studio/Batch/local-narration
entry scripts, not only `app/`. Code/settings changes conservatively require a
new submission; automatic per-stage migration across implementation revisions
is not implemented. This does **not** delete old media or require regeneration
when its existing stage-specific receipts still allow reuse under a new task.

## Compatibility and limits

Old Studio requests without the marker keep the previous validation path. They
are not assigned a fictional submission-time code contract. A versioned request
with missing/corrupt task data cannot fall back to this legacy path. Old queue
records acquire a contract at their first new dispatch; existing legacy results
remain readable. The separate synchronous `hv_ops.run_batch` wrapper keeps its
existing snapshot/resume policy and receives direct V2 identities per call.

No extra ledger is created to replenish credit. Distinct explicit jobs retain
distinct authorizations; a Batch task may bind the existing shared batch ledger.
A busy child that never acquired the episode lease is reconciled identically
whether its supervisor is still alive or recovering: roll back the process
attempt, preserve spending, leave it queued. This is not atomic parent/child lock
handoff; a child may still have been spawned.

Existing statuses are not replaced. `stages_done` still means the current V2
execution actually completed those steps; a task reference is not evidence that
any stage ran or that a video was approved. Resource-aware concurrency, remote
TTS result lookup/reconciliation, trustworthy production scenario event capture,
and real audiovisual acceptance remain separate unfinished work.

## Verification

`tests/test_task_identity.py` covers preserved task IDs and rotated attempts,
code/input/settings drift, malformed schemas, scope cleanup, budget substitution,
Studio resume/worker linkage, V2 state/progress linkage, replayed child results,
crashed-supervisor contention recovery and an actual separate Python child that
rejects changed input before invoking production. Other synthetic queue children
now emit the versioned result binding rather than weakening the loader for tests.

The read-only Windows CI runs the focused safety suite, the complete production
suite, and affected shared/legacy compatibility tests. The JUnit artifact for the
exact commit is the authority for pass/fail counts. Tests use temporary episodes
and stubbed providers; they are not real-provider or content-quality approval.
