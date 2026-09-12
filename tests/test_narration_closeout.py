
import pytest

pytestmark = pytest.mark.production

from pathlib import Path
from app.services import narration_pipeline as N, subtitle_alignment as A, audio_disposition as D
from app.services.provenance import atomic_json,fingerprint,file_sha256
from test_production_v2 import assembly,wav


def test_punctuation_compilation_preserves_source_and_mapping(tmp_path):
    source='孤本——当年李渊……行军3.5里...结束。'
    from app.services.spoken_text import compile_text
    text,transform=compile_text(source)
    assert text=='孤本，当年李渊。行军3.5里。结束。'
    assert len(transform['source_map'])==len(text)
    assert [source[a:b] for a,b in transform['source_map'] if b-a>1]==['——','……','...']
    u={'source_text':source,'approved_spoken_text':source}
    asset={'model':'cosyvoice-v3.5-flash','voice_id':'fixture','version':'v1'}
    request=N.compile_request(u,asset)
    assert request['text']==text and request['source_text']==source
    assert u['source_text']==source
    assert fingerprint(request)!=fingerprint(dict(request,text=source))
    with pytest.raises(ValueError,match='rewrite'):
        N.compile_request(dict(u,approved_spoken_text='改写正文。'),asset)


def recognize_words(words):
    return {'segments':[{'words':[{'word':w,'start':i+.1,'end':i+.9,'probability':.9} for i,w in enumerate(words)]}]}


def test_numeric_spelling_maps_complete_original_without_guessing_time():
    u={'unit_id':'u','text':'他十八岁。','sample_start':0,'sample_end':96000}
    cues,issues=A.align_unit(u,recognize_words(['他','18','岁']),24000)
    assert not issues
    A.validate_cues({'alignment':[u]},cues)
    assert cues[0]['text']=='他十八岁。'
    assert (cues[0]['sample_start'],cues[0]['sample_end'])==(2400,69600)
    _,issues=A.align_unit(u,recognize_words(['他','80','岁']),24000)
    assert any(i['kind']=='text_mismatch' for i in issues)


def test_phrase_span_keeps_internal_source_difference_visible():
    u={'unit_id':'u','text':'李渊起兵。','sample_start':0,'sample_end':96000}
    cues,issues=A.align_unit(u,recognize_words(['李','源','起兵']),24000)
    A.validate_cues({'alignment':[u]},cues)
    assert cues[0]['text']=='李渊起兵。'
    assert cues[0]['alignment_source']=='asr_phrase_span_draft'
    assert any(i['kind']=='phrase_contains_unmatched_source' for i in issues)
    assert any(i['kind']=='text_mismatch' for i in issues)


def test_phrase_breaks_keep_words_and_book_title_intact():
    source='历史还漏下了一本没来得及烧毁的孤本——在随军大帐里写下《大唐创业起居注》。'
    u={'unit_id':'u','text':source,'sample_start':0,'sample_end':len(source)*24000}
    cues,issues=A.align_unit(u,recognize_words(list(source)),24000)
    A.validate_cues({'alignment':[u]},cues)
    assert len(cues)==2
    assert cues[0]['text'].endswith('孤本——')
    assert '《大唐创业起居注》' in cues[1]['text']


def test_rejected_take_cannot_reuse_cache_or_completed_response(tmp_path,monkeypatch):
    monkeypatch.setattr('app.paths.state_dir',lambda:tmp_path/'state')
    manifest,plan,asset,calls,provider,checker=assembly(tmp_path)
    m=N.validate_assembly(manifest);take=m['takes'][0]
    # Both synthetic units have identical PCM, so rejection is content-wide.
    D.reject_take(take,'test-user','extra syllable')
    with pytest.raises(ValueError,match='budget'):
        N.synthesize(plan,asset,max_requests=0,max_characters=0,provider=provider,checker=checker)
    assert len(calls)==2
    with pytest.raises(ValueError,match='rejected'):D.require_usable(m)
    with pytest.raises(ValueError,match='rejected'):
        N.record_listening_review(manifest,'test-user','attempt to approve old bytes','approved')
    # Immutable audio stays inspectable.
    assert N.validate_assembly(manifest)['master_sha256']==m['master_sha256']


def test_rejection_invalidates_resume_identity(tmp_path,tmp_episodes,monkeypatch):
    from app.services.production_identity import input_identity
    monkeypatch.setattr('app.paths.state_dir',lambda:tmp_path/'state')
    before=input_identity('fixture',['tts'])
    atomic_json(tmp_path/'state/audio_rejections/example.json',{'decision':'rejected'})
    assert input_identity('fixture',['tts'])!=before


def test_rejected_audio_cannot_enter_render_or_publication(tmp_path,monkeypatch):
    from app.services import production_v2 as P
    monkeypatch.setattr('app.paths.state_dir',lambda:tmp_path/'state')
    manifest,_,_,_,_,_=assembly(tmp_path)
    D.reject_take(N.read(manifest)['takes'][0],'test-user','extra syllable')
    with pytest.raises(ValueError,match='rejected'):
        P.render(manifest,tmp_path/'missing-timeline',tmp_path/'render')
    receipt=tmp_path/'receipt.json';atomic_json(receipt,{'manifest_path':str(manifest)})
    review=tmp_path/'review.json';atomic_json(review,{})
    with pytest.raises(ValueError,match='rejected'):
        P._publish(receipt,review,review,tmp_path/'publish.json')
    assert not (tmp_path/'publish.json').exists()


def test_local_recheck_of_cached_take_does_not_rewrite_old_quality(tmp_path,monkeypatch):
    from app.config.config import get_settings
    from app.services import asr_check
    monkeypatch.setattr('app.paths.state_dir',lambda:tmp_path/'state')
    manifest,plan,asset,calls,provider,checker=assembly(tmp_path)
    old=N.read(manifest);path=Path(old['takes'][0]['audio_path']).with_name('quality.json')
    old_hash=file_sha256(path)
    monkeypatch.setattr(get_settings().get_profile().asr,'backend','local-whisper')
    def recheck(audio,text):
        return asr_check.QualityResult('failed',file_sha256(audio),fingerprint(text),
                                      findings=[{'kind':'insertion'}])
    monkeypatch.setattr(asr_check,'check',recheck)
    new=N.synthesize(plan,asset,max_requests=0,max_characters=0,provider=provider)
    assert len(calls)==2
    assert N.read(new)['technical_quality']=='pending'
    assert N.read(new)['takes'][0]['quality']['status']=='failed'
    assert file_sha256(path)==old_hash
