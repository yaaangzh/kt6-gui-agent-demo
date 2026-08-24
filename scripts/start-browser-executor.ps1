[CmdletBinding()]
param(
    [string]$PythonPath = "",
    [string]$ChromePath = "",
    [int]$CdpPort = 9222,
    [int]$BackendPort = 8787,
    [string]$InitialTargetUrl = "about:blank"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtimeRoot = Join-Path $projectRoot "runtime_data"
$workspace = Join-Path $runtimeRoot "browser_harness_workspace"
$chromeProfile = Join-Path $runtimeRoot "chrome-cdp-profile"
$extensionPath = Join-Path $projectRoot "browser_extension"
$logDir = Join-Path $runtimeRoot "logs"
$cdpUrl = "http://127.0.0.1:$CdpPort"
$backendUrl = "http://127.0.0.1:$BackendPort"

New-Item -ItemType Directory -Force -Path $workspace, $chromeProfile, $logDir | Out-Null

function Test-JsonEndpoint {
    param([Parameter(Mandatory)][string]$Url)
    try {
        return Invoke-RestMethod -Uri $Url -TimeoutSec 2
    }
    catch {
        return $null
    }
}

function Wait-JsonEndpoint {
    param(
        [Parameter(Mandatory)][string]$Url,
        [Parameter(Mandatory)][int]$TimeoutSeconds
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $result = Test-JsonEndpoint -Url $Url
        if ($null -ne $result) {
            return $result
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    return $null
}

function Resolve-PythonExecutable {
    if ($PythonPath) {
        return (Resolve-Path -LiteralPath $PythonPath).Path
    }
    $candidates = @(
        (Join-Path $projectRoot ".venv-browser-executor\Scripts\python.exe"),
        (Join-Path $runtimeRoot "tools\python312\python.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            & $candidate -c "import browser_harness" 2>$null
            if ($LASTEXITCODE -eq 0) {
                return (Resolve-Path -LiteralPath $candidate).Path
            }
        }
    }
    throw "找不到已安装 browser-harness 的 Python。请先按 test.md 创建 .venv-browser-executor。"
}

function Resolve-ChromeExecutable {
    if ($ChromePath) {
        return (Resolve-Path -LiteralPath $ChromePath).Path
    }
    $command = Get-Command chrome.exe -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }
    $candidates = @(
        (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
        (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe"),
        (Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe")
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw "找不到 Chrome 或 Edge。可通过 -ChromePath 显式指定浏览器路径。"
}

$python = Resolve-PythonExecutable
$env:KT6_BROWSER_EXECUTION_DRIVER = "browser_harness"
$env:KT6_BROWSER_HARNESS_CDP_URL = $cdpUrl
$env:BU_CDP_URL = $cdpUrl
$env:BH_AGENT_WORKSPACE = $workspace

$cdp = Test-JsonEndpoint -Url "$cdpUrl/json/version"
if ($null -eq $cdp) {
    $browser = Resolve-ChromeExecutable
    $arguments = @(
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=$CdpPort",
        "--user-data-dir=$chromeProfile",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions-except=$extensionPath",
        "--load-extension=$extensionPath",
        $InitialTargetUrl
    )
    Start-Process -FilePath $browser -ArgumentList $arguments | Out-Null
    $cdp = Wait-JsonEndpoint -Url "$cdpUrl/json/version" -TimeoutSeconds 15
    if ($null -eq $cdp) {
        throw "浏览器已启动，但 CDP $cdpUrl 在 15 秒内没有就绪。"
    }
}
Write-Host "[ready] Chrome CDP: $cdpUrl"

& $python -m kt6_backend.execution.runtime_preflight ensure `
    --cdp-url $cdpUrl `
    --workspace $workspace `
    --wait 15
if ($LASTEXITCODE -ne 0) {
    throw "Browser Harness 预热失败。"
}
Write-Host "[ready] Browser Harness"

$backend = Test-JsonEndpoint -Url "$backendUrl/api/health"
if ($null -eq $backend) {
    $stdoutLog = Join-Path $logDir "browser-executor-backend.stdout.log"
    $stderrLog = Join-Path $logDir "browser-executor-backend.stderr.log"
    Start-Process `
        -FilePath $python `
        -ArgumentList @("-m", "kt6_backend.app") `
        -WorkingDirectory $projectRoot `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -WindowStyle Hidden | Out-Null
    $backend = Wait-JsonEndpoint -Url "$backendUrl/api/health" -TimeoutSeconds 15
    if ($null -eq $backend) {
        throw "KT6 后端在 15 秒内没有就绪，请检查 $stderrLog。"
    }
}
Write-Host "[ready] KT6 backend: $backendUrl"

$execution = $null
$executionDeadline = [DateTime]::UtcNow.AddSeconds(15)
do {
    $execution = Test-JsonEndpoint -Url "$backendUrl/api/execution/health"
    if ($null -ne $execution -and $execution.ready) {
        break
    }
    Start-Sleep -Milliseconds 250
} while ([DateTime]::UtcNow -lt $executionDeadline)
if ($null -eq $execution) {
    throw "后端未提供执行链健康接口；请关闭旧后端进程后重新运行本脚本。"
}
if (-not $execution.ready) {
    $executionJson = $execution | ConvertTo-Json -Compress -Depth 6
    throw "执行链在 15 秒内仍未就绪：$executionJson"
}

Write-Host ""
Write-Host "KT6 浏览器执行链已就绪。"
Write-Host "Runner: $backendUrl/execution-runner.html"
Write-Host "Chrome 扩展: 点击工具栏中的 KT6 Browser Agent 打开侧边栏。"
Write-Host "Runner 与受控 Target Chrome 必须保持为两个独立页面。"
