"""Local speech-boundary corrections, never content approvals or new narration."""
import copy
import math
from pathlib import Path

from app.services.narration_pipeline import read
from app.services.provenance import atomic_json, file_sha256, fingerprint


def range_identity(ep_id, source_sha, master_sha):
    from app import paths
    return range_fingerprints(paths.episode_dir(ep_id)/'production/v2/assemblies', source_sha, master_sha)


def range_fingerprints(destination, source_sha, master_sha):
    key = fingerprint([source_sha, master_sha])
    path = Path(destination)/'alignment-ranges'/(key+'.json')
    return {path.name: file_sha256(path)} if path.is_file() else {}


def validate_range_inputs(manifest):
    recording = manifest.get('source_recording') or {}
    directory = Path(manifest['master_path']).parent.parent
    if not recording.get('delivered_sha256'):
        if (directory/'alignment-ranges').is_dir():
            raise ValueError('局部语音定位已变化或缺少绑定的录音版本')
        return
    saved = manifest.get('alignment_range_inputs', {})
    if not saved and not (directory/'alignment-ranges').is_dir():
        return
    source_sha = read(manifest['plan_path'])['source_sha256']
    key = fingerprint([source_sha, recording['delivered_sha256']])+'.json'
    previous = {key: saved[key]} if key in saved else {}
    if previous != range_fingerprints(directory, source_sha, recording['delivered_sha256']):
        raise ValueError('局部语音定位已变化，请继续本地旁白检查以更新切片和字幕')


def prepare(plan, recording, rows, destination, *, with_inputs=False):
    from app.services.process_lock import file_lock
    destination = Path(destination)
    with file_lock((destination/'alignment-recovery.json').with_suffix('.lock')):
        result = _prepare(plan, recording, rows, destination)
        inputs = range_fingerprints(destination, plan['source_sha256'], recording['delivered_sha256'])
        return (result, inputs) if with_inputs else result


def _prepare(plan, recording, rows, destination):
    destination=Path(destination)
    key=fingerprint([plan['source_sha256'],recording['delivered_sha256']])
    ranges_path=destination/'alignment-ranges'/(key+'.json')
    result=copy.deepcopy(rows)
    correction=read(ranges_path) if ranges_path.is_file() else {}
    if correction and (correction.get('key')!=key or correction.get('source_sha256')!=plan['source_sha256']
                       or correction.get('master_sha256')!=recording['delivered_sha256']):
        raise ValueError('局部定位对应其他正文或录音')
    edits=correction.get('ranges',{})
    if set(edits)-{r['frame'] for r in result}:raise ValueError('局部定位包含未知旁白段')
    for row in result:
        if row['frame'] in edits:
            start,end=edits[row['frame']]
            if not all(type(v) in (int,float) and math.isfinite(v) for v in (start,end)) or not 0<=start<end<=recording['duration_s']:
                raise ValueError('局部定位超出母带')
            row.update(start=start,end=end,coverage=0.,manual_location=True)
    units={u['unit_id']:u for u in plan['units']}
    report_rows=[]
    for i,row in enumerate(result):
        previous=next((r['end'] for r in reversed(result[:i]) if r.get('end') is not None),0.)
        following=next((r['start'] for r in result[i+1:] if r.get('start') is not None),recording['duration_s'])
        overlap=(row.get('start') is not None and (row['start']<previous or row['end']>following))
        report_rows.append(dict(unit_id=row['frame'],text=units[row['frame']]['display_text'],
            start=row.get('start'),end=row.get('end'),window_start=previous,window_end=following,
            needs_location=row.get('start') is None or overlap,manual_location=bool(row.get('manual_location'))))
    report=dict(schema_version=1,key=key,source_sha256=plan['source_sha256'],
        master_path=recording['delivered_path'],master_sha256=recording['delivered_sha256'],
        duration_seconds=recording['duration_s'],ranges_path=str(ranges_path.resolve()),rows=report_rows,
        ranges_sha256=file_sha256(ranges_path) if ranges_path.is_file() else None,
        approved=False,interpretation='定位只提供声学窗口，仍须本地强制对齐和质量检查')
    atomic_json(destination/'alignment-recovery.json',report)
    return result


def save_range(report_path, unit_id, start, end, *, expected_sha256):
    """Record an operator-selected audible range; retain previous selections."""
    from app.services.process_lock import file_lock
    report_path=Path(report_path)
    with file_lock(report_path.with_suffix('.lock')):
        if file_sha256(report_path)!=expected_sha256:raise ValueError('定位信息已更新，请重新载入')
        report=read(report_path)
        if file_sha256(report['master_path'])!=report['master_sha256']:raise ValueError('录音已变化，请重新准备定位')
        rows=report['rows']; row=next((r for r in rows if r['unit_id']==unit_id),None)
        if row is None:raise ValueError('未知旁白段')
        if not all(type(v) in (int,float) and math.isfinite(v) for v in (start,end)) or not 0<=start<end<=report['duration_seconds']:
            raise ValueError('起止位置须在母带内，且终点晚于起点')
        target=report_path.parent/'alignment-ranges'/(report['key']+'.json')
        if Path(report['ranges_path']).resolve()!=target.resolve():raise ValueError('定位路径不符')
        current_hash = file_sha256(target) if target.is_file() else None
        if current_hash != report.get('ranges_sha256'):
            raise ValueError('定位信息已更新，请重新准备当前录音定位')
        value=read(target) if target.exists() else dict(key=report['key'],source_sha256=report['source_sha256'],
            master_sha256=report['master_sha256'],ranges={})
        # Check the latest saved adjacent corrections too, before accepting a new range.
        index=next(i for i,r in enumerate(rows) if r['unit_id']==unit_id)
        before=[value['ranges'].get(r['unit_id'],[r['start'],r['end']])[1] for r in rows[:index]]
        after=[value['ranges'].get(r['unit_id'],[r['start'],r['end']])[0] for r in rows[index+1:]]
        if any(x is not None and start<x for x in before) or any(x is not None and end>x for x in after):
            raise ValueError('区间与邻段重叠，请先核对边界')
        if target.exists():atomic_json(target.parent/'history'/(file_sha256(target)+'.json'),value)
        value['ranges'][unit_id]=[start,end]
        atomic_json(target,value)
        row.update(start=start, end=end, manual_location=True, needs_location=False)
        for i, item in enumerate(rows):
            item['window_start'] = next((r['end'] for r in reversed(rows[:i]) if r['end'] is not None), 0.)
            item['window_end'] = next((r['start'] for r in rows[i+1:] if r['start'] is not None), report['duration_seconds'])
            item['needs_location'] = (item['start'] is None or item['start'] < item['window_start']
                                      or item['end'] > item['window_end'])
        report['ranges_sha256'] = file_sha256(target)
        atomic_json(report_path, report)
    return target
