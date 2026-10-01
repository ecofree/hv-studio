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
- Default acceptance runs only the `production` test group. For component changes,
  run the affected files with `-m shared` or `-m legacy` as appropriate. Run all
  groups only when cross-cutting changes or a concrete failure justify it.
- Do not pin current engine/backend settings globally in test fixtures or use
  total passing-test counts as production readiness evidence.
- Never emit Base64 or image data URLs. Inspect batches via contact sheets and
  at most two full-resolution images per tool call.
- Long jobs (image generation, compose, acceptance) must run **detached**: start them
  outside the tool call's own lifetime, redirect output to a log file, and poll the
  log in short increments. A tool call that waits on a child process will kill that
  child when the call times out (2026-09-29: image generation stopped at 27/36 and
  had to be resumed).
- Generate a single missing plate with `episode produce ... --image-key <KEY>`
  (repeatable). Without it the image stage pulls in every missing key and the budget
  is spent before the one wanted image is reached.
- Provider calls use separate connect/read timeouts (`HV_IMAGE_CONNECT_TIMEOUT`,
  default 20s; `HV_IMAGE_READ_TIMEOUT`, default 180s). Keep them split: a hung
  connect cannot be rescued by a long read timeout.
- **The project look is fixed: never restyle it.** `episodes/*/image_prompts.json`
  `en` describes subject matter only — people, action, setting, props, light. The
  look comes from `profile.image.style_suffix` + `negative_clause`, which
  `build_image_jobs` appends to every prompt. Never write style words into `en`
  (cartoon, anime, ink-wash, 水墨, flat, infographic, photorealistic, 3d render,
  chiaroscuro, silk-scroll) and never ask for text rendered inside the image
  (`negative_clause` forbids it). Writing those words fights the negative clause
  and the model resolves the conflict toward your prompt, which silently replaces
  the house look — this happened on caomin-01 and required regenerating all 36
  plates. To change the look, change the config suffix for every episode at once,
  never per-episode prompt edits.
