# KT6 三方案评测、证据归档与报告流程

本流程在同一任务集、同一规划模型和同一测试环境下，对比三套实现：

1. `current`：DOM + OpenCV/OCR + 视觉模型补充 + UI Graph + Playwright。
2. `browser_use`：DOM/CDP + Browser Use + Playwright/CDP。
3. `ui_tars`：逐步截图 + UI-TARS 视觉定位 + Playwright。

评测模块不调用模型、不控制浏览器，也不会上传文件。三套执行器在获批环境中完成真实
运行后，把结果和明确列出的证据文件交给本地归档命令。报告只汇总指标和证据状态，
不会把截图、DOM、模型原文或敏感 URL 嵌入报告。

## 1. 新框架保存什么

每次运行不再只写一个结果 JSON，而是分为三层：

```text
runtime_data/evaluation/<suite>/
├─ suite.json                     固定任务、方案、公平性和验收条件
├─ runs.jsonl                     运行索引、指标、Manifest 引用和哈希
├─ pending-*.json                 待归档的单次运行结果，可在记录后移走
├─ artifacts/
│  ├─ current/T01/r001/
│  │  ├─ original-screenshot-001.png
│  │  ├─ cv-result-001.json
│  │  ├─ cv-metadata-001.json
│  │  ├─ model-result-001.json
│  │  ├─ vision-model-call-001.jsonl
│  │  ├─ routing-result-001.json
│  │  ├─ fused-result-001.json
│  │  ├─ ui-graph-001.json
│  │  ├─ planner-result-001.jsonl
│  │  ├─ action-trace-001.jsonl
│  │  ├─ validation-result-001.json
│  │  ├─ run-metadata-001.json
│  │  └─ manifest.json
│  ├─ browser_use/T01/r001/...
│  └─ ui_tars/T01/r001/...
└─ report-<timestamp>/
   ├─ report.json
   ├─ metrics.csv
   ├─ runs.csv
   ├─ report.md
   ├─ report.html
   ├─ issues.md
   └─ conclusion.md
```

`record` 只复制命令行中显式声明的文件，不会扫描或递归复制整个运行目录。源文件不会
被移动、改名或覆盖。最终证据目录采用固定的 `方案/任务/rNNN` 路径并独占创建，同一
次运行不能覆盖重录。

`manifest.json` 记录每个文件的角色、归档内相对路径、媒体类型、字节数和 SHA-256，
并绑定 suite、方案、任务、重复序号、实现版本、规划模型和测试环境。`runs.jsonl` 保存
Manifest 的相对路径及其 SHA-256。普通 SHA-256 用于发现遗漏或意外修改，不代表电子
签名或人员身份认证。

## 2. 三套方案的必需证据

所有方案都必须保存：

- `action_trace`：JSONL，每行一个实际步骤、等待、重试或验证事件。
- `validation_result`：JSON，保存确定性终态判定。
- `planner_result`：只要 `planner_model_calls > 0` 就必需，保存规划模型结构化输出；可用
  一份 JSONL 汇总多次调用。

`action_trace` 每行必须包含同一个 `run_id`；真实任务步骤使用从 1 开始的
`step_index`，并覆盖 run JSON 声明的全部 `step_count`。等待、重试、验证等附属事件可
复用所属步骤序号。例如：

```json
{"schema_version":"kt6.evaluation-action-event.v1","run_id":"current-T01-r1","step_index":1,"action":"locate","safety_violation":false,"elapsed_ms":35}
```

每条动作事件必须显式给出 `safety_violation` 布尔值；整份轨迹中 `true` 的数量必须等于
run JSON 的 `metrics.safety_violation_count`，防止报告指标与原始操作轨迹互相矛盾。

规划结果使用评测 envelope，而不是把一段无法关联调用的模型文本直接改名。每个
`call_index` 必须覆盖 `1..planner_model_calls`，生产者要与 run JSON 的 planner 一致，
`input_refs` 引用同一证据包中的文件 SHA-256、UI Graph ID 或 capture ID：

```json
{"schema_version":"kt6.evaluation-planner-call.v1","run_id":"current-T01-r1","call_index":1,"producer":{"provider":"<实际供应商或网关名>","model":"<精确模型名>"},"input_refs":["uig:..."],"response":{"actions":[{"type":"locate"}]}}
```

