"""
超分服务（M1 实装：PIL Lanczos 2x；M2 升级 RealESRGAN）。

必要性：
  - gpt-image-2 实际产出 940×1672（请求 1080×1920），小于 1080p 输出
  - 需放大到 1880×3344 提供 Ken Burns 裁切余量 (800, 1424)

M1：PIL Image.resize(LANCZOS) 2x
  - 零依赖、快、够做运镜余量；但质感不如 RealESRGAN
  - 定位：M1 竖切片先打通"运镜余量"验证，M2 换真超分
M2：RealESRGAN_x2plus（tile=512，6GB 显存上限）
  - basicsr 编译问题待解决（pip 从 GitHub 源装 / 换 cupy 后端）

接口保持 upscale(src, dst) -> dst，便于 M1→M2 无缝切换。
"""
from __future__ import annotations
from app.services.provenance import file_sha256, atomic_json
import json

import os

from pathlib import Path

# C5（2026-09-05）：此处原有一句 `warnings.filterwarnings("ignore")`。
# 它是**进程级**的：任何 import 到本模块的调用方（也就是整条出片链）都会被
# 连坐，把项目自己的 DeprecationWarning / RuntimeWarning 一起吞掉。
# 删掉后若有第三方库噪音，应当用 `with warnings.catch_warnings():` 局部包裹，
# 而不是全局静音——静音范围必须等于问题范围。

# 单一真源：engine 由 config.yaml image.postprocess_upscale.engine 决定。
# 实装仅 pil_lanczos（零依赖、快、够做运镜余量）；RealESRGAN_x2plus 为 M2
# 升级（basicsr 编译问题未解决），config 不应声明为已启用。此处保留常量
# 作为兜底（config 读取失败时退回 pil_lanczos），避免与 config 双头漂移。
_DEFAULT_ENGINE = "pil_lanczos"


def _engine() -> str:
    try:
        from app.config.config import get_settings
        eng = get_settings().image.postprocess_upscale.engine
        return eng or _DEFAULT_ENGINE
    except Exception:
        return _DEFAULT_ENGINE


def upscale(src: str | Path, dst: str | Path | None = None) -> Path:
    """
    将 src 图片放大 2 倍，写 dst。
    dst 缺省时写到 src 同目录的 {stem}@2x{ext}。
    返回放大后图片路径。

    健壮性（2026-09-04 修）：
      合成中途被中断/内存不足时，img.save() 可能留下【半截文件】。
      下游 Image.open() 读到截断图会返回错误尺寸的 crop 结果，使
      frame.tobytes() 长度偏离 W*H*3，整个 rawvideo 管道从此逐帧错位，
      最终产出 NAL 全损、时长虚高的损坏 mp4（且 ffmpeg 仍返回 rc=0）。

      故此处三道保险：
        1) 已存在且校验通过 → 直接复用，不重写（也省一次昂贵 Lanczos）
        2) 原子写：先写 .tmp 再 os.replace，杜绝半截文件
        3) 写后回读校验，失败即删临时文件并抛错，绝不留坏缓存
    """
    src_p = Path(src)
    if dst is None:
        dst_p = src_p.with_name(f"{src_p.stem}@2x{src_p.suffix}")
    else:
        dst_p = Path(dst)
    dst_p.parent.mkdir(parents=True, exist_ok=True)

    engine = _engine()
    if engine == "pil_lanczos":
        from PIL import Image

        # 1) 复用：已存在且能被完整解码，且尺寸确为源图 2 倍，
        #   且源图未被重新生成（P0-2 修复）。
        #   —— 失效检测：若 @2x 的 mtime < 源图 mtime，说明源图在 @2x 之后被
        #      重新生成（重跑 m2_scenes / hanwu_new_scenes 等），@2x 内容已过期，
        #      必须重做，否则静默复用旧画面。尺寸相同但内容变（同尺寸重生成）
        #      也能被 mtime 捕获——mtime 不是内容指纹，但对本仓「重跑生图脚本
        #      覆盖源图」的变更模式已足够（超分是廉价 Lanczos，误判只多算一次）。
        if dst_p.exists():
            try:
                with Image.open(src_p) as _s, Image.open(dst_p) as _d:
                    sw, sh = _s.size
                    _d.load()
                    if _d.size == (sw * 2, sh * 2):
                        dst_mt = dst_p.stat().st_mtime_ns
                        src_mt = src_p.stat().st_mtime_ns
                        identity=dst_p.with_suffix(dst_p.suffix+".source.json")
                        if identity.exists():
                            record=json.loads(identity.read_text(encoding="utf-8"))
                            if record.get("source_sha256")==file_sha256(src_p) and record.get("output_sha256")==file_sha256(dst_p):
                                return dst_p
            except Exception:
                pass  # 坏缓存 → 落到下面重新生成

        with Image.open(src_p) as im:
            w, h = im.size
            img2 = im.resize((w * 2, h * 2), Image.LANCZOS)

        # 2) 原子写（临时文件必须保留 .png 后缀，否则 PIL 无法推断格式）
        tmp_p = dst_p.with_name(f"{dst_p.stem}.tmp{dst_p.suffix}")
        img2.save(tmp_p)

        # 3) 回读校验
        try:
            with Image.open(tmp_p) as chk:
                chk.load()
                if chk.size != (w * 2, h * 2):
                    raise ValueError(f"超分尺寸异常 {chk.size}")
        except Exception:
            try:
                tmp_p.unlink()
            except Exception:
                pass
            raise
        os.replace(tmp_p, dst_p)
        atomic_json(dst_p.with_suffix(dst_p.suffix+".source.json"),
                    {"source_sha256":file_sha256(src_p),"output_sha256":file_sha256(dst_p),"engine":engine})
        return dst_p
    elif engine == "realesrgan":
        raise NotImplementedError(
            "M2: RealESRGAN_x2plus（basicsr 编译问题待解决）"
        )
    else:
        raise ValueError(f"未知 engine: {engine}（config image.postprocess_upscale.engine）")
