"""Tell an operator the current episode state and the single next command.

The function only reads status that production already recorded. It does not
approve content, raise a budget, or decide that a paid call should happen.
"""
from __future__ import annotations

import json
from pathlib import Path

import psutil

STAGE_ORDER = (
    "script", "tts", "delivery", "quality", "alignment",
    "images", "timeline", "compose", "verify",
)

LABELS = {
    "NOT_STARTED": "尚未开始",
    "PREPARED": "已建立生产记录",
    "RUNNING": "生产中",
    "INTERRUPTED": "生产中断，可续跑",
    "BLOCKED": "需要处理问题",
    "STALE": "输入已变化，需要更新候选",
    "CANDIDATE": "候选片待审核",
    "STAGES_COMPLETE": "本次阶段已完成",
    "APPROVED": "已有本地发布记录",
    "LEGACY_OUTPUT": "已有旧成片",
}

_BLOCKER_TEXT = (
    ("source changed", "原稿已改，和上次生产记录对不上"),
    ("candidate inputs changed", "候选片用的素材已变，需要按当前输入重做"),
    ("candidate is missing or changed", "候选片文件缺失或被改过"),
    ("audio findings await disposition", "有语音疑点还没写试听结论"),
    ("delivery directions await review", "有导演提示还没处置"),
    ("delivery cues have not been processed", "旁白里的导演提示还没进入处置"),
    ("current candidate requires full listening", "候选片还要听审、看画面、核史实；机器检查不能代替"),
    ("production state unreadable", "生产记录读不出来"),
    ("no current v2 candidate", "当前原稿还没有候选片"),
    ("missing v2 plan", "还没有旁白计划"),
    ("episode has no source narration", "缺少解说原稿 script.md"),
    ("source narration missing", "缺少解说原稿 script.md"),
    ("requested sample exceeds", "额度不够覆盖全部旁白，不会截断原稿去调用"),
    ("budget limits changed", "同一预算标识的额度被改过，需要换一个新的 --budget-id"),
    ("voice asset changed", "音色版本变了，需要重跑配音"),
    ("compiled narration request changed", "口播请求变了，需要重跑配音"),
    ("timeline requires images", "时间轴还缺画面或对齐结果"),
    ("missing reviewed shot", "还没有镜头时间轴"),
    ("budget ledger unreadable", "预算账本读不出来"),
    ("explicit storyboard and image prompts", "还没有分镜或图片提示词"),
    ("require current input approval", "无人生产队列还没有当前输入审核"),
)


def explain_blocker(text: str) -> str:
    raw = str(text or "").strip()
    for needle, chinese in _BLOCKER_TEXT:
        if needle in raw:
            return chinese
    return raw


def split_ids(ids):
    real = [i for i in ids if not str(i).startswith("smoke-")]
    smoke = [i for i in ids if str(i).startswith("smoke-")]
    return real, smoke


def _produce(episode_id: str, *extra: str) -> str:
    parts = [".\\cli.bat", "episode", "produce", episode_id, *extra]
    return " ".join(parts)


