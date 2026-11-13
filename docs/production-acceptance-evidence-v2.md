# Production acceptance evidence v2

`production_readiness.assess()` no longer accepts the legacy boolean fields
(`kill_resume=true`, `real_provider_models=true`, etc.). It requires
five independently recorded scenario directories:

- `multi_episode_current_rebuild`
- `kill_resume`
- `timeout_recovery`
- `real_provider_models`
- `unattended_worker_full_batch`

Each directory must contain:

- `run.json`: schema version 1, scenario, run_id, source_fingerprint,
  git_revision, status=`passed`, and execution timestamps.
- `events.json`: run_id-bound events satisfying the scenario's required
  event contract. Real-provider evidence must include a non-mock successful
  provider event bound to a hash-verified provider receipt.
- `artifacts.json`: a non-empty mapping of actual output paths to SHA-256.

After the real test/production harness has written these files, compile them with:

```powershell
python bin/record_production_validation.py `
  --source-fingerprint <fingerprint-from-current-check> `
  --git-revision <full-git-sha> `
  --episodes <episode-1> <episode-2> `
  --evidence-root <path-to-scenario-evidence>
```

The collector only validates and binds existing evidence. It does not run a
provider, inject a fault, or create a passing event. A missing scenario keeps
production readiness blocked. Hashes detect later changes; they are integrity
checks, not cryptographic signatures or proof that a trusted runner authored
the original files. Independent execution and review remain required.
