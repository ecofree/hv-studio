"""
时序校验断言服务（M2 实装，§5.4 契约落地）。

合成前必须全部通过，任一失败即中止。提供：
  - assert_episode_timing(ep) -> dict     汇总报告（含 pass/fail 每项）
  - TimingError                            任一断言失败时抛出（含明细）

五个断言（TECH_DESIGN.md §5.4，参数由本模块顶部常量承载以便复用/测试）：
  1. Σ(镜头旁白片段) ≈ Σ(帧旁白)   ± 100 ms  —— 防音频在 frame→shot 切分中丢失/重复
  2. 每个镜头帧数 ≥ 40（1.6s @25fps）    下限保护（防画面闪切）
  3. 每个镜头帧数 ≤ 200（8s @25fps）     上限保护（防静止过长掉完播率）
  4. 字幕首帧时间戳 ≥ 0                  硬约束
  5. 视频流总帧数 = Σ(镜头帧数)          严格相等（帧数对齐无漂移）

口径说明：
  呼吸 pad(100 head + 150 tail) 由合成层按句边界另加，落在 shot.duration_ms
  里，不参与 #1 音频一致性比较。故额外补一条：每镜 duration ≥ 其旁白片段，
  保证音频放得进画面（总对齐由 #5 兜底）。
"""
from __future__ import annotations

from app.models import Episode, Shot

# FPS / HEAD_MS / TAIL_MS 不再在此另写一份：统一从 spec 导入（整改 C1）。
# 此前这里写着「与 motion.FPS 同步」的手抄副本 —— 注释承诺同步，但没有任何
# 机制保证同步，改一处忘一处就是静默的时间轴错位。re-export 只为兼容既有写法。
from app.services.spec import (  # noqa: F401  (re-export)
    FPS, HEAD_MS, TAIL_MS, MAX_FRAMES, MIN_FRAMES,
)
# H 批：MIN_FRAMES / MAX_FRAMES 也搬到 spec 了（它们是输出规格，且 script.py
# 的切分下限由它们反推）。此处只留 re-export，不再有第二份定义。
# 断言阈值
SUM_TOLERANCE_MS = 100     # #1


class TimingError(RuntimeError):
    """任一断言失败时抛出，message 带失败明细。"""
    pass


def _group_of(frame_id: str) -> str:
    """帧所属「源句子组」——单一真源在 spec（呼吸间隙按组边界插入）。"""
    from app.services.spec import group_of
    return group_of(frame_id)


