"""Unsupported acting directions must fail before any model or network call."""
import importlib.util
import json
from pathlib import Path
import pytest

pytestmark=pytest.mark.production


@pytest.fixture
def voice_script():
    path=Path(__file__).resolve().parents[1]/'resource/风趣历史.writing-DNA/scripts/generate_qwen3_local_audio.py'
    spec=importlib.util.spec_from_file_location('local_voice_direction_fixture',path)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def test_clone_rejects_ineffective_instruction_before_network(voice_script,monkeypatch,tmp_path):
    with pytest.raises(ValueError,match='clone'):
        voice_script.call_tts_api('旁白',instruct='紧张',mode='clone')
    with pytest.raises(ValueError,match='clone'):
        voice_script.render_act('一','旁白',str(tmp_path/'out.wav'),instruct='紧张')


def test_retired_clone_entry_refuses_before_spend_or_voice_substitution(voice_script,monkeypatch,tmp_path):
    import io,wave
    from app.services import studio_voice as V
    from app.services.provider_budget import ProviderBudget
    data=io.BytesIO()
    with wave.open(data,'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(24000);w.writeframes(b'\0\0'*24000)
    ref=tmp_path/'same-reference.wav';ref.write_bytes(data.getvalue())
    req=V.request('旁白',speaker='clone_kai',mode='clone',instruction=None,reference=ref,model='fixture',engine_revision='1')
    budget=ProviderBudget(tmp_path/'budget.json',tts_requests=1,characters=20)
    with pytest.raises(ValueError,match='统一入口'):
        V.render_take(req,tmp_path/'takes',voice_script.call_tts_api,budget=budget)
    assert budget.snapshot()['used']['tts_requests']==0
    assert req['speaker']=='clone_kai' and req['mode']=='clone'
