"""
CosyVoice2 语音合成服务（整段合成）。

通道：V-API (https://api.gpt.ge, provider 标为 V-API) 的 OpenAI 兼容中转。
已实测（2026-09-03，key 见 .env `vapi_cosyvoice-v2`）：
  - GET  /v1/models          → data 含 {"id":"CosyVoice2"}
  - POST /v1/audio/speech    → 有效 wav（PCM s16le 24kHz mono），中文正常
  - voice 预设与 DashScope 官方 CosyVoice preset 一致，可逐音色换
  - 非法 voice 名不报错、静默用默认音色 → 中转参数校验宽松

★ 与 edge-tts 逐句方案的差别：本模块按【整段旁白】一次性合成，
   从根上消除"逐句拼接 → 机械念清单"。要按语义段喂，不要按 frame 逐句喂。

用法：
    from app.services.cosyvoice import CosyVoice2Synthesizer
    svc = CosyVoice2Synthesizer()
    wav, dur_ms = svc.synthesize("整段旁白文本", voice="longlaotu", out_wav=Path(...))
"""
from __future__ import annotations

import os
import json
import uuid
import hashlib
import importlib.metadata
from app.services.provenance import atomic_json, file_sha256, fingerprint
import subprocess
import time
from pathlib import Path
from typing import Optional

import requests

from app.config.config import ROOT, env_get

# ---------------------------------------------------------------------------
# 常量 / 通道配置
# ---------------------------------------------------------------------------
ENV_KEY_NAME = "vapi_cosyvoice-v2"     # .env 里存 V-API 令牌的变量名
DEFAULT_BASE_URL = "https://api.gpt.ge"
DEFAULT_MODEL = "CosyVoice2"
DEFAULT_VOICE = "longlaotu"            # 暂定占位；等你定音色后改这里
TIMEOUT_S = 120
RETRIES = 3
RETRY_BACKOFF_S = 3

# 实测可用的 preset 音色（V-API 与 DashScope CosyVoice preset 对齐）
KNOWN_VOICES = [
    "longlaotu", "longwan", "longxiaochun", "longcheng", "longhua",
    "Coffee", "Bella", "George", "Luca", "Sofia", "Ethan", "Aria",
    "Sarah", "Jessica", "Emma",
]


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _load_env_key() -> tuple[str, str]:
    """读 V-API key + base_url。返回 (base_url, api_key)。

    C4：改走 config.env_get（.env 优先、真实环境变量兜底），
    不再自己抄一份 .env 解析（原实现不去引号，与 config 侧语义不等价）。
    """
    api_key = env_get(ENV_KEY_NAME)
    base_url = env_get("cosyvoice_API_BASE", DEFAULT_BASE_URL)
    if not api_key:
        raise RuntimeError(
            f"[cosyvoice] 未找到 {ENV_KEY_NAME} 令牌，请在 D:\\hv\\.env 补："
            f"{ENV_KEY_NAME}=sk-..."
        )
    return base_url.rstrip("/"), api_key


def _wav_duration_ms(wav: Path) -> int:
    """ffprobe 取 wav 时长(ms)。"""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(wav)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return int(float(out) * 1000)


def _load_dotenv_for_process() -> None:
    """把 .env 里 {ENV_KEY_NAME} 注入进程环境，供 requests 以外的库复用。"""
    base, key = _load_env_key()
    os.environ.setdefault(ENV_KEY_NAME, key)
    os.environ.setdefault("cosyvoice_API_BASE", base)


