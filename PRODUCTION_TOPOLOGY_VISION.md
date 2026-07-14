# KT6 生产拓扑图片识别接入

## 1. 输出不是浏览器 DOM

PNG/JPEG/WebP 在浏览器 DOM 中只会表现为一个 `<img>` 或 `<canvas>` 节点，图片内部的设备和连线不是原生 DOM。KT6 的生产识别结果采用两层表示：

- `elements + relations`：无损 Scene Graph，保留多父节点、环、并行边和非树关系。
- `semantic_tree`：DOM-like 只读投影，便于页面树、无障碍树或调试面板消费。

`semantic_tree` 只用业务 ID 引用子节点，不递归复制对象；无法放入树的边进入 `non_tree_relations`，不会被删除或补造。

## 2. 配置生产视觉服务

KT6 使用 `HTTPTopologyVisionAdapter` 调用生产视觉服务。服务未配置时，原行为保持不变，只保存截图并返回 `requires_vision_model=true`。

PowerShell 配置示例：

```powershell
$env:KT6_VISION_ENDPOINT = 'https://vision.example.com/v1/topology'
$env:KT6_VISION_API_KEY = '<production-secret>'
$env:KT6_VISION_TIMEOUT_SECONDS = '60'

python -m kt6_backend.app
```

配置规则：

- `KT6_VISION_ENDPOINT`：远程地址必须使用 HTTPS；仅 loopback 地址允许 HTTP。
- `KT6_VISION_API_KEY`：可选，通过 `Authorization: Bearer ...` 发送，不写入日志或异常。
- `KT6_VISION_TIMEOUT_SECONDS`：可选，默认 30 秒，范围 `(0, 300]`。
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

使用专用 CLI：

```powershell
python -m kt6_backend.topology_image_cli D:\data\topology.png `
  --api-base http://127.0.0.1:8787 `
  --source-id enterprise-v1 `
  --out D:\data\topology-result.json
```

CLI 会：

- 校验 PNG/JPEG/WebP、真实图片宽高、1 亿像素和 5 MB 上限；
- 强制 `dom.elements=[]`、`adapter_scene=null`、无 topology text、单图片；
- 比较本地图片 SHA-256 与服务端 provenance；
- 输出节点、边、置信度、问题和 `semantic_tree`。

退出码：

- `0`：确认是真实像素识别，且至少识别到一个对象。
- `2`：未识别、Adapter 失败，或结果被 Renderer/DOM/Text 路径抢占。
- `3`：图片、参数或 HTTP 请求错误。

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

HTTP 视觉 Adapter 固定 `supports_actionable_grounding=false`。模型识别出的业务 ID 尚未与生产资产库独立核验，因此图片结果默认只用于分析，即使坐标和置信度都合法，也不能触发 GUI 或设备动作。

生产部署还应完成：

- 在 KT6 HTTP API 前增加认证、TLS、限流和审计网关。
- 为 `runtime_data/page_captures/` 配置访问权限、脱敏、TTL 和删除策略。
- 确认拓扑图片允许发送到所配置的视觉服务，满足数据外发要求。
- 接入资产库 exact binding 后，再设计单独的可执行 grounding 授权流程。

当前服务已限制 JSON 请求体为 32 MB；单图限制 5 MB，视觉响应默认限制 2 MB。
