# -*- coding: utf-8 -*-
"""分段渲染 + 断点续跑测试（2026-09-04）。

锁住 compose 长片分段路径的关键承诺：
  1) 压小 segment_max_sec 后，产出 ≥2 段，concat 成片可解码、无 .tmp 残留
  2) 已存在且帧数 == 预期 的段被跳过（断点续跑真实生效，不是每次全量重编）
  3) 段完成判据用逐镜求和口径——若退化成一次舍入，11/15 段判不完（见
     test_frame_accounting，这里是它的端到端验证）
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.legacy


import json
import os
import subprocess
import wave
from pathlib import Path


from conftest import NEEDS_FFMPEG

N_SHOTS = 24          # 24 镜，每镜 ~3s → ~1800 帧
SHOT_MS = 3200        # 单镜时长（错开整帧边界制造舍入分叉：3200ms→80帧整? 3200*25/1000=80 整）


def _ep_dir(ep_id: str) -> Path:
    """当前生效的 episodes 根下该集目录。

    P1-6：不再用模块级 `ROOT/"episodes"` 常量——那是写死真实目录的写法。
    调用 app.paths（调用期求值），测试期被 tmp_episodes 重定向到 tmp。
    """
    from app.paths import episode_dir
    return episode_dir(ep_id)


def _build_multi_episode(tmp_path, ep_id):
    """造 N 镜静态 episode：共享一张小图 + 一段 wav。写在**当前** episodes 根。"""
    from tests.conftest import make_small_images, make_small_wav
    src_p, big_p = make_small_images(tmp_path)
    wav = make_small_wav(tmp_path, dur_s=3.2)
    with wave.open(str(wav), "rb") as w:
        real_dur = int(w.getnframes() / float(w.getframerate()) * 1000)

    frames, shots = [], {}
    for i in range(N_SHOTS):
        fid = f"frame-{i:03d}"
        sid = f"shot-{i:03d}"
        frames.append({
            "id": fid, "text": f"第{i}镜测试", "narration_text": f"第{i}镜测试",
            "audio_path": str(wav).replace("\\", "/"), "audio_duration_ms": real_dur,
            "char_align": [], "subtitle": [], "shot_ids": [sid],
        })
        shots[sid] = {
            "id": sid, "frame_id": fid, "index": i, "type": "ken_burns",
            "is_hero": False, "narration": "", "shot_type": "medium",
            "duration_ms": SHOT_MS, "audio_start_ms": 0,
            "audio_end_ms": SHOT_MS, "audio_segment_path": None,
            "prompt": "", "negative_prompt": "", "expression": "", "pose": "",
            "ref_pack_id": None, "ref_pack_paths": [],
            "image_path": str(src_p).replace("\\", "/"),
            "subtitle_text": f"第{i}镜", "face_sim": None, "face_sim_warn": False,
            "retry_count": 0, "last_error": None, "stage": {},
            "motion": {"type": "static", "intensity": 0.0,
                       "start_xy": [0.5, 0.5], "end_xy": [0.5, 0.5],
                       "start_zoom": 1.0, "end_zoom": 1.0, "easing": "linear",
                       "grade": None},
        }
    d = {
        "id": ep_id, "title": ep_id, "episode_number": 1, "schema_version": "1.0",
        "status": "draft", "frames": frames, "shots": shots, "state": {},
        "total_seconds": N_SHOTS * SHOT_MS / 1000,
    }
    from app.paths import episode_json
    ep_json = episode_json(ep_id)
    ep_json.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return ep_id, src_p, big_p, real_dur


@pytest.fixture
def segmented_episode(tmp_path, monkeypatch, tmp_episodes):
    from app.config.config import get_settings
    cfg = get_settings().compose
    # 压低门槛强制走分段路径 + 压小段长制造 ≥2 段
    monkeypatch.setattr(cfg, "segment_min_frames", 100)     # 1800 帧 > 100 → 分段
    monkeypatch.setattr(cfg, "segment_max_sec", 8.0)        # 每段 ≤8s → 24*3.2/8 ≈ 9 段
    ep_id, src_p, big_p, dur = _build_multi_episode(tmp_path, f"segtest-{os.getpid()}")
    yield ep_id, big_p, dur
    # 无清理：tmp_episodes 重定向后一切都落在 tmp_path 里，pytest 自行回收。
    # 此前这里按 pid 精确 unlink + rmtree 真实 episodes/segtest-*，是误删风险源。


@NEEDS_FFMPEG
def test_segmented_compose_produces_valid_mp4(segmented_episode):
    ep_id, big_p, dur = segmented_episode
    from app.services.compose import compose_episode
    out = _ep_dir(ep_id) / "out"
    final = out / f"{ep_id}_m1.mp4"
    assert not final.exists(), "前提：无旧成片"

    p = compose_episode(ep_id)
    assert Path(p).exists() and Path(p).stat().st_size > 1024

    # 无 .tmp 残留（段 concat / mux 都应原子收口）
    tmps = list(out.glob("*.tmp")) + list(out.glob("*.mp4.tmp")) + \
           list((_ep_dir(ep_id) / "build" / "segments").glob("*.tmp"))
    assert tmps == [], f"残留 .tmp: {tmps}"

    # 至少产出多段
    seg_dir = _ep_dir(ep_id) / "build" / "segments"
    segs = sorted(seg_dir.glob("seg-*.mp4"))
    assert len(segs) >= 2, f"应分段产出, 实际 {len(segs)} 段"

    # 成片可解码、有音轨
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_type", "-of", "csv=p=0", str(final)],
                       capture_output=True, text=True)
    assert "video" in r.stdout and "audio" in r.stdout, \
        f"应含视频+音轨: {r.stdout}"
    dec = subprocess.run(["ffmpeg", "-v", "error", "-i", str(final),
                          "-f", "null", "-"], capture_output=True, text=True)
    assert dec.returncode == 0 and dec.stderr.strip() == "", \
        f"decode 错误: {dec.stderr[-400:]}"


@NEEDS_FFMPEG
def test_segments_are_skipped_when_already_done(segmented_episode, monkeypatch):
    """首跑产出段后，模拟中断重跑：已存在且帧数对得上的段被跳过（不重编）。"""
    ep_id, big_p, dur = segmented_episode
    from app.services.compose import (
        compose_episode, _encode_video_span, expected_frames, _segments_of,
    )
    # 先完整跑一遍，让所有段落盘
    compose_episode(ep_id)

    from app.services.compose import _probe_segment_frames
    from app.services.motion import FPS
    from app.managers import load
    ep = load(ep_id)
    shots = sorted(ep.shots.values(), key=lambda x: x.index)
    tl = [(s, 0, s.duration_ms) for s in shots]
    from app.config.config import get_settings
    cfg = get_settings().compose
    segs = _segments_of(tl, cfg.segment_max_sec)
    seg_dir = _ep_dir(ep_id) / "build" / "segments"

    # 手工验证：每个已有段的帧数 == expected_frames(段)（断点续跑能命中）
    for i, sl in enumerate(segs):
        p = seg_dir / f"seg-{i:03d}.mp4"
        assert p.exists(), f"seg-{i:03d} 缺失"
        nf = _probe_segment_frames(p)
        exp = expected_frames(sl)
        assert nf == exp, f"seg-{i:03d}: 帧数 {nf}≠预期 {exp}（判据不命中，续跑失效）"

    # 关键：重跑 compose 时用桩计数 _encode_video_span，断言零次重编
    calls = []
    orig = _encode_video_span

    def spy(*a, **k):
        calls.append(a)
        return orig(*a, **k)

    monkeypatch.setattr("app.services.compose._encode_video_span", spy)
    compose_episode(ep_id)   # 第二次跑，所有段已就绪
    assert calls == [], f"断点续跑应跳过所有段, 却重编了 {len(calls)} 次"


# ---------------------------------------------------------------------------
# G 批（2026-09-05）：段跳过判据必须带「参数指纹」，只按帧数判断会漏
# ---------------------------------------------------------------------------
def test_stale_segments_are_invalidated_when_params_change(tmp_path):
    """改了不影响帧数的参数（编码器/字幕/分段模式）也必须作废旧段。

    只按「段存在 + 帧数对」判断的话，改字幕样式、grade、cq/preset、编码器
    之后重跑会全部命中跳过、成片纹丝不动。最阴的一种：NVENC 中途降级
    libx264 后重跑，前 N 段仍跳过 → 硬件段+软件段混合编码被永久固化。
    """
    from app.services.compose import (
        _seg_params_fingerprint, _sync_segment_params,
    )

    seg_dir = tmp_path / "segments"
    seg_dir.mkdir()
    tl, segs = [], [[], []]
    vc_nv = ["-c:v", "h264_nvenc", "-cq", "20"]
    vc_sw = ["-c:v", "libx264", "-crf", "23"]

    def _reset():
        for p in seg_dir.glob("seg-*.mp4"):
            p.unlink()
        (seg_dir / "seg-000.mp4").write_text("x")
        (seg_dir / "seg-001.mp4").write_text("x")

    def _left():
        return sorted(p.name for p in seg_dir.glob("seg-*.mp4"))

    # 指纹本身：编码器不同 → 必须不同（否则降级检测不到）
    assert _seg_params_fingerprint(tl, vc_nv, "segmented", segs) != \
        _seg_params_fingerprint(tl, vc_sw, "segmented", segs), \
        "编码器变了指纹却一样 → NVENC 降级后旧段不会被作废"
    # 分段模式不同 → 必须不同（短片与长片都写 seg-000.mp4）
    assert _seg_params_fingerprint(tl, vc_nv, "single", [tl]) != \
        _seg_params_fingerprint(tl, vc_nv, "segmented", segs), \
        "短片/长片指纹却一样 → 整片会被当成长片的第 0 段跳过"

    # 1) 首次跑（无 params.json）：旧段没有指纹记录，一律作废（保守但正确）
    _reset()
    _sync_segment_params(seg_dir, tl, vc_nv, "segmented", segs)
    assert _left() == [], "无指纹记录的旧段必须作废（无法确认参数一致）"

    # 2) 参数不变再跑：段必须保留 —— 否则断点续跑就废了
    _reset()
    _sync_segment_params(seg_dir, tl, vc_nv, "segmented", segs)
    assert _left() == ["seg-000.mp4", "seg-001.mp4"], \
        "参数没变却作废旧段 → 断点续跑失效，每次都全量重编"

    # 3) 换编码器（NVENC 降级场景）：必须作废，防混合编码固化
    _reset()
    _sync_segment_params(seg_dir, tl, vc_sw, "segmented", segs)
    assert _left() == [], "编码器变了却复用旧段 → 成片硬件+软件混合编码固化"

    # 4) 换分段模式：必须作废，防「整片」被当成「长片的第 0 段」跳过
    _reset()
    _sync_segment_params(seg_dir, tl, vc_sw, "single", [tl])
    assert _left() == [], "分段模式变了却复用旧段 → 短片切长片会漏掉后半段"


# ---------------------------------------------------------------------------
# 2026-09-26：段指纹补 ffmpeg 版本 + 镜头时长改以磁盘 wav 为真源
# ---------------------------------------------------------------------------


def test_image_transition_only_on_image_change_and_keeps_frame_budget():
    """换图黑场：只加在真正换图的边界，且**不改变总帧数**。

    两条都是硬要求：
      - 同底图相邻镜头之间绝不能加黑场 —— 那是连续运镜（shot_builder 的
        _chained_motion），中间插一段黑等于人为制造一次剪辑，把刚修好的
        连续性又破坏掉。
      - 不加帧、不动音频 —— 原始设计（每个换图点插 K 帧黑 + K 帧静音）会同时
        改动 frames_for_duration_ms、expected_frames、分段边界和音频时钟，
        历史上正是这些口径分裂导致过「11 段永远判不完」。改为在既有帧内调
        亮度，总帧数一字不变，音画对位天然保持。
    """
    from types import SimpleNamespace
    from app.services.compose import _image_transitions, expected_frames

    def mk(i, img, n_ms):
        sh = SimpleNamespace(id=f"shot-{i:03d}", image_path=f"/x/{img}.png",
                             duration_ms=n_ms, frame_id=f"frame-{i:03d}")
        return sh

    A, B, C = "A", "B", "C"
    # A 的两镜 | B 的两镜 | C 的一镜。换图点只有 A->B 和 B->C 两处。
    shots = [mk(0, A, 2000), mk(1, A, 2000),
             mk(2, B, 2000), mk(3, B, 2000),
             mk(4, C, 2000)]
    timeline = [(sh, i * 2000, i * 2000 + 2000) for i, sh in enumerate(shots)]
    fpm = {sh.id: expected_frames([(sh, 0, 0)]) for sh, _, _ in timeline}
    before = expected_frames(timeline)

    off = _image_transitions(timeline, fpm, 0)
    assert off == {}, "fade=0 不该算出任何过渡"
    assert expected_frames(timeline) == before, "过渡改变了总帧数"

    tr = _image_transitions(timeline, fpm, 6)
    # 首镜没有前序 → 不淡入；A 内部（0->1）不淡出 → (0,0) 不入字典
    assert tr.get("shot-000") in (None, (0, 0)), "首镜不该有淡入、A 内部不该有淡出"
    # A 的末镜（->B 换图）淡出
    assert tr["shot-001"][1] > 0, "A->B 换图：A 的末镜应淡出"
    # B 的首镜（<-A 换图）淡入；B 内部（2->3）不淡出
    assert tr["shot-002"][0] > 0, "A->B 换图：B 的首镜应淡入"
    assert tr["shot-002"][1] == 0, "B 内部（2->3 同图）不该有淡出"
    # B 的末镜（->C 换图）淡出；C 的首镜淡入
    assert tr["shot-003"][1] > 0, "B->C 换图：B 的末镜应淡出"
    assert tr["shot-004"][0] > 0, "B->C 换图：C 的首镜应淡入"
    # 不越界、不重叠
    for sid, (fi, fo) in tr.items():
        n = fpm[sid]
        assert 0 <= fi <= 6 and 0 <= fo <= 6 and fi + fo <= n, f"{sid} 越界/重叠 (n={n})"
    assert expected_frames(timeline) == before, "过渡改变了总帧数"


def test_fade_to_black_returns_new_frame_and_clamps():
    """_fade_to_black 必须返回新帧。

    PIL 的 ``Image.point(lut)`` **返回新图、不修改原图**（本机 PIL 12.3 实测）。
    写成 `frame.point(lut)` 而不接返回值 = 静默空操作，黑场过渡会「配了但
    什么都没发生」—— 正是本项目反复出现的那类「配了不等于生效」的坑。
    """
    from PIL import Image
    from app.services.compose import _fade_to_black

    f = Image.new("RGB", (8, 8), (200, 200, 200))
    assert _fade_to_black(f, 0.0) is f, "amount=0 应原样返回入参"
    assert _fade_to_black(f, 1.0).getpixel((0, 0)) == (0, 0, 0), "amount=1 应全黑"
    v = _fade_to_black(f, 0.5).getpixel((0, 0))[0]
    assert 95 <= v <= 105, f"amount=0.5 应约 100，实得 {v}"
    assert _fade_to_black(f, 2.0).getpixel((0, 0))[0] == 0, "amount>1 应夹到全黑"
    assert _fade_to_black(f, -1.0) is f, "amount<0 应夹到原样"


def test_transition_frames_is_part_of_the_segment_fingerprint():
    """过渡帧数进了段指纹：改了就必须重编，否则「配了等于没改」。"""
    from app.services.compose import _seg_params_fingerprint
    vc = ["-c:v", "libx264"]
    a = _seg_params_fingerprint([], vc, "single", [[]], 0)
    b = _seg_params_fingerprint([], vc, "single", [[]], 6)
    assert a != b, "过渡帧数变了指纹却一样 → 旧段会被复用，新过渡不生效"


def test_ffmpeg_version_is_part_of_the_segment_fingerprint(monkeypatch):
    """ffmpeg 版本必须进指纹：升级后重跑不能复用旧版本编的段。

    编解码器行为随 ffmpeg 版本变（默认封装、色彩空间转换、码率控制的实现差异）。
    2026-09-26 本机从 8.x 换到 9.0.2，若指纹不含版本，升级后重跑会复用旧段 ——
    同一成片混着两种编码器的输出，而 verify 只数帧数，看不出来。
    v2 链路早有 runtime['ffmpeg']（production_v2._render），legacy 这条曾漏。
    """
    from app.services import compose

    monkeypatch.setattr(compose, "_ffmpeg_version", lambda: "ffmpeg version 8.1.1")
    a = compose._seg_params_fingerprint([], ["-c:v", "libx264"], "single", [[]])
    monkeypatch.setattr(compose, "_ffmpeg_version", lambda: "ffmpeg version 9.0.2")
    b = compose._seg_params_fingerprint([], ["-c:v", "libx264"], "single", [[]])
    assert a != b, "ffmpeg 版本变了指纹却一样 → 旧版本编的段会被复用"


def test_shot_length_follows_the_wav_not_stale_json(tmp_episodes, smoke_episode,
                                                   monkeypatch):
    """镜头时长必须跟随磁盘 wav，而不是 json 里可能过时的 audio_duration_ms。

    旧实现按 json 值算镜头长度、却把真实 wav 摆到音频轨。两者不同步时（TTS 重跑后
    json 过时、手工换过 wav），每镜的差值逐镜累积成音画整体漂移，而 verify 只数总帧
    数、永远看不出来。现已改为磁盘为准，json 值降级为「是否过时」的交叉校验。

    这里把 json 的 audio_duration_ms 改到与真实 wav 差 100ms —— 仍在
    _WAV_TOLERANCE_MS=120 容差内，所以**不会**被 _verify_audio_vs_disk 拒掉。于是一
    个只信 json 的实现会安静地漂 100ms，而正确实现必须按 wav 真值排布。
    断言方式：跑真实的时序构造 + 拼旁白（只桩掉编码），看 narration.wav 实际长度。
    """
    import json as _json
    import wave as _wave
    from pathlib import Path

    ep_id, _src, _big, real_ms = smoke_episode
    ep_json = tmp_episodes / f"{ep_id}.json"
    d = _json.loads(ep_json.read_text(encoding="utf-8"))
    stale_ms = real_ms + 100
    d["frames"][0]["audio_duration_ms"] = stale_ms
    ep_json.write_text(_json.dumps(d, ensure_ascii=False), encoding="utf-8")

    # 只桩掉编码与 mux；时序构造、wav 读取、拼旁白、长度断言全部走真代码
    from app.services import compose
    monkeypatch.setattr(compose, "_encode_video_span",
                        lambda *a, **k: pytest.fail("本测试不关心编码"))
    monkeypatch.setattr(compose, "_concat_segments",
                        lambda *a, **k: pytest.fail("本测试不关心拼接"))
    monkeypatch.setattr(compose, "check_font_available", lambda *a, **k: None)
    # verify_media / upscale 在 compose 里是「函数内导入」或「已绑定名」，
    # 要打到真正被查的名字上，否则 setattr 会建出一个没人用的新属性。
    monkeypatch.setattr("app.services.media_integrity.verify_media",
                        lambda *a, **k: {"status": "stubbed"})
    monkeypatch.setattr(compose, "_probe_segment_frames", lambda p: 1)
    monkeypatch.setattr(compose, "_sync_segment_params", lambda *a, **k: None)
    monkeypatch.setattr(compose, "upscale", lambda p: Path(p), raising=False)

    # pytest.fail 抛的是 BaseException 子类（Failed），不是 Exception，所以抓
    # BaseException。失败点是刻意为之：我们要的产物 narration.wav 在编码前已落盘。
    with pytest.raises(BaseException) as exc:
        compose.compose_episode(ep_id)
    assert "本测试不关心" in str(exc.value), f"应失败在编码桩上，实际：{exc.value}"

    nwav = Path(compose.episode_dir(ep_id)) / "build" / "narration.wav"
    assert nwav.is_file(), "narration.wav 未生成"
    with _wave.open(str(nwav), "rb") as w:
        samples = w.getnframes()

    from app.services.spec import TAIL_MS
    # 首镜没有 head：compose 的 head_of 要求 prev_g is not None，即只在「组发生
    # 变化」时给 head，序列第一镜拿不到。所以首镜总长 = wav 真值 + tail。
    from_disk = int(compose.NARRATION_SR * (real_ms + TAIL_MS) / 1000)
    from_stale = int(compose.NARRATION_SR * (stale_ms + TAIL_MS) / 1000)
    assert abs(samples - from_disk) <= 2, \
        f"narration.wav {samples} 采样，应按磁盘真值 {real_ms}ms 排布（≈{from_disk}）"
    assert abs(samples - from_stale) > 2, \
        f"narration.wav {samples} 采样竟贴合过时的 json 值 {stale_ms}ms —— " \
        f"镜头时长仍在信 json，音画会整体漂 {stale_ms - real_ms}ms"