# ---------------------------------------------------------------------------
# 合成器
# ---------------------------------------------------------------------------
class CosyVoice2Synthesizer:
    """V-API CosyVoice2 整段语音合成器。"""

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str = DEFAULT_MODEL, voice: str = DEFAULT_VOICE,
                 timeout_s: int = TIMEOUT_S):
        if not api_key:
            base_url, api_key = _load_env_key()
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.timeout_s = timeout_s
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    def _speech(self, text: str, voice: str, response_format: str = "wav") -> bytes:
        """POST /v1/audio/speech，返回音频字节。失败自动重试。"""
        url = f"{self.base_url}/v1/audio/speech"
        payload = {
            "model": self.model,
            "input": text,
            "voice": voice,
            "response_format": response_format,
        }
        last_err: Exception | None = None
        for attempt in range(RETRIES):
            try:
                r = requests.post(url, json=payload, headers=self.headers,
                                  timeout=self.timeout_s)
                if r.status_code == 200:
                    # 校验是否真是音频(wav 头 RIFF)而非 json 错误
                    if r.content[:4] == b"RIFF":
                        return r.content
                    # 200 但非音频 → 可能是包装错误
                    raise RuntimeError(f"200 但非音频: {r.content[:200]!r}")
                last_err = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            except Exception as e:  # noqa: BLE001
                last_err = e
            if attempt < RETRIES - 1:
                time.sleep(RETRY_BACKOFF_S * (attempt + 1))
        raise RuntimeError(f"[cosyvoice] 合成失败(重试{RETRIES}次): {last_err}")

    # ------------------------------------------------------------------
    def synthesize(self, text: str, out_wav: Path, voice: str | None = None,
                   overwrite: bool = False) -> tuple[Path, int]:
        """
        整段合成一段旁白。幂等：若 out_wav 已存在且非 overwrite → 直接返回。
        返回 (wav_path, duration_ms)。
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("[cosyvoice] 空文本无法合成")
        out_wav = Path(out_wav)
        voice = voice or self.voice

        if out_wav.exists() and not overwrite:
            return out_wav, _wav_duration_ms(out_wav)

        out_wav.parent.mkdir(parents=True, exist_ok=True)
        data = self._speech(text, voice)
        # 原子写：先写临时再改名，避免中断留下半个文件
        tmp = out_wav.with_suffix(out_wav.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(out_wav)
        return out_wav, _wav_duration_ms(out_wav)

    # ------------------------------------------------------------------
    def probe_voices(self, text: str, out_dir: Path,
                     voices: list[str] | None = None) -> list[dict]:
        """批量合成同一句到 out_dir，用于音色试听对比。返回时长明细。"""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        voices = voices or KNOWN_VOICES
        result = []
        for v in voices:
            wav = out_dir / f"{v}.wav"
            try:
                _, dur = self.synthesize(text, wav, voice=v, overwrite=True)
                result.append({"voice": v, "ok": True, "dur_s": round(dur / 1000, 2)})
            except Exception as e:  # noqa: BLE001
                result.append({"voice": v, "ok": False, "err": str(e)})
        return result


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------
def synthesize_whole(text: str, out_wav: Path, voice: str = DEFAULT_VOICE,
                     overwrite: bool = False) -> tuple[Path, int]:
    """模块级便捷函数：整段合成一段旁白。"""
    return CosyVoice2Synthesizer(voice=voice).synthesize(
        text, out_wav, voice=voice, overwrite=overwrite
    )


def available_voice_names() -> list[str]:
    return list(KNOWN_VOICES)


# ===========================================================================
# ▼▼▼ DashScope (阿里云百炼) CosyVoice v3.5 通道 ▼▼▼
# ---------------------------------------------------------------------------
# 与上面 V-API 通道并存：V-API 已实测全女声、弃用；本通道为新的主用通道。
# 音源走「语音设计」(voice design)：文字描述 → 生成专属 voice_id。
# 模型档位：cosyvoice-v3.5-flash(¥0.8/万) / cosyvoice-v3.5-plus(¥1.5/万)。
# 官方说明：https://help.aliyun.com/zh/model-studio/developer-reference/
#   声音复刻 & 语音设计：cosyvoice-clone-api-reference / voice-design
# ===========================================================================

DASHSCOPE_ENV_KEY = "DASHSCOPE_API_KEY"
DASHSCOPE_ENV_WS = "DASHSCOPE_WORKSPACE"   # 可选：业务空间域名前缀，如 ws-ba6d5skqpa8m020f
DASHSCOPE_ENV_WORKSPACE_ID = "DASHSCOPE_WORKSPACE_ID"  # 若 workspace 是纯 ID（非域名前缀）用它
# 声音设计/复刻 customization 端点（北京地域）。若配了 workspace 域名前缀则自动切换专属域名。
_DEFAULT_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization"
# 2026-09-08 升到 plus：与 flash 的价差在绝对值上可忽略（14.6min 片
# ¥0.54 vs ¥0.29），但韵律与质感是更高档位，A/B 里用户也明显更认可 plus。
DEFAULT_COSYVOICE_MODEL = "cosyvoice-v3.5-plus"
DEFAULT_BJ_WS_TMPL = "https://{ws}.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/customization"
_BJ_WS_WS_TMPL = "wss://{ws}.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"


def _resolve_voice_design_endpoint() -> str:
    """根据 .env 的 workspace 配置决定声音设计端点（北京专属域名优先）。"""
    # I 批（2026-09-05）：原为 `os.environ.get(...) or _env_get(...)`（环境变量
    # 优先），与下面的 _resolve_synth_ws_url 及 :390 的 `_env_get(...)`（.env
    # 优先）**语义相反** —— 同一个 key 两种优先级，改了其中一处另一处就不生效。
    # 统一按 C4 定的语义：.env 优先，真实环境变量兜底。
    ws = _env_get(DASHSCOPE_ENV_WS)
    if ws:
        ws = ws.strip().rstrip("/")
        # 支持两种写法：ws-ba6d5skqpa8m020f 或完整 ws-ba6d5...cn-beijing.maas.aliyuncs.com
        if "." in ws and ("maas" in ws or "aliyuncs" in ws):
            host = ws if ws.startswith("http") else f"https://{ws}"
            return f"{host}/api/v1/services/audio/tts/customization"
        return DEFAULT_BJ_WS_TMPL.format(ws=ws)
    return _DEFAULT_ENDPOINT


def _resolve_synth_ws_url() -> str | None:
    """根据 .env 的 workspace 决定语音合成 WebSocket URL（北京专属）。无则 None。"""
    # I 批（2026-09-05）：同上，统一为 .env 优先（原本与 :390 相反）。
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
# 沉稳纪录片男声的语音设计描述（可再调；中文表述，供 voice design 理解）
# 2026-09-08：原 prompt 里写了「语速平稳」「适合…新闻播报」，等于指示语音设计
# 产出播报腔 —— 出片后用户反馈「语音太平、没有起伏」。改成戏剧化描述。
# ⚠️ 换 prompt 必须换 prefix：design_voice 按 (prefix, target_model) 缓存，
#    复用旧 prefix 会拿回旧音色，改了等于没改。
DEFAULT_VOICE_PROMPT = (
    "成熟的中年男声，嗓音低沉浑厚、带胸腔共鸣、富有磁性。"
    "叙述节奏起伏明显：平铺时舒缓低沉，紧张时语速收紧、语气压低，"
    "关键处会刻意放慢并加重，句末有自然的收束与停顿。"
    "情绪饱满、有戏剧张力与压迫感，适合悬念感强的历史解说，"
    "避免一字一顿的播音腔和新闻播报腔。"
)


# ---------------------------------------------------------------------------
# 语音设计（voice design）：文字描述 → 专属 voice_id（可含试听预览）
# ---------------------------------------------------------------------------
def design_voice(
    voice_prompt: str = DEFAULT_VOICE_PROMPT,
    preview_text: str | None = None,
    prefix: str = "hvnarr",
    target_model: str | None = None,
    sample_rate: int = 24000,
    response_format: str = "mp3",
    api_key: str | None = None,
    endpoint: str | None = None,
) -> dict:
    """
    用文字描述在 DashScope 创建/复用一个语音设计专属音色。
    返回 dict：{voice_id, created(新/复用), preview_path?, preview_b64?}
    幂等：以 (prefix, target_model) 缓存在磁盘 .cache/voice_design_<prefix>.json，
          已有缓存则直接复用 voice_id，不再重复扣费。
    """
    import json as _json
    import base64

    import requests as _requests

    api_key = api_key or _dashscope_key()
    target_model = target_model or DEFAULT_COSYVOICE_MODEL
    endpoint = endpoint or _resolve_voice_design_endpoint()
    cache_dir = ROOT / ".cache"
    cache_file = cache_dir / f"voice_design_{prefix}_{target_model}.json"

    # ---- 命中缓存则复用 ----
    if cache_file.exists():
        try:
            cached = _json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("voice_prompt") != voice_prompt:
                raise ValueError("Voice design cache prompt is unverified or changed; register an explicit VoiceAsset or use a new prefix after review")
            cached.pop("preview_b64", None)
            cached["cached"] = True
            return cached
        except (OSError, _json.JSONDecodeError):
            pass

    payload = {
        "model": "voice-enrollment",
        "input": {
            "action": "create_voice",
            "target_model": target_model,
            "voice_prompt": voice_prompt,
            "prefix": prefix,
        },
    }
    if preview_text:
        payload["input"]["preview_text"] = preview_text
    payload["parameters"] = {
        "sample_rate": sample_rate,
        "response_format": response_format,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # 非北京 region 需该头；北京(默认)可留空由服务端放行
        "X-DashScope-DataInspection": "disable",
    }
    resp = _requests.post(endpoint, json=payload, headers=headers, timeout=90)
    if resp.status_code != 200:
        raise RuntimeError(f"[voice-design] HTTP {resp.status_code}: {resp.text[:300]}")

    data = resp.json().get("output", {})
    voice_id = data.get("voice_id")
    if not voice_id:
        raise RuntimeError(f"[voice-design] 未返回 voice_id: {resp.text[:300]}")

    out = {
        "voice_id": voice_id,
        "created": True,
        "cached": False,
        "preview_b64": data.get("audio", ""),
        "model": target_model,
        "voice_prompt": voice_prompt,
        "preview_text": preview_text,
        "approval_status": "pending",
    }

    # 落盘缓存（含 preview 信息，便于日后试听/复用）
    cache_dir.mkdir(parents=True, exist_ok=True)


    # 若带试听文本且有 preview_b64 → 解码保存试听文件
    preview_path = None
    if preview_text and out.get("preview_b64"):
        try:
            preview_path = cache_dir / f"voice_design_{prefix}_preview.wav"
            raw = base64.b64decode(out["preview_b64"])
            preview_path.write_bytes(raw)
            out["preview_path"] = str(preview_path)
        except Exception:  # noqa: BLE001
            preview_path = None
    out["preview_path"] = str(preview_path) if preview_path else None
    out.pop("preview_b64",None)
    if preview_path: out["preview_sha256"]=file_sha256(preview_path)
    atomic_json(cache_file,out)
    return out


# ---------------------------------------------------------------------------
# DashScope CosyVoice v3.5 合成器
# ---------------------------------------------------------------------------
class DashScopeCosyVoiceSynthesizer:
    """
    阿里云百炼 CosyVoice v3.5 整段合成器（web 同步通道，用 dashscope SDK）。
    与 CosyVoice2Synthesizer 保持一致的整段/幂等/原子写语义，便于管线无缝切换。

    用法：
        from app.services.cosyvoice import DashScopeCosyVoiceSynthesizer
        svc = DashScopeCosyVoiceSynthesizer(voice=voice_id, enable_ssml=enable_ssml)  # voice_id 来自 design_voice()
        wav, dur_ms = svc.synthesize("整段旁白", out_wav=Path(...))
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
        log = ROOT / "state" / "tts_requests" / (attempt_id + ".json")
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
            raise ValueError("[dashscope-cosyvoice] 需要 voice（先 design_voice() 取 voice_id）")

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