def projected_shot_pacing(ep: Episode) -> dict:
    """在【还没有 shots】的阶段，从 frames 投影每镜将占多少帧。

    为什么要在花钱前算这个（lishimin-03 实测事故，2026-09-28）：
      镜长上限 #3（≤200 帧）原本只在 compose 阶段断言，而 compose 在
      59 张图全部付完钱之后才跑。那一集 285 帧里有 2 帧是旁白真放慢到
      8.03s / 8.24s（18 字、29 字，都没超切帧器的 MAX_FRAG_CHARS），
      结果是：预算花完 → 才被拦 → 重切帧 → 重装配 → 重合成。
      切帧器按 TTS_CHARS_PER_SEC=3.9 估算，而外部真人旁白有戏剧性停顿，
      字数根本不是时长的可靠预测；只有**实测音频**能提前判定。

    投影必须与 compose 完全同口径，否则会出现假阳性（把合规集拦下来）。
    compose 的呼吸段规则（compose.py:962-970）：
      head = HEAD_MS  当且仅当 存在前一帧且组号不同
      tail = TAIL_MS  当且仅当 是最后一帧 或 后一帧组号不同
    返回 {ok, fails, per_frame, over, under, total_ms}。
    """
    from app.services.motion import frames_for_duration_ms

    frames = ep.frames
    n = len(frames)
    per: dict[str, dict] = {}
    over: list[dict] = []
    under: list[dict] = []
    for i, f in enumerate(frames):
        g = _group_of(f.id)
        prev_g = _group_of(frames[i - 1].id) if i else None
        next_g = _group_of(frames[i + 1].id) if i + 1 < n else None
        head = HEAD_MS if (prev_g is not None and g != prev_g) else 0
        tail = TAIL_MS if (next_g is None or next_g != g) else 0
        audio = f.audio_duration_ms or 0
        shot_ms = audio + head + tail
        nf = frames_for_duration_ms(shot_ms, FPS)
        row = dict(frame_id=f.id, audio_ms=audio, head_ms=head, tail_ms=tail,
                   shot_ms=shot_ms, frames=nf, chars=len(f.narration_text or ""))
        per[f.id] = row
        if nf > MAX_FRAMES:
            over.append(row)
        elif nf < MIN_FRAMES:
            under.append(row)

    fails: list[str] = []
    for r in over:
        fails.append(
            f"{r['frame_id']}: 旁白 {r['audio_ms']}ms + 呼吸 {r['head_ms']+r['tail_ms']}ms "
            f"= {r['shot_ms']}ms -> {r['frames']} 帧 > {MAX_FRAMES}"
            f"（超 {r['frames'] - MAX_FRAMES} 帧；实测 {r['chars']} 字）")
    for r in under:
        fails.append(
            f"{r['frame_id']}: 旁白 {r['audio_ms']}ms + 呼吸 {r['head_ms']+r['tail_ms']}ms "
            f"= {r['shot_ms']}ms -> {r['frames']} 帧 < {MIN_FRAMES}"
            f"（欠 {MIN_FRAMES - r['frames']} 帧）")
    if any((f.audio_duration_ms or 0) == 0 for f in frames) and frames:
        n_zero = sum(1 for f in frames if (f.audio_duration_ms or 0) == 0)
        fails.insert(0, f"有 {n_zero} 帧 audio_duration_ms=0：旁白音频尚未就绪，"
                        f"此时判断镜长没有意义（先跑 tts 或导入外部音频）")
    return {"ok": not fails, "fails": fails, "per_frame": per,
            "over": over, "under": under,
            "total_ms": sum((f.audio_duration_ms or 0) for f in frames)}


def assert_pacing_or_raise(ep: Episode) -> dict:
    """花钱前的镜长预检。失败即 raise TimingError（此时尚未发起任何 provider 调用）。"""
    rep = projected_shot_pacing(ep)
    if not rep["ok"]:
        detail = "\n  - ".join(rep["fails"])
        raise TimingError(
            "镜长预检失败（发生在生图之前，未产生任何图片费用）:\n"
            f"  - {detail}\n"
            f"  处置：超限帧需按**实测音频**在旁白真实停顿处切开（字幕同步断句），"
            f"或重跑 --stage script 调整切分；欠限帧需与相邻帧合并。"
            f"不要靠改 MAX_FRAMES 绕过——那是成片规格，不是可调参数。")
    return rep


def _shot_frames(shot: Shot) -> int:
    """镜头帧数——委托 motion.frames_for_duration_ms（全项目唯一真值口径），
    与 plan_frames / compose.expected_frames 保证永不口径分裂。"""
    from app.services.motion import frames_for_duration_ms
    return frames_for_duration_ms(shot.duration_ms or 0, FPS)


def shot_report(shot: Shot) -> dict:
    """单个镜头的时序断言（#2/#3）。返回 {ok, fails:[msg], frames}。"""
    fails: list[str] = []
    nf = _shot_frames(shot)
    if nf < MIN_FRAMES:
        fails.append(f"{shot.id}: 帧数 {nf} < {MIN_FRAMES}({MIN_FRAMES/FPS:.1f}s 下限)")
    if nf > MAX_FRAMES:
        fails.append(f"{shot.id}: 帧数 {nf} > {MAX_FRAMES}({MAX_FRAMES/FPS:.1f}s 上限)")
    return {"ok": not fails, "fails": fails, "frames": nf,
            "duration_ms": shot.duration_ms}


