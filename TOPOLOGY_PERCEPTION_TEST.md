# KT6 拓扑界面感知测试

## 1. 本轮测试对象

本轮输入是带 Unicode 连线的拓扑文本、设备详情表和说明文字，不是原始 PNG/JPEG 截图。因此它验证的是：

```text
结构化文本 / 外部 OCR 转写
-> 设备与关系抽取
-> 多证据融合
-> 歧义与冲突保留
-> Scene Graph
```

它不能证明 OCR、图标检测或像素连线追踪的准确率。真实图片测试必须另行提供原图，并通过 `CanvasVisionAdapter` 读取已落盘的截图像素。

黄金样例位于 `tests/fixtures/enterprise_topology_ocr.txt`。

## 2. 严格 Ground Truth

| 指标 | 期望值 | 说明 |
|---|---:|---|
| 设备节点 | 22 | 1 GW、1 CORE、1 AGG、6 ACC、13 个 AP 前缀实体 |
| 明确关系 | 19 | 图中 7 条，详情表“下方AP”12 条 |
| 主连通分量节点 | 20 | GW、CORE、6 ACC、12 个下挂实体 |
| 连通分量 | 3 | 主分量、AGG-003、AP-007 |
| 孤立节点 | 2 | AGG-003、AP-007 |
| 无向环秩 | 0 | 当前证据中没有环 |
| 非设备视觉组 | 7 | 1 个 Trunk、6 个“AP群” |

硬性验收规则：

1. 不因架构说明自动补出 `CORE-001 -> AGG-003 -> ACC-*`；`AGG-003` 保留为表中明确存在但无已知边的设备。
2. `AP-007` 的“独立接入”不等于已知父节点，也不等于物理断开。
3. `ACC-022` 与 `AP-022`、`ACC-006` 与 `AP-006` 必须保持为不同实体。
4. `主干线 (Trunk)` 和“AP群”是视觉分组，不是设备。
5. “下方AP”只表示下游归属，不推断直连、PoE、端口或控制器关系。
6. `LSW`、`?`、`ZTE`、`FS`、`ONU` 保留为原始标记；不把不确定说明升级为确定事实。

## 3. 当前识别流程

`TopologyTextRecognizer` 使用标准库做保守解析：

1. 统一 CRLF、Tab 和公共缩进，同时保留 Unicode 图形的内部几何。
2. 将证据分为拓扑图、设备详情表、特殊标记和架构说明四层。
3. 用完整设备 ID 识别实体，业务 ID 规范化为小写下划线形式，例如 `ACC-022 -> acc_022`。
4. 只在完整箭头和分叉结构成立时生成图中关系。
5. 用详情表补充型号、角色和显式下游归属；说明文字只产生注释，不补边。
6. 对 `AP-022 (LSW)`、`AP-029 (?)`、`AP-061 (ONU)` 保留类型冲突候选，不强制判为 AP。
7. 输出统一 Scene Graph、证据跨度、问题列表和结构指标。

输入超限、表格截断、Trunk 箭头残缺或存在悬空边时，结果会标记 `usable_for_analysis=false`；页面服务不会选择这类部分语义。

页面采集 API 使用受限字段提交文本，调用方不能自行伪造 provenance：

```json
{
  "topology_text": {
    "kind": "user_provided_ascii",
    "format": "ascii_diagram_with_device_table",
    "source_id": "enterprise-topology-v1",
    "text": "...完整拓扑文本..."
  }
}
```

`kind` 只允许 `user_provided_ascii` 或 `external_ocr_transcript`，文本上限为 100,000 字符。

## 4. 像素、文本与可执行性的边界

页面感知按以下优先级选择证据：

```text
Renderer Scene
-> 带业务绑定的 DOM
-> 成功的 CanvasVisionAdapter
-> 提供的拓扑文本
-> 未识别 Canvas 截图
-> 普通 DOM
```

关键 provenance 字段：

| 来源 | `semantic_source` | `pixel_inference_performed` | `actionable_grounding` |
|---|---|---:|---:|
| Renderer Adapter | `canvas_renderer_adapter` | false | 有业务绑定时 true |
| Local CV/OCR | `canvas_pixels` | true | false；`adapter_id=local-cv-ocr` |
| HTTP Canvas Vision | `canvas_pixels` | true | 默认 false；资产库 exact binding 后才可另行授权 |
| CodeAgent read-tool Vision | `canvas_pixels` | true | false |
| 用户文本 | `provided_text` | false | false |
| 外部 OCR 转写 | `external_ocr_transcript` | false | false |
| 仅截图未识别 | `unrecognized_canvas_pixels` | false | false |

文本场景的 `text-grid` 坐标只用于定位证据，不能转成 GUI 点击坐标。Runtime 在动作入口再次检查 `actionable_grounding`；显式为 false 时拒绝动作，不获取资源锁，也不启动动作 Playbook。

## 5. 如何复现

运行文本识别专项测试：

```powershell
python -m unittest tests.test_topology_text_recognizer -v
```

运行页面感知与安全门禁测试：

```powershell
python -m unittest tests.test_page_perception tests.test_runtime -v
```

运行全量回归：

```powershell
python -m unittest discover -s tests -t .
```

当前结果为 136/136 通过。

## 6. 生产环境真实图片测试

本地 RapidOCR/OpenCV、生产 HTTP Vision、CodeAgent read-tool Adapter 和 pixels-only CLI 已完成，部署及命令见 `PRODUCTION_TOPOLOGY_VISION.md`。其中 `local_cv_ocr` 不依赖 Agent 或外部 API，只依赖本机 RapidOCR ONNX 与 OpenCV；CodeAgent 路径会要求逐帧成功 `read` 事件，拒绝“未读图直接返回 JSON”的假阳性。真实截图测试应准备同一拓扑的原始 PNG/JPEG 和独立标注 JSON，并分别统计：

- 设备框检测 precision / recall；
- 设备 ID OCR 的完整匹配率；
- 关系端点与方向准确率；
- Trunk、分组框和设备节点的分类准确率；
- 特殊标记保留率与不确定性校准；
- 无效坐标、低置信度、跨 Canvas 和模型异常时的 fail-closed 行为；
- 同一界面重复截图的对象 ID 稳定性和 Scene revision 变化。

只有通过真实像素输入得到的结果才允许设置 `pixel_inference_performed=true`；“截图旁附一段人工文本”仍属于文本语义重建，不能计入视觉准确率。

只验证单张图片时，可以在 KT6 Demo 根目录执行：

```powershell
python -m pip install -r requirements-local-vision.txt

Remove-Item Env:KT6_VISION_ENDPOINT -ErrorAction SilentlyContinue
Remove-Item Env:KT6_VISION_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:KT6_CODEAGENT_EXECUTABLE -ErrorAction SilentlyContinue
Remove-Item Env:KT6_CODEAGENT_AGENT -ErrorAction SilentlyContinue
Remove-Item Env:KT6_VISION_TIMEOUT_SECONDS -ErrorAction SilentlyContinue
$env:KT6_VISION_DRIVER = 'local_cv_ocr'

python -m kt6_backend.app
```

另开终端，复用现有单图 CLI：

```powershell
python -m kt6_backend.topology_image_cli D:\data\topology.png `
  --api-base http://127.0.0.1:8787 `
  --source-id enterprise-local-cv-v1 `
  --out D:\data\topology-local-result.json
```

除通用像素验收字段外，本地模式还必须满足：

```text
scene.provenance.adapter_id == local-cv-ocr
scene.actionable_grounding == false
```