def advise(episode_id: str, status: dict | None, *, script_exists: bool = True) -> dict:
    """Return headline, command and notes for one already-loaded status."""
    status = dict(status or {})
    code = status.get("status") or "NOT_STARTED"
    raw_blockers = [str(b) for b in status.get("blockers") or [] if str(b).strip()]
    if not status.get("candidate_path"):
        raw_blockers = [b for b in raw_blockers if "current candidate requires full listening" not in b]
    done = [s for s in (status.get("stages_done") or []) if s in STAGE_ORDER]
    advice = {
        "status": code,
        "label": progress_label({"status": code, "stages_done": done}),
        "done": done,
        "blockers": [explain_blocker(b) for b in raw_blockers],
        "headline": "",
        "command": None,
        "notes": [],
    }
    joined = "\n".join(raw_blockers)

    def finish(**fields):
        advice.update(fields)
        if status.get('quality_pending_units') and not status.get('candidate_path'):
            advice['notes'] = list(advice.get('notes', [])) + [
                '机器标记的语音疑点保留到完整试听处理；可以继续制作候选，不会因此重新配音或自动确认。']
        changed = status.get("renderer_changed") or []
        if changed and all("渲染器源码" not in (n or "") for n in advice.get("notes", [])):
            advice["notes"] = list(advice.get("notes", [])) + [
                f"渲染器源码自该候选渲染后有 {len(changed)} 处变化（{', '.join(Path(p).name for p in changed[:5])}"
                + ("……" if len(changed) > 5 else "")
                + "），成片字节与已有评审不受影响；下次重渲会用新代码并需要重新评审。",
            ]
        return advice

    if code == "RUNNING":
        stage = status.get("active_stage") or "未知阶段"
        return finish(
            headline=f"这一集正在生产（{stage}）。等这次结束再操作，不要同时再开一轮。",
        )

    if code == "INTERRUPTED":
        return finish(
            headline="上次生产中断。重跑会接着已完成的阶段继续，不会把已有成片当成已审核。",
            command=_produce(episode_id),
            notes=["续跑沿用原来的 --budget-id。没有新的付费授权时，不要加大请求、字符或图片额度。"],
        )

    if code == "APPROVED":
        return finish(
            headline="这一集已有和当前候选绑定的本地发布记录。",
            notes=[f"记录在 episodes/{episode_id}/production/v2/published.json。这只是本地指针，不会上传平台。"],
        )

    if status.get("quality_pending_units") and status.get('candidate_path'):
        notes = [
            "在控制台里点开每段，听原音后填写审核人和发现。",
            "识别错了选「音频正确，识别有误」；真有口误选「确有口误，需要重做」。",
            "重做配音需要另外的额度，程序不会自动调用。",
            "命令行同样有效：hv narration review-quality <quality报告> <unit_id> "
            "--reviewer <姓名> --findings <结论> --decision asr_false_positive|spoken_error",
            "注：manifest 里的 technical_quality 只是装配快照，门禁只看上面的待处置数。",
        ]
        if code == "STALE":
            notes.append("候选片已和当前输入脱节。先写下这些语音结论，再从 script 阶段更新候选。")
        return finish(
            headline=f"有 {len(status['quality_pending_units'])} 段语音疑点等待试听结论。",
            command=".\\webui.bat",
            notes=notes,
        )

    if status.get("delivery_pending") and status.get('candidate_path'):
        notes = ["听完对应片段后，接受当前效果，或标明仍要调整。未处置完不能当作已审核。",
                 "命令行：hv narration review-direction <manifest> <cue_id> "
                 "--reviewer <姓名> --findings <结论> --decision accepted_existing|needs_revision"]
        if code == "STALE":
            notes.append("候选片已和当前输入脱节。先处置这些提示，再更新候选。")
        return finish(
            headline=f"有 {len(status['delivery_pending'])} 项导演提示等待处置。",
            command=".\\webui.bat",
            notes=notes,
        )

    if code == "LEGACY_OUTPUT":
        names = [str(name) for name in status.get("legacy_outputs") or []]
        shown = "、".join(names[:3])
        location = (
            f"成片：episodes/{episode_id}/out/{shown}"
            if shown else f"成片在 episodes/{episode_id}/out/ 。"
        )
        return finish(
            headline="这集已有旧流程成片，没有当前 v2 生产记录。",
            notes=[
                location,
                "这些文件保持原样。它们不是 v2 候选，也没有本地发布审核。",
                "只有在要按当前链路重做候选时，才从旁白计划开始：",
                _produce(episode_id, "--stage", "script"),
                "这一步不调用付费接口。继续生图或配音前必须另给额度和 --budget-id。",
            ],
        )

    if not script_exists and code in {"NOT_STARTED", "PREPARED", "BLOCKED"}:
        return finish(
            headline="还没有解说原稿。",
            notes=[
                f"把原稿保存为 episodes/{episode_id}/script.md，然后运行：",
                _produce(episode_id, "--stage", "script"),
            ],
        )

    stale_markers = (
        "source changed", "candidate inputs changed", "candidate is missing",
        "narration plan is not", "no current v2 candidate",
    )
    if code == "STALE" or any(marker in joined for marker in stale_markers):
        return finish(
            headline="输入已变化，旧候选不能继续当当前结果。",
            command=_produce(episode_id, "--stage", "script"),
            notes=["这一步只重建计划，不调用付费接口。缺图或缺口播时，再单独给出额度和 --budget-id。"],
        )

    if code == "BLOCKED":
        return finish(**_blocked(episode_id, joined))

    if status.get("candidate_path") and code in {"CANDIDATE", "STAGES_COMPLETE"}:
        return finish(
            headline="候选片已经生成，还差人工听审、画面和史实审核。",
            command=".\\webui.bat",
            notes=[
                "先处理完语音疑点和导演提示，再听完整片、看画面。",
                f"候选文件：{status['candidate_path']}",
                "结论要用真实审核人和真实发现来记。机器对齐不能代替听审。",
                "命令行同样有效（审核人/发现必须逐条手写）：",
                "hv narration review-quality <quality报告> <unit_id> --reviewer <姓名> --findings <结论> --decision asr_false_positive|spoken_error",
                "hv narration review-audio <manifest> --reviewer <姓名> --findings <结论> --decision approved|rejected",
                "hv narration review-video <candidate.json> --reviewer <姓名> --findings <结论> --decision approved|rejected",
                "hv narration publish <candidate.json> <听审记录> <视审记录> <published.json>",
            ],
        )

    nxt = next((stage for stage in STAGE_ORDER if stage not in done), None)
    if nxt is None:
        return finish(
            headline="记录里的阶段已经跑完。用只读校验确认候选是否仍对应当前输入。",
            command=_produce(episode_id, "--stage", "verify", "--timeout", "600"),
        )
    return finish(**_stage_step(episode_id, nxt))


