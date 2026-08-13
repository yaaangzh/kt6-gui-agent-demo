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

## 自动化测试

```powershell
python -m unittest `
  tests.test_openai_compatible_api `
  tests.test_openai_compatible_topology_model `
  tests.test_openai_compatible_ui_graph_reasoner `
  tests.test_hybrid_canvas_vision `
  tests.test_vision_cache_coordinator `
  tests.test_app
```

单元测试使用注入的假 HTTP transport，不会请求真实 API。真实联调前必须确认任务文本、
DOM 和 CV/OCR 业务标识允许发送到目标 endpoint；测试区数据不能直接发送到公网服务。
