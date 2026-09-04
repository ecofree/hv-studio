# 本地 CosyVoice2-0.5B 部署方案（RTX 2060 / 6GB）

> 状态：**方案待你拍板**。V-API 中转(api.gpt.ge)的 CosyVoice2 经你试听确认为"全是女声、音色库不可用"已排除。
> 本方案回答你上一轮的问题："没有其它本地方案了吗？" —— **有：CosyVoice2 官方 0.5B 轻量版**。
> 已在项目 `app/services/cosyvoice.py` 保留 OpenAI 兼容的整段合成模块，未来引擎本地化后只需替换实现层。

---

## 一、为什么本地方案可行（你的 2060 能跑）

关键证据（多方实测交叉验证）：

| 来源 | 结论 |
|---|---|
| **SynchroAvatar（真实项目）** | 明确写："6GB（RTX 2060）**勉强可跑**"，开 `FP16` 省 ~1GB |
| **GTX1660S 6GB 实测**（比 2060 弱） | INT8 + 流式 + CPU 卸载 → **3.9GB 稳定**，音质 4.3/5 |
| **官方模型** | `FunAudioLLM/CosyVoice2-0.5B`，Apache 开源，中文 MOS 5.53 |
| **你的环境** | hv venv 已有 **torch 2.14 + CUDA 12.6**，识别到 RTX 2060（capability 7.5）→ 环境已就绪 |

**关键架构判断**：与 Qwen3-TTS 在 Turing 上 FP16 会 NaN 不同，**CosyVoice2 实测可在 2060 上 FP16 跑**（SynchroAvatar 就是铁证）。2060 不是本地方案的死路。

---

## 二、本地方案的最大价值：解决"找不到合适男声"

你在 V-API 上遇到"全是女声"，本质是**中转站音色库缺失**。但 CosyVoice2 官方 preset 里有明确适合历史旁白的男声：

| preset 音色名 | 特质 | 场景 |
|---|---|---|
| `longshuo_v2` 龙硕 | 博才干练男 | 新闻播报 |
| `longsanshu` 龙三叔 | **沉稳质感男** | 有声书 ← 最贴历史旁白 |
| `longxiu_v2` 龙修 | 博才说书男 | 有声书 |
| `libai_v2` 李白 | 古代诗仙男 | 诗词朗诵 |
| `longcheng_v2` 龙橙 | 阳光男声 | 通用 |

**更强的：zero-shot 克隆**。你只要给 3-5 秒的沉稳男声样音，就能克隆出**理想的历史旁白男声**，全系列统一声线——**彻底摆脱"依赖某个中转站音色库对不对"**。

---

## 三、部署路径（Windows 原生，无需 WSL2）

> ⚠️ 有来源明确建议：**别用 WSL2 跑**，磁盘 IO 慢；直接 Windows 原生。

### 路线 A（推荐，最贴近官方）：独立 conda 环境

```bash
# 1. 装 Miniforge (Windows x86_64)，用 Miniforge Prompt 执行
# 2. 建独立环境(pynini 必须 conda-forge，pip 编不过)
conda create -n cosyvoice -y python=3.10 pynini=2.1.6 -c conda-forge
conda activate cosyvoice

# 3. 拉代码(必须 --recursive 拿子模块)
git clone --recursive https://github.com/FunAudioLLM/CosyVoice.git
cd CosyVoice
git submodule update --init --recursive

# 4. 装 CUDA 版 PyTorch(你 2060 用 cu121)
pip install torch==2.3.1 torchaudio==2.3.1 --index-url https://download.pytorch.org/whl/cu121

# 5. openai-whisper 需要固定老 setuptools(否则 pkg_resources 报错)
pip install "setuptools==80.9.0" wheel
pip install --no-build-isolation "openai-whisper==20231117"

# 6. 装其余依赖 + GPU onnxruntime
pip install -r requirements.txt
pip uninstall -y onnxruntime && pip install onnxruntime-gpu==1.18.0

# 7. 下载模型(国内走 ModelScope 快)
python -c "from modelscope import snapshot_download; snapshot_download('iic/CosyVoice2-0.5B', local_dir='pretrained_models/CosyVoice2-0.5B')"
```

### 路线 B（更轻）：Ollama 一键（若有 Ollama）
```bash
ollama run cosyvoice2   # 下载 Q4 量化版，给 HTTP 端点
```

### 2060 专属运行优化（关键）
```python
# 用 CosyVoice2 时
cosyvoice = CosyVoice2('pretrained_models/CosyVoice2-0.5B',
                       load_jit=True, load_onnx=False, load_trt=False)
# 长文本：切短句(≤50字/块) + 流式 + 关 ttsfrd 文本前端，防爆 6GB 显存
```

---

## 四、必须诚实提醒的取舍

| 维度 | 本地 0.5B | 云端官方百炼 |
|---|---|---|
| 成本 | 零（一次性装环境+下载） | ~¥45/30集 |
| 音质 | MOS 5.53（高，但低于云端满血） | 满血，preset 全、有情感 |
| 男声 | preset 全 + 可克隆 | preset 全 + 可克隆 + 情感指令 |
| 显存 | **与本地图像(AU)/视频抢 6GB，须串行** | 零占用 |
| 环境 | 需装 conda + 下载几 GB 模型 | 只需 API key |
| 生成速度 | 2060 较慢 | 云端快 |

**核心权衡**：你的 6GB 同时要跑本地图像生成(AU)和视频合成。**TTS 若也本地跑，会拖慢整条流水线**（必须等图像跑完才轮到 TTS）。这正是"本地 TTS"在这个项目里的**结构性成本**。

---

## 五、建议的决策（供你参考）

**如果**：你追求"纯离线、零边际成本、能克隆任意男声、不介意装环境 + 流水线串行慢一些" → **选本地 A**。

**如果**：你更看重"整条视频流水线快、TTS 不占用宝贵的 6GB、音质上限更高" → **云端官方百炼**（V-API 失败只是中转问题，官方百炼没这问题，且 ~¥45/30集几乎可忽略）。

**折中（我倾向）**：**云端官方百炼 CosyVoice2 做主 TTS**（快 + 不抢显存 + preset 全），**本地 CosyVoice2 留作"克隆专属男声人设"的兜底/离线备胎**。先花 ¥45 级成本把视频流水跑通，若对音质/成本不满再切本地。

---

## 待你决定
1. 走 **A 本地 / B 云端官方百炼 / C 折中(云主本备)**？
2. 若走云端：你能否去 [阿里云百炼](https://bailian.console.aliyun.com/) 免费开通拿一个 API Key？
3. 若走本地：需要确认你机器**剩余磁盘空间**（模型+环境约需 20GB）再开工。
