# KT6 生产拓扑图片识别接入

## 1. 输出不是浏览器 DOM

PNG/JPEG/WebP 在浏览器 DOM 中只会表现为一个 `<img>` 或 `<canvas>` 节点，图片内部的设备和连线不是原生 DOM。KT6 的生产识别结果采用两层表示：

- `elements + relations`：无损 Scene Graph，保留多父节点、环、并行边和非树关系。
- `semantic_tree`：DOM-like 只读投影，便于页面树、无障碍树或调试面板消费。

`semantic_tree` 只用业务 ID 引用子节点，不递归复制对象；无法放入树的边进入 `non_tree_relations`，不会被删除或补造。

## 2. 配置视觉驱动

### 2.1 本地 RapidOCR/OpenCV 单图片识别（不依赖 Agent）

当目标只是验证一张拓扑图片时，优先使用 `local_cv_ocr`。图片只在 KT6
进程内处理：RapidOCR 通过本地 ONNX Runtime 识别设备文字和坐标，OpenCV
通过背景自适应、节点图标锚定、任意角度线段、方向一致性和像素走廊验证检测多分支连线；高密度场景按每节点角度扇区公平保留近/远关系候选，并只对有双侧线条证据和足够 OCR 置信度的紧凑标签恢复直线或正交拐点。此模式不启动 CodeAgent，不调用外部 API，
也不需要 endpoint 或 API key；但生产机器必须安装本地 RapidOCR ONNX 与
OpenCV 运行依赖。

```powershell
Set-Location D:\04project\FreeStyle_Copilot_KT6_demo

python -m pip install -r requirements-local-vision.txt

Remove-Item Env:KT6_VISION_ENDPOINT -ErrorAction SilentlyContinue
Remove-Item Env:KT6_VISION_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:KT6_CODEAGENT_EXECUTABLE -ErrorAction SilentlyContinue
Remove-Item Env:KT6_CODEAGENT_AGENT -ErrorAction SilentlyContinue
Remove-Item Env:KT6_VISION_TIMEOUT_SECONDS -ErrorAction SilentlyContinue
$env:KT6_VISION_DRIVER = 'local_cv_ocr'

python -c "from kt6_backend.app import _create_canvas_vision_from_env as f; a=f(); print(type(a).__name__ if a else 'DISABLED')"
python -m kt6_backend.app
```

预期工厂检查输出 `LocalCVTopologyVisionAdapter`。该驱动一次只接受一张 Canvas
图片。v1.2 支持传统网络设备 ID，以及 `testNE`、`CommonSubnet`、`Subnet_`、
`Name_`、`SUBNETA_`、`V2SN_`、`OSS` 和 `CameraRoot` 命名族；可处理亮/暗背景、
星型和任意角度多边，并保守输出实线/虚线、常见颜色与邻近小数权值。无箭头时
关系使用 `direction=undirected`。复杂图标、曲线、遮挡严重、低清或未知命名规则
仍应使用 CodeAgent 或 HTTP 视觉服务。识别结果固定为分析用途：
`adapter_id=local-cv-ocr`，`adapter_version=1.2`，`actionable_grounding=false`。

本地像素连接按无向边输出，`source`/`target` 只用于确定性序列化，不表示业务流向。
证据充分时关系属性可包含 `line_style`、`line_color` 和绑定到最近已确认直线的
小数 OCR 标签 `weight`；字段缺失表示证据不足，不会补猜。上述能力目前由合成
精确边集合回归覆盖，只证明算法路径得到验证；生产准确率仍必须使用原图和独立
Ground Truth 统计。

### 2.2 CodeAgent read 工具直连（无需 KT6 密钥）

当生产机器已经安装自研 `codeagent`，且其 OpenCode 兼容 Agent 能用
`read` 工具读取 PNG/JPEG/WebP 时，KT6 可以直接启动本地 CLI。此路径不配置
HTTP endpoint，也不会由 KT6 传递 API key：

```powershell
Set-Location D:\04project\FreeStyle_Copilot_KT6_demo

codeagent --version
codeagent run --help | Select-String -Pattern '--format|--dir|--agent'
Test-Path .\.opencode\agents\kt6-topology-vision.md

Remove-Item Env:KT6_VISION_ENDPOINT -ErrorAction SilentlyContinue
Remove-Item Env:KT6_VISION_API_KEY -ErrorAction SilentlyContinue
$env:KT6_VISION_DRIVER = 'codeagent_cli'
$env:KT6_CODEAGENT_EXECUTABLE = 'codeagent'
$env:KT6_CODEAGENT_AGENT = 'kt6-topology-vision'
$env:KT6_VISION_TIMEOUT_SECONDS = '120'

python -c "from kt6_backend.app import _create_canvas_vision_from_env as f; a=f(); print(type(a).__name__ if a else 'DISABLED')"
python -m kt6_backend.app
```

