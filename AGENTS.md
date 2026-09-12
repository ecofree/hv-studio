# Production changes

- Preserve source narration, old raw takes, approved media, and image caches.
- Use `.venv-core/Scripts/python.exe`; local ASR runs in `.venv-asr`.
- Run tests in their isolated directories. Never sweep historical episode folders.
- Actual provider calls require explicit durable budgets. Retries and uncertain
  outcomes consume attempts; cached work does not. Never silently change voices.
- Do not manufacture listening, factual, visual or artistic approvals.
- Validate current source, input hashes, frame/sample counts and subtitle geometry
  before calling an output a valid candidate. A candidate is not an approved video.
- Use `scripts/ai-project-check.ps1` for acceptance and preserve its evidence.
- Never emit Base64 or image data URLs. Inspect batches via contact sheets and
  at most two full-resolution images per tool call.
