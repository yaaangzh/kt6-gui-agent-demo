# Browser Use + 大模型 API 对照组

本分支 `eval-browser-use` 实现第二套对照方案：Browser Use 负责 DOM/CDP 感知与浏览器
动作，可配置的 OpenAI-compatible API 负责每一步操作规划；成功与否由本地确定性页面
条件判断，不使用 Browser Use 自带的模型裁判。DeepSeek、Qwen、GLM 或内网网关均可
作为供应商，模型和 endpoint 由运行参数确定。

该执行器与主干的评测框架分层：

```text
Browser Use/CDP -> 可配置模型 API -> Browser Use 单步动作
                -> 本地终态断言
                -> DOM/模型调用/动作/验收证据
                -> main 中的统一归档和报告
```

## 1. 安装

建议使用 Python 3.11-3.13 的独立虚拟环境，不把 Browser Use 的大量依赖装入 KT6
生产环境：

```powershell
py -3.12 -m venv .venv-browser-use
.\.venv-browser-use\Scripts\Activate.ps1
python -m pip install -r .\requirements-evaluation-browser-use.txt
python -m browser_use install
```

当前锁定 `browser-use==0.13.7`。升级版本时必须重新运行本分支单元测试和真实页面冒烟
测试，因为 Agent、Browser 和历史对象属于上游接口。

## 2. 执行任务文件

每个任务使用独立 JSON。`allowed_hosts` 同时约束初始 URL、Browser Use 导航和动作审计；
`validation.assertions` 是本地验收条件，当前支持 `url_equals`、`url_contains`、
`title_contains` 和 `text_contains`：

```json
{
  "schema_version": "kt6.evaluation-execution-task.v1",
  "suite_id": "nce-ip-simple-query-28",
  "task_id": "T01",
  "instruction": "打开告警页面并查看严重告警。",
  "start_url": "https://nce.example.test/home",
  "allowed_hosts": ["nce.example.test"],
  "step_limit": 10,
  "validation": {
    "method": "page_state",
    "assertions": [
      {"kind": "url_contains", "value": "/alarms"},
      {"kind": "text_contains", "value": "严重告警"}
    ]
  }
}
```

模型声称 `done/success` 不会直接计为成功。所有断言通过、动作轨迹没有越域或危险动作，
最终结果才是 `success`。

## 3. API 配置

先创建一次根目录 `.env`，后续运行 CLI 会自动读取。API key 不进入命令行、suite、
运行 JSON 或报告：

```powershell
Copy-Item .\.env.example .\.env
notepad .\.env
```

在 `.env` 中填写：

```dotenv
KT6_MODEL_API_PROVIDER=<供应商或内网网关标识>
KT6_MODEL_API_BASE_URL=https://<获批网关>/v1
KT6_MODEL_API_KEY=<只在本机.env保存>
KT6_MODEL_API_MODEL=<评测统一的精确模型名>
KT6_MODEL_API_ALLOWED_HOSTS=<获批网关的精确主机名>
KT6_BROWSER_USE_CDP_URL=http://127.0.0.1:9222
```

远程 endpoint 必须同时满足 HTTPS、精确主机白名单和命令行
`--allow-remote-model`。这是显式数据出区门禁：受限测试区页面 DOM、任务文本、URL 或
账号信息未经批准不得发给任何公网模型。受限环境应改用批准的内网兼容网关，并只把该
网关主机加入白名单。

Browser Use 匿名遥测与 Cloud Sync 会在导入库前关闭；视觉输入关闭，避免截图进入规划
模型。Browser Use 默认 `evaluate`、文件读写、上传和站外搜索工具也被排除。

## 4. 执行一次任务

确保 `suite.json` 已是 `status=ready`，且 suite 中共享规划模型与环境编号和本次参数一致：

```powershell
python -m kt6_backend.browser_use_evaluation_cli `
  --suite .\runtime_data\evaluation\nce\suite.json `
  --task .\runtime_data\evaluation\nce\tasks\T01.json `
  --runs .\runtime_data\evaluation\nce\runs.jsonl `
  --workspace .\runtime_data\evaluation\nce\raw-browser-use `
  --repetition 1 `
  --implementation-revision '<当前提交哈希>' `
  --environment-id '<suite中的环境编号>' `
  --allow-remote-model
```

连接现有浏览器时，CDP URL 只允许 `localhost`、`127.0.0.1` 或 `::1`。不设置 CDP URL
时，Browser Use 启动隔离 Chromium。`allowed_domains` 使用任务文件中的精确主机集合。

## 5. 证据与报告

一次可归档运行保存：

- `dom_snapshot`：最终页面状态和本次实际交互的 DOM 元素；
- `planner_result`：每个 Browser Use 步骤的模型结构化响应，并记录真实供应商与模型名；
- `action_trace`：每一步动作、耗时、错误和安全违规；
- `browser_use_elements`：供测试区复查的精简历史；
- `validation_result`：确定性终态判断。

原始证据仅保存在 `runtime_data/`，统一报告不嵌 DOM 或模型原文。若 Browser Use 没有
返回可审计历史，执行器保留 raw workspace 供本地诊断，但不会伪造一条可参与排名的
运行记录。

## 6. 测试

单元测试使用注入的 Fake Backend，不依赖浏览器、API key 或公网：

```powershell
python -m unittest `
  tests.test_browser_use_evaluation `
  tests.test_evaluation_executor `
  tests.test_openai_compatible_api
```

真实冒烟测试需在获批网络和测试页面执行，至少检查：供应商和模型精确版本、API 调用
次数、动作步数、越域拒绝、DOM 证据完整性、确定性验收与最终报告排名门禁。