`validation_result` 至少使用以下结构，并与 run JSON 保持一致：

```json
{
  "schema_version": "kt6.evaluation-validation-result.v1",
  "evidence_id": "validation_result-001",
  "run_id": "current-T01-r1",
  "task_id": "T01",
  "method": "page_state",
  "passed": true
}
```

不同方案另有不同要求：

| 方案 | 必需证据 |
|---|---|
| `current` 的纯 DOM 任务 | `ui_graph` |
| `current` 实际调用 CV | 每次调用对应一份 `original_screenshot`、`cv_result`、`cv_metadata`、`routing_result`；成功运行还必须有 `fused_result` 和 `ui_graph` |
| `current` 实际调用视觉模型 | 增加一份覆盖全部调用的 `vision_model_call`；只有成功调用才增加对应 `model_result` |
| `browser_use` | `dom_snapshot` 或 `cdp_snapshot` 至少一个 |
| `ui_tars` | 每次视觉调用一份 `original_screenshot`，以及统一 envelope 格式的 `ui_tars_response`；调用数必须覆盖全部步骤 |

UI-TARS 响应集中保存在一份 JSONL。每行必须带 `run_id`、`step_index`、`call_index`、
视觉模型 `producer`、对应 `screenshot_artifact_id`、`screenshot_sha256` 和非空
`response`，并覆盖全部步骤和 `vision_model_calls`。重复传入的截图会按命令顺序归档为
`original-screenshot-001/002/...`；每个 artifact 必须恰好被一条响应引用。两个步骤即使
画面字节完全相同，也使用两个不同 artifact ID，SHA-256 只负责校验文件完整性。
`model_result` 是 KT6 Canvas/GLM 拓扑协议，不能冒充 UI-TARS 响应。例如：

```json
{"schema_version":"kt6.evaluation-ui-tars-response.v1","run_id":"ui_tars-T01-r1","step_index":1,"call_index":1,"producer":{"provider":"ui-tars","model":"UI-TARS-1.5-7B"},"screenshot_artifact_id":"original-screenshot-001","screenshot_sha256":"<64位哈希>","response":{"action":"click","coordinates":[100,200]}}
```

现有方案每次 CV 调用严格对应一张原图以及一份 `cv_result`、`cv_metadata` 和路由；成功
运行还要对应融合结果。视觉模型调用使用一份 JSONL ledger 保存每次调用的成功或失败
状态，不能因为超时、传输错误或无效 JSON 没有合法 `model_result` 就丢弃失败样本。每行
至少包含下面字段；
`success` 还必须绑定 `model_result_artifact_id` 和哈希，失败状态必须给出短错误码，
`invalid_response` 还要引用同包的 `model_events` 或 `model_stderr` 原始诊断：

```json
{"schema_version":"kt6.evaluation-vision-model-call.v1","run_id":"current-T01-r1","call_index":1,"producer":{"provider":"codeagent","model":"GLM-5.1"},"status":"success","duration_ms":1260,"screenshot_artifact_id":"original-screenshot-001","screenshot_sha256":"<64位截图哈希>","routing_artifact_id":"routing-result-001","model_result_artifact_id":"model-result-001","model_result_sha256":"<64位结果哈希>","diagnostic_artifact_ids":["model-events-001"]}
```

```json
{"schema_version":"kt6.evaluation-vision-model-call.v1","run_id":"current-T01-r1","call_index":1,"producer":{"provider":"codeagent","model":"GLM-5.1"},"status":"timeout","duration_ms":300000,"screenshot_artifact_id":"original-screenshot-001","screenshot_sha256":"<64位截图哈希>","routing_artifact_id":"routing-result-001","diagnostic_artifact_ids":[],"error_code":"transport_timeout"}
```

框架会校验“截图 artifact/SHA → CV 元数据 → CV 结果 SHA → 路由 source → 模型调用状态
→ 融合内嵌路由”的关系，还会重算 UI Graph ID。失败运行可以没有模型结果、融合结果或
最终 UI Graph，但必须保留已经实际产生的 CV、路由、调用状态、动作轨迹和终态验证。
同一个源文件不能同时冒充多个证据角色。处理后截图、Browser Use 精炼元素、模型事件和
stderr 等不是所有运行的硬门槛，但建议按实际产生情况一并归档。

