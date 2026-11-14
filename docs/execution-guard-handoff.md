# Execution ownership hardening — review handoff

This slice reuses the kernel-held `.produce.lock` in each episode directory.
Studio submission/resume, Studio worker, direct local narration, V2 and the
legacy entry alias participate in one synchronous, per-process/thread lease.
Nested calls retain the lease; other threads/processes fail before media writes
or paid work. A process death releases the kernel lease; the lock file remains.

The queue probes known busy episodes BEFORE admission, spawning and incrementing
attempts, then proceeds to other episodes. The child independently acquires the
lease because a preflight probe cannot guarantee ownership after a race. A race
lost after spawning still uses a process attempt and bounded RETRY_WAIT; this is
not a claim of atomic cross-process reservation handoff or a complete scheduler.
The conservative global storage lock remains. Different explicit authorizations
still have separate budgets; this patch does not silently merge/refill them.

Also fixes defects in the earlier unexecuted patch: missing pytest scope markers,
Header object defaults in direct auth calls, unreadable readiness diagnostics,
pre-dispatch TTS budget failures being overwritten as uncertain, and missing
provider receipt attributes masking the original failure. TTS remote recovery,
real scenario evidence producers and broad identity migration remain unfinished.

## Verification

The publishing workflow applies only the bounded source/test/doc changes, runs
compileall and focused offline tests with the repository core lock on Windows,
and pushes code only after these pass. Its run logs/JUnit XML are the authority
for results, not this document. No providers/model workers or historical media
are used. PR remains Draft; no production readiness is asserted.

Codex should inspect the final source commit (not just the updater), then run:

```powershell
python -m pytest -q tests/test_episode_execution.py tests/test_api_auth_gate.py tests/test_tts_uncertain_attempt.py tests/test_production_admission_audio_mode.py tests/test_production_readiness.py tests/test_rebuild_freeze_and_drift_split.py tests/test_unattended_operations.py tests/test_studio_process_repair.py tests/test_narration_closeout.py
python -m pytest -q -m production
```

Preserve actual results, failures and environment limitations. Never substitute
these offline checks for full video listening/visual review or real-provider
acceptance. Do not merge until independently reviewed.
