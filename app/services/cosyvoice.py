"""Budgeted DashScope synthesis for the current narration pipeline.

Voices come from registered assets. Retired V-API, cloud Qwen, automatic voice
creation and standalone synthesis entrypoints have been removed.
"""
from __future__ import annotations
import os
import json
import uuid
import hashlib
import importlib.metadata
import subprocess
import time
from pathlib import Path
from typing import Optional
from app.services.provenance import atomic_json, file_sha256, fingerprint
from app.config.config import ROOT, env_get

DASHSCOPE_ENV_KEY = "DASHSCOPE_API_KEY"
DASHSCOPE_ENV_WS = "DASHSCOPE_WORKSPACE"
DEFAULT_COSYVOICE_MODEL = "cosyvoice-v3.5-plus"
_BJ_WS_WS_TMPL = "wss://{ws}.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"


def _resolve_synth_ws_url() -> str | None:
    """根据 .env 的 workspace 决定语音合成 WebSocket URL（北京专属）。无则 None。"""
    ws = _env_get(DASHSCOPE_ENV_WS)
    if ws:
        ws = ws.strip().rstrip("/")
        if "cn-beijing.maas.aliyuncs.com" in ws:
            # 给了完整 http(s) 主机 → 转 wss
            host = ws.replace("https://", "").replace("http://", "").split("/")[0]
            return f"wss://{host}/api-ws/v1/inference"
        if "." in ws and ("maas" in ws or "aliyuncs" in ws):
            host = ws.replace("https://", "").replace("http://", "").split("/")[0]
            return f"wss://{host}/api-ws/v1/inference"
        return _BJ_WS_WS_TMPL.format(ws=ws)
    return None