def _blocked(episode_id: str, joined: str) -> dict:
    if "no source narration" in joined or "source narration missing" in joined:
        return {
            "headline": "缺少解说原稿。",
            "notes": [f"保存 episodes/{episode_id}/script.md 后再从 script 阶段开始。"],
        }
    if "budget" in joined or "exceeds explicit" in joined:
        return {
            "headline": "停在额度上。额度是硬上限，不够就不会截断原稿去调用。",
            "command": _produce(
                episode_id, "--max-requests", "N", "--max-characters", "C",
                "--max-images", "I", "--budget-id", "<新的授权标识>",
            ),
            "notes": ["续跑同一轮授权时沿用原来的 --budget-id。改额度必须换新标识。失败和超时也计入已用额度；"
                      "图片额度按实际尝试计数；--max-image-retries 只是增加同一个总上限，不是独立重试池。"],
        }
    if any(token in joined for token in ("voice asset", "compiled narration", "run stage tts", "missing v2 narration")):
        return {
            "headline": "停在配音。和当前请求一致的缓存不会重新调用。",
            "command": _produce(
                episode_id, "--stage", "tts", "--max-requests", "N",
                "--max-characters", "C", "--budget-id", "<本次授权>",
            ),
            "notes": ["没有新的配音授权就不要执行带额度的命令。只想确认缓存时，去掉额度参数再跑 tts 阶段。"],
        }
    if 'missing visual briefs' in joined:
        return {
            'headline': '缺少独立的画面描述，尚未调用图片服务。',
            'notes': ['在本集 production/v2/source_image_briefs.json 填写所列镜头的主体、行动与环境；'
                      '生成目录里的 storyboard 是输出，修改它不会成为下一次输入。'],
        }
    if "image" in joined or "timeline requires images" in joined:
        return {
            "headline": "停在画面，或时间轴还缺底图。",
            "command": _produce(
                episode_id, "--stage", "images", "--max-images", "N",
                "--budget-id", "<本次授权>",
            ),
            "notes": ["N 是图片调用总次数，失败、超时和重试都占用；已取得的响应先恢复，不因尺寸不同重新付费生成。"],
        }
    if any(token in joined for token in ("alignment", "local unavailable", "local-whisper", "ASR")):
        return {
            "headline": "停在本地语音识别或对齐。",
            "command": "powershell -File bin\\setup_local_asr.ps1 -Python <Python3.13路径>",
            "notes": ["本地识别不可用时不会改走云端。"],
        }
    if "timeline" in joined:
        return {
            "headline": "停在镜头时间轴。",
            "command": _produce(episode_id, "--stage", "timeline"),
        }
    return {
        "headline": "生产停住了。默认额度为 0：已有缓存会接着用，需要新调用时会在花钱前停住。",
        "command": _produce(episode_id),
    }