# ---------------------------------------------------------------------------
# DashScope 通道辅助
# ---------------------------------------------------------------------------
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
            f"[dashscope-cosyvoice] 未配置 {DASHSCOPE_ENV_KEY}，请在 D:\\hv\\.env 填："
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


# ---------------------------------------------------------------------------
# DashScope 便捷入口（推荐给管线用）
# ---------------------------------------------------------------------------
_FLASH_VOICE_PREFIX = "hvnflash"          # 与 probe_v35.py 里 flash 音色 prefix 一致，命中缓存

# voice-design 接口的强制项：只给 voice_prompt 会被拒（HTTP 400）。
# 这是一段用于「生成音色时试听」的样例文本，不影响后续任意文本的合成。
_VOICE_DESIGN_PREVIEW_TEXT = (
    "然而张骞始终手持旌节，不肯屈服。"
)


# 2026-09-08 起用这个 prefix（对应上面的新音色描述）。换描述必须换 prefix，
# 否则 design_voice 命中旧缓存，改了等于没改。
_VOICE_PREFIX = "hvndrama"


def get_voice_id(model: str | None = None,
                 voice_prompt: str = DEFAULT_VOICE_PROMPT) -> str:
    """取**指定模型**的 voice_id（按模型缓存，首次创建会少量扣费）。

    ⚠️ voice_id 与模型强绑定：拿 flash 的 voice_id 去跑 plus → HTTP 418。
       所以这里按 model 分别 design，缓存键是 (prefix, target_model)。
    """
    from app.config.config import get_settings
    if get_settings().get_profile().voice.voice_asset:
        from app.services.voice_assets import resolve_voice_asset
        return resolve_voice_asset(model or _cosyvoice_model_from_env())[1]["voice_id"]
    return design_voice(
        voice_prompt=voice_prompt,
        preview_text=_VOICE_DESIGN_PREVIEW_TEXT,
        prefix=_VOICE_PREFIX,
        target_model=model or _cosyvoice_model_from_env(),
    )["voice_id"]


