# 大模型 API + UI-TARS API 对照组

本分支 `eval-ui-tars` 实现第三套对照方案：可配置的大模型 API 生成当前步骤目标，
UI-TARS OpenAI-compatible API 基于当前截图返回一个坐标动作，Playwright 只负责截图
和执行该动作。三组评测应使用 suite 指定的同一供应商和精确规划模型，同时单独测量
UI-TARS 的视觉定位能力。

```text
Playwright 当前截图
  -> 可配置大模型 API 生成当前子目标（不接收图片）
  -> UI-TARS API 接收子目标和截图，返回一个动作
  -> 严格动作语法和坐标边界校验
  -> Playwright 执行一个动作
  -> 本地确定性终态验收
  -> 截图/两类模型调用/动作/验收证据归档
```

## 1. 安装

使用独立虚拟环境，不修改 KT6 生产依赖：

```powershell
py -3.12 -m venv .venv-ui-tars
.\.venv-ui-tars\Scripts\Activate.ps1
python -m pip install -r .\requirements-evaluation-ui-tars.txt
python -m playwright install chromium
```

执行器不要求把 UI-TARS 模型装进项目。UI-TARS-1.5-7B 可以部署在批准的内网服务，或
使用提供 OpenAI Chat Completions 兼容接口的获批推理 endpoint。

## 2. 两个 API

```powershell
$env:KT6_MODEL_API_PROVIDER = '<规划模型供应商或内网网关标识>'
$env:KT6_MODEL_API_BASE_URL = 'https://<获批规划模型网关>/v1'
$env:KT6_MODEL_API_KEY = '<仅当前测试窗口>'
$env:KT6_MODEL_API_MODEL = '<三组统一的精确模型名>'
$env:KT6_MODEL_API_ALLOWED_HOSTS = '<获批规划模型网关主机>'

$env:KT6_UI_TARS_API_BASE_URL = 'https://<批准的UI-TARS服务>/v1'
$env:KT6_UI_TARS_API_KEY = '<仅当前测试窗口>'
$env:KT6_UI_TARS_API_PROVIDER = 'ui-tars'
$env:KT6_UI_TARS_MODEL = '<UI-TARS精确模型名>'
$env:KT6_UI_TARS_API_ALLOWED_HOSTS = '<批准的UI-TARS服务主机>'
```

两个远程 endpoint 都要求 HTTPS、精确主机白名单和各自的显式放行参数。规划模型接收
任务、截图哈希和历史动作；UI-TARS 会接收页面截图。受限测试区数据未经批准不得发送到
任何公网 endpoint，应改用内网兼容服务。API key 不写命令行、证据或报告。

## 3. 动作安全边界

UI-TARS 输出必须恰好包含一行 `Action:`，执行器使用 Python AST 只解析字面量，不执行
模型代码。支持：

```text
click / left_double / right_single / drag
type / hotkey / scroll / wait / finished / call_user
```

坐标默认采用 UI-TARS 常用的 `0..1000` 空间，严格映射到本次截图的 CSS viewport；越界
坐标直接拒绝，不做自动夹紧。热键使用小型白名单，`CTRL+L` 等可绕过站点导航门禁的
组合不允许。Playwright 还会阻断不在任务 `allowed_hosts` 中的导航请求。

默认只生成和校验动作，不执行。真实对照实验必须显式加 `--execute-actions`，并只在
隔离、可恢复的基准任务中使用；当前项目生产安全动作链和真实 UI-TARS 评测执行器是
两套不同授权边界。

## 4. 任务验收

任务 JSON 与 Browser Use 分支使用同一 `kt6.evaluation-execution-task.v1` 格式，并在
`validation.assertions` 中给出本地确定性条件。支持 `url_equals`、`url_contains`、
`title_contains`、`text_contains`。UI-TARS 或规划模型声称完成不算成功；只有模型返回
`finished()`、断言全部通过、且真实执行已显式授权时才计为成功。

## 5. 执行一次

```powershell
python -m kt6_backend.ui_tars_evaluation_cli `
  --suite .\runtime_data\evaluation\nce\suite.json `
  --task .\runtime_data\evaluation\nce\tasks\T01.json `
  --runs .\runtime_data\evaluation\nce\runs.jsonl `
  --workspace .\runtime_data\evaluation\nce\raw-ui-tars `
  --repetition 1 `
  --implementation-revision '<当前提交哈希>' `
  --environment-id '<suite中的环境编号>' `
  --allow-remote-planner `
  --allow-remote-vision `
  --execute-actions
```

若使用已登录浏览器，可设置 `KT6_UI_TARS_CDP_URL=http://127.0.0.1:9222`；CDP 只接受
loopback。默认启动隔离 Chromium，viewport 为 `1920x1080`。

## 6. 证据和指标

每一步都会归档一张 `original_screenshot`，即使连续两步像素完全相同也保留两个独立
artifact ID。另保存：

- `planner_result`：suite 指定的统一规划模型逐步子目标，含真实供应商和模型名；
- `ui_tars_response`：模型原始 prediction、严格解析后的动作、截图 ID 和 SHA-256；
- `ui_tars_actions`：坐标模式和动作清单；
- `action_trace`：实际状态、耗时和失败；
- `validation_result`：本地确定性终态结果。

`planner_model_calls` 与 `vision_model_calls` 分开统计，报告中的总模型调用数是两者之和。
原始截图和模型文本只留在 `runtime_data/`，不会嵌入领导版报告。

## 7. 测试

```powershell
python -m unittest `
  tests.test_ui_tars_evaluation `
  tests.test_evaluation_executor `
  tests.test_openai_compatible_api
```

单元测试使用 Fake Planner、Fake UI-TARS 和 Fake Browser，不需要 API 或浏览器。真实冒烟
测试还需覆盖：UI-TARS endpoint 格式、相同截图的独立步骤绑定、坐标缩放、越界拒绝、
站外导航阻断、模型超时、最终断言和报告证据重验。
