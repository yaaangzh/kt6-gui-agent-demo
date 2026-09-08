# KT6 意图驱动 LUI-GUI 联动 Runtime

KT6 / FreeStyleCopilot 是面向无线网络运维场景的工程原型。它把自然语言任务、真实页面
感知、语义 Action Plan、人在环确认、受控浏览器操作和确定性结果验证串成一条可审计链路。

当前 `feature/eval-browser-harness` 分支复用用户已经打开的日常 Chrome。Chrome Side
Panel 只选择当前标签页并承载任务输入和确认；Browser Harness 负责连接该标签页、采集
DOM/CDP/截图以及执行固定 `type`/`click`。系统不会创建专用浏览器 profile，也不依赖固定
远程调试端口。

## 当前执行链

```text
当前 Chrome 标签页
→ 扩展提交精确 Target ID + URL
→ Browser Harness 绑定同一 Tab/session
→ fresh DOMSnapshot + AXTree + 页面截图
→ PagePerceptionService 生成多源 UI Graph
→ OpenAI-compatible API 生成 kt6.action-plan.v1
→ 用户检查并确认
→ 每步重新采集和 Grounding
→ 固定 type/click 适配器执行
→ 新 capture + OutcomeVerifier 确定性验证
```

Action Plan 只保存语义目标，不保存临时 Target ID、backend node id、selector 或坐标。
点击已派发也不代表业务成功；只有后续新页面证据通过对应 Verifier，任务才进入成功状态。

## 页面感知

Browser Harness 主路径每次采集：

- `Page.getFrameTree` 和当前 URL；
- `DOMSnapshot.captureSnapshot`；
- 每个 frame 的 `Accessibility.getFullAXTree`；
- viewport 和浏览器版本；
- 当前可见区域的低质量预览截图；
- 检测到原生 Canvas 时，对最大的可见 Canvas 生成精确区域截图。

后端把 `dom`、`cdp`、`page_api`、`vision`、`text` 证据统一放入 UI Graph，同时保留
来源、frame、父子关系、可访问名称、backend node id、业务声明和交互候选状态。

当前感知选择：

| 路线 | 配置 | 用途 |
|---|---|---|
| DOM/CDP | 无需视觉配置 | 普通输入框、按钮、链接、菜单、表格 |
| 本地 CV/OCR | `KT6_VISION_DRIVER=local_cv_ocr` | 不调用模型的 Canvas 分析补充 |
| 模型视觉 API | `KT6_VISION_DRIVER=openai_compatible` | 将有界 Canvas 图片发给支持图片的 Chat Completions 模型 |
| Hybrid | `KT6_VISION_DRIVER=hybrid` | 本地 CV 优先，自适应判断后把截图和 CV/OCR 上下文一起发给模型，再确定性融合 |

`page_api` 和 topology text 是其他采集入口的可选证据，不是当前 Browser Harness
Side Panel 的默认输入。视觉、页面 API 和文本结果均不能自行授予操作权限。

## 模型配置

所有大模型调用都通过 API 完成，不启动本地模型 CLI 子进程。

自然语言 Action Plan 使用 OpenAI-compatible Chat Completions：

```dotenv
KT6_MODEL_API_PROVIDER=<provider-or-internal-gateway>
KT6_MODEL_API_BASE_URL=https://<approved-model-gateway>/v1
KT6_MODEL_API_KEY=<secret>
KT6_MODEL_API_MODEL=<exact-model-name>
KT6_MODEL_API_ALLOWED_HOSTS=<approved-model-gateway-host>
KT6_MODEL_API_MAX_TOKENS=4096
KT6_MODEL_API_TIMEOUT_SECONDS=60
```

Canvas 视觉复用上述 API 网关、密钥、获批主机、token 上限和超时。要启用
“本地 CV/OCR → 自动分类 → 按需模型补充 → 确定性融合”，增加：

```dotenv
KT6_VISION_DRIVER=hybrid
# 可选：同一网关下单独指定视觉模型；省略时使用 KT6_MODEL_API_MODEL
KT6_VISION_MODEL=<支持图片输入和JSON输出的模型名>
```

模型需要支持 Chat Completions 的 `image_url` 内容块与 JSON 输出。一次请求包含经过哈希、
尺寸校验的 Canvas 截图及有界 CV 节点、连线、OCR 标识候选；不发送本地图片路径或完整页面
URL。不再使用 `/v1/topology` 专用视觉协议及其独立 endpoint/key 配置。

Hybrid 的 `auto` 分类规则保持不变：高质量散点只保留节点；清晰结构拓扑直接使用 CV；
复杂或无有效 CV 结果时最多调用模型一次，随后严格校验结果并融合。模型调用失败按既有逻辑
保留可用 CV 证据，`vision_routing.execution_status` 会标明降级。相同截图可使用缓存，模型、
网关或 token 设置改变后不会复用旧模型结果。`vision_model_call` 记录当次调用与用量；
缓存命中时调用数为 0，原始信息放在 `source_call_count` / `source_usage`，不重复计费统计。
视觉输出仍是分析证据，不授予点击权限。

安装本地识别依赖（`local_cv_ocr` 和 `hybrid` 需要）：

```powershell
python -m pip install -r requirements-local-vision.txt
```

