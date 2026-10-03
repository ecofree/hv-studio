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
- **The two pipelines are not interchangeable, and neither is retired.**
  `produce()` defaults to `v2`; `legacy-preview` refuses the paid stages
  (`assets`/`tts`/`images`) outright, so **v2 is the only path that can spend
  money**. `--image-key` works on both; a scoped v2 image run leaves the episode
  deliberately incomplete and `timeline` refuses to build until every declared
  plate is present.
- **v2 has no polyphone substitution, by design — do not port it in.**
  `spoken_text.compile_text` is punctuation-only and hard-asserts that the
  alnum content is unchanged, so 可汗 stays 可汗 all the way to the subtitle.
  The `subtitle_fidelity` polyphone gate is a legacy-path guard for
  `narration_text`; v2 does not have that field. v2's own subtitle check is
  `production_v2.validate_timeline` (`cue["text"] != unit["text"][i:j]`), which
  runs at both compose and verify.
- **Shot granularity differs by design; do not import legacy limits into v2.**
  legacy shots are ~11-char fragments bounded to 40–200 frames. v2 shots are one
  per narration unit and are only required to span at least one frame —
  `validate_timeline` has **no upper bound**, and observed single shots reach
  1254 frames (~50s). Enforcing `MAX_FRAMES` in v2 would invalidate every
  existing v2 timeline (laogongtuan 59/69 shots, lishimin-01 61/78,
  lishimin-01b 54/57 all exceed it). That is a product decision, not a bug fix.
- **`episodes/<id>.json` is the only source of truth for a delivered episode's
  frame structure — `split_script` is not a recovery path.** Frames are hand-
  edited after splitting (caomin-01: 4 dropped, 2 pairs merged, 2 bed markers;
  the 242-entry audio plan in `state/` is one revision behind the delivered 238).
  Nothing records those edits, so re-running the splitter on the same script
  yields a different frame list. Copy `episodes/<id>.json` before any tooling
  that might write it, and never let a sandbox copy keep the original `id`:
  `save(ep)` writes by `ep.id`, so a copied fixture silently overwrites the real
  episode (this happened on caomin-01, 2026-10-01).
- `ffmpeg`/`ffprobe` are resolved from `PATH`, not from the repo. Put
  `tools/ffmpeg/bin` on `PATH` before running `NEEDS_FFMPEG` tests or
  `bin/check_project.py`. A bare `pytest` otherwise reports ~12
  `FileNotFoundError: ffmpeg` failures that read as regressions but are only a
  missing `PATH` entry — check this before believing a red suite. Invoking
  `scripts/ai-project-check.ps1` through `powershell -File` also needs an explicit
  `-ProjectRoot`; `$PSScriptRoot` is empty in that mode and the script's default
  fails at parameter binding.
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