def assert_episode_timing(
    ep: Episode,
    audio_total_ms: int | None = None,
    video_total_frames: int | None = None,
) -> dict:
    """
    对整集做 §5.4 时序断言。任一失败抛 TimingError。
    返回报告 dict（方便调用方打印/记录）。

    参数：
      audio_total_ms     #1 用：旁白拼接总时长（可由调用方传帧时长和）
                         None 时内部用 Σ shot.audio(端-始) 近似
      video_total_frames #5 用：成片实际视频帧数（合成后回填）
                         None 时跳过 #5（合成前无法知道）
    """
    # 若只有 frames 无 shots（script 阶段），退化：仅检查 frame 自身无负时长
    if not ep.shots:
        return _assert_frames_only(ep)

    report: dict = {"ok": True, "fails": [], "per_shot": {}}

    # #2/#3 逐镜帧数
    for sid in sorted(ep.shots):
        shot = ep.shots[sid]
        sr = shot_report(shot)
        report["per_shot"][sid] = sr
        if not sr["ok"]:
            report["ok"] = False
            report["fails"].extend(sr["fails"])

    # #1 镜头总时长 ≈ 音频总时长 ±100ms
    # #1 音频不丢不重：Σ(shot 分配音频时长) ≈ Σ(frame.audio_duration_ms)
    #    口径：shot.audio_end - audio_start = 该 shot 承载的旁白片段时长。
    #    合成时呼吸 pad(100+150ms) 落在 shot.duration 里，不影响此断言；
    #    本断言专防「旁白片段在 frame→shot 切分时被丢失/重复」。
    shot_audio_ms = sum(
        (shot.audio_end_ms or 0) - (shot.audio_start_ms or 0)
        for shot in ep.shots.values()
    )
    if audio_total_ms is None:
        audio_total_ms = sum((f.audio_duration_ms or 0) for f in ep.frames)
    diff = abs(shot_audio_ms - audio_total_ms)
    report["sum"] = {"shot_audio_ms": shot_audio_ms,
                     "frame_audio_ms": audio_total_ms, "diff_ms": diff}
    if audio_total_ms and diff > SUM_TOLERANCE_MS:
        report["ok"] = False
        report["fails"].append(
            f"Σ镜头旁白片段 {shot_audio_ms}ms ≠ Σ帧旁白 {audio_total_ms}ms "
            f"(差 {diff}ms > 允差 {SUM_TOLERANCE_MS}ms；音频可能丢/重)")

    # 补充：每镜 duration ≥ 其旁白片段（音频必须放得进画面；呼吸 pad 是
    #    合成层按句边界另加的，不在此判，最终由 #5 总帧数对齐兜底）
    for sid, shot in ep.shots.items():
        span = (shot.audio_end_ms or 0) - (shot.audio_start_ms or 0)
        if (shot.duration_ms or 0) < span:
            report["ok"] = False
            report["fails"].append(
                f"{sid}: duration {shot.duration_ms}ms < 旁白 {span}ms"
                f"（音频放不进画面）")

    # #4 字幕首帧 ≥ 0：字幕时间轴从 0 开始，任何 shot 音频起始 < 0 即错
    for sid, shot in ep.shots.items():
        if (shot.audio_start_ms or 0) < 0 or (shot.audio_end_ms or 0) < 0:
            report["ok"] = False
            report["fails"].append(f"{sid}: 音频时间戳为负 "
                                   f"({shot.audio_start_ms}~{shot.audio_end_ms})")

    # #5 视频总帧数 = 末镜结束帧（合成后校验）
    if video_total_frames is not None:
        last_end = sum(_shot_frames(s) for s in ep.shots.values())
        report["video"] = {"actual": video_total_frames, "expected": last_end}
        if video_total_frames != last_end:
            report["ok"] = False
            report["fails"].append(
                f"视频总帧数 {video_total_frames} ≠ Σ镜头帧 {last_end}")

    report["fails"] = report["fails"]  # already
    return report


def _assert_frames_only(ep: Episode) -> dict:
    """script 阶段（仅 frames）：校验无负/空时长即过。"""
    fails = []
    for f in ep.frames:
        if (f.audio_duration_ms or 0) < 0:
            fails.append(f"{f.id}: audio_duration_ms 为负")
    return {"ok": not fails, "fails": fails, "per_shot": {}}


def assert_timing_or_raise(
    ep: Episode,
    audio_total_ms: int | None = None,
    video_total_frames: int | None = None,
) -> dict:
    """带抛出的便捷入口：失败即 raise TimingError。"""
    rep = assert_episode_timing(ep, audio_total_ms, video_total_frames)
    if not rep["ok"]:
        detail = "\n  - ".join(rep["fails"])
        raise TimingError(f"时序断言失败:\n  - {detail}")
    return rep
