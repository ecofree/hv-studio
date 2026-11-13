import io
import wave
import pytest
from app.services import studio_voice as V
from app.services.provider_budget import ProviderBudget,BudgetExceeded

pytestmark=pytest.mark.production

def sound():
    b=io.BytesIO()
    with wave.open(b,'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(b'\x01\x00'*24000)
    return b.getvalue()

def req(text='第一句。',instruction='平静'):
    return V.request(text,speaker='same-speaker',mode='custom',instruction=instruction,model='fixture',engine_revision='fixture-1')

def test_matching_cache_zero_calls_but_text_and_direction_change_identity(tmp_path):
    calls=[]
    def provider(*a,**k):calls.append((a,k));return sound()
    budget=ProviderBudget(tmp_path/'budget.json',tts_requests=3,characters=100)
    first,_=V.render_take(req(),tmp_path/'takes',provider,budget=budget)
    again,_=V.render_take(req(),tmp_path/'takes',provider)
    other,_=V.render_take(req('改过的正文。'),tmp_path/'takes',provider,budget=budget)
    emotional,_=V.render_take(req(instruction='紧张但克制'),tmp_path/'takes',provider,budget=budget)
    assert first==again and len({first,other,emotional})==3 and len(calls)==3
    assert budget.snapshot()['used']['tts_requests']==3

def test_no_budget_cannot_call_and_timeout_is_not_silently_retried(tmp_path):
    calls=[]
    def provider(*a,**k):calls.append(1);raise TimeoutError('unknown')
    with pytest.raises(ValueError,match='budget'):V.render_take(req(),tmp_path/'takes',provider)
    b=ProviderBudget(tmp_path/'budget.json',tts_requests=2,characters=100)
    with pytest.raises(TimeoutError):V.render_take(req(),tmp_path/'takes',provider,budget=b)
    with pytest.raises(ValueError,match='uncertain'):V.render_take(req(),tmp_path/'takes',provider,budget=b)
    assert len(calls)==1 and b.snapshot()['used']['tts_requests']==1
    with pytest.raises(TimeoutError):V.render_take(req(),tmp_path/'takes',provider,budget=b,retry_uncertain=True)
    assert len(calls)==2 and b.snapshot()['used']['tts_requests']==2

def test_changed_old_output_refused_before_spend(tmp_path):
    out=tmp_path/'old.wav';out.write_bytes(sound());calls=[]
    def provider(*a,**k):calls.append(1);return sound()
    b=ProviderBudget(tmp_path/'budget.json',tts_requests=1,characters=100)
    with pytest.raises(ValueError,match='receipt'):
        V.render_act([('正文','自然')],out,speaker='same',mode='custom',model='fixture',engine_revision='1',provider=provider,budget=b)
    assert not calls and b.snapshot()['used']['tts_requests']==0

def test_direct_adapter_dispatch_is_denied():
    with pytest.raises(ValueError,match='budgeted'):V.require_dispatch('x','same','custom','',None)

def test_tampered_wav_is_not_used(tmp_path):
    b=ProviderBudget(tmp_path/'budget.json',tts_requests=1,characters=100)
    path,_=V.render_take(req(),tmp_path/'takes',lambda *a,**k:sound(),budget=b)
    path.write_bytes(sound()+b'corrupt')
    with pytest.raises(ValueError,match='bytes changed'):V.render_take(req(),tmp_path/'takes',lambda *a,**k:None)
