"""A local listening page for subtitle drafts, with no implicit approval action."""
import html
import json
import shutil
import re
from pathlib import Path
from app.services.narration_pipeline import read,validate_assembly
from app.services.provenance import file_sha256


def create(draft_path):
    draft=read(draft_path);manifest=validate_assembly(draft['manifest_path'])
    target=Path(draft_path).parent
    audio=target/'review-audio.wav'
    if not audio.exists():shutil.copy2(manifest['master_path'],audio)
    if file_sha256(audio)!=manifest['master_sha256']:raise ValueError('review audio changed')
    sr=manifest['sample_rate'];cues=draft['subtitles']
    rows=[]
    for i,c in enumerate(cues,1):
        start=c['sample_start']/sr;end=c['sample_end']/sr
        rows.append(f'<button class="cue" data-start="{start:.6f}" data-end="{end:.6f}" aria-label="播放第 {i} 条字幕">'
                    f'<span class="number">{i:02}</span><span class="words">{html.escape(c["text"])}</span>'
                    f'<span class="time">{start:.2f} — {end:.2f}s</span></button>')
    original=''.join(f'<p>{html.escape(u["text"])}</p>' for u in manifest['alignment'])
    units={u['unit_id']:(i,u['text']) for i,u in enumerate(manifest['alignment'],1)}
    def describe(issue):
        number,text=units.get(issue.get('unit_id'),('?', ''))
        kind=issue['kind']
        message={'text_mismatch':'原文与自动听写有差异，请对照完整原文试听。',
                 'low_word_confidence':'识别不确定：'+issue.get('text',''),
                 'invalid_word_time':'时间边界异常，请重新定位。',
                 'phrase_contains_unmatched_source':'整句保留原文，但句内有识别差异，请核对：'+issue.get('text',''),
                 'audio_rejected':'此段音频已被试听否决，需要替换后重新审核。',
                 'long_phrase':'该句较长，请确认字幕排版与阅读时间：'+issue.get('text',''),
                 'overlapping_word_times':'字幕时间重叠，请重新定位。'}.get(kind,'需要人工核对。')
        if kind=='unmapped_source':
            a,b=issue['range'];message='尚未定位到时间：'+text[a:b]
        return f'第 {number} 段 · {message}'
    issues=''.join(f'<li>{html.escape(describe(i))}</li>' for i in draft['issues']) or '<li>自动映射未发现缺项；仍需试听确认时间边界。</li>'
    page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>旁白校对 · 李世民</title><style>
:root{--ink:#182d2a;--muted:#61716b;--paper:#f7f5ee;--line:#d6ddd5;--accent:#b15027}*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:"Microsoft YaHei",sans-serif}main{max-width:1100px;margin:auto;padding:48px 30px}
header{display:flex;align-items:end;justify-content:space-between;border-bottom:2px solid var(--ink);padding-bottom:25px;gap:24px}small{letter-spacing:.16em;color:var(--muted)}
h1{font:500 42px "Source Han Serif SC","SimSun",serif;margin:12px 0 0}h2{font:500 24px "SimSun",serif}.badge{color:var(--accent);border:1px solid;padding:8px 14px;white-space:nowrap}
.intro{color:var(--muted);line-height:1.9;max-width:760px}.player{position:sticky;top:0;z-index:1;background:var(--paper);padding:15px 0;border-bottom:1px solid var(--line)}audio{width:100%}
.now{font:500 24px "SimSun",serif;min-height:42px;margin:12px 0}.columns{display:grid;grid-template-columns:1.45fr 1fr;gap:42px;margin-top:20px}
.cue{width:100%;display:grid;grid-template-columns:36px 1fr;gap:7px;text-align:left;color:inherit;background:transparent;border:0;border-bottom:1px solid var(--line);padding:16px 10px;cursor:pointer}
.cue:hover,.cue.active{background:#e1e8df}.cue:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.number{font:13px Consolas,monospace;color:var(--muted);grid-row:span 2;padding-top:3px}
.words{font-size:18px;line-height:1.6}.time{font:12px Consolas,monospace;color:var(--muted)}aside{line-height:1.95}aside p{font-family:"SimSun",serif;font-size:18px}details{border-top:1px solid var(--line);margin:30px 0;padding-top:20px}
summary{cursor:pointer;color:var(--accent)}li{overflow-wrap:anywhere;font:13px/1.8 Consolas,"Microsoft YaHei",sans-serif;margin:12px 0}footer{font-size:13px;color:var(--muted);border-top:1px solid var(--line);padding-top:20px}
@media(max-width:720px){main{padding:24px 18px}h1{font-size:32px}.columns{grid-template-columns:1fr;gap:16px}header{align-items:start}.badge{font-size:12px}}
</style><main><header><div><small>NARRATION / LISTENING REVIEW</small><h1>旁白与字幕，逐句校对</h1></div><span class="badge">待人工审核</span></header>
<p class="intro">点击一句可从对应位置试听。请检查读音、语速和字幕切换是否自然。左侧是按原文短语整理的字幕草稿，右侧保留完整原文；句内识别差异会保留为待核对项，本页不代表审核通过。 DISPOSITION</p>
<section class="player"><audio id="audio" controls preload="metadata" src="review-audio.wav"></audio><div class="now" id="now" aria-live="polite">点击播放，或选择一句字幕</div></section>
<div class="columns"><section><h2>字幕草稿 · CUECOUNT 条</h2>CUEROWS</section><aside><h2>完整原文</h2>SOURCETEXT</aside></div>
<details><summary>自动检查记录 · ISSUECOUNT 项</summary><ul>ISSUES</ul></details>
<footer>本地检查页 · 点击与播放不会记录审核批准。请反馈有问题的时间点；更改字幕后需要重新确认。<br>音频版本 AUDIOHASH</footer></main>
<script>const audio=document.getElementById('audio'),now=document.getElementById('now'),rows=[...document.querySelectorAll('.cue')];
for(const row of rows)row.addEventListener('click',()=>{audio.currentTime=Number(row.dataset.start);audio.play().catch(()=>{})});
audio.addEventListener('timeupdate',()=>{let active=null;for(const row of rows){const yes=audio.currentTime>=Number(row.dataset.start)&&audio.currentTime<Number(row.dataset.end);row.classList.toggle('active',yes);if(yes)active=row}
now.textContent=active?active.querySelector('.words').textContent:'（停顿 / 当前未映射字幕）'});</script></html>'''
    values={'CUECOUNT':str(len(cues)),'CUEROWS':''.join(rows),'SOURCETEXT':original,
        'ISSUECOUNT':str(len(draft['issues'])),'ISSUES':issues,'AUDIOHASH':manifest['master_sha256'][:16],
        'DISPOSITION':'其中有音频已被试听否决，当前仅供问题复查。' if any(i['kind']=='audio_rejected' for i in draft['issues']) else ''}
    page=re.sub('|'.join(values),lambda m:values[m.group()],page)
    path=target/'review.html';path.write_text(page,encoding='utf-8');return path
