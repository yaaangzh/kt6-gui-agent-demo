# 现有方案：OpenCV/OCR + 大模型 API

本分支 `eval-current` 保留当前方案的本地几何识别、路由、确定性融合、UI Graph 和
安全校验，只将模型补充层改为可配置的 OpenAI-compatible Chat Completions API。
DeepSeek、Qwen、GLM 或内部网关都只是可选供应商；只要接口兼容且通过数据安全审批，
无需修改业务代码即可切换。使用其他专有协议的模型需要另写协议 Adapter。

## 数据链路

```text
Canvas/SVG 截图
→ 本地 OpenCV/OCR
→ 场景路由
→ 有界 CV/OCR JSON（不含截图、Base64、本地路径和完整页面 URL）
→ 可配置大模型 API 做语义标准化
→ TopologyModelContract 严格校验
→ 确定性融合 / UI Graph / dry-run DAG 校验
```

当前 Adapter 按文本模型使用。截图 SHA-256 只用于数据血缘，
`screenshot_sent_to_model=false`；不得把这条路线描述为大模型直接看图。

## 配置

推荐先创建一次根目录 `.env`，以后启动后端无需重复在终端赋值：

```powershell
Copy-Item .\.env.example .\.env
notepad .\.env
```

在 `.env` 中填写：

```dotenv
KT6_VISION_DRIVER=hybrid
KT6_HYBRID_MODEL_DRIVER=openai_compatible
KT6_MODEL_API_PROVIDER=<供应商或内部网关标识>
KT6_MODEL_API_BASE_URL=https://<获批网关>/v1
KT6_MODEL_API_ALLOWED_HOSTS=<获批网关的精确主机名>
KT6_MODEL_API_KEY=<仅保存在本机.env>
KT6_MODEL_API_MODEL=<当前实际可用的精确模型名>
KT6_MODEL_API_MAX_TOKENS=4096
KT6_VISION_TIMEOUT_SECONDS=60
```

例如接入 DeepSeek、Qwen、GLM 或自建 vLLM 网关时，只替换 `PROVIDER`、`BASE_URL`、
`ALLOWED_HOSTS`、`API_KEY` 和 `MODEL`，其余链路不变。供应商专有参数默认不会发送；
确需使用时应由对应 Adapter 显式配置并补兼容性测试。

如需让 UI Graph 规划也走同一个 API：

```dotenv
KT6_UI_GRAPH_REASONER_DRIVER=openai_compatible
KT6_UI_GRAPH_REASONER_TIMEOUT_SECONDS=60
```

不要同时配置 `KT6_VISION_ENDPOINT`、`KT6_VISION_API_KEY` 或 CodeAgent 参数。远程 API
只允许 HTTPS，并且 host 必须精确出现在 `KT6_MODEL_API_ALLOWED_HOSTS`；客户端禁止
重定向、压缩响应、重复 JSON key、NaN/Infinity 和 `finish_reason=length`。

## 启动与检查

```powershell
python -m kt6_backend.app
```

```powershell
$health = Invoke-RestMethod -Uri 'http://127.0.0.1:8787/api/health'
$health.canvas_vision
$health.ui_graph_reasoning
```

画布结果仍通过现有扩展异步采集入口产生；UI Graph 规划仍必须保持
`dry_run_only=true`、`safe_for_execution=false`。

## 运行一次自动评测

`eval-current` 现在提供单次图片评测入口，自动完成本地 CV/OCR、按路由调用模型 API、
结果融合、UI Graph、确定性断言、原始证据归档和 `runs.jsonl` 追加，不再需要手工拼接
各阶段 JSON。

先准备一个 `status=ready`、包含 Canvas 任务的 suite。快速冒烟时可将
`docs/current-evaluation-task.example.json` 复制到评测目录，并保证其中的 `suite_id`、
`task_id`、验证方法与 suite 一致。正式准确率测试应把 `minimum_object_count` 换成明确的
`object_exists`、`link_exists` 或 `text_contains` 断言。

```powershell
$evalDir = '.\runtime_data\evaluation\current-image-smoke'
$revision = git rev-parse --short HEAD

python -m kt6_backend.current_evaluation_cli `
  --suite "$evalDir\suite.json" `
  --task "$evalDir\task-T01.json" `
  --image 'D:\测试图片\topology.png' `
  --runs "$evalDir\runs.jsonl" `
  --workspace "$evalDir\raw" `
  --repetition 1 `
  --implementation-revision $revision `
  --environment-id '<测试环境编号>' `
  --allow-remote-model
```

如果 API 是本机 loopback 服务，不需要 `--allow-remote-model`；非本机 endpoint 必须显式
添加该参数。CLI 会自动读取根目录 `.env`，不会在终端显示 key、模型原文或绝对源路径。

成功后主要结果位于：

```text
runtime_data/evaluation/current-image-smoke/
├─ runs.jsonl
├─ raw/current-T01-r1/
│  ├─ original-screenshot-001.png
│  ├─ cv-result-001.json
│  ├─ cv-metadata-001.json
│  ├─ routing-result-001.json
│  ├─ model-result-001.json           # 仅 model_assist 成功时
│  ├─ vision-model-call-001.jsonl     # 仅实际调用模型时
│  ├─ fused-result-001.json
│  ├─ ui-graph-001.json
│  ├─ action-trace-001.jsonl
│  └─ validation-result-001.json
└─ artifacts/current/T01/r001/
   ├─ manifest.json
   └─ 上述证据的不可覆盖归档副本
```

模型超时、传输错误或无效 JSON 也会作为失败运行写入报告数据，不会从完成率中消失。
当前入口评测的是图片识别链路，不控制真实浏览器，也不等同于 NCE 页面任务端到端完成率。

## 自动化测试

```powershell
python -m unittest `
  tests.test_current_evaluation `
  tests.test_openai_compatible_api `
  tests.test_openai_compatible_topology_model `
  tests.test_openai_compatible_ui_graph_reasoner `
  tests.test_hybrid_canvas_vision `
  tests.test_vision_cache_coordinator `
  tests.test_app
```

单元测试使用注入的假 HTTP transport，不会请求真实 API。真实联调前必须确认任务文本、
DOM 和 CV/OCR 业务标识允许发送到目标 endpoint；测试区数据不能直接发送到公网服务。
