# OSS 方案对比基准

目标系统无法把测试截图外传，但可以接收外部数据，因此对比在测试环境内完成：

1. 在测试环境部署 OmniParser server（权重从 Hugging Face 下载，环境可接收）；
2. 把本分支代码推到测试环境；
3. 用同一页面分别跑基线模式和 OmniParser 模式各采集一次；
4. 导出两次 capture JSON，用 `compare_vision.py` 对比指标；
5. 人工核对节点/连线/坐标真值后填表。

## 试点一：视觉识别（当前 OpenCV+GLM vs 官方 OmniParser + SoM）

两个模式通过环境变量切换，不需要改代码、不需要外传图片。

### OmniParser server 部署

官方仓库 `microsoft/OmniParser`（`omnitool/omniparserserver`），权重约 1–2 GB：

```bash
cd omnitool/omniparserserver
python -m omniparserserver --device cuda --caption_model_path ../../weights/icon_caption_florence
# 默认 http://127.0.0.1:8000/parse/
```

无 GPU 时用 `--device cpu`，单张截图约 2–5 秒；GPU 约 0.3–0.8 秒。

### 模式与环境变量

| 模式 | 环境变量 | 说明 |
|------|----------|------|
| 基线 hybrid | `KT6_VISION_DRIVER=hybrid`，`KT6_HYBRID_MODEL_DRIVER=codeagent_cli`（或 http） | 现有流程：CV 结果作为上下文，GLM 直接看图 |
| OmniParser | `KT6_VISION_DRIVER=omniparser`，`KT6_OMNIPARSER_ENDPOINT=http://127.0.0.1:8000/parse/`，`KT6_HYBRID_MODEL_DRIVER=codeagent_cli`（或 http） | 先调 OmniParser 得到 SoM 标注图+结构化元素，再喂给 GLM |

可选的 `KT6_OMNIPARSER_TIMEOUT_SECONDS`（默认 120，上限 600）。

### 对比步骤

```powershell
# 基线
$env:KT6_VISION_DRIVER="hybrid"
$env:KT6_HYBRID_MODEL_DRIVER="codeagent_cli"
python -m kt6_backend.app

# 同一页面采集一次，记录 capture_id（保存为 baseline.json）
# 重启为 OmniParser 模式（先确保 server 已启动）
$env:KT6_VISION_DRIVER="omniparser"
$env:KT6_OMNIPARSER_ENDPOINT="http://127.0.0.1:8000/parse/"
python -m kt6_backend.app

# 同一页面再采集一次，记录 capture_id（保存为 omniparser.json）
python -m benchmark.compare_vision baseline.json omniparser.json
```

### 指标

| 指标 | 含义 |
|------|------|
| `selected_mode` / `scene_type` | 是否走视觉识别 |
| `objects` / `links` | 识别出的节点/关系数量，需人工核对真值 |
| `vision_cache_status` | 对比时两次都应为 `miss`（或都强制刷新），保证公平 |
| `omniparser.parsed` | OmniParser server 是否解析成功 |
| `omniparser.elements` / `texts` / `icons` | 解析出的元素总数与文本/图标数量 |
| `omniparser.model_ms` / `total_ms` | GLM 耗时与整次感知耗时 |

### 注意

- `KT6_OMNIPARSER_ENDPOINT` 必须显式配置：缺配置或 server 不可达时直接报错，不做静默降级，避免产出无效对比数据；
- OmniParser 返回的是通用 UI 元素（含导航/按钮），不是最终拓扑节点；识别结果由 GLM 决定，元素只作为 SoM 证据；
- 两个模式的结果都强制 analysis-only，不影响动作安全边界；
- 同一截图会生成 `runtime_data/omniparser_som/som_*.png`，可用于人工核对框与编号。

## 试点二（后续）：DOM vs Playwright AXTree

计划用 Chrome DevTools Protocol 的 `Accessibility.getFullAXTree` 生成可访问性快照，与现有 content-collector 的 DOM 候选对比“采集完整性 / 可操作性命中率 / 选择器稳定性”。等试点一跑完再实现。
