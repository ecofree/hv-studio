# PR #1 merge reconciliation — 2026-11-15

The user explicitly requested pushing and merging this engineering batch. This
record supersedes the earlier instruction to keep PR #1 in Draft, not any media
approval or real-production acceptance requirement.

## One authoritative task contract

The remote implementation at `7316aef980a2aaf61fd12b0375b1e5d6e19418b2` is the
integration baseline. The downloadable `hv-studio-task-identity-delivery.zip`
was an alternative implementation based on `c991324a`. Do NOT apply its patch
or publishing script on top of the integrated branch or main.

| Alternative package behavior | Integrated implementation |
| --- | --- |
| `production_task.py` stable link and attempt | Existing `production_identity.py` task specification and execution reference; no second module |
| Studio submit, process, checkpoint, resume/result association | `studio_jobs.py`, `bin/studio_job.py`, immutable `task.json` and per-attempt `execution.json` |
| Batch live/recovery result checks and pre-lease contention | `batch_worker.py`, `queue_records.py`; one result reconciliation path |
| V2 identity/progress and verify-only read-only behavior | `production_identity.task_bound_run`, `episode_production.py`, `production_status.py` |
| Worker implementation binding | `implementation_identity` includes actual worker entry scripts |
| New provider-attempt attribution, including image threads | Completed in this reconciliation using scoped handles and existing `execution` references |
| Stage timing attribution | Completed in this reconciliation in `production_metrics.py` |

The integrated contract additionally binds ordered stages, authored inputs,
implementation/settings and the exact budget path/limits. These protections are
retained rather than replaced by the older package's lighter attribution link.
Direct CLI/API invocations intentionally have distinct task IDs; an explicit
Studio/Batch resume keeps the original task and gets a new attempt.

## Missing behavior reconciled

A `ProviderBudget` created inside a task captures its verified task context.
A pre-existing budget handed to V2 is copied with `for_current_task()`; the copy
uses the SAME ledger and limits without writing a new authorization, mutating
the original handle, or relabeling previous reservations. TTS/image scopes and
reservation calls restore only that task context in worker threads. A scoped
handle cannot silently become a different attempt; explicit rebinding requires
an active authorized task and matching path/limits. New ledger rows store the
existing three-field `execution` reference, not scripts or credentials.

Stage timing rows snapshot the same reference. A timestamp or task match does
not mean the stage passed, a candidate is approved, or production is ready.

## Verification and publication

Baseline Windows run: https://github.com/ecofree/hv-studio/actions/runs/43663713605
Its exact JUnit artifacts report 121 focused, 542 production, and 28 selected
shared/legacy tests passing with no failures/errors/skips. Focused tests overlap
production; do not add counts. These are baseline results, not results for the
new attribution changes. The read-only CI must pass again at the final feature
head before merge; its exact-commit artifacts are authoritative.

The retained workflow now runs on main pushes as well as feature pushes and PRs.
No publisher workflow, duplicate task module, font binary, generated media, or
old package test log is needed in the merged source tree.

## Not claimed complete

Resource-aware multi-episode parallelism, atomic cross-process ownership
handoff, supplier-side TTS result retrieval/operator reconciliation, trusted
real-production scenario capture and human listening/visual acceptance remain
unfinished. Engineering merge is not a production-ready certificate. Historical
media, creative source text, original recordings and approvals are unchanged.