def _stage_step(episode_id: str, stage: str) -> dict:
    if stage == "script":
        return {
            "headline": "下一步只做旁白计划，不调用付费接口。",
            "command": _produce(episode_id, "--stage", "script"),
        }
    if stage == "images":
        return {
            "headline": "下一步是底图。身份相符的已有图会复用，不会重新付费。",
            "command": _produce(
                episode_id, "--stage", "images", "--max-images", "N",
                "--budget-id", "<本次授权>",
            ),
            "notes": [
                "把 N 换成这次允许的图片调用次数，含失败和重试。没有授权就不要执行。",
                "只想确认缓存是否够用时，运行：",
                _produce(episode_id, "--stage", "images"),
            ],
        }
    if stage == "tts":
        return {
            "headline": "下一步是配音。和当前口播请求一致的缓存不会重新调用。",
            "command": _produce(
                episode_id, "--stage", "tts", "--max-requests", "N",
                "--max-characters", "C", "--budget-id", "<本次授权>",
            ),
            "notes": [
                "没有新的配音授权就不要执行带额度的命令。",
                "只想确认缓存是否够用时，运行：",
                _produce(episode_id, "--stage", "tts"),
            ],
        }
    names = {
        "delivery": "导演提示处置",
        "quality": "语音质检",
        "alignment": "字幕对齐",
        "timeline": "镜头时间轴",
        "compose": "合成候选",
        "verify": "只读校验",
    }
    return {
        "headline": f"下一步是{names.get(stage, stage)}。这一步不新调用生图或配音。",
        "command": _produce(episode_id, "--stage", stage),
    }


def peek(episode_id: str) -> tuple[dict, bool]:
    """Read the saved run record without hashing media or upgrading episode files."""
    from app.paths import episode_dir

    folder = episode_dir(episode_id)
    script_exists = (folder / "script.md").is_file()
    legacy = _legacy_outputs(folder)
    state_path = folder / "production" / "v2" / "run.json"
    if not state_path.is_file():
        if legacy:
            return {
                "status": "LEGACY_OUTPUT",
                "blockers": [],
                "stages_done": [],
                "legacy_outputs": legacy,
            }, script_exists
        return {"status": "NOT_STARTED", "blockers": [], "stages_done": []}, script_exists
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError, TypeError):
        return {
            "status": "BLOCKED",
            "blockers": ["production state unreadable: json"],
            "stages_done": [],
        }, script_exists
    if not isinstance(state, dict):
        return {
            "status": "BLOCKED",
            "blockers": ["production state unreadable: json"],
            "stages_done": [],
        }, script_exists
    last = state.get("last_result") if isinstance(state.get("last_result"), dict) else {}
    status = {
        "status": last.get("status") or "PREPARED",
        "blockers": [last["error"]] if last.get("error") else [],
        "stages_done": _stages_from_artifacts(folder, state),
        "active_stage": state.get("active_stage"),
        "candidate_path": None,
    }
    if state.get("running"):
        try:
            alive = psutil.Process(state.get("owner_pid", -1)).create_time() == state.get("owner_created_at")
        except (psutil.Error, ValueError, TypeError, OSError):
            alive = False
        status["status"] = "RUNNING" if alive else "INTERRUPTED"
    return status, script_exists


