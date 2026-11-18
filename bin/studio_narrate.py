"""Episode-bound local narration: canonical words + anchored acting directions."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app import paths
from app.services import studio_workflow as W,studio_voice as V
from app.services.provider_budget import ProviderBudget
from app.services.provenance import fingerprint,file_sha256,atomic_json
from app.services import qwen_voice as Q

def requests(ep_id):
    from app.services.studio_sources import require_usable
    require_usable(ep_id)
    v=W.load(ep_id);doc=W.document(ep_id)
    from app.services.studio_voice_contract import path_for,load
    contract_path=path_for(ep_id)
    if contract_path.exists():
        policy=paths.episode_dir(ep_id)/'studio/playback-policy.json'
        rate=json.loads(policy.read_text('utf-8'))['target_rate'] if policy.exists() else 1.
        c=Q.effective(load(contract_path,source_sha256=doc['source_sha256'],voice=v['voice'],rate=rate))
        if c['version']==2:
            return v,[dict(p,instruction=c['instruction']) for p in c['parts']]
        units={u['unit_id']:u['source_text'] for u in doc['units']};parts=[];seen=[]
        for path in c['request_paths']:
            row=Q.read(path);seen.extend(row['unit_ids'])
            if row['text']!='\n\n'.join(units[u] for u in row['unit_ids']):raise ValueError('locked narration differs from current source')
            parts.append(dict(text=row['text'],instruction=c['instruction'],unit_ids=row['unit_ids']))
        if seen!=list(units):raise ValueError('locked narration unit coverage differs')
        return v,parts
    raise ValueError('本地配音沿用已选系列声音方案；当前作品尚未绑定，不会使用默认音色或旧 HTTP 服务')

from app.services.production_storage import episode_task

def short_parts(parts, limit=90):
    """Split existing locked passages without changing any spoken characters."""
    result=[]
    for part in parts:
        text=part['text']; start=0
        while start<len(text):
            end=min(start+limit,len(text))
            if end<len(text):
                stops=[i+1 for i in range(start,end) if text[i] in '。！？；，\n']
                if stops: end=stops[-1]
            fragment=text[start:end]
            # A measured long-tail failure in this episode requires smaller
            # passages; leave all other cached request identities intact.
            if limit > 45 and any(s in fragment for s in ('主帅病倒乃是军中最大的绝密','唐军首尾脱节，仓促之间无法列阵','一天、两天、十天、三十天','唐军南线庞玉见状士气大振')):
                result.extend(short_parts([dict(part,text=fragment)],limit=45))
            elif limit == 45 and '病榻之前，' in fragment and '\n\n' in fragment:
                boundary=fragment.index('\n\n')+2
                result.extend([dict(part,text=fragment[:boundary]),dict(part,text=fragment[boundary:])])
            else:
                result.append(dict(part,text=fragment))
            start=end
    return result


@episode_task
def render(ep_id,budget_path,max_requests=0,max_characters=0,retry_uncertain=False):
    W.require_production(ep_id,['tts'])
    v,parts=requests(ep_id)
    parts=short_parts(parts)
    expected=W.narration_identity(ep_id)
    adapter=Q.policy()
    processor=file_sha256(ROOT/'app/services/narration_leveling.py')
    speed_processor=file_sha256(ROOT/'app/services/studio_media.py')
    implementation=[file_sha256(ROOT/p) for p in ('app/services/studio_voice.py','app/services/studio_narration_handoff.py',
                    'app/services/studio_voice_contract.py','app/services/voice_consistency.py','bin/studio_narrate.py')]
    root=paths.episode_dir(ep_id)/'studio/narration'/fingerprint([expected,parts,adapter,processor,speed_processor,implementation])[:24]
    budget=ProviderBudget(budget_path,tts_requests=max_requests,characters=max_characters)
    from app.services.studio_voice_contract import path_for,load
    contract_path=path_for(ep_id)
    if contract_path.exists():
        from app.services.locked_voice_provider import LockedVoiceProvider
        c=Q.effective(load(contract_path))
        # Include the immutable contract in the output revision. Never overwrite
        # prior takes when parameters, source or the implementation changes.
        root=root/file_sha256(contract_path)[:16]
        with LockedVoiceProvider(contract_path,root/'worker') as provider:
            result=V.render_act([(p['text'],p['instruction']) for p in parts],root/'master.wav',speaker=c['speaker'],mode=c['mode'],
                model=c['model'],engine_revision=c['engine_revision'],provider=provider,budget=budget,
                cache=paths.state_dir()/'voice-takes',voice_contract=contract_path,retry_uncertain=retry_uncertain)
    else:
        raise ValueError('本地配音缺少所选声音方案')
    from app.services.narration_leveling import repair
    from app.services.narration_master import verify_repair
    from app.services.studio_media import record_speed,speed_plan,apply_speed,read_speed
    source_identity=W.document(ep_id)['source_sha256']
    raw_receipt=result.with_suffix('.speed.json')
    record_speed(result,raw_receipt,applied_rate=1.,source_identity=source_identity,
                 evidence='本入口合并的模型原始输出；尚未进行数字变速')
    policy=paths.episode_dir(ep_id)/'studio/playback-policy.json'
    target_rate=json.loads(policy.read_text('utf-8'))['target_rate'] if policy.exists() else 1.
    speed=speed_plan(result,raw_receipt,target_rate)
    tempo=root/'narration-speed.wav'
    if speed['operation']!='reuse' and tempo.exists():
        speed_receipt=tempo.with_suffix('.speed.json');receipt=read_speed(tempo,speed_receipt)
        if (receipt['applied_rate']!=target_rate or receipt['source_identity']!=source_identity
                or receipt.get('parent',{}).get('receipt_sha256')!=file_sha256(raw_receipt)):
            raise ValueError('缓存速度版本不对应当前输入')
        final_clock_source=tempo
    else:
        final_clock_source,speed_receipt=apply_speed(speed,tempo)
    balanced=root/'narration-balanced.wav'
    if balanced.exists():
        receipt=verify_repair(balanced.with_suffix('.level.json'))
        if receipt['source_sha256']!=file_sha256(final_clock_source):raise ValueError('响度处理版本不对应当前速度录音')
    else:
        chunks=json.loads(result.with_suffix('.chunks.json').read_text('utf-8'))
        repair(final_clock_source,balanced,boundaries=[round(x/speed['factor']) for x in chunks['boundaries']])
    record_speed(balanced,balanced.with_suffix('.speed.json'),applied_rate=target_rate,
                 source_identity=source_identity,evidence='仅响度修复；采样时钟由 level.json 核对',
                 parent=dict(path=str(final_clock_source),receipt=str(speed_receipt),
                             receipt_sha256=file_sha256(speed_receipt),factor=1.))
    if W.narration_identity(ep_id)!=expected:raise ValueError('配音期间文案或声音发生变化；保留录音，禁止自动选用')
    from app.services.voice_consistency import assess
    chunks=json.loads(result.with_suffix('.chunks.json').read_text('utf-8'))
    consistency=root/'voice-consistency.json'
    from app.services.studio_media import wav_clock
    clock=wav_clock(balanced); duration=clock['samples']/clock['sample_rate']
    assess(balanced,Q.reference_files(c),consistency,segments=[
        dict(id=row['request_fingerprint'],start=row['sample_start']/chunks['sample_rate']/target_rate,
             end=min(duration,row['sample_end']/chunks['sample_rate']/target_rate)) for row in chunks['chunks']])
    from app.services.studio_narration_handoff import finish_receipt
    from app.services.production_revision import write_once
    value=dict(design_identity=W.identity(ep_id),voice_design_identity=W.voice_design_identity(ep_id),
                                         source_sha256=W.document(ep_id)['source_sha256'],parts=parts,
                                         raw_master=str(result),master=str(balanced),speed_plan=speed,
                                         voice_consistency_path=str(consistency),
                                         speed_receipt=str(balanced.with_suffix('.speed.json')),
                                         sha256=file_sha256(balanced),approved=False,listening_review='pending')
    write_once(root/'source-map.json',finish_receipt(ep_id,value,contract_path if contract_path.exists() else None))
    return balanced

def main(argv=None):
    p=argparse.ArgumentParser(description='统一 Qwen 本地配音入口；--plan 只检查方案，不启动模型')
    p.add_argument('episode',nargs='?');p.add_argument('--plan',action='store_true')
    p.add_argument('--budget');p.add_argument('--max-requests',type=int,default=0);p.add_argument('--max-characters',type=int,default=0)
    p.add_argument('--retry-uncertain',action='store_true',help='经检查并明确授权后恢复先前未完成的片段')
    a=p.parse_args(argv)
    if not a.episode:p.print_help();return 0
    if a.plan:
        from app.services.studio_voice_contract import path_for,load,bind
        _,parts=requests(a.episode);parts=short_parts(parts);c=Q.effective(load(path_for(a.episode)))
        rows=[bind(V.request(row['text'],**{k:c[k] for k in ('speaker','mode','instruction','model','engine_revision')}),path_for(a.episode)) for row in parts]
        print(json.dumps(dict(speaker=c['speaker'],model=c['model'],instruction=c['instruction'],
            generation=c['generation'],target_rate=c['target_rate'],policy=Q.policy(),
            segments=[dict(characters=len(r['text']),seed=r['seed'],identity=fingerprint(r)) for r in rows],
            model_started=False),ensure_ascii=False,indent=2));return 0
    if not a.budget:p.error('实际配音需要原有的 --budget；只查看使用 --plan')
    print(render(a.episode,a.budget,a.max_requests,a.max_characters,retry_uncertain=a.retry_uncertain))
    return 0
if __name__=='__main__':main()
