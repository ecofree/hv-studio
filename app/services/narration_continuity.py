"""Verify continuous narration lineage and exact tail-repair sample accounting."""
import copy
from pathlib import Path
import wave

from app.services.provenance import file_sha256


def audio_binding(path):
    path=Path(path).resolve()
    with wave.open(str(path),'rb') as w:
        return dict(path=str(path),sha256=file_sha256(path),samples=w.getnframes(),
                    sample_rate=w.getframerate(),channels=w.getnchannels(),sample_width=w.getsampwidth())


def _pcm(path, sha, sr):
    if file_sha256(path)!=sha: raise ValueError('continuous audio hash mismatch')
    with wave.open(str(path),'rb') as w:
        if w.getframerate()!=sr or w.getnchannels()!=1 or w.getsampwidth()!=2:
            raise ValueError('continuous audio clock/format mismatch')
        return w.readframes(w.getnframes())


def corrected_accounting(original, joins):
    """Read-only reconstruction: compare every intended sample to existing output."""
    r=copy.deepcopy(original);sr=r['sample_rate']
    if len(joins)!=len(r['chunks'])-1 or any(type(x)!=int or x<0 for x in joins):
        raise ValueError('invalid chunk joins')
    original_pcm=_pcm(r['source_raw_path'],r['source_raw_sha256'],sr)
    output_pcm=_pcm(r['output_path'],r['output_sha256'],sr)
    original_cursor=output_cursor=0
    removed=inserted=0
    for i,row in enumerate(r['chunks']):
        pcm=_pcm(row['raw_path'],row['raw_sha256'],sr)
        if len(pcm)//2!=row['samples_before']:raise ValueError('chunk source sample count mismatch')
        repair=row.get('repair')
        row['samples_after_silence_trim']=row.get('samples_after_silence_trim',row['samples_after'])
        if repair:
            patch=_pcm(repair['repair_raw_path'],repair['repair_raw_sha256'],sr)
            a,b=repair['repair_speech_start_sample'],repair['repair_speech_end_sample']
            keep=repair['original_speech_end_sample']
            if not 0<=a<b<=len(patch)//2 or not 0<=keep<=len(pcm)//2:
                raise ValueError('invalid repair splice bounds')
            addition=b'\0\0'*round(sr*.35)+patch[a*2:b*2]
        else:
            keep=row['samples_after_silence_trim'];addition=b''
            if not 0<=keep<=len(pcm)//2:raise ValueError('invalid retained source length')
        final=pcm[:keep*2]+addition
        if original_pcm[original_cursor:original_cursor+len(pcm)]!=pcm:
            raise ValueError('source chunks do not reconstruct original master')
        if output_pcm[output_cursor:output_cursor+len(final)]!=final:
            raise ValueError('repair chunks do not reconstruct output master')
        original_cursor+=len(pcm);output_cursor+=len(final)
        row.update(retained_source_samples=keep,removed_source_samples=row['samples_before']-keep,
                   inserted_samples=len(addition)//2,samples_after=len(final)//2,
                   removed_seconds=(row['samples_before']-keep)/sr,
                   inserted_seconds=len(addition)/2/sr,
                   net_removed_seconds=(len(pcm)-len(final))/2/sr)
        removed+=row['removed_source_samples'];inserted+=row['inserted_samples']
        if i<len(joins):
            silence=b'\0\0'*joins[i]
            if (original_pcm[original_cursor:original_cursor+len(silence)]!=silence or
                output_pcm[output_cursor:output_cursor+len(silence)]!=silence):
                raise ValueError('join silence differs from master')
            original_cursor+=len(silence);output_cursor+=len(silence)
    if original_cursor!=len(original_pcm) or output_cursor!=len(output_pcm):
        raise ValueError('repair accounting does not cover entire master')
    if original_cursor//2!=r['samples_before'] or output_cursor//2!=r['samples_after']:
        raise ValueError('repair master sample count mismatch')
    r.update(accounting_version=2,join_samples=joins,
             removed_source_samples=removed,inserted_samples=inserted,
             net_removed_samples=removed-inserted,
             removed_source_seconds=removed/sr,inserted_seconds=inserted/sr,
             net_removed_seconds=(removed-inserted)/sr,
             reconstruction='exact_pcm_verified_no_media_written')
    return r


def validate_continuous(manifest):
    """Shared assembly gate, including older continuous manifests with bad hashes."""
    c=manifest.get('continuous_tts')
    if not c:return
    raw=audio_binding(c['raw_path'])
    if raw['sha256']!=c['raw_sha256']:raise ValueError('continuous raw audio hash mismatch')
    if c.get('lineage_version')==1:
        from app.services.narration_pipeline import read
        for kind in ['original_raw','repaired_raw']:
            if audio_binding(c[kind]['path'])!=c[kind]:raise ValueError('continuous '+kind+' binding mismatch')
        if raw!=c['repaired_raw']:raise ValueError('continuous selected raw differs from repaired raw')
        if file_sha256(c['repair_receipt_path'])!=c['repair_receipt_sha256']:
            raise ValueError('continuous repair receipt changed')
        receipt=read(c['repair_receipt_path'])
        if (receipt['source_raw_path']!=c['original_raw']['path'] or
            receipt['source_raw_sha256']!=c['original_raw']['sha256'] or
            receipt['output_path']!=raw['path'] or receipt['output_sha256']!=raw['sha256']):
            raise ValueError('continuous repair lineage mismatch')
        if corrected_accounting(receipt,receipt['join_samples'])!=receipt:
            raise ValueError('continuous repair sample accounting mismatch')