def _file_inside(folder, value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        path = Path(value).resolve()
        return path.is_file() and path.is_relative_to(Path(folder).resolve())
    except (OSError, ValueError):
        return False


def _stages_from_artifacts(folder, state: dict) -> list[str]:
    """Recover progress from saved files. The last run only lists the stages it requested."""
    present = set()
    if _file_inside(folder, state.get("plan_path")):
        present.add("script")
    assets = state.get("image_assets")
    if isinstance(assets, dict) and assets:
        present.add("images")
    manifest = state.get("manifest_path")
    if _file_inside(folder, manifest):
        present.add("tts")
        if _delivery_recorded(folder, manifest):
            present.add("delivery")
    for stage, key in (
        ("quality", "quality_path"),
        ("alignment", "alignment_path"),
        ("timeline", "timeline_path"),
    ):
        if _file_inside(folder, state.get(key)):
            present.add(stage)
    if _file_inside(folder, state.get("receipt_path")):
        present.add("compose")
        last = state.get("last_result") if isinstance(state.get("last_result"), dict) else {}
        if "verify" in (last.get("stages_done") or []) and last.get("status") in {"CANDIDATE", "APPROVED"}:
            present.add("verify")
    return [stage for stage in STAGE_ORDER if stage in present]


def _delivery_recorded(folder, manifest_path: str) -> bool:
    path = Path(manifest_path).resolve()
    try:
        if not path.is_relative_to(Path(folder).resolve()):
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError, UnicodeError):
        return False
    return isinstance(data, dict) and "delivery_dispositions" in data


def _legacy_outputs(folder) -> list[str]:
    out = folder / "out"
    if not out.is_dir():
        return []
    return sorted(path.name for path in out.glob("*.mp4") if path.is_file())


def operator_status(episode_id: str) -> tuple[dict, bool]:
    """Use the full production status only when a v2 run record exists."""
    peeked, script_exists = peek(episode_id)
    if peeked.get("status") in {"NOT_STARTED", "LEGACY_OUTPUT"}:
        return peeked, script_exists
    from app.services.production_status import get_status
    full = get_status(episode_id)
    full["stages_done"] = peeked.get("stages_done") or []
    return full, script_exists


def list_rows(include_smoke: bool = False) -> tuple[list[dict], int]:
    from app.paths import episodes_dir

    ids = sorted(p.stem for p in episodes_dir().glob("*.json"))
    real, smoke = split_ids(ids)
    chosen = real + (smoke if include_smoke else [])
    if not chosen:
        chosen = smoke
    rows = []
    for episode_id in chosen:
        try:
            status, _script = peek(episode_id)
        except (OSError, ValueError):
            status = {"status": "BLOCKED"}
        rows.append({
            "episode": episode_id,
            "status": status.get("status") or "NOT_STARTED",
            "label": progress_label(status),
            "smoke": episode_id.startswith("smoke-"),
        })
    hidden = 0 if include_smoke else len(smoke)
    return rows, hidden


def progress_label(status: dict) -> str:
    code = status.get("status") or "NOT_STARTED"
    if code not in {"PREPARED", "STAGES_COMPLETE"}:
        return LABELS.get(code, code)
    done = set(status.get("stages_done") or [])
    nxt = next((stage for stage in STAGE_ORDER if stage not in done), None)
    names = {
        "script": "待做旁白计划",
        "images": "待做底图",
        "tts": "待做配音",
        "delivery": "待处理导演提示",
        "quality": "待做语音质检",
        "alignment": "待做字幕对齐",
        "timeline": "待做时间轴",
        "compose": "待合成",
        "verify": "待校验",
    }
    if nxt:
        return names[nxt]
    return LABELS.get(code, code)


def format_list(rows: list[dict], hidden_smoke: int = 0) -> str:
    if not rows:
        return "没有剧集。先建立 episodes/<id>.json，并把原稿放到 episodes/<id>/script.md。"
    lines = ["剧集                    状态                  查看下一步"]
    for row in rows:
        lines.append(f"{row['episode']:<24}{row['label']:<22}.\\cli.bat status {row['episode']}")
    if hidden_smoke:
        lines.append(f"另有 {hidden_smoke} 个冒烟残留未列出。要看它们：.\\cli.bat status --smoke")
    lines.append("看某一集该做什么：.\\cli.bat status <id>")
    return "\n".join(lines)


def format_detail(episode_id: str, advice: dict) -> str:
    lines = [f"{episode_id}  {advice['label']}", advice["headline"]]
    if advice.get("done"):
        lines.append("已完成阶段：" + " → ".join(advice["done"]))
    if advice.get("blockers"):
        lines.append("当前问题：")
        lines.extend(f"  - {item}" for item in advice["blockers"])
    if advice.get("command"):
        lines.append("下一步：")
        lines.append(f"  {advice['command']}")
    lines.extend(advice.get("notes") or [])
    return "\n".join(lines)
