# KT6 当前交接说明

## 1. 当前分支与目标

当前开发分支：`feature/eval-browser-harness`。

该分支验证以下架构：

```text
日常 Chrome 当前 Tab
→ Browser Agent Side Panel 选择精确 Target
→ Browser Harness 绑定 Tab/session
→ KT6 fresh capture + UI Graph
→ OpenAI-compatible API 生成 Action Plan
→ 人在环确认
→ 固定 type/click
→ 新 capture + 确定性结果验证
```

浏览器扩展不 attach、不代发 CDP，也不读取目标页面内容。Browser Harness 负责实际浏览器
连接，KT6 只暴露固定采集和操作能力，不接受模型生成的脚本或任意协议命令。

## 2. 模型调用边界

所有大模型调用统一走 API。本仓库不再包含本地模型 CLI 子进程适配器、离线模型 CLI、
专用事件日志或进程重试实现。

规划模型配置：

```dotenv
KT6_MODEL_API_PROVIDER=<provider-or-internal-gateway>
KT6_MODEL_API_BASE_URL=https://<approved-model-gateway>/v1
KT6_MODEL_API_KEY=<secret>
KT6_MODEL_API_MODEL=<exact-model-name>
KT6_MODEL_API_ALLOWED_HOSTS=<approved-model-gateway-host>
KT6_MODEL_API_MAX_TOKENS=4096
KT6_MODEL_API_TIMEOUT_SECONDS=60
```

Canvas 视觉有以下配置状态：

- 未配置：普通 DOM/CDP 页面主路径；
- `local_cv_ocr`：本机 RapidOCR/OpenCV，不调用模型；
- `openai_compatible`：直接调用支持图片输入的 Chat Completions 模型；
- `hybrid`：本地 CV 优先，自适应路由后将截图与 CV/OCR 上下文一起传给模型并融合。

API/Hybrid 视觉使用独立的 OpenAI-compatible 网关、密钥、模型、主机限制和超时：

```dotenv
KT6_VISION_DRIVER=hybrid
KT6_VISION_API_PROVIDER=<vision-provider-or-internal-gateway>
KT6_VISION_API_BASE_URL=https://<approved-vision-gateway>/v1
KT6_VISION_API_KEY=<secret>
KT6_VISION_API_MODEL=<支持image_url和JSON输出的模型名>
KT6_VISION_API_ALLOWED_HOSTS=<approved-vision-gateway-host>
KT6_VISION_API_MAX_TOKENS=4096
KT6_VISION_API_TIMEOUT_SECONDS=60
```

`hybrid` 和 `local_cv_ocr` 都需安装 `requirements-local-vision.txt`。分类/路由按任务所需
能力选择：可信 CV 节点可直接满足 `nodes_only`；只有连通关系或语义能力明确不足时才调用
模型。CV 无效时最多调用一次模型；无修复调用
或自动重试。输入图片经过尺寸/哈希校验，CV/OCR 上下文按已有契约限量压缩；不发送路径、
完整 URL。输出仍用严格拓扑契约和既有确定性融合，保留路由、模型调用和缓存证据。
缓存指纹包含模型/网关/token 设置；缓存命中不重复计算模型调用与 token，原始用量另标
`source_call_count` / `source_usage`。视觉 API 不回退到规划 API；原 `http` driver 与专用
`/v1/topology` 协议已移除。

远程模型 endpoint 必须经过数据出区审批；真实 key、截图、DOM/CDP、模型原文和真实运行
证据不能提交 Git。

## 3. 页面感知与执行

Browser Harness 每次 fresh capture 固定读取 frame tree、DOMSnapshot、Accessibility Tree
和 layout metrics，不再生成未展示的整页预览。普通 DOM 流程不采集 Canvas；任务或
Grounding 需要视觉时才截取最大的可见 Canvas 区域。后端将
DOM、CDP、Canvas、可选 page API 和文本证据融合到统一 UI Graph。

规划模型只接收按自然语言任务相关性排序的 48 KiB UI Graph 投影，不再接收通用 256 KiB
投影。`ingest` 可直接返回 UI Graph 和 Action Snapshot，Runner 不再从 SQLite 重读同一份
多 MB capture。等待轮询使用最多 2 份内存临时证据，失败即丢弃并清理截图，只有最终成功
证据落库。API 状态记录浏览器采集、感知、投影和模型调用分段耗时。

计划只包含语义目标。执行时 `TargetGrounderRegistry` 先尝试唯一 DOM/CDP Grounding，找不到
才尝试配置的 Vision Grounding。点击和输入前重新校验 URL、frame、backend node、属性、
可访问名称、viewport、box model 和 hit-test。动作后重新采集，由 Verifier Registry 判断
输入值、选中状态、元素出现或 URL 变化。

