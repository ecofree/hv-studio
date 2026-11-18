#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hv-studio 官方统一 TTS 生产级入口 (tools/render_episode_tts.py)
【方案 B+/C 终极标准收敛 · 40分钟超长篇 3 道声学锚定锁闭环实现】

严格践行：
1. 输入源锁定：强制以 AST 结构化产物 spoken_units.json 为唯一输入源，彻底隔离 Markdown 与构图标注；
2. 幕边界物理隔离断块：每一幕独立切块，绝不跨幕合并；
3. “90% 声学基底 + 10% 4档标准枚举”：剧本语气动态映射至标准枚举，注入 Style Prompt；
4. Zero-Trim 指令级语境参考：上一切片尾句作为 context_reference 注入，严禁硬裁音频；
5. 防误读时长门禁：实际时长/预期时长 > 1.25 自动重试；
6. Golden Anchor 黄金首切片相对门禁：|F0 - anchor_f0| <= 5.0Hz, 语速偏差 <= 8%，最大4次最优兜底；
7. 自适应底噪无缝拼接 + 35ms 等功率余弦交叉淡入淡出，导出 -16.0 LUFS 广播级母带。
"""

import os
import sys
import re
import io
import time
import json
import argparse
import wave
import numpy as np
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TOOLS_DIR = ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from phonetic_engine import normalize_phonetics
from audio_mastering_engine import (
    shape_chunk_audio,
    stitch_wav_sequence,
    master_pure_voice_track
)
from gemini_tts_engine import (
    render_chunk_with_guardrail,
    split_text_into_chunks,
    smooth_punctuation,
    LOCKED_MODEL,
    DEFAULT_VOICE,
    CONSTANT_STYLE
)


def export_spoken_units_from_ast(episode_dir: Path, force_rebuild: bool = False) -> list[dict]:
    """
    通过官方 script_handoff AST 解析器生成绝对纯净的 spoken_units.json。
    彻底绝缘所有【视听构图】和舞台提示词，同时精准提取每幕的【语气】导演指令。
    """
    spoken_units_json_path = episode_dir / "editorial" / "spoken_units.json"
    if spoken_units_json_path.exists() and not force_rebuild:
        try:
            with open(spoken_units_json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data and isinstance(data, list):
                return data
        except Exception:
            pass

    # 从 narration.md 派生 AST
    narration_md = episode_dir / "editorial" / "narration.md"
    if not narration_md.exists():
        # 兼容从 current.json 查找 source.md
        current_json = episode_dir / "editorial" / "current.json"
        if current_json.exists():
            c_data = json.loads(current_json.read_text(encoding="utf-8"))
            narration_md = Path(c_data["source"])

    if not narration_md.exists():
        raise FileNotFoundError(f"找不到剧本文件: {narration_md}")

    from app.services.script_handoff import derive
    raw_source = narration_md.read_text(encoding="utf-8-sig")
    payload = derive(raw_source)
    
    doc_units = {u["unit_id"]: u["approved_spoken_text"] for u in json.loads(payload["document.json"])["units"]}
    authoring_acts = json.loads(payload["authoring.json"])["acts"]

    # 提取每幕的【语气】指令
    # 在 raw_source 中按幕标题切分找【语气：...】
    act_blocks = re.split(r"(?=^#{1,4}\s*【?第[一二三四五六七八九十]+幕)", raw_source, flags=re.M)
    act_tone_map = {}
    for block in act_blocks:
        m_head = re.search(r"【?第[一二三四五六七八九十]+幕[：\]][^\n]+", block)
        if m_head:
            head_title = m_head.group(0).strip("【】# ")
            m_tone = re.search(r"【语气[：:]([^】]+)】", block)
            tone_val = m_tone.group(1).strip() if m_tone else ""
            act_tone_map[head_title] = tone_val

    spoken_acts = []
    for idx, act in enumerate(authoring_acts, 1):
        heading = act["heading"]
        m_title = re.search(r"【(.*?)】", heading)
        title = m_title.group(1) if m_title else heading.strip("# ")
        
        # 查找本幕语气
        matched_tone = ""
        for h_k, t_v in act_tone_map.items():
            if title in h_k or h_k in title:
                matched_tone = t_v
                break

        # 汇集纯台词文本
        spoken_text_parts = [doc_units[uid] for uid in act["unit_ids"] if uid in doc_units]
        full_act_text = "\n\n".join(spoken_text_parts)

        spoken_acts.append({
            "act_index": idx,
            "act_title": title,
            "tone": matched_tone,
            "spoken_text": full_act_text
        })

    with open(spoken_units_json_path, "w", encoding="utf-8") as f:
        json.dump(spoken_acts, f, ensure_ascii=False, indent=2)
    print(f">>> [AST 结构化导出完成] 已生成唯一受控输入: {spoken_units_json_path} (共 {len(spoken_acts)} 幕)")
    return spoken_acts


def render_episode(
    episode_id: str,
    voice: str = DEFAULT_VOICE,
    force_clean: bool = False,
    max_retries: int = 4
):
    episode_dir = ROOT / "workspace" / "episodes" / episode_id
    if not episode_dir.exists():
        raise FileNotFoundError(f"找不到剧本目录: {episode_dir}")

    output_dir = episode_dir / "editorial" / "audio_gemini_charon"
    chunks_raw_dir = output_dir / "chunks_raw"
    chunks_shaped_dir = output_dir / "chunks_shaped"

    output_dir.mkdir(parents=True, exist_ok=True)
    chunks_raw_dir.mkdir(parents=True, exist_ok=True)
    chunks_shaped_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print(f"【隋乱唐兴·官方工业级音频渲染】 集号: {episode_id}")
    print(f"锁定模型: {LOCKED_MODEL} | 官方静态音色: {voice}")
    print("40分钟超长篇 3 道声学锚定锁已全面激活：")
    print(" - 锁 1: 90% 声学基底 + 10% 4档标准枚举语气映射")
    print(" - 锁 2: Golden Anchor (首切片) 相对门禁 (|F0-anchor|<=5Hz, CPM偏差<=8%, 最多4次重试)")
    print(" - 锁 3: 强标点下收束切块 + 自适应环境底噪填充 + 35ms 等功率余弦缝合")
    print("=" * 80)

    if force_clean:
        print(">>> [强制清理] 彻底物理清空旧切片与母带...")
        for old_f in list(chunks_raw_dir.glob("*.wav")) + list(chunks_shaped_dir.glob("*.wav")):
            try: old_f.unlink()
            except Exception: pass
        for old_act in output_dir.glob("*.wav"):
            try: old_act.unlink()
            except Exception: pass

    # 1. 加载唯一的 AST 结构化纯台词
    acts = export_spoken_units_from_ast(episode_dir, force_rebuild=force_clean)
    print(f"已加载 AST 纯台词: 全剧共 {len(acts)} 幕。\n")

    t_start = time.time()
    golden_anchor_f0 = None
    golden_anchor_cpm = None
    all_chunks_stats = []

    act_wav_paths = []

    for act_idx, act in enumerate(acts, 1):
        title = act["act_title"]
        tone = act["tone"]
        raw_text = act["spoken_text"]
        
        # 多音字注音规范化
        clean_text = normalize_phonetics(raw_text)
        
        clean_title = re.sub(r'[\\/*?:"<>|]', "", title).strip()
        print(f">>> 正在处理 第 {act_idx}/{len(acts)} 幕: {clean_title} (语气指令: '{tone or '默认平静叙事'}')")

        # 幕边界强制物理断块，绝不跨幕合并
        chunks = split_text_into_chunks(clean_text)
        print(f"  - 幕内强标点安全切块: 已等分为 {len(chunks)} 个黄金片段 (收束于句末标点)")

        act_chunk_shaped_wavs = []
        last_chunk_tail_sentence = ""  # 跨切片语境连续性参考

        for c_idx, chunk in enumerate(chunks, 1):
            raw_chunk_file = chunks_raw_dir / f"act_{act_idx:02d}_chunk_{c_idx:02d}.wav"
            shaped_chunk_file = chunks_shaped_dir / f"act_{act_idx:02d}_chunk_{c_idx:02d}_shaped.wav"
            
            cn_words = len(re.findall(r'[\u4e00-\u9fa5]', chunk))
            print(f"    - 切片 {c_idx}/{len(chunks)} ({cn_words} 汉字)... ", end="", flush=True)

            need_render = True
            if raw_chunk_file.exists() and raw_chunk_file.stat().st_size > 1024:
                # 检查缓存是否满足门禁
                try:
                    from gemini_tts_engine import detect_fundamental_f0, get_wav_duration
                    f_bytes = raw_chunk_file.read_bytes()
                    c_med, c_mean, c_p90, _ = detect_fundamental_f0(f_bytes)
                    c_dur = get_wav_duration(f_bytes)
                    c_cpm = (cn_words / c_dur) * 60.0 if c_dur > 0 else 0
                    
                    # 绝对门禁检查
                    f0_ok = (95.0 <= c_med <= 128.0 and c_p90 <= 212.0)
                    
                    # 相对门禁检查
                    rel_ok = True
                    if golden_anchor_f0 is not None:
                        if abs(c_med - golden_anchor_f0) > 5.0:
                            rel_ok = False
                    if golden_anchor_cpm is not None:
                        if abs(c_cpm - golden_anchor_cpm) / golden_anchor_cpm > 0.08:
                            rel_ok = False
                            
                    if f0_ok and rel_ok:
                        need_render = False
                        print(f"[缓存命中: F0={c_med:.1f}Hz, CPM={c_cpm:.0f}] -> ", end="", flush=True)
                        all_chunks_stats.append({
                            "chunk_id": f"act_{act_idx:02d}_{c_idx:02d}",
                            "words": cn_words,
                            "duration": c_dur,
                            "cpm": c_cpm,
                            "med_f0": c_med
                        })
                except Exception:
                    need_render = True

            if need_render:
                c_t0 = time.time()
                wav_bytes, used_model, key_suffix, stats = render_chunk_with_guardrail(
                    text_chunk=chunk,
                    voice=voice,
                    tone_text=tone,
                    context_reference=last_chunk_tail_sentence,
                    anchor_f0=golden_anchor_f0,
                    anchor_cpm=golden_anchor_cpm,
                    max_retries=max_retries,
                    target_male=True
                )
                raw_chunk_file.write_bytes(wav_bytes)
                c_dt = time.time() - c_t0
                
                # 若为全剧第 1 块，锁定为全局 Golden Anchor 标尺
                if golden_anchor_f0 is None:
                    golden_anchor_f0 = stats["med_f0"]
                    golden_anchor_cpm = stats["cpm"]
                    print(f"\n  ★ [锁定全剧 Golden Anchor 基准标尺] F0={golden_anchor_f0:.1f}Hz, 语速={golden_anchor_cpm:.0f} CPM ★")

                all_chunks_stats.append({
                    "chunk_id": f"act_{act_idx:02d}_{c_idx:02d}",
                    "words": cn_words,
                    "duration": stats["duration"],
                    "cpm": stats["cpm"],
                    "med_f0": stats["med_f0"]
                })
                
                fallback_flag = " [最优兜底]" if stats.get("is_fallback") else ""
                print(f"[渲染合格({stats['attempts']}次{fallback_flag}): {c_dt:.1f}s | F0={stats['med_f0']:.1f}Hz | 语速={stats['cpm']:.0f}CPM] -> ", end="", flush=True)

            # 频谱塑形与 EBU-R128 预均衡
            if not (shaped_chunk_file.exists() and shaped_chunk_file.stat().st_size > 1024):
                shape_chunk_audio(raw_chunk_file, shaped_chunk_file)
                print(f"[塑形完成]")
            else:
                print(f"[塑形缓存]")

            act_chunk_shaped_wavs.append(shaped_chunk_file)

            # 提取本切片最后一整句，传递给下一个切片作为 Zero-Trim 语境指示
            sent_matches = re.findall(r'[^。！？\n]+[。！？]', chunk)
            if sent_matches:
                last_chunk_tail_sentence = sent_matches[-1].strip()
            else:
                last_chunk_tail_sentence = chunk[-40:]

            time.sleep(0.2)

        # 幕内缝合（填充自适应底噪 + 35ms 等功率余弦交叉淡入淡出，200ms 气口）
        act_wav = output_dir / f"act_{act_idx:02d}_{clean_title}.wav"
        stitch_wav_sequence(act_chunk_shaped_wavs, act_wav, pause_ms=200, crossfade_ms=35)
        act_wav_paths.append(act_wav)
        print(f"  => 第 {act_idx} 幕完成: {act_wav.name} ({act_wav.stat().st_size / 1024:.1f} KB)\n")

    # 全剧母带终混
    print("=" * 80)
    print("开始全剧广播级终混：幕间填充自适应底噪 (300ms 气口) -> 锁定 -16.0 LUFS 广播母带...")
    temp_voice_master = output_dir / "temp_voice_master_unmasked.wav"
    stitch_wav_sequence(act_wav_paths, temp_voice_master, pause_ms=300, crossfade_ms=35)

    final_master_wav = output_dir / f"{episode_id}_完整解说母带_{voice}.wav"
    master_pure_voice_track(temp_voice_master, final_master_wav)

    if temp_voice_master.exists():
        temp_voice_master.unlink()

    # 全剧统计与审计指标计算
    total_sec = 0
    with wave.open(str(final_master_wav), 'rb') as w:
        frames = w.getnframes()
        rate = w.getframerate()
        total_sec = frames / float(rate)

    m, s = divmod(int(total_sec), 60)
    total_time = time.time() - t_start

    # 计算 F0 标准差与语速 CPM 标准差
    f0_values = [s["med_f0"] for s in all_chunks_stats if s["med_f0"] > 0]
    cpm_values = [s["cpm"] for s in all_chunks_stats if s["cpm"] > 0]
    
    f0_std = float(np.std(f0_values)) if f0_values else 0.0
    cpm_std = float(np.std(cpm_values)) if cpm_values else 0.0
    mean_f0 = float(np.mean(f0_values)) if f0_values else 0.0
    mean_cpm = float(np.mean(cpm_values)) if cpm_values else 0.0

    print("=" * 80)
    print("【40分钟长剧长程一致性验收审计报告】")
    print(f"输出母带路径: {final_master_wav}")
    print(f"全剧总时长: {m} 分 {s} 秒 ({total_sec:.1f} 秒) | 渲染总耗时: {total_time:.1f} 秒")
    print(f"总计切片数: {len(all_chunks_stats)} 个 | 全剧总字数: {sum(s['words'] for s in all_chunks_stats)} 汉字")
    print(f"★ 基频一致性: 均值 = {mean_f0:.1f} Hz | 标准差 = {f0_std:.2f} Hz (验收目标: < 3.5 Hz) -> {'[合格]' if f0_std < 3.5 else '[超标]'}")
    print(f"★ 语速一致性: 均值 = {mean_cpm:.1f} CPM | 标准差 = {cpm_std:.2f} CPM (验收目标: < 10.0 CPM) -> {'[合格]' if cpm_std < 10.0 else '[超标]'}")
    print("试听抽检时间节点推荐：")
    print(f" - 前部节点 (第 1 分钟):  00:01:00")
    print(f" - 中部节点 (第 20 分钟): 00:20:00")
    print(f" - 尾部节点 (第 38 分钟): {min(m-1, 38):02d}:00:00")
    print("=" * 80)

    # 导出元数据留底
    metadata = {
        "episode_id": episode_id,
        "voice": voice,
        "golden_anchor_f0": golden_anchor_f0,
        "golden_anchor_cpm": golden_anchor_cpm,
        "f0_std": f0_std,
        "cpm_std": cpm_std,
        "mean_f0": mean_f0,
        "mean_cpm": mean_cpm,
        "total_duration_sec": total_sec,
        "chunks": all_chunks_stats
    }
    meta_path = output_dir / "master_metadata.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description="hv-studio 官方工业级音频渲染 CLI")
    parser.add_argument("--ep", required=True, help="剧集 ID，例如 sltx-18-user-qianshuiyuan-hexi")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="官方静态音色，默认 Charon")
    parser.add_argument("--force-clean", action="store_true", help="强制清空旧切片重新渲染")
    parser.add_argument("--max-retries", type=int, default=4, help="切片门禁最大重试次数")
    args = parser.parse_args()

    render_episode(
        episode_id=args.ep,
        voice=args.voice,
        force_clean=args.force_clean,
        max_retries=args.max_retries
    )


if __name__ == "__main__":
    main()
