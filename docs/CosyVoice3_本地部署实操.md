# CosyVoice3-0.5B 本地部署实操记录（RTX 2060 / 6GB）

> 状态：**部署进行中**。用户拍板首选本地 CosyVoice3-0.5B（V-API 中转不可用、不走云端百炼）。
> 本文档记录真实部署步骤与踩坑，供复现与回归。最终以 2060 FP16 实测是否 OOM 为成败标准。

---

## 〇、为什么是 CosyVoice3 而不是 CosyVoice2

| 维度 | CosyVoice3-0.5B | CosyVoice2-0.5B |
|---|---|---|
| 发布 | 2025-12-15 | 2024 |
| 参数 | 0.5B | 0.5B |
| 情绪控制 | **9 种，自然语言指令** ← 命中历史旁白"沉稳"需求 | 仅预设/克隆 |
| 方言 | 9 语种 + 18+ 中文方言 | 中英日粤等 |
| 克隆 | 3s 零样本 + 跨语种 | 有 |
| 文本前端 | wetext 兜底（Win 无需 ttsfrd） | 同 |
| 显存 | **~5.2GB 起步**（3090 实测基线） | 略低 |
| 开源 | Apache-2.0（仅 0.5B；论文 1.5B 未开源） | Apache-2.0 |

**2060 判断**：CosyVoice3 处于"贴边可试"（有 Win 6G 卡先例，须短句+关前端+FP16），但架构比 v2 更重（LLM Qwen2 + DiT flow + HiFT 多段推理），有实测报告称 3050-6GB 启动即 OOM。**必须实测，不可盲信"能跑"。**

---

## 一、环境准备（已完成）

### 1.1 环境真相
- 系统无 conda-in-PATH，但 **D:\anaconda 有 conda 24.11.3**
- hv 的 3.13 venv 不能跑 CosyVoice3（官方硬性要求 **Python 3.10**）

### 1.2 建 cosyvoice3 环境（conda）
```bash
# conda 配清华镜像
/d/anaconda/Scripts/conda.exe config --set report_errors false
/d/anaconda/Scripts/conda.exe config --add channels https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main/
/d/anaconda/Scripts/conda.exe config --add channels https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/free/
/d/anaconda/Scripts/conda.exe config --add channels https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge/
# 建环境
/d/anaconda/Scripts/conda.exe create -n cosyvoice3 -y python=3.10
# Python 解释器路径
/d/anaconda/envs/cosyvoice3/python.exe   # Python 3.10.21
```

### 1.3 部署目录
```
D:\hv\cosyvoice3\
  official\    # git clone --depth1 https://github.com/QwenAudio/CosyVoice.git
  models\      # 模型权重
  dl_model.py  # modelscope 下载脚本
  install_deps.sh  # 依赖安装（阿里云镜像）
  test_cosyvoice3.py  # 2060 实测脚本
```

---

## 二、下载模型（已完成，9.1GB）

```python
# dl_model.py
from modelscope import snapshot_download
snapshot_download('FunAudioLLM/Fun-CosyVoice3-0.5B-2512',
                  local_dir=r'D:\hv\cosyvoice3\models\Fun-CosyVoice3-0.5B-2512')
```

关键文件清单（**speech_tokenizer 是 v3**，与 v2 不同）：
| 文件 | 大小 | 用途 |
|---|---|---|
| llm.pt | 2.0G | LLM (Qwen2) 权重 |
| llm.rl.pt | 2.0G | RL 增强版（可选） |
| flow.pt | 1.3G | DiT flow 权重 |
| flow.decoder.estimator.fp32.onnx | 1.3G | DiT ONNX（可选加速） |
| hift.pt | 83M | HiFT 声码器 |
| campplus.onnx | 28M | 声纹特征提取 |
| speech_tokenizer_v3.onnx | 970M | **S3-tokenizer-v3** 语音分词 |

`configuration.json` 里配 `speech_tokenizer_v3`；`AutoModel` 按模型类型自动加载 v3。

---

## 三、安装依赖（进行中）

核心依赖已摸清（官方 requirements.txt）：
- **torch==2.3.1 + torchaudio==2.3.1**（cu121，2060 支持 CUDA 12.1）
- **transformers==4.51.3**（版本错 → 音频乱码/噪音，铁坑）
- numpy==1.26.4（onnxruntime 1.18 不兼容 numpy 2.x）
- HyperPyYAML, omegaconf, hydra-core（config 解析）
- conformer, diffusers, x-transformers（模型结构）
- soundfile, librosa, pyworld（音频）
- onnxruntime==1.18.0（Win 用 CPU 版；campplus+speech_tokenizer 辅助）
- wetext（文本前端，替代 Linux-only 的 ttsfrd）
- modelscope, gdown, wget, protobuf, pyarrow, pydantic, rich, inflect, networkx, matplotlib, lightning, tensorboard, fastapi, uvicorn, grpcio 等

**Windows 关键点**：
1. torch cu121 只从 pytorch.org 官方源，国内慢（~1.2MB/s，2.5G 需 ~30min）
2. openai-whisper 需固定老 setuptools 且最后装（本项目跑纯推理暂可不装）
3. ttsfrd 是 Linux-only wheel，Win import 失败自动 fallback wetext → 无需 pynini

`install_deps.sh` 用阿里云镜像装除 torch 外的所有包。

---

## 四、2060 实测脚本

```bash
/d/anaconda/envs/cosyvoice3/python.exe D:/hv/cosyvoice3/test_cosyvoice3.py \
  --prompt-wav <男声参考.wav> --prompt-text "参考音频内容..."
```

脚本要点（test_cosyvoice3.py）：
- `AutoModel(model_dir, fp16=True)` 开 FP16 省显存
- `inference_zero_shot(text, prompt_text, prompt_wav, stream=False, text_frontend=False)`
  - `text_frontend=False` 关文本前端省显存（Win 6G 卡关键）
  - `stream=False` 一次性整段
- `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` 缓解假性 OOM
- 打印 peak vram + 输出时长

---

## 五、成败判断与后续

- **成功**（FP16 不 OOM 且音质可听）→ 定男声参考音 → 接入 hv 流水线（替换 per-frame edge-tts，见 `app/services/cosyvoice.py` 的整段合成架构）
- **OOM** → 依次尝试：短句切分 → 关 ttsfrd/前端 → 降流式 chunk → 若仍 OOM 则退回 CosyVoice2-0.5B（2060 社区证据更充分）

## 参考音素材问题
CosyVoice3 zero_shot 克隆需 3-10s 清晰参考音频 + 对应文字。项目目前无理想"沉稳男声历史旁白"样音。
待跑通流程后需用户提供一段男声样音（有声书/纪录片男声），或用 preset（CosyVoice2 系的 longshuo 龙硕/longsanshu 龙三叔等可作参考来源）。