def get_flash_voice_id(voice_prompt: str = DEFAULT_VOICE_PROMPT) -> str:
    """（兼容旧调用）显式取 flash 的 voice_id。"""
    return get_voice_id("cosyvoice-v3.5-flash", voice_prompt)


def synthesize_dashscope_whole(
    text: str,
    out_audio: Path,
    voice_prompt: str = DEFAULT_VOICE_PROMPT,
    voice_id: str | None = None,
    model: str | None = None,
    response_format: str = "mp3",
    overwrite: bool = False,
    pitch_rate: float | None = None,
    speech_rate: float | None = None,
    volume: int | None = None,
    instruction: str | None = None,
    enable_ssml: bool = False,
) -> tuple[Path, int]:
    """
    用 DashScope CosyVoice v3.5 整段合成旁白（plus + 缓存男声）。
    这是管线应使用的入口：自动取/复用该模型的 voice_id，合成整段文本落盘。

    音色塑形（可选透传，见 DashScopeCosyVoiceSynthesizer.__init__）：
      pitch_rate<1 → 压低音调、更浑厚；instruction → 自然语言风格引导。

    ⚠️ 先定 model 再取 voice_id：voice_id 与模型必须匹配，
       拿 flash 的 voice_id 去跑 plus 会 HTTP 418（2026-09-08 实测）。
    返回 (out_path, duration_ms)。
    """
    model = model or _cosyvoice_model_from_env()
    voice_id = voice_id or get_voice_id(model, voice_prompt)
    svc = DashScopeCosyVoiceSynthesizer(
        voice=voice_id, model=model, response_format=response_format,
        pitch_rate=pitch_rate, speech_rate=speech_rate,
        volume=volume, instruction=instruction, enable_ssml=enable_ssml,
    )
    return svc.synthesize(text, out_audio, overwrite=overwrite)