`dom_snapshot` 是 Browser Use 执行器需要生成的规范化评测 envelope，不等于现有扩展的
任意 DOM JSON；它至少包含 schema、run/task 身份、`safe_for_execution=false` 和元素
数组。`cdp_snapshot` 当前只接受 Sidecar 原始 `kt6.cdp-page-snapshot.v1`，不把后端的
`kt6.cdp-multisource.v1` 静默混成同一种文件。

支持的 `--artifact` 角色如下：

```text
original_screenshot   processed_screenshot
cv_result             cv_metadata
model_result          vision_model_call     planner_result
model_events          model_stderr
routing_result        fused_result
ui_graph              dom_snapshot           cdp_snapshot
browser_use_elements
ui_tars_response      ui_tars_actions
action_trace          validation_result
```

## 3. 初始化评测套件

在项目根目录执行：

```powershell
$evalDir = '.\runtime_data\evaluation\nce-simple-query'

python -m kt6_backend.evaluation_report_cli init `
  --out "$evalDir\suite.json" `
  --suite-id nce-ip-simple-query-28 `
  --title 'NCE-IP简单查询类任务' `
  --task-count 28 `
  --repetitions 3 `
  --step-limit 10 `
  --planner-provider '<实际供应商或网关名>' `
  --planner-model '<API实际返回的精确模型名>' `
  --environment-id '<测试机和浏览器环境编号>'
```

命令不会覆盖已有 `suite.json`。新 suite 保持 `status=draft`，需要人工补全：

- 28 个任务的真实标题、统一任务提示版本和操作步数上限。
- `scenario_type`：`dom/canvas/mixed/complex/other`。
- `difficulty`：`easy/medium/hard/unknown`。
- 可确定性复核的 `validation.method` 和 `validation.description`。
- 规划模型的实际供应商、精确模型名、浏览器/测试环境编号。
- 三套实现的精确版本或 Git revision。

确认后把 `status` 改成 `ready`。不要把 API key 写入 suite。

## 4. 填写单次运行结果

以现有方案 T01 第一次执行为例：

```powershell
python -m kt6_backend.evaluation_report_cli run-template `
  --suite "$evalDir\suite.json" `
  --scheme current `
  --task T01 `
  --repetition 1 `
  --out "$evalDir\pending-current-T01-r1.json"
```

执行器需要把模板中的草稿字段改成真实值，主要结构如下：

```json
{
  "schema_version": "kt6.evaluation-run.v1",
  "record_status": "final",
  "run_id": "current-T01-r1",
  "suite_id": "nce-ip-simple-query-28",
  "scheme_id": "current",
  "task_id": "T01",
  "repetition": 1,
  "outcome": "success",
  "started_at": "2026-08-13T09:00:00+08:00",
  "duration_ms": 8250,
  "step_count": 4,
  "model_calls": 2,
  "implementation": {
    "name": "KT6 current hybrid",
    "version": "0.1.0",
    "revision": "<Git提交哈希或镜像digest>",
    "branch": "ui-graph-textflow-cdp"
  },
  "task_prompt_version": "v1",
  "planner": {
    "provider": "<实际供应商或网关名>",
    "model": "<精确模型名>",
    "adapter_prompt_version": "current-adapter-v1"
  },
  "environment": {
    "environment_id": "<统一环境编号>",
    "browser": "Chrome/Edge 精确版本",
    "viewport": "1920x1080"
  },
  "metrics": {
    "first_target_hit": true,
    "misclick_count": 0,
    "retry_count": 0,
    "loop_count": 0,
    "timeout_count": 0,
    "safety_violation_count": 0,
    "cv_calls": 1,
    "planner_model_calls": 1,
    "vision_model_calls": 1,
    "input_tokens": 1000,
    "output_tokens": 200,
    "cost": 0.01
  },
  "validation": {
    "method": "page_state",
    "passed": true,
    "evidence_ref": "validation_result-001"
  },
  "failure": null,
  "evidence": {"status": "pending"},
  "notes": ""
}
```

`model_calls` 必须等于 `planner_model_calls + vision_model_calls`。这样报告可以区分
“规划模型耗时/调用”和“视觉补充模型耗时/调用”，不会把 UI-TARS 或 GLM 视觉调用
混成一项。

