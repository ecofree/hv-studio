"""
人脸检测 + 特征提取封装（M2 实装：insightface buffalo_l / CPU）。

底层能力供 character_lock 使用：
  - detect_faces(img_path) -> list[Face]  检测一张图里所有人脸
  - embed(img_path)        -> np.ndarray  图中最大/最清晰人脸 → 512d embed
  - cosine(a, b)           -> float       两个 embed 余弦相似度（ArcFace 用）
  - detect_and_embed(img)  -> (bbox, embed|None)

设计要点
────────
1. 单例 + 惰性加载：insightface 首次加载 buffalo_l（det + rec 约 150MB）
   需要 1~2s，之后复用。用模块级 `_app` 缓存，进程内只 load 一次。
2. CPU provider：本机 RTX 2060 6GB 但 commit 内存紧张（M1 已踩坑），
   onnxruntime CUDA provider 与现有 torch cu126 版本要匹配且占显存；
   M2 先用 CPU（det 640×640 单张 <100ms 可接受），M3 若慢再上 CUDA。
3. 检测到多人时返回面积最大的脸（画面主人物）。
4. ArcFace 的 embed 是 L2 归一化后的 512 维，余弦相似度即点积。

安装（Windows py3.13，踩过坑，记录在案）：
  pip install --ignore-installed --no-deps onnxruntime onnx easydict
      tqdm opencv-python-headless protobuf ml_dtypes scikit-image scipy
      lazy_loader albumentations albucore stringzilla numkong
  再装第三方预编译 wheel：insightface-0.7.3-cp313-cp313-win_amd64.whl
  并给 insightface/app/mask_renderer.py 打补丁：把 albumentations / face3d
  / data 改为可选惰性 import（缺失置 None），因为该项目只用 FaceAnalysis
  （det+rec），从不调用 MaskRenderer/MaskAugmentation。
  详见 .workbuddy/memory/2026-09-03.md
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# insightface 会缓存模型到 ~/.insightface，显式设置便于管理
os.environ.setdefault(
    "INSIGHTFACE_HOME",
    os.path.join(os.path.expanduser("~"), ".insightface"),
)

_app = None          # FaceAnalysis 单例
_det_size = (640, 640)


@dataclass
class FaceBox:
    """一张检测到的人脸。bbox 像素坐标在原始图上。"""
    x0: int
    y0: int
    x1: int
    y1: int
    conf: float
    embedding: np.ndarray | None  # 512d L2 归一化（recognition 模型输出）
    age: int | None = None
    gender: str | None = None


def _get_app():
    global _app
    if _app is None:
        from insightface.app import FaceAnalysis
        _app = FaceAnalysis(
            name="buffalo_l", root=os.environ["INSIGHTFACE_HOME"],
            providers=["CPUExecutionProvider"],
        )
        _app.prepare(ctx_id=0, det_size=_det_size)
    return _app


def _norm_embed(emb: np.ndarray) -> np.ndarray:
    """ArcFace 输出已 L2 归一化，这里兜底再归一一次。"""
    n = np.linalg.norm(emb)
    return emb / n if n > 0 else emb


def detect_faces(img_path: str | Path, to_rgb: bool = True) -> list[FaceBox]:
    """检测一张图的所有人脸。无模型/无法识别时返回空列表。"""
    from insightface.utils import face_align
    app = _get_app()
    import cv2

    img = cv2.imread(str(img_path))
    if img is None:
        raise ValueError(f"无法读取图片: {img_path}")
    faces = app.get(img)
    out: list[FaceBox] = []
    for f in faces:
        x0, y0, x1, y1 = (int(v) for v in f.bbox)
        out.append(FaceBox(
            x0=x0, y0=y0, x1=x1, y1=y1,
            conf=float(f.det_score) if f.det_score is not None else 1.0,
            embedding=_norm_embed(f.normed_embedding) if f.normed_embedding is not None else None,
            age=int(f.age) if f.age is not None else None,
            gender=f.gender if f.gender else None,
        ))
    return out


def embed_face(img_path: str | Path) -> tuple[np.ndarray | None, dict | None]:
    """
    返回 (embed, meta)。取面积最大的脸（画面主人物）。
    embed 为 512d L2 归一化；无人脸返回 (None, None)。
    """
    faces = detect_faces(img_path)
    if not faces:
        return None, None
    # 面积最大的脸
    best = max(faces, key=lambda fb: (fb.x1 - fb.x0) * (fb.y1 - fb.y0))
    if best.embedding is None:
        return None, {"x0": best.x0, "y0": best.y0, "x1": best.x1, "y1": best.y1}
    return best.embedding, {
        "x0": best.x0, "y0": best.y0, "x1": best.x1, "y1": best.y1,
        "conf": best.conf, "age": best.age, "gender": best.gender,
    }


def embed_faces_multi(img_path: str | Path) -> list[tuple[np.ndarray, dict]]:
    """返回图中所有人脸 (embed, meta) 列表。"""
    res = []
    for fb in detect_faces(img_path):
        if fb.embedding is not None:
            res.append((fb.embedding, {
                "x0": fb.x0, "y0": fb.y0, "x1": fb.x1, "y1": fb.y1,
                "conf": fb.conf, "age": fb.age, "gender": fb.gender,
            }))
    return res


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """两个归一化 embed 的余弦相似度。ArcFace 惯例直接点积即可。"""
    a = _norm_embed(a)
    b = _norm_embed(b)
    return float(np.dot(a, b))


def face_count(img_path: str | Path) -> int:
    return len(detect_faces(img_path))