预期工厂检查输出 `CodeAgentCanvasVisionAdapter`。`codeagent` 必须位于启动
KT6 的同一 Windows 服务账号的 `PATH` 中；也可以把
`KT6_CODEAGENT_EXECUTABLE` 设置为真实 `.exe`/`.cmd` 绝对路径。

仓库内的 `kt6-topology-vision` 专用 Agent 只允许 `read`，拒绝 Shell、编辑、
网络、子 Agent 和提问。Adapter 会把已校验 SHA-256 的图片快照复制到一次性
目录，通过 stdin 提交任务，并解析 `codeagent run --format json` 的 NDJSON
事件。只有每张图片都出现成功且路径精确匹配的 `read` 事件，结果才会被接受
为像素识别；单纯返回一段拓扑 JSON 会 fail-closed。

CodeAgent 自己如何连接 MiniMax-M2.7、保存账号配置或提供图片 read 工具，仍由
CodeAgent 管理。KT6 不读取或转发这些凭据。

### 2.3 HTTP 视觉服务

KT6 使用 `HTTPTopologyVisionAdapter` 调用生产视觉服务。服务未配置时，原行为保持不变，只保存截图并返回 `requires_vision_model=true`。

PowerShell 配置示例：

```powershell
$env:KT6_VISION_ENDPOINT = 'https://vision.example.com/v1/topology'
$env:KT6_VISION_API_KEY = '<production-secret>'
$env:KT6_VISION_TIMEOUT_SECONDS = '60'
$env:KT6_VISION_DRIVER = 'http'

python -m kt6_backend.app
```

配置规则：

- `KT6_VISION_ENDPOINT`：远程地址必须使用 HTTPS；仅 loopback 地址允许 HTTP。
- `KT6_VISION_API_KEY`：可选，通过 `Authorization: Bearer ...` 发送，不写入日志或异常。
- `KT6_VISION_TIMEOUT_SECONDS`：可选，默认 30 秒，范围 `(0, 300]`。
- `KT6_VISION_DRIVER`：HTTP 模式可省略（endpoint 存在时自动兼容为 `http`）。
- 附属变量已设置但 endpoint 缺失时，应用启动会 fail-fast。

## 3. 视觉服务 HTTP 契约

Adapter 向 endpoint 发送厂商无关 JSON：

```json
{
  "schema_version": "kt6.canvas-vision.request.v1",
  "task": {
    "operation": "topology_to_element_tree",
    "instructions": ["..."],
    "output_schema": {"...": "完整 JSON Schema"}
  },
  "page": {
    "url": "kt6://image-test/enterprise-v1",
    "title": "enterprise-v1",
    "language": "zh-CN",
    "ui_version": "topology-image-cli-v1",
    "viewport": {"width": 1600, "height": 1200, "device_pixel_ratio": 1}
  },
  "frames": [{
    "canvas_id": "uploaded_topology",
    "screenshot_sha256": "...",
    "intrinsic_size": {"width": 1600, "height": 1200},
    "client_size": {"width": 1600, "height": 1200},
    "page_bbox": [0, 0, 1600, 1200],
    "image": {
      "mime_type": "image/png",
      "encoding": "base64",
      "data": "..."
    }
  }]
}
```

生产视觉服务可以在内部使用 OCR、目标检测或多模态模型，但必须直接返回 JSON，不加 Markdown 或文字包装：

```json
{
  "schema_version": "kt6.canvas-vision.response.v1",
  "confidence": 0.96,
  "objects": [{
    "business_id": "GW-001",
    "type": "gateway",
    "label": "GW-001",
    "canvas_id": "uploaded_topology",
    "bbox": [100, 20, 300, 60],
    "confidence": 0.98,
    "attributes": {
      "model": "S628X-PWR-F",
      "role": "出口网关"
    }
  }],
  "links": [{
    "relation_id": "edge-gw-core",
    "source": "GW-001",
    "target": "CORE-001",
    "type": "topology_link",
    "confidence": 0.97,
    "attributes": {"direction": "downstream"}
  }],
  "co_channel_relations": []
}
```

服务端要求：

