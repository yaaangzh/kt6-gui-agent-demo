# AGENTS.md

本文件适用于 `D:\yangzehui\FreeStyleCopilot` 的所有本地分支。开始修改前依次阅读
`README.md`、`CODEX_HANDOFF.md`、`test.md`，再检查当前分支、状态、最近提交和 stash。
不得只依据旧对话、缓存哈希或另一个分支的文件继续开发。

## 1. 项目与分支

KT6 / FreeStyleCopilot 是无线网络运维 PoC，把自然语言意图、页面感知、拓扑理解、
业务 Playbook、人在环确认和受控操作串成可审计链路。

| 分支 | 职责 |
|---|---|
| `main` | Runtime、页面感知、安全动作链和公共评测/证据框架 |
| `eval-current` | 现有 OpenCV/OCR + 任意获批 OpenAI-compatible 模型 API |
| `eval-browser-use` | Browser Use/CDP + 任意获批 OpenAI-compatible 规划模型 API |
| `eval-ui-tars` | 通用规划模型 API + 独立 UI-TARS 视觉定位 API + Playwright |
| `ui-graph-textflow-cdp` | CDP、多源 UI Graph、操作 DAG 的实验分支 |
| `br_omniParser` | 已停止的 OmniParser 试点，仅保留历史对照，不混入当前方案 |

功能实验保持分支隔离；公共基础设施必须同步到所有仍保留的分支。公共修改包括：

- `.gitignore`、`.env.example`、`kt6_backend/env_config.py`；
- 评测结果、证据归档、统一报告和共享 API 客户端；
- `README.md`、`CODEX_HANDOFF.md`、`test.md`、`AGENTS.md` 中的公共规则；
- 与上述公共代码直接对应的测试。

同步公共提交时保留各分支专属实现和文档，不用某一分支的 README 整文件覆盖其他分支。
未经用户明确要求，不合并功能分支、不变基、不删除分支、不清理 stash、不创建 PR、不
改写远端历史。

## 2. 公共架构

```text
用户自然语言
→ Intent Agent / Playbook Router
→ Runtime 状态机
→ Page / Scene Perception
→ DOM、Canvas、CV/OCR、模型或 CDP 证据
→ 人在环确认
→ dry-run 安全动作计划
```

主要 Runtime 文件：

```text
kt6_backend/app.py
kt6_backend/runtime.py
kt6_backend/agent.py
kt6_backend/router.py
kt6_backend/page_perception.py
kt6_backend/page_capture_jobs.py
kt6_backend/safe_dom_actions.py
```

浏览器扩展在一次显式采集中处理 DOM/ARIA 与 Canvas/SVG；耗时识别由异步 capture job
执行。扩展 capture 与 CDP Sidecar capture 是独立采集，不能误称已经实时合并。

Canvas 基线路线：

```text
截图 → OpenCV/OCR → 可选模型语义补充 → 确定性融合 → UI/文本证据
```

测试区内部 GLM5.1 通过 `D:\03CodeAgent\CodeAgentCLI\codeagent.bat` 使用，不是独立
HTTP endpoint。外部或内网大模型实验统一使用可配置的 OpenAI-compatible Chat
Completions endpoint；UI-TARS 的截图定位服务使用独立配置。

## 3. 统一本地配置

所有分支都使用根目录 `.env`。首次使用：

```powershell
Copy-Item .\.env.example .\.env
notepad .\.env
```

后端和评测 CLI 会自动加载；进程中已经存在的同名环境变量优先。修改 `.env` 后必须
重启后端或重新运行 CLI。`.env`、截图、模型原文、DOM/CDP 快照和真实评测结果不得提交。

关键公共变量：

```text
KT6_MODEL_API_PROVIDER
KT6_MODEL_API_BASE_URL
KT6_MODEL_API_KEY
KT6_MODEL_API_MODEL
KT6_MODEL_API_ALLOWED_HOSTS
```

UI-TARS 另用：

```text
KT6_UI_TARS_API_PROVIDER
KT6_UI_TARS_API_BASE_URL
KT6_UI_TARS_API_KEY
KT6_UI_TARS_MODEL
KT6_UI_TARS_API_ALLOWED_HOSTS
```

不得把 API key 写进命令行、suite、运行 JSON、报告、健康检查或日志。远程 endpoint
必须经过数据出区审批并使用精确 host 白名单；测试区页面数据默认不得发送到公网模型。

## 4. 分支专属边界

### 4.1 `eval-current`

OpenCV/OCR 在本地运行，模型 API 只接收有界 CV/OCR JSON，不接收截图、Base64、本地
路径或完整页面 URL。模型结果必须通过严格拓扑契约，再进入确定性融合。

### 4.2 `eval-browser-use`