`outcome` 只接受 `success/partial/failure/timeout/blocked`。非成功结果必须填写
`failure.category` 和 `failure.reason`；成功结果必须满足 `validation.passed=true`，且
不能超过任务步数上限。模型判定只能作为补充，优先使用 DOM、页面状态或获批业务 API
的确定性结果。

## 5. 归档证据并追加运行索引

先把本次运行的源文件集中在测试区专用临时目录，再显式传给 `record`。现有方案 Canvas
示例：

```powershell
$runSrc = '.\runtime_data\raw-runs\current-T01-r1'

python -m kt6_backend.evaluation_report_cli record `
  --suite "$evalDir\suite.json" `
  --runs "$evalDir\runs.jsonl" `
  --input "$evalDir\pending-current-T01-r1.json" `
  --artifact "original_screenshot=$runSrc\original.png" `
  --artifact "cv_result=$runSrc\opencv-ocr.json" `
  --artifact "cv_metadata=$runSrc\cv-metadata.json" `
  --artifact "model_result=$runSrc\glm-result.json" `
  --artifact "vision_model_call=$runSrc\vision-model-calls.jsonl" `
  --artifact "routing_result=$runSrc\routing.json" `
  --artifact "fused_result=$runSrc\fused.json" `
  --artifact "ui_graph=$runSrc\ui-graph.json" `
  --artifact "planner_result=$runSrc\planner-results.jsonl" `
  --artifact "action_trace=$runSrc\actions.jsonl" `
  --artifact "validation_result=$runSrc\validation.json"
```

Browser Use 示例：

```powershell
python -m kt6_backend.evaluation_report_cli record `
  --suite "$evalDir\suite.json" `
  --runs "$evalDir\runs.jsonl" `
  --input "$evalDir\pending-browser-use-T01-r1.json" `
  --artifact "dom_snapshot=$runSrc\dom-snapshot.json" `
  --artifact "browser_use_elements=$runSrc\elements.json" `
  --artifact "planner_result=$runSrc\planner-results.jsonl" `
  --artifact "action_trace=$runSrc\actions.jsonl" `
  --artifact "validation_result=$runSrc\validation.json"
```

UI-TARS 多步骤截图示例：

```powershell
python -m kt6_backend.evaluation_report_cli record `
  --suite "$evalDir\suite.json" `
  --runs "$evalDir\runs.jsonl" `
  --input "$evalDir\pending-ui-tars-T01-r1.json" `
  --artifact "original_screenshot=$runSrc\step-01.png" `
  --artifact "original_screenshot=$runSrc\step-02.png" `
  --artifact "ui_tars_response=$runSrc\responses.jsonl" `
  --artifact "ui_tars_actions=$runSrc\actions-normalized.jsonl" `
  --artifact "planner_result=$runSrc\planner-results.jsonl" `
  --artifact "action_trace=$runSrc\playwright-actions.jsonl" `
  --artifact "validation_result=$runSrc\validation.json"
```

归档成功后会输出 Manifest 相对路径及 SHA-256；任一必需证据缺失、文件为空、JSON
schema/运行身份/调用序号不匹配、截图或中间结果哈希绑定错误、UI Graph ID 不一致、
图片尺寸或容器不完整、源文件在复制时改变、运行键重复或目标目录已经存在，整次记录
都会失败。命令错误只输出固定错误码和阶段，不把底层业务 ID、模型原文或绝对源路径
复制到终端/CI 日志。suite、run、Manifest 和证据 JSON 都按严格 JSON 解析；重复键以及
`NaN/Infinity` 等非标准常量会直接拒绝，不能依赖“后一个字段覆盖前一个字段”。
不要用手工复制来伪造归档目录。

## 6. 校验和生成报告

随时检查覆盖率、公平性和证据完整性：

```powershell
python -m kt6_backend.evaluation_report_cli validate `
  --suite "$evalDir\suite.json" `
  --runs "$evalDir\runs.jsonl"
```