只调用视觉模型可将 driver 改为 `openai_compatible`。普通 DOM 页面无需视觉驱动；只配
`KT6_MODEL_API_*` 不会启用截图发送。

配置统一放在根目录 `.env`：

```powershell
Copy-Item .\.env.example .\.env
notepad .\.env
```

`.env` 已被 Git 忽略。不要把真实密钥写入命令行、日志、suite、报告或提交记录。修改
`.env` 后必须重启后端。测试区页面数据未经审批不得发送到公网模型服务。

## 启动 Browser Harness 演示

安装依赖：

```powershell
python -m pip install -r requirements-browser-executor.txt
```

启动或复用后端：

```powershell
.\scripts\start-browser-executor.ps1

# 可选：在现有 Chrome 中打开一个新标签页
.\scripts\start-browser-executor.ps1 -InitialTargetUrl "https://www.baidu.com/"
```

首次使用：

1. 打开 `chrome://extensions`，启用开发者模式。
2. 选择“加载已解压的扩展程序”，加载仓库的 `browser_extension` 目录。
3. 打开 `chrome://inspect/#remote-debugging`，允许当前 Chrome 实例远程调试。
4. 回到目标 HTTP/HTTPS 页面，点击 “KT6 Browser Agent”。
5. 输入自然语言任务，生成计划，检查后确认执行。
6. Browser Harness 第一次连接时，在 Chrome 提示中点击 Allow。

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8787/api/health
Invoke-RestMethod http://127.0.0.1:8787/api/execution/health
```

如果提示 8787 正在运行旧 Runtime，先终止占用该端口的旧后端，再重新运行启动脚本。

## 执行安全边界

- 浏览器执行默认关闭，只有 `KT6_BROWSER_EXECUTION_DRIVER=browser_harness` 才启用。
- 公网 HTTP/HTTPS 默认允许；本机、RFC1918、链路本地和保留地址默认拒绝。
- 隔离测试私网页面时才设置 `KT6_EXECUTION_ALLOW_PRIVATE_NETWORKS=1`。
- UI Graph 和所有节点始终保持 `safe_for_execution=false`。
- DOM Grounding 必须得到唯一、可见、未禁用且带正整数 backend node id 的候选。
- `type` 仅允许普通 textbox/searchbox/combobox，拒绝密码、文件、隐藏和只读控件。
- `click` 前重新校验 URL、frame、节点身份、属性、box model、viewport 和 hit-test。
- 视觉点击必须绑定当前 Canvas backend node、当前截图坐标空间和唯一高置信目标。
- 不接受模型生成的 JavaScript、Python、任意按键序列、helper 或 raw CDP。
- 每个动作后都必须重新采集页面并由确定性 Verifier 判断结果。

## 主要代码

```text
kt6_backend/app.py                         后端服务工厂和 HTTP API
kt6_backend/page_perception.py             页面证据规范化、持久化和 UI Graph
kt6_backend/execution/live_page_capture.py Browser Harness 固定 CDP 采集
kt6_backend/execution/browser_harness_client.py 日常 Chrome 连接与实时复核
kt6_backend/execution/action_planner.py     OpenAI-compatible Action Plan
kt6_backend/execution/grounding.py          DOM/CDP 优先、Vision 兜底 Grounding
kt6_backend/execution/scenario_runner.py    fresh capture → execute → verify 循环
kt6_backend/execution/verifier.py           确定性结果验证
kt6_backend/openai_canvas_vision.py         截图 + CV 上下文的多模态 API 适配器
kt6_backend/local_cv_canvas_vision.py       本地 RapidOCR/OpenCV 适配器
kt6_backend/hybrid_canvas_vision.py         本地 CV + 多模态 API 自适应融合
kt6_backend/openai_compatible_api.py        通用模型 API 客户端
browser_extension/                         Chrome Side Panel 扩展
scripts/start-browser-executor.ps1          Windows 统一启动入口
```

## 测试

快速验证当前 Browser Harness 主链：

```powershell
python -m unittest `
  tests.test_app `
  tests.test_browser_executor `
  tests.test_execution_scenario `
  tests.test_execution_e2e `
  tests.test_openai_canvas_vision `
  tests.test_hybrid_canvas_vision `
  tests.test_topology_cv_cli

python -m compileall kt6_backend
git diff --check
```

完整回归：

```powershell
python -m unittest discover -s tests
```

真实 Chrome 验收必须从 Side Panel 发起，并记录最终 run 状态；自动化单元测试通过不能
替代真实页面验收。

## 当前边界

- 当前资产库存、无线指标、根因输入和设备动作结果仍包含 Mock 数据。
- 尚未接入真实 NCE/FEBS 服务端身份、权限和设备 API。
- Canvas 视觉真实图片准确率仍需独立黄金数据集验证。
- Browser Harness 一次只服务一个受控规划或执行会话，不并行切换多个目标标签页。
- 目标页面主动导航、登录失效或 DOM 重建会导致当前 Grounding 失效，需要重新生成计划。

分支职责、统一开发规则见 [AGENTS.md](./AGENTS.md)，测试与实机验收细节见
[test.md](./test.md)，当前交接状态见 [CODEX_HANDOFF.md](./CODEX_HANDOFF.md)。