Browser Use 负责 DOM/CDP 感知与动作，规划模型通过通用 API 调用；`use_vision=false`，
规划模型不接收截图。成功必须由本地确定性页面断言判断，不能信任模型自报成功。

### 4.3 `eval-ui-tars`

规划模型只生成步骤目标；UI-TARS 服务单独接收截图并返回坐标动作。两类 API 的
provider/model 必须分别记录。默认不执行动作，真实基准执行必须显式授权
`--execute-actions` 并限制在隔离、可恢复任务中。

### 4.4 `ui-graph-textflow-cdp`

UI Graph 统一 `dom/cdp/page_api/vision/text` 来源，并生成 `locate/click/wait/verify`
操作 DAG。当前 UI Graph CodeAgent CLI Adapter 尚未接通；未配置 HTTP Reasoner 时规划
接口返回 503 是预期边界，不能写成“内部 GLM UI Graph 编排已实机完成”。

### 4.5 `br_omniParser`

该分支只保留历史试点。除公共基础设施同步和必要安全修复外，不继续扩展 OmniParser，
也不将其结果混入当前三方案报告。

## 5. 安全约束

- UI Graph、节点、边和操作计划必须保持 `safe_for_execution=false`。
- 默认计划必须 `dry_run_only=true`。
- `interaction.candidate=true` 仅表示允许进入校验，不表示已授权点击。
- 只有具有稳定引用的 DOM 节点或正整数 backend node id 的 CDP 节点可成为候选。
- page_api、vision、text 只能辅助理解，不能授权点击。
- disabled、blocked、`@capture:` 及大小写变体不得成为有效目标。
- 原始 `actionable=true`、business_id、element_id 不能绕过候选门禁。
- 页面 API 只读取显式 `window.__KT6_PAGE_ADAPTER__`，不拦截任意 fetch/XHR。
- CDP 只允许 `localhost`、`127.0.0.1` 或 `::1`。

### 5.1 性能与实现范围

> **强制性能原则：请避免考虑很多很多极少数情况，尽量少地把备选方案整合到你的代码里，
> 不要导致后端计算非常冗长，坚决避免前端响应特别慢。**

- 默认只实现需求明确的主路径；备选方案必须有真实高频问题、测试证据或明确需求支撑。
- 不在热路径堆叠多套模型、解析器、启发式规则、重复重试或层层 fallback；实验能力默认关闭。
- 后端限制模型调用、重试、全图扫描和重复计算，避免串行等待及无界循环。
- 前端优先立即反馈，耗时采集和识别放到异步任务，避免重复采集、重复渲染和阻塞主线程。
- 修改主链路时必须检查响应耗时和调用次数；性能优化不能绕过既有安全与数据边界。

## 6. 公共评测框架

`evaluation_artifacts.py` 归档显式证据并生成 SHA-256 Manifest；
`evaluation_report.py` / `evaluation_report_cli.py` 检查覆盖率、公平性、证据完整性、安全
违规和同模型自评偏差。报告模块本身不调用模型或浏览器。

证据、suite 和 runs 必须放在 `runtime_data/` 或测试区专用目录。证据缺失、哈希异常、
调用/步骤覆盖不全或语义绑定不一致的运行不能参与排名。报告不得嵌入截图、DOM、模型
原文、完整 URL、失败自由文本或密钥。

## 7. 常用命令

```powershell
git status -sb
git branch --show-current
git branch -vv
git log -5 --oneline --decorate
git stash list
```

```powershell
python -m unittest discover -s tests
python -m unittest tests.test_env_config
python -m unittest tests.test_evaluation_artifacts tests.test_evaluation_report
git diff --check
```

分支专属命令和真实测试流程见 `test.md`。

## 8. Git 与工作区

- `review.md` 是用户未跟踪文件，除非用户明确要求，否则不修改、不暂存、不提交。
- OmniParser WIP 在 stash 中；只核对，不自动 apply/drop。
- 只暂存当前任务文件，不使用 `git add -A`。
- 不使用 `git reset --hard`、`git checkout --` 清理用户修改。
- `runtime_data/` 可能保存耗时模型结果，不无依据删除。
- 判断远端前先 fetch；push 成功前不得宣称已上传。
- 只在用户明确要求时 push、建 PR、合并或删除分支。

## 9. 文档一致性

公共代码变化必须在所有分支同步检查：

1. `README.md` 是否说明当前分支能力与真实边界；
2. `test.md` 是否包含可执行命令、配置方式和最新测试事实；
3. `AGENTS.md` 是否保留公共同步、安全和 Git 规则；
4. `CODEX_HANDOFF.md` 是否记录最新公共能力和分支职责；
5. 方案文档是否仍使用当前环境变量和分支名。

文档与代码冲突时，以当前代码和真实测试证据为准，并在同一任务修正文档。