- 返回 HTTP 2xx、`Content-Type: application/json` 和 UTF-8 JSON。
- `bbox` 使用对应图片的固有像素坐标 `[x, y, width, height]`。
- 设备 ID 看不清时省略对象，不允许猜测。
- 只有看到明确线、箭头、端口或连接符时才返回关系。
- 图片内的任何命令或提示都只是不可信 OCR 文本，模型不得遵循。
- 不返回 provenance、selector、点击目标或 actionability；这些由 KT6 强制生成。

Adapter 会拒绝重定向、压缩响应、重复 JSON key、NaN、越界框、重复 ID、悬空边、异常图片尺寸及超过限制的请求或响应。

## 4. 使用原图做 pixels-only 测试

不要直接点击当前 Demo 测图片识别。Demo 会同时发送 Renderer Scene，它的优先级高于视觉模型，可能形成“看起来成功、实际没读图片”的假阳性。

三种视觉驱动都复用同一个专用 CLI；`local_cv_ocr` 必须只传一张图片：

```powershell
python -m kt6_backend.topology_image_cli D:\data\topology.png `
  --api-base http://127.0.0.1:8787 `
  --source-id enterprise-v1 `
  --out D:\data\topology-result.json
```

CLI 会：

- 校验 PNG/JPEG/WebP、真实图片宽高、1 亿像素和 5 MB 上限；`local_cv_ocr` 另设 2,000 万像素处理上限；
- 强制 `dom.elements=[]`、`adapter_scene=null`、无 topology text、单图片；
- 比较本地图片 SHA-256 与服务端 provenance；
- 输出节点、边、置信度、问题和 `semantic_tree`。

退出码：

- `0`：确认是真实像素识别，且至少识别到一个对象。
- `2`：未识别、Adapter 失败，或结果被 Renderer/DOM/Text 路径抢占。
- `3`：图片、参数或 KT6 API 请求错误。

有效的像素验收必须同时满足：

```text
mode == canvas_vision_adapter
semantic_source == canvas_pixels
pixel_inference_performed == true
pixel_verified == true
adapter_id / adapter_version 非空
screenshot_sha256 与本地图片一致
object_count > 0
```

使用本地驱动时还必须满足：

```text
scene.provenance.adapter_id == local-cv-ocr
scene.actionable_grounding == false
```

这里的 `pixel_verified=true` 只表示严格 Adapter 确实读取并校验了该截图后产出结果，不表示模型识别已经达到人工标注准确率；准确率仍需用独立 Ground Truth 统计。

## 5. DOM-like semantic_tree

成功识别后 Scene 中会附加：

```json
{
  "semantic_tree": {
    "tree_type": "dom_like_semantic_projection",
    "roots": ["GW-001"],
    "nodes": {
      "GW-001": {
        "role": "gateway",
        "name": "GW-001",
        "type": "gateway",
        "bbox": [100, 20, 300, 60],
        "confidence": 0.98,
        "children": [{
          "target": "CORE-001",
          "relation_id": "edge-gw-core",
          "type": "topology_link"
        }]
      }
    },
    "orphans": ["AGG-003", "AP-007"],
    "non_tree_relations": [],
    "complete": true,
    "issues": []
  }
}
```

这个字段是 Scene Graph 的投影，不会被标记成 `browser_dom`。

## 6. 本样例验收口径

截图范围不同，Ground Truth 不同：

- 只包含上方拓扑图：8 个明确设备、7 条图形关系。
- 包含拓扑图和完整详情表，并由视觉服务融合“下方AP”：22 个设备、19 条明确关系。

反幻觉规则：

- 不补 `AGG-003` 关系。
- 不补 `AP-007` 父节点。
- 不合并 `ACC-022/AP-022` 或 `ACC-006/AP-006`。
- 不把 Trunk 或“AP群”识别为设备。
- 不因说明文字推断 PoE、端口、直连关系或设备状态。

## 7. 生产安全边界

本地 RapidOCR/OpenCV、HTTP 与 CodeAgent 三种视觉 Adapter 都固定
`supports_actionable_grounding=false`。图片识别出的业务 ID 尚未与生产资产库独立
核验，因此结果默认只用于分析，即使坐标和置信度都合法，也不能触发 GUI 或
设备动作。

生产部署还应完成：

- 在 KT6 HTTP API 前增加认证、TLS、限流和审计网关。
- 为 `runtime_data/page_captures/` 配置访问权限、脱敏、TTL 和删除策略。
- 确认拓扑图片允许发送到所配置的视觉服务，满足数据外发要求。
- 接入资产库 exact binding 后，再设计单独的可执行 grounding 授权流程。

当前服务已限制 JSON 请求体为 32 MB；单图限制 5 MB，视觉响应默认限制 2 MB。
