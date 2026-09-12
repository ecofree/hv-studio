"""
配置加载 + .env 渠道池发现。

设计要点：
1. 主配置走 YAML（app/config/config.yaml），可读性高、易 diff
2. 密钥走 .env（不被 git 跟踪）
3. subrouter.ai 的三渠道密钥自动发现：扫描 .env 中所有 "{provider}_gpt-image-2=sk-..."
   模式，构成可轮询 + 失败降级的渠道池。命名约定来自用户：aicost/qcyc/cxk。
4. polyphone.yaml 与 voices.yaml 是独立的"长期资产"，单独文件，频繁追加。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent.parent  # D:\hv
APP_DIR = ROOT / "app"
CONFIG_DIR = APP_DIR / "config"
ENV_PATH = ROOT / ".env"


# ---------------------------------------------------------------------------
# .env 解析 —— 全项目唯一入口（C4 收敛）
# ---------------------------------------------------------------------------
# 收敛前项目里有 4 份互相不等价的 .env 读取：
#   1. 本文件的 load_env()            （去引号，只读 .env）
#   2. SettingsConfigDict(env_file=)  （pydantic-settings 读同一份 .env）
#   3. services/cosyvoice.py:_load_env_key()（不去引号，.env 优先 + os.environ 兜底）
#   4. services/cosyvoice.py:_load_env()    （同上，且每次调用都重读一遍文件）
# 差异曾是隐性坑：同一份 .env，config 侧拿到去引号的值、cosyvoice 侧拿到带引号的值。
# 现在统一为：解析只有本函数一份；取值一律走 env_get()（.env 优先，环境变量兜底）。
#
# 缓存按 (mtime, size) 失效 —— 改了 .env 自动重读，不需要手工清缓存。
_ENV_CACHE: dict[str, tuple[tuple[int, int], dict[str, str]]] = {}


def load_env(path: Path | str | None = None) -> dict[str, str]:
    """解析 .env → dict。默认读项目根的 .env。"""
    p = Path(path) if path is not None else ENV_PATH
    try:
        st = p.stat()
        stamp = (int(st.st_mtime_ns), st.st_size)
    except OSError:            # 文件不存在（.env 未纳入 git，允许缺失）
        return {}
    hit = _ENV_CACHE.get(str(p))
    if hit is not None and hit[0] == stamp:
        return dict(hit[1])    # 返回副本，防调用方改脏缓存

    env: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    _ENV_CACHE[str(p)] = (stamp, env)
    return dict(env)


def env_get(name: str, default: str = "", path: Path | str | None = None) -> str:
    """取值：.env 优先，真实环境变量兜底（保持 cosyvoice 侧原有语义）。"""
    v = load_env(path).get(name)
    if v:
        return v
    return os.environ.get(name, default)


# ---------------------------------------------------------------------------
# 渠道池发现
# ---------------------------------------------------------------------------
class Channel(BaseModel):
    """subrouter.ai 一个渠道 = 一个 sk- key = 一份独立配额。"""
    name: str
    api_key: str
    base_url: str
    model: str
    models: list[str] = Field(default_factory=list)
    enabled: bool = True
    max_concurrency: int = Field(default=1, ge=1, le=16)

    def supports(self, model: str) -> bool:
        return self.enabled and model in (self.models or [self.model])

    @property
    def auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}


def discover_channels(env: dict[str, str], model: str) -> list[Channel]:
    """
    扫描 .env，发现所有 "{provider}_{model}={key}" 形式的密钥。
    例：aicost_gpt-image-2=sk-... → Channel(name="aicost", api_key=sk-...)
    """
    suffix = f"_{model}"
    # 用 \b + 后缀锚定，避免 gpt-image-2_API_BASE 之类前缀撞车
    pattern = re.compile(rf"^([a-z0-9_-]+){re.escape(suffix)}$")
    base = env.get(f"{model}_API_BASE", "https://subrouter.ai").rstrip("/")
    channels: list[Channel] = []
    for k, v in env.items():
        m = pattern.match(k)
        if m and v.startswith("sk-"):
            channels.append(Channel(
                name=m.group(1),
                api_key=v,
                base_url=base,
                model=model,
            ))
    return channels


class ChannelRoute(BaseModel):
    """Explicit supplier capabilities; credentials remain in the existing env file."""
    model_config = {"extra": "forbid"}
    name: str
    key_env: str
    base_env: str = "gpt-image-2_API_BASE"
    models: list[str] = Field(min_length=1)
    enabled: bool = True
    max_concurrency: int = Field(default=1, ge=1, le=16)


def configured_channels(env: dict[str, str], routes: list[dict]) -> list[Channel]:
    result = []
    names = set()
    for entry in routes:
        route = ChannelRoute.model_validate(entry)
        if route.name.casefold() in names:
            raise ValueError(f"duplicate image channel: {route.name}")
        names.add(route.name.casefold())
        key = env.get(route.key_env, "")
        if not route.enabled or not key:
            continue
        result.append(Channel(
            name=route.name, api_key=key,
            base_url=env.get(route.base_env, "https://subrouter.ai").rstrip("/"),
            model=route.models[0], models=route.models,
            max_concurrency=route.max_concurrency,
        ))
    return result


# ---------------------------------------------------------------------------
# YAML 主配置
# ---------------------------------------------------------------------------
class ProfileImage(BaseModel):
    engine: str
    # ★ 关键修订：size 不再是"分辨率"，是"长宽比选择器"。
    #   实际分辨率由 subrouter 钳制为 1,572,864 = 1024x1536，公式：
    #     h = sqrt(1572864 / ratio); w = ratio * h
    #   推荐 "1080x1920"（9:16 精确），预计产出 940x1672。
    size: str = "1080x1920"
    quality: str = "low"      # low 即可拿满 1.57MP，high 价 35x 但分辨率相同（待验）
    upscale: int = 2
    style_suffix: str = ""
    negative_clause: str = ""
    identity_anchor_template: str = ""
    require_expression_spec: bool = True
    require_pose_spec: bool = True


class ProfileVoice(BaseModel):
    voice_asset: str | None = None
    """音色塑形（CosyVoice v3.5 合成参数，可选；None = 服务端默认）。"""
    engine: str
    # fallback_engine 已删（P1-3 审计）：全仓零消费者，配置在撒谎。
    # 2026-09-08 起引擎失败直接 raise（--no-fallback 语义）。
    fallback_engine: str | None = None  # 已废弃，勿配置
    speaker: str
    speed: float = 1.0
    pitch: float = 0.0
    emotion: str = "steady"

    # ---- CosyVoice 音色塑形 ----
    # pitch_rate < 1 → 压低音调，声音更沉更浑厚（改善"声音薄"的首选）
    # instruction   → 自然语言风格引导，如"用低沉浑厚、胸腔共鸣的声音朗读"
    pitch_rate: float | None = None
    speech_rate: float | None = None
    volume: int | None = None
    instruction: str | None = None

    # ---- qwen-audio-3.0-tts（engine: qwen 时生效）----
    # qwen_model      : qwen-audio-3.0-tts-plus / -flash
    # qwen_instruction: 全局风格指令（W1 实测：全程一条最稳，组间不漂移）
    # qwen_hot_fix    : 官方多音字修复 {"pronunciation":[{词:拼音}],
    #                   "replace":[{词:替换文本}]}。
    #                   ⚠️ pronunciation 是全局替换，量词类（石 dan4/shí）
    #                   必须用短语级 replace，否则误伤同形字（磨刀石踩坑）。
    qwen_model: str | None = None
    qwen_instruction: str | None = None
    qwen_hot_fix: dict | None = None
    qwen_rate: float | None = None
    # cosyvoice 侧模型（None → cosyvoice.DEFAULT_COSYVOICE_MODEL / env）
    cosyvoice_model: str | None = None

    # 逐句 SSML（rate 定起伏 + phoneme 定读音）——**引擎无关**：
    # cosyvoice-v3.5 与 qwen-audio-3.0 都实测支持。原名 qwen_ssml 语义太窄
    # （2026-09-09 切回 cosyvoice 时发现：只对 qwen 生效会让切引擎后静默
    # 退回指令模式 → 组间漂移）。
    tts_ssml: bool = False


# H 批（2026-09-05）：ProfileRhythm 整段删除。
# 六个键（fps/pad_head_ms/pad_tail_ms/min_shot_ms/max_shot_ms/split_threshold_ms）
# 全仓零消费者——唯一两处「匹配」是 script.py 注释里提了一句数值。
# fps/pad_head/pad_tail 的真源在 app/services/spec.py，min/max_shot_ms 由
# 门禁常量反推（见 spec.MIN_SHOT_MS / MAX_SHOT_MS）。留着只会让人以为改这里有效。


class ProfileSubtitle(BaseModel):
    font: str
    font_size: int
    primary_colour: str
    outline_colour: str
    back_colour: str
    outline: int
    shadow: int
    margin_bottom: int
    margin_top: int
    max_chars_per_line: int
    min_duration_ms: int
    max_duration_ms: int
    keyword: dict[str, Any] = Field(default_factory=dict)


class ProfileMotion(BaseModel):
    default_type: str
    default_intensity: float
    easing: str
    # ★ 输入分辨率不再是固定 [2048, 3584]，而是 dynamic 字段；
    #   实际值由 upscale.py 计算（基于 gpt-image-2 返回图真实像素）
    input_resolution: list[int] | str = "dynamic"
    output_resolution: list[int]
    crop_window: list[int]
    # 同上：pan_margin_px 改为 dynamic
    pan_margin_px: list[int] | str = "dynamic"
    max_zoom: float
    conservative_mode: bool = True
    conservative_pan_ratio: float = 0.6
    downscale_filter: str = "lanczos"
    forbid_consecutive_same: bool = True
    zoom_easing_formula: str


class ProfileBGM(BaseModel):
    volume: float
    fade_out_sec: int


class ProfileASR(BaseModel):
    backend: str = "dashscope"
    python_executable: str = ".venv-asr/Scripts/python.exe"
    model_dir: str = ".cache/asr/faster-whisper-small"
    threads: int = Field(default=4, ge=1, le=16)
    timeout_seconds: int = Field(default=180, ge=1)


class StyleProfile(BaseModel):
    name: str
    image: ProfileImage
    voice: ProfileVoice
    subtitle: ProfileSubtitle
    motion: ProfileMotion
    bgm: ProfileBGM
    asr: ProfileASR = Field(default_factory=ProfileASR)


# ---------------------------------------------------------------------------
# 模型 / 引擎配置
# ---------------------------------------------------------------------------
# 注：顶层 tts 死配置(qwen3-tts/kokoro/silent-placeholder)已删——voice.py 实际
# 用 cosyvoice(主)/edge(回退)，由 profile.voice 塑形参数 + voice.py 常量驱动，
# H 批（2026-09-05）：AlignerConfig 随 alignment.py 一并删除。
# 它零消费者（全仓 grep 除此处外无引用），却是个**必填**字段——想从
# config.yaml 里删掉 aligner 段会直接 ValidationError。死配置还强制存在，
# 只会让人以为「强制对齐」这道能力还在。


class ImageEngineConfig(BaseModel):
    """单引擎配置。降级链中的非 API 项可能只填 engine。"""
    model_config = {"extra": "allow"}

    engine: str
    model: str | None = None
    api_base: str | None = None
    api_key_env: str | None = None
    endpoint_generate: str | None = None
    endpoint_edit: str | None = None
    size: str | None = None
    quality: str | None = None
    n: int | None = 1
    response_format: str | None = "b64_json"
    timeout_s: int | None = 300
    max_reference_images: int | None = 16
    concurrency: int | None = 6
    requires_upscale: bool | None = True
    supports_seed: bool | None = False
    supports_character_id: bool | None = False
    strict_param_validation: bool | None = False
    # 降级项附注（不参与运行逻辑）
    note: str | None = None


class ImagePostprocessUpscale(BaseModel):
    enabled: bool
    engine: str
    model: str
    scale: int
    device: str
    tile: int
    output_size_dynamic: bool = True   # 由 upscale.py 算（基于返回图真实像素 × scale）


# C3（2026-09-05）：ImageConsistencyConfig 随 character_lock/face 一并删除。
# image.consistency 从来只被声明、从未被读取（全仓 grep 零消费者），
# 它是「定妆包 + 人脸相似度」那套已删能力的配置项，留着只会让人以为能力还在。


class ImageConfig(BaseModel):
    primary: ImageEngineConfig
    postprocess_upscale: ImagePostprocessUpscale
    fallbacks: list[ImageEngineConfig]


class VideoPrimaryConfig(BaseModel):
    engine: str
    api_base: str
    model: str
    endpoint: str
    task_based: bool = True
    image_field: str | None = None      # 待 T2 探测确定
    duration_sec: int = 5
    timeout_s: int = 900
    poll_interval_s: int = 10
    concurrency: int = 3
    match_duration_to_audio: bool = True
    max_waste_ratio: float = 0.35
    face_sim_threshold: float = 0.60
    on_face_drift: str = "fallback_to_kenburns"


class VideoConfig(BaseModel):
    enabled: bool
    strategy: str
    hero_shots_per_episode: list[int]
    primary: VideoPrimaryConfig
    fallbacks: list[str]


# H 批（2026-09-05）：顶层 MotionConfig 删除。运镜实现全在
# app/services/motion.py，config 的 supersample/pan_impl/zoom_impl/fallbacks
# 四个键全仓零消费者（此处匹配到的 `.motion` 是 Shot 的运镜字段，不是这个）。


class ComposeConfig(BaseModel):
    video_encoder: str
    preset: str
    cq: int
    fallbacks: list[dict[str, Any]]
    audio_codec: str
    audio_bitrate: str
    # P1 2.1（compose 收口超时）：>0 时 ffmpeg wait 超过该秒数即 kill 并抛错，
    # 防编码卡死导致 compose 永久挂起。0 或缺省 = 不设超时（保持历史行为，保守）。
    timeout_sec: float = 0
    # 分段渲染（2026-09-04）：按段预渲染视频再 concat copy + 一次混音，
    # 把 21min 长片的失败域从整段降到单段，并可断点续跑。
    segment_max_sec: float = 90.0   # 单段最长秒数（在镜头边界切，不切镜中）
    # v2 production 使用 PIL 逐帧字幕/调色，单帧成本高于 legacy compose；
    # 单独收窄段长，确保默认 300s 的段级 watchdog 能收口。
    v2_segment_max_sec: float = 45.0
    segment_min_frames: int = 2500  # 全片预期帧数 < 此值仍走旧单段路径（保短片行为不变）


class VRAMConfig(BaseModel):
    reported_total_GB: float
    reported_free_GB: float
    measured_usable_GB: float
    unallocatable_GB: float
    cause: str
    rule: str


class RuntimeConfig(BaseModel):
    python_venv: str
    ffmpeg: str
    vram: VRAMConfig
    nvidia_smi_available: bool


# ---------------------------------------------------------------------------
# 总 Settings
# ---------------------------------------------------------------------------
class Settings(BaseSettings):
    """
    总配置。从 .env 读密钥，从 config.yaml 读结构。
    渠道池在 init 时通过 discover_channels() 动态构建。
    """
    # C4：不再让 pydantic 去读 .env。Settings 的 8 个 env 字段在 load() 里
    # 全部由显式 kwargs 传入，**init kwargs 优先级高于 env_file / 环境变量**，
    # 所以这里的 env_file 从来就是死读取——它唯一的作用是让人以为改 .env 会
    # 同时影响两条路径，而实际上渠道池是另一份 load_env() 算出来的。
    # 收敛后：.env 只有 load_env() 一个读者，env_get() 一个取值口。
    model_config = SettingsConfigDict(extra="ignore")

    # ---- 来自 .env ----
    image_model: str = "gpt-image-2"
    image_size: str = "1080x1920"
    image_quality: str = "low"
    gpt_image_2_api_base: str = "https://subrouter.ai"
    video_model: str = "gpt-image-2"
    video_endpoint: str = "/v1/videos"
    video_timeout_s: int = 900
    video_poll_interval_s: int = 10

    @field_validator("gpt_image_2_api_base", mode="before")
    @classmethod
    def _norm_base(cls, v):
        return str(v).rstrip("/") if v else v

    # ---- 来自 config.yaml ----
    profile_name: str = "ancient-realistic"
    profiles: dict[str, StyleProfile] = Field(default_factory=dict)
    image: ImageConfig
    video: VideoConfig
    compose: ComposeConfig
    runtime: RuntimeConfig

    # ---- 渠道池（动态）----
    channels: list[Channel] = Field(default_factory=list)

    @classmethod
    def load(cls) -> "Settings":
        env = load_env(ENV_PATH)
        raw = yaml.safe_load((CONFIG_DIR / "config.yaml").read_text(encoding="utf-8"))

        # .env 字段（用 alias-style 取值，兼容 Pydantic 的 env 命名转换）
        env_image_model = env.get("IMAGE_MODEL", "gpt-image-2")
        env_image_size = env.get("IMAGE_SIZE", "1080x1920")
        env_image_quality = env.get("IMAGE_QUALITY", "low")
        env_base = env.get(f"{env_image_model}_API_BASE", "https://subrouter.ai").rstrip("/")
        env_video_model = env.get("VIDEO_MODEL", env_image_model)
        env_video_endpoint = env.get("VIDEO_ENDPOINT", "/v1/videos")
        env_video_timeout = int(env.get("VIDEO_TIMEOUT_S", "900"))
        env_video_poll = int(env.get("VIDEO_POLL_INTERVAL_S", "10"))

        profiles = {k: StyleProfile(**v) for k, v in raw.get("profiles", {}).items()}
        profile_name = raw.get("default_profile", "ancient-realistic")

        channels = (configured_channels(env, raw["image_channels"])
                    if "image_channels" in raw else discover_channels(env, env_image_model))

        return cls(
            image_model=env_image_model,
            image_size=env_image_size,
            image_quality=env_image_quality,
            gpt_image_2_api_base=env_base,
            video_model=env_video_model,
            video_endpoint=env_video_endpoint,
            video_timeout_s=env_video_timeout,
            video_poll_interval_s=env_video_poll,
            profile_name=profile_name,
            profiles=profiles,
            image=ImageConfig(**raw["image"]),
            video=VideoConfig(**raw["video"]),
            compose=ComposeConfig(**raw["compose"]),
            runtime=RuntimeConfig(**raw["runtime"]),
            channels=channels,
        )

    def get_profile(self) -> StyleProfile:
        return self.profiles[self.profile_name]

    def get_image_size_requested(self) -> tuple[int, int]:
        """请求的 size（仅用于长宽比）。"""
        w, h = self.image_size.split("x")
        return int(w), int(h)

    def predict_actual_image_dims(self) -> tuple[int, int]:
        """预测 subrouter 返回图的真实像素。

        公式与真源（SUBROUTER_PIXEL_CAP / predict_actual_dims）都在
        app/services/spec.py —— H 批收口：此前这里有一份 MP_FIXED = 1024*1536
        和一份照抄的公式，与 app/models/relay.py 的 SUBROUTER_PIXEL_CAP
        互为副本，改一处忘一处就会让「hv config 打印的尺寸」和「relay
        按尺寸算配额」对不上。
        """
        from app.services.spec import predict_actual_dims
        w, h = self.get_image_size_requested()
        return predict_actual_dims(w, h)

    def predict_upscaled_dims(self) -> tuple[int, int]:
        """超分后尺寸 = 实测像素 × scale。"""
        w, h = self.predict_actual_image_dims()
        scale = self.image.postprocess_upscale.scale
        return w * scale, h * scale


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------
_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.load()
    return _settings


# 方便命令行调试
if __name__ == "__main__":
    s = get_settings()
    print("=" * 60)
    print(f"profile      : {s.profile_name}")
    print(f"image model  : {s.image_model}")
    print(f"image size   : {s.image_size}  (请求)")
    print(f"actual dims  : {s.predict_actual_image_dims()}  (subrouter 预测)")
    print(f"upscaled     : {s.predict_upscaled_dims()}")
    print(f"channels     : {[c.name for c in s.channels]}")
    print(f"video        : {s.video.primary.engine} ({s.video.primary.endpoint})")
    print(f"vram         : measured={s.runtime.vram.measured_usable_GB}GB")