业务流程不设置页面等待截止时间。`verify` 和 `wait` 持续 fresh capture，直到确定性条件
成立或用户通过 Side Panel 取消；取消状态依次为 `cancelling`、`cancelled`。Browser Harness
每次 IPC/CDP 调用仍保留短存活超时，通信失败会明确结束运行，且不会自动重放已经派发的
`type` / `click`。

任何标签页切换、非预期导航、目标歧义、页面截断、目标遮挡或身份变化都应进入明确失败
状态；验证暂未满足只继续观察，不得把派发回执当成成功。

## 4. 启动方式

```powershell
cd D:\yangzehui\FreeStyleCopilot
Copy-Item .\.env.example .\.env
notepad .\.env
python -m pip install -r requirements-browser-executor.txt
.\scripts\start-browser-executor.ps1
```

可选打开目标页：

```powershell
.\scripts\start-browser-executor.ps1 -InitialTargetUrl "https://www.baidu.com/"
```

Chrome 一次性准备：

1. `chrome://extensions` 开启开发者模式，加载 `browser_extension`。
2. 代码更新后在扩展管理页点“重新加载”。
3. `chrome://inspect/#remote-debugging` 允许当前 Chrome 实例远程调试。
4. 第一次连接时在 Chrome 提示中点击 Allow。

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8787/api/health
Invoke-RestMethod http://127.0.0.1:8787/api/execution/health
```

`/api/execution/health` 至少应显示：

```text
configured=true
planner_configured=true
transport=browser_harness
browser_harness.ready=true
```

## 5. 当前主要文件

```text
kt6_backend/app.py
kt6_backend/openai_compatible_api.py
kt6_backend/page_perception.py
kt6_backend/ui_graph.py
kt6_backend/openai_canvas_vision.py
kt6_backend/local_cv_canvas_vision.py
kt6_backend/hybrid_canvas_vision.py
kt6_backend/topology_model_contract.py
kt6_backend/topology_fusion.py
kt6_backend/execution/action_planner.py
kt6_backend/execution/live_page_capture.py
kt6_backend/execution/browser_harness_client.py
kt6_backend/execution/browser_executor.py
kt6_backend/execution/grounding.py
kt6_backend/execution/scenario_runner.py
kt6_backend/execution/verifier.py
browser_extension/manifest.json
browser_extension/background.js
browser_extension/sidepanel.html
browser_extension/sidepanel.js
scripts/start-browser-executor.ps1
```

## 6. 验证命令

2026-09-08 多模态视觉专项 107 项通过；独立视觉 URL 配置及关联主链 99 项通过，命令及范围
见 `test.md` 第 10 节。后端编译和 diff 空白检查通过。此次没有调用真实模型或操作 Chrome，
本机 `.env` 仍为 `local_cv_ocr`，真实多模态识别需配置 `hybrid` 与完整的
`KT6_VISION_API_*` 后重启验收。

```powershell
python -m unittest `
  tests.test_app `
  tests.test_openai_canvas_vision `
  tests.test_hybrid_canvas_vision `
  tests.test_topology_model_contract `
  tests.test_topology_cv_cli `
  tests.test_browser_executor `
  tests.test_execution_scenario `
  tests.test_execution_e2e

python -m compileall kt6_backend
node --check browser_extension/background.js
node --check browser_extension/sidepanel.js
git diff --check
```

完整回归：

```powershell
python -m unittest discover -s tests
```

自动化测试不操作真实 Chrome。实机验收必须重新加载扩展，从目标标签页 Side Panel 发起，
记录计划、run 状态和每一步验证结果。

## 7. 已知边界

- 当前资产和业务工具仍含 Mock 数据，不代表真实设备下发。
- `permissions` 测试字段不是生产身份授权。
- Canvas 视觉识别结果默认是分析证据，生产使用前需要黄金数据准确率评测。
- 同一后端只接受一个浏览器规划或执行会话；busy 请求不排队。
- 页面导航、标签页变化或 DOM 重建会使当前绑定失效，必须重新采集或重新生成计划。
- 后端只支持固定 `type`/`click`；不增加任意 JavaScript、Python、按键序列或 raw CDP。

## 8. Git 与接手检查

新任务开始先执行：

```powershell
git status -sb
git branch --show-current
git branch -vv
git log -5 --oneline --decorate
git stash list
```

注意：

- `deliverables/`、`runtime_data/` 和用户未跟踪文件不得误提交。
- OmniParser WIP stash 只核对，不自动 apply/drop。
- 只暂存当前任务文件，不使用 `git add -A`。
- 未经用户明确要求，不合并、变基、删除分支、创建 PR 或推送。
- push 前必须 fetch；只有远端命令成功后才能说已上传。
- 代码、README、`test.md`、`AGENTS.md` 和本文件必须保持一致。