class DashScopeCosyVoiceSynthesizer:
    """
    阿里云百炼 CosyVoice v3.5 整段合成器（web 同步通道，用 dashscope SDK）。
    由当前旁白管线传入注册音色和持久预算，响应先落盘再解码。
    """

    def __init__(self, voice: str | None = None, model: str | None = None,
                 api_key: str | None = None, response_format: str = "mp3",
                 sample_rate: int = 24000, timeout_s: int = 120,
                 pitch_rate: float | None = None,
                 speech_rate: float | None = None,
                 volume: int | None = None,
                 instruction: str | None = None,
                 enable_ssml: bool = False, hot_fix: dict | None = None, seed: int = 0):
        """
        response_format: "mp3" 或 "wav"（决定输出封装）。
        sample_rate: CosyVoice 原生 24000。管线后期统一转目标采样率即可。

        音色塑形参数（dashscope SpeechSynthesizer 原生支持，可选）：
          pitch_rate  : 音调倍率，默认 1.0。<1 压低音调 → 声音更沉、更浑厚；
                        >1 抬高音调 → 更亮更薄。改善"声音薄"首选调这个。
          speech_rate : 语速倍率，默认 1.0。<1 放慢（更稳重）。
          volume      : 音量 0–100，默认 50。
          instruction : 自然语言风格指令，如"用低沉浑厚、胸腔共鸣的声音朗读"。
                        与 voice_prompt(声音设计) 不同，它是每次合成时生效的风格引导。
        """
        import os as _os
        self.api_key = api_key or _dashscope_key()
        # 注入环境变量，供 dashscope SDK 使用
        _os.environ.setdefault(DASHSCOPE_ENV_KEY, self.api_key)
        self.model = model or _cosyvoice_model_from_env()
        self.voice = voice
        self.sample_rate = sample_rate
        self.response_format = (response_format or "mp3").lower()
        self.timeout_s = timeout_s
        # 音色塑形（None 表示交给服务端默认）
        self.pitch_rate = pitch_rate
        self.speech_rate = speech_rate
        self.volume = volume
        self.instruction = instruction
        self.enable_ssml = bool(enable_ssml)
        self.hot_fix = hot_fix
        self.seed = seed
        # workspace 配置（北京专属域名合成必需）
        self.workspace = _env_get(DASHSCOPE_ENV_WS) or None

    # ------------------------------------------------------------------
    def _format_enum(self):
        """根据 response_format + sample_rate 映射到 AudioFormat 枚举。"""
        from dashscope.audio.tts_v2 import AudioFormat
        sr = int(self.sample_rate or 24000)
        want_wav = self.response_format == "wav"
        # 选最接近的官方档位：mp3→MP3_xxx；wav→WAV_xxx
        table = {
            (True, 8000): AudioFormat.MP3_8000HZ_MONO_128KBPS,
            (True, 16000): AudioFormat.MP3_16000HZ_MONO_128KBPS,
            (True, 22050): AudioFormat.MP3_22050HZ_MONO_256KBPS,
            (True, 24000): AudioFormat.MP3_24000HZ_MONO_256KBPS,
            (True, 44100): AudioFormat.MP3_44100HZ_MONO_256KBPS,
            (True, 48000): AudioFormat.MP3_48000HZ_MONO_256KBPS,
            (False, 8000): AudioFormat.WAV_8000HZ_MONO_16BIT,
            (False, 16000): AudioFormat.WAV_16000HZ_MONO_16BIT,
            (False, 22050): AudioFormat.WAV_22050HZ_MONO_16BIT,
            (False, 24000): AudioFormat.WAV_24000HZ_MONO_16BIT,
            (False, 44100): AudioFormat.WAV_44100HZ_MONO_16BIT,
            (False, 48000): AudioFormat.WAV_48000HZ_MONO_16BIT,
        }
        return table.get((not want_wav, sr), AudioFormat.DEFAULT)

    # ------------------------------------------------------------------
    def _build_synthesizer(self, voice: str):
        """惰性构造 dashscope SpeechSynthesizer（每段一次，避免共享并发问题）。"""
        import dashscope
        from dashscope.audio.tts_v2 import SpeechSynthesizer

        dashscope.api_key = self.api_key
        # 北京专属域名（workspace）合成需指向专属 WebSocket URL
        ws_url = _resolve_synth_ws_url()
        if ws_url:
            dashscope.base_websocket_api_url = ws_url
        params: dict = {
            "model": self.model,
            "voice": voice,
            "format": self._format_enum(),
        }
        if self.workspace:
            # workspace 作为 header 传递（SDK 拼 X-DashScope-WorkSpace）
            params["workspace"] = self.workspace
        # 音色塑形：仅传入非 None 的项，避免覆盖服务端默认（尤其 volume 默认 50）
        if self.pitch_rate is not None:
            params["pitch_rate"] = float(self.pitch_rate)
        if self.speech_rate is not None:
            params["speech_rate"] = float(self.speech_rate)
        if self.volume is not None:
            params["volume"] = int(self.volume)
        if self.instruction:
            params["instruction"] = str(self.instruction)
        params["seed"] = self.seed
        if self.hot_fix:
            params["hot_fix"] = self.hot_fix
        if self.enable_ssml:
            # SSML：text 里带 <speak> 标签，需显式开启（cosyvoice-v3.5 实测支持）
            params["additional_params"] = {"enable_ssml": True}
        return SpeechSynthesizer(**params)

    # ------------------------------------------------------------------
    def _speech_bytes(self, text: str, voice: str) -> bytes:
        """调用 SpeechSynthesizer.call(text) 返回二进制音频字节。"""
        from app.services.provider_budget import current_tts_budget
        from app.services.provenance import fingerprint
        budget = current_tts_budget()
        synth = self._build_synthesizer(voice)
        attempt_id = uuid.uuid4().hex
        record = {"schema_version":"2.0", "attempt_id":attempt_id,
                  "adapter_version":"cosyvoice-sdk-v2", "model":self.model,
                  "voice_id":voice, "voice_asset_version":getattr(self,"voice_asset_version",None),
                  "sdk_version":importlib.metadata.version("dashscope"),
                  "endpoint":_resolve_synth_ws_url() or "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
                  "text":text, "instruction":self.instruction,
                  "pitch_rate":self.pitch_rate if self.pitch_rate is not None else 1.0,
                  "speech_rate":self.speech_rate if self.speech_rate is not None else 1.0,
                  "volume":self.volume if self.volume is not None else 50,
                  "hot_fix":self.hot_fix,"seed":self.seed,
                  "format":self.response_format, "sample_rate":self.sample_rate,
                  "transport_messages":[], "status":"started", "request_id":None}
        from app.paths import state_dir
        log = state_dir() / "tts_requests" / (attempt_id + ".json")
        # Observe the SDK's serialized boundary, including call() mutations.
        send_name = "_SpeechSynthesizer__send_str"
        original = getattr(synth, send_name, None)
        if original:
            def observed(message):
                payload = json.loads(message) if isinstance(message,str) else message
                record["transport_messages"].append(payload)
                record["request_id"] = payload.get("header",{}).get("task_id")
                atomic_json(log, record)
                return original(message)
            setattr(synth,send_name,observed)
        atomic_json(log,record)
        self.last_request_path = log
        started = time.monotonic()
        reservation = budget.reserve('tts', fingerprint(record), len(text))
        try:
            audio = synth.call(text, timeout_millis=int(self.timeout_s*1000))
            if hasattr(audio,"read"): audio=audio.read()
            if not isinstance(audio,bytes) or not audio:
                raise RuntimeError("provider returned no binary audio")
            record["status"] = "succeeded"
            record["audio_sha256"] = hashlib.sha256(audio).hexdigest()
            return audio
        except Exception as exc:
            record["status"] = "failed"
            # Do not serialize exception bodies, URLs with credentials, or SDK objects.
            record["error"] = {"type":type(exc).__name__, "message":"synthesis failed; inspect provider request_id"}
            raise
        finally:
            budget.finish(reservation, record['status']=='succeeded')
            record["elapsed_seconds"] = time.monotonic()-started
            atomic_json(log,record)

    # ------------------------------------------------------------------
    def synthesize(self, text: str, out_audio: Path, voice: str | None = None,
                   overwrite: bool = False) -> tuple[Path, int]:
        """
        整段合成一段旁白 → 落盘音频文件。
        幂等：若 out_audio 已存在且非 overwrite → 直接返回时长。
        返回 (out_path, duration_ms)。
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("[dashscope-cosyvoice] 空文本无法合成")
        out_audio = Path(out_audio)
        voice = voice or self.voice
        if not voice:
            raise ValueError("[dashscope-cosyvoice] 需要已注册音色中的 voice_id")

        if out_audio.exists() and not overwrite:
            return out_audio, _audio_duration_ms(out_audio)

        out_audio.parent.mkdir(parents=True, exist_ok=True)
        data = self._speech_bytes(text, voice)
        take_dir = out_audio.parent / "takes" / uuid.uuid4().hex
        take_dir.mkdir(parents=True)
        raw = take_dir / ("raw." + self.response_format)
        raw.write_bytes(data)
        request_path = getattr(self,"last_request_path",None)
        if request_path:
            atomic_json(take_dir / "request.json", json.loads(request_path.read_text(encoding="utf-8")))
        atomic_json(take_dir / "manifest.json", {"raw":str(raw), "sha256":file_sha256(raw),
                    "status":"candidate", "listening_review":"pending"})
        tmp = out_audio.with_suffix(out_audio.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(out_audio)
        return out_audio, _audio_duration_ms(out_audio)


def _env_get(name: str) -> str:
    """读 {name}（.env 优先，真实环境变量兜底）。

    C4：委托给 config.env_get。原先这里是第四份 .env 解析，且**每次调用都重读
    一遍文件**（DashScope 通道初始化会连调数次）；现在统一走带 mtime 缓存的那份。
    """
    return env_get(name)


def _dashscope_key() -> str:
    """读取 DASHSCOPE_API_KEY（.env 或环境变量）。"""
    key = _env_get(DASHSCOPE_ENV_KEY)
    if not key or key.startswith("sk-待填写") or "待" in key:
        raise RuntimeError(
            f"[dashscope-cosyvoice] 未配置 {DASHSCOPE_ENV_KEY}，请在 {ROOT / '.env'} 填："
            f"{DASHSCOPE_ENV_KEY}=sk-你的百炼Key"
        )
    return key


def _cosyvoice_model_from_env() -> str:
    return _env_get("COSYVOICE_V35_MODEL") or DEFAULT_COSYVOICE_MODEL


def _audio_duration_ms(audio: Path) -> int:
    """ffprobe 取音频时长(ms)，mp3/wav 通吃。"""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(audio)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return int(float(out) * 1000)
