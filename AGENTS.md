# 当前工作台流程（用户整改指令，2026-11-05）

- 流程及写作证据粒度以 `docs/studio-workflow-policy.md` 为准，覆盖下文和复制的 Writing-DNA 中冲突的逐句考据、逐段主张与强制样片要求。
- 保留当前作者正文与素材。冻结快照作为已选版本；外部文件更新不自动替换本集。史实确认保留，样片可选；配音不以画面完成为前提。
- 不改原项目 D:/hv。本工作台的新政策不要求同步回写其规则。

# 当前剧本创作来源（用户同步，2026-10-21）

- 后续创作以用户在原项目 `D:/hv/resource/风趣历史.writing-DNA/` 修改的规则和文案为准。本项目对应目录中的副本已逐字同步；继续工作前先读取当前文件，不使用旧记忆代替。
- 玄奘当前正文：`resource/风趣历史.writing-DNA/scripts/初唐异人传_第一集_西行五万里：被通缉的硬汉玄奘.md`；配套文本：同目录 `初唐异人传_第一集_Gemini_TTS专版.md`。二者本次仅按原字节复制，没有改写。
- 不使用助手之前的 `workspace/editorial-reviews/ytrm-01-story-revision-20261021/` 修订稿作为创作或制作输入。旧样片（包括 `ytrm-01-xz-1p2-pilot-02-factcheck`）及其文案只保留用于追溯，不是后续创作底稿。
- 本次仅同步来源文件。当前旧音轨、字幕、画面和单集生产绑定均保留，不能称为新版文案对应的产物。用户要求暂不配音、不渲染；在新的制作指令到来前不得启动这些工序。
- 不为适配工具而擅自改写、删去标注或改动用户原文。同步与旧版本备份记录见 `workspace/state/source-sync-20261021/manifest.json` 和 `同步说明.txt`。

# Production changes

- This is the independent studio copy. Active episodes and run state live under
  `workspace/episodes` and `workspace/state`, via `app.paths`. Root `episodes/`
  is the preserved historical library. Never promote copied approvals or run
  copied live queue state. Start `studio.bat`; keep the original `D:/hv` unchanged.
- New studio production uses current source-bound key-fact confirmation; pilots are optional.
  Imported drafts remain unapproved. The Qwen model service is an external local
  dependency; do not start it or spend generation budget as part of UI tests.

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
- **Every v2 episode opens with a title card and marks every act.** The sidecar
  is `episodes/<id>/overlays.json`; A full run checks it before paid image dispatch; compose-only work applies
  the same authoring and validation, preserving an edited file. Act copy
  is editorial: title ≤18 characters, subtitle ≤26, and cards sit in the upper
  sky band unless the plate has faces there (`anchor_y` moves one card). See
  `app/services/overlay_author.py` and
  `docs/douyin-attention-playbook.md`; legacy episodes keep using
  `bin/build_overlays.py`.
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
