"""Episode-bound local narration: canonical words + anchored acting directions."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'resource/风趣历史.writing-DNA/scripts'))
from app import paths
from app.services import studio_workflow as W,studio_voice as V
from app.services.provider_budget import ProviderBudget
from app.services.provenance import fingerprint,file_sha256,atomic_json
from generate_qwen3_local_audio import split_text_into_chunks,call_tts_api

def requests(ep_id):
    from app.services.studio_sources import require_usable
    require_usable(ep_id)
    v=W.load(ep_id);doc=W.document(ep_id)
    from app.services.studio_voice_contract import path_for,load
    contract_path=path_for(ep_id)
    if contract_path.exists():
        policy=paths.episode_dir(ep_id)/'studio/playback-policy.json'
        rate=json.loads(policy.read_text('utf-8'))['target_rate'] if policy.exists() else 1.
        c=load(contract_path,source_sha256=doc['source_sha256'],voice=v['voice'],rate=rate)
        if c['version']==2:
            return v,[dict(p,instruction=c['instruction']) for p in c['parts']]
        units={u['unit_id']:u['source_text'] for u in doc['units']};parts=[];seen=[]
        for path in c['request_paths']:
            row=json.loads(Path(path).read_text('utf-8'));seen.extend(row['unit_ids'])
            if row['text']!='\n\n'.join(units[u] for u in row['unit_ids']):raise ValueError('locked narration differs from current source')
            parts.append(dict(text=row['text'],instruction=c['instruction'],unit_ids=row['unit_ids']))
        if seen!=list(units):raise ValueError('locked narration unit coverage differs')
        return v,parts
    from app.services.studio_workspace import config
    if config(paths.ROOT) and paths.episode_dir(ep_id).resolve().is_relative_to((paths.ROOT/'workspace').resolve()):
        raise ValueError('新项目配音必须先绑定已确认的系列声音方案；不会退回未锁定的声音路径')
    if v['voice']['model']!='Qwen3-TTS-12Hz-1.7B-CustomVoice':
        raise ValueError('此本地入口只支持已配置的 Qwen3 CustomVoice；不会替换模型或音色')
    parts=[]
    for u in doc['units']:
        cursor=0
        for text in split_text_into_chunks(u['source_text'],max_chars=160):
            start=u['source_text'].find(text,cursor)
            if start<0:raise ValueError('切分改变正文，拒绝配音')
            end=start+len(text);cursor=end
            directions=[b['performance'] for b in v['beats'] if b['unit_id']==u['unit_id'] and start<=b['text_start']<end and b.get('performance')]
            instruction='。'.join([v['voice']['delivery']]+directions)
            parts.append(dict(text=text,instruction=instruction,unit_id=u['unit_id'],text_start=start,text_end=end))
    return v,parts

def render(ep_id,budget_path,max_requests=0,max_characters=0):
    W.require_production(ep_id,['tts'])
    v,parts=requests(ep_id)
    expected=W.narration_identity(ep_id)
    adapter=file_sha256(ROOT/'resource/风趣历史.writing-DNA/scripts/generate_qwen3_local_audio.py')
    processor=file_sha256(ROOT/'app/services/narration_leveling.py')
    speed_processor=file_sha256(ROOT/'app/services/studio_media.py')
    implementation=[file_sha256(ROOT/p) for p in ('app/services/studio_voice.py','app/services/studio_narration_handoff.py',
                    'app/services/studio_voice_contract.py','bin/studio_narrate.py')]
    root=paths.episode_dir(ep_id)/'studio/narration'/fingerprint([expected,parts,adapter,processor,speed_processor,implementation])[:24]
    budget=ProviderBudget(budget_path,tts_requests=max_requests,characters=max_characters)
    from app.services.studio_voice_contract import path_for,load
    contract_path=path_for(ep_id)
    if contract_path.exists():
        from app.services.locked_voice_provider import LockedVoiceProvider
        c=load(contract_path)
        # Include the immutable contract in the output revision. Never overwrite
        # prior takes when parameters, source or the implementation changes.
        root=root/file_sha256(contract_path)[:16]
        with LockedVoiceProvider(contract_path,root/'worker') as provider:
            result=V.render_act([(p['text'],p['instruction']) for p in parts],root/'master.wav',speaker=c['speaker'],mode=c['mode'],
                model=c['model'],engine_revision=c['engine_revision'],provider=provider,budget=budget,
                cache=paths.state_dir()/'voice-takes',voice_contract=contract_path)
    else:
        result=V.render_act([(p['text'],p['instruction']) for p in parts],root/'master.wav',speaker=v['voice']['identity'],mode='custom',
            model=v['voice']['model'],engine_revision=adapter,
            provider=call_tts_api,budget=budget,cache=paths.state_dir()/'voice-takes')
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
    from app.services.studio_narration_handoff import finish_receipt
    from app.services.production_revision import write_once
    value=dict(design_identity=W.identity(ep_id),voice_design_identity=W.voice_design_identity(ep_id),
                                         source_sha256=W.document(ep_id)['source_sha256'],parts=parts,
                                         raw_master=str(result),master=str(balanced),speed_plan=speed,
                                         speed_receipt=str(balanced.with_suffix('.speed.json')),
                                         sha256=file_sha256(balanced),approved=False,listening_review='pending')
    write_once(root/'source-map.json',finish_receipt(ep_id,value,contract_path if contract_path.exists() else None))
    return balanced

def main():
    p=argparse.ArgumentParser(description='正文与逐句表演标注进入同一配音请求；默认零调用')
    p.add_argument('episode');p.add_argument('--budget',required=True);p.add_argument('--max-requests',type=int,default=0);p.add_argument('--max-characters',type=int,default=0)
    a=p.parse_args();print(render(a.episode,a.budget,a.max_requests,a.max_characters))
if __name__=='__main__':main()
