"""Place authored graphic states on observed speech and omit fully covered image jobs."""
from collections import defaultdict

from app.services import studio_graphics as G
from app.services.existing_images import ExistingImage
from app.services.provenance import file_sha256
from app.services.spec import VideoFormat


class GraphicPlate(ExistingImage):
    """A local book-keeping plate for a storyboard key fully replaced by graphics."""


def plate(eid, digest):
    path = G.media_path(eid,digest,'graphics-plates')
    if not path.is_file() or file_sha256(path)!=digest:
        raise ValueError('本地图文画面缺失或已改变，请在制作页重新保存该图文')
    return path


def _scene_ranges(ep, document, scene, directing):
    starts,total = G.offsets(document)
    units = document['units']
    if 'scenes' in scene:
        from app.services.production_timeline import scene_bindings
        manifest = {'alignment':[dict(text=u['source_text']) for u in units]}
        ranges = [(s['image'],s['source_start'],s['source_end']) for s in scene_bindings(ep,manifest,scene)]
    else:
        rows = scene.get('unit_scenes')
        if rows is None:
            rows = [dict(unit_id=u['unit_id'],image=f'unit-{i:03}') for i,u in enumerate(units)]
        ranges = [(s['image'],starts[s['unit_id']],b) for s,b in zip(rows,[*list(starts.values())[1:],total])]
    base_keys = {key for key,_,_ in ranges}
    events = directing.get('visual_events',[])
    if events:
        if directing.get('source_identity')!=G.source_key(document):
            raise ValueError('导演节点属于旧版旁白')
        by_id = {u['unit_id']:u['source_text'] for u in units}
        bounds = []
        for event in events:
            anchor = event['at']
            if anchor=={'at':'start'}:
                bounds.append(0)
            else:
                uid = anchor['unit_id']
                bounds.append(starts[uid]+sum(c.isalnum() for c in by_id[uid][:anchor['text_start']]))
        if bounds[0]!=0 or bounds!=sorted(set(bounds)) or bounds[-1]>=total:
            raise ValueError('导演镜头须从片头开始，并按旁白顺序推进')
        ranges = [(e['image_id'],a,b) for e,a,b in zip(events,bounds,bounds[1:]+[total])]
    return ranges,base_keys


def local_jobs(ep, document, scene, directing):
    plan,digest = G.current(ep.id,document)
    if not plan:
        return {}
    entries = plan['graphics']
    spans = G.intervals(entries,document)
    ranges = defaultdict(list)
    visible,base_keys = _scene_ranges(ep,document,scene,directing)
    for key,start,end in visible:
        ranges[key].append((start,end))
    jobs = {}
    fmt = VideoFormat(getattr(ep,'video_format',VideoFormat.PORTRAIT))
    verified = {sha:plate(ep.id,sha) for sha in {
        sha for entry in entries.values() for sha in entry['plates'][fmt.value]}}
    for key in sorted(base_keys | ranges.keys()):
        uses = ranges.get(key,[])
        # Directed shots replace the whole base slate. An unused base key only
        # needs a local stand-in for the intermediate scene-binding pass.
        covered = [] if uses else [spans[0][2]]
        for a,b in uses:
            cursor = a
            for start,stop,gid,_,markers in spans:
                # An explicit word can follow opening silence or punctuation that
                # shares a normalized offset. Keep the underlying art at that edge.
                if (markers[0] and start==a) or (markers[-1] and stop==b):
                    continue
                if start<=cursor<stop:
                    covered.append(gid)
                    cursor = min(b,stop)
                    if cursor==b:
                        break
            if cursor!=b:
                break
        else:
            if covered:
                sha = entries[covered[0]]['plates'][fmt.value][0]
                path = verified[sha]
                jobs[key] = GraphicPlate(key,dict(path=str(path),sha256=sha),
                                         G.path_for(ep.id),{'graphics_sha256':digest})
    return jobs


def check_current(eid, timeline):
    path = G.path_for(eid)
    recorded = timeline.get('inputs',{}).get(str(path.resolve()))
    plan,revision=G.snapshot(eid)
    if revision!=recorded:
        raise ValueError('本集图文已变化，请重新建立时间轴')
    if plan: G.check_sources(eid,plan['graphics'])


def apply(eid, manifest, evidence, timeline):
    from app.services.directing import anchor_sample
    from app.services.production_timeline import acoustic_offsets
    from app.services.production_v2 import frame_boundary
    document = {'units':[dict(unit_id=u['unit_id'],source_text=u['text']) for u in manifest['alignment']]}
    plan,digest = G.current(eid,document)
    if not plan:
        return
    sidecar = str(G.path_for(eid).resolve())
    if timeline['inputs'].get(sidecar)!=digest:
        raise ValueError('图文在建立时间轴时更新，请重试')
    fmt = VideoFormat.from_record(timeline)
    anchors = acoustic_offsets(manifest,evidence)
    spans = []
    for start,stop,gid,beats,markers in G.intervals(plan['graphics'],document):
        s = plan['graphics'][gid]
        times = []
        for offset,marker in zip(beats+[stop],markers):
            try:
                times.append(anchor_sample(marker,manifest,evidence) if marker else anchors[offset])
            except (KeyError,ValueError) as exc:
                raise ValueError(f'「{s["title"]}」的切换原句须从实测词首开始，请调整原句') from exc
        if times!=sorted(set(times)) or (spans and times[0]<spans[-1][1]):
            raise ValueError('图文在录音中的切点重叠，请调整原句')
        if s.get('timing')=='span':
            start,end=times
            pages=len(s['plates'][fmt.value])
            times=[start+(end-start)*i//pages for i in range(pages)]+[end]
            frames=[frame_boundary(t,manifest['sample_rate']) for t in times]
            if len(set(frames))!=len(frames):
                raise ValueError('所选展示时段不足以容纳每一页，请延长展示时段或拆表')
        for i,(a,b) in enumerate(zip(times,times[1:])):
            sha = s['plates'][fmt.value][i]
            p = str(plate(eid,sha))
            timeline['inputs'][p] = sha
            spans.append((a,b,dict(image_id='graphic-'+gid+'-'+str(i+1),
                image_path=p, graphic=dict(id=gid,step=i+1,title=s['title']),
                motion=dict(type='static',start_zoom=1.,end_zoom=1.,start_xy=[.5,.5],end_xy=[.5,.5],
                            geometry_version='fit_cover_v2'))))
    shots = timeline['shots']
    cuts = sorted({v for s in shots for v in (s['sample_start'],s['sample_end'])}
                  | {v for a,b,_ in spans for v in (a,b)})
    result = []
    scene = graphic = 0
    for a,b in zip(cuts,cuts[1:]):
        while shots[scene]['sample_end']<=a:
            scene += 1
        while graphic<len(spans) and spans[graphic][1]<=a:
            graphic += 1
        original = shots[scene]
        showing = graphic<len(spans) and spans[graphic][0]<=a
        if showing:
            source = spans[graphic][2]
            if result and result[-1].get('graphic')==source['graphic']:
                result[-1]['sample_end'] = b
                continue
        else:
            source = original
        row = dict(source,sample_start=a,sample_end=b,id=f'visual-{len(result):04}')
        if not showing and (a,b)!=(source['sample_start'],source['sample_end']):
            row['camera_samples'] = [source['sample_start'],source['sample_end']]
        result.append(row)
    rate = manifest['sample_rate']
    for shot in result:
        if frame_boundary(shot['sample_end'],rate)<=frame_boundary(shot['sample_start'],rate):
            raise ValueError('图文与相邻切点不足一帧，请调整图文起止原句')
    timeline['shots'] = result