# ---------------------------------------------------------------------------
# qwen-audio-3.0-tts（HTTP 非流式；支持 instruction / hot_fix / seed）
# ---------------------------------------------------------------------------
# 为什么不走 SDK：dashscope 的 SpeechSynthesizer.call() 不暴露 instruction 与
# hot_fix 参数（2026-09-08 实测），而这两个是 qwen 路线的核心能力
#（情感风格 + 官方多音字修复）。走 HTTP 非流式，音频从返回 URL 下载。

_QWEN_HTTP_URL = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer"


def synthesize_qwen_whole(
    text: str,
    out_audio: Path,
    voice: str | None = None,
    model: str = "qwen-audio-3.0-tts-plus",
    instruction: str | None = None,
    hot_fix: dict | None = None,
    response_format: str = "mp3",
    sample_rate: int = 24000,
    overwrite: bool = False,
    rate: float | None = None,
    enable_ssml: bool = False,
) -> tuple[Path, int]:
    """qwen-audio-3.0-tts 整段合成（HTTP 非流式）。

    instruction：自然语言风格指令（如纪录片解说）。⚠️ 与 CosyVoice 同名参数，
    但行为是「整段风格设定」—— 实测组间一致性远好于 CosyVoice 的 instruction。
    hot_fix：官方多音字修复 {"pronunciation": [{"石": "dan4"}],
    "replace": [{"七十万石": "七十万担"}]}。
    ⚠️ pronunciation 是**全局**替换：单字 key 会误伤同形字（实测「磨刀石」
    被石→dan4 带坏），量词类务必用短语级 replace。
    返回 (out_path, duration_ms)。
    """
    import base64

    import requests as _rq

    if out_audio.exists() and not overwrite:
        return out_audio, _audio_duration_ms(out_audio)
    inp: dict = {"text": text, "voice": voice, "format": response_format,
                 "sample_rate": sample_rate}
    if instruction:
        inp["instruction"] = instruction
    if hot_fix:
        inp["hot_fix"] = hot_fix
    if rate is not None:
        inp["rate"] = float(rate)
    if enable_ssml:
        inp["enable_ssml"] = True
    r = _rq.post(_QWEN_HTTP_URL, timeout=300,
                 json={"model": model, "input": inp},
                 headers={"Authorization": f"Bearer {_dashscope_key()}",
                          "Content-Type": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"[qwen-tts] HTTP {r.status_code}: {r.text[:200]}")
    au = (r.json().get("output") or {}).get("audio") or {}
    if au.get("data"):
        raw = base64.b64decode(au["data"])
    elif au.get("url"):
        raw = _rq.get(au["url"], timeout=180).content
    else:
        raise RuntimeError(f"[qwen-tts] 无音频: {str(r.json())[:200]}")
    out_audio.parent.mkdir(parents=True, exist_ok=True)
    out_audio.write_bytes(raw)
    return out_audio, _audio_duration_ms(out_audio)


# ---------------------------------------------------------------------------
# CLI：整段合成一段文本到文件
# ---------------------------------------------------------------------------
def main():
    import argparse
    ap = argparse.ArgumentParser(description="CosyVoice 整段合成一段旁白")
    ap.add_argument("--text", required=True, help="要合成的整段旁白")
    ap.add_argument("--out", required=True, help="输出音频路径")
    ap.add_argument("--voice", default=DEFAULT_VOICE, help="音色名（V-API 通道）")
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    wav, dur = synthesize_whole(args.text, Path(args.out),
                                voice=args.voice, overwrite=args.overwrite)
    print(f"OK: {wav}  {dur/1000:.2f}s  voice={args.voice}")


if __name__ == "__main__":
    main()