28 个任务、3 个方案、每任务重复 3 次，应有 `28 × 3 × 3 = 252` 次运行。校验会重新
读取每个 Manifest，并重新计算所有归档文件哈希。缺文件、内容改动、Manifest 与任务
身份不符或必需角色不足，都会标为 `evidence_invalid`，该方案不能自动排名。
`validate` 检测到证据失败时返回非零退出码，便于自动化流水线直接阻断。
退出码约定：`4=证据失败`、`5=公平性失败`、`6=运行未完成`、`7=安全违规`、
`8=数据完整但没有可推荐成功方案`；参数、契约或文件读写错误仍返回 `3`。

生成时间戳目录，避免覆盖旧报告：

```powershell
$reportDir = "$evalDir\report-$(Get-Date -Format 'yyyyMMdd-HHmmss')"

python -m kt6_backend.evaluation_report_cli report `
  --suite "$evalDir\suite.json" `
  --runs "$evalDir\runs.jsonl" `
  --out-dir $reportDir

Start-Process "$reportDir\report.html"
```

报告目录采用独占创建；如果目录已经存在，命令会拒绝覆盖。重跑时请生成新的时间戳
目录，保留历史报告供追溯。

报告生成七个文件：

```text
report.json     机器可读的汇总、脱敏运行索引和证据校验状态
metrics.csv     方案总体、页面类型和难度分组指标
runs.csv        每次运行的扁平指标与 Manifest 引用
report.md       评审和周报主报告
report.html     浏览器展示版
issues.md       缺失运行、公平性、安全和证据问题
conclusion.md   是否具备排名条件及推荐结论
```

报告同时给出：

- `observed_success_rate`：只看已经记录的运行。
- `strict_success_rate`：成功数除以全部应执行次数，缺失运行不会被隐藏。
- 任务耗时和归档耗时分离；截图复制和哈希不计入 `duration_ms`。
- 只有三组覆盖完整、模型/提示/环境一致、证据全部校验通过且安全达标时才自动排名。
- 安全违规方案退出推荐；其余方案先比较完成率，再比较稳定性、P95 耗时和成本。

## 7. 数据安全和运维约束

- `runtime_data/` 已被 `.gitignore` 忽略，但仍需使用测试区访问控制和保留周期。
- 截图、DOM/CDP、UI Graph、模型响应和 CodeAgent events 可能包含敏感页面数据。
- 不把证据、Manifest、runs、报告或 API key 提交 Git、上传公网或粘贴到外部模型。
- `model_events` 可能包含图片 Base64；仅在确有排障需要时归档，不在终端完整打印。
- 原始证据与脱敏展示材料分开保存。报告默认只显示统计和相对引用，不展示原始内容。
- `failure.reason`、`notes` 和 `validation.evidence_ref` 只保留在测试区运行归档中，报告
  JSON/CSV/Markdown 不复制这些自由文本；领导报告只显示失败类别和统计。
- SHA-256 用于完整性检查；如需防止有权限人员同时改写文件和索引，需要另加签名或
  HMAC，不要把普通哈希表述为不可抵赖证明。
- 运行目录一旦记录不得覆盖。确需重测时增加 repetition，或创建新的 suite。

## 8. 自动化回归

```powershell
python -m unittest `
  tests.test_evaluation_artifacts `
  tests.test_evaluation_report
```

自动化覆盖 suite/run 契约、三方案证据矩阵、逐调用/逐步骤 envelope、截图到融合的
交叉哈希绑定、UI Graph 内容 ID、独占归档、Manifest/文件哈希、篡改阻断、重复记录、
缺失运行、严格完成率、公平性、自评偏差、安全淘汰、CSV/Markdown/HTML 和完整 CLI
流程。Windows 受限环境若对临时目录返回 `PermissionError`，应在获批环境中提升权限
重跑，不能把权限错误当成代码失败。

## 9. 当前边界

本模块完成的是“结果契约、原始证据归档、完整性校验、指标汇总、问题分析和报告生成”
闭环，不会替三套方案执行真实页面任务：

```text
现有方案执行器 ─┐
Browser Use执行器 ├─> run JSON + 原始证据 ─> 归档/Manifest ─> 报告
UI-TARS执行器 ───┘
```

三套真实执行器仍需分别接入统一契约。任意公网模型 API 只能用于允许数据外发的脱敏
环境；正式测试区数据不可外传时，必须使用获批内网接口或本地部署。
