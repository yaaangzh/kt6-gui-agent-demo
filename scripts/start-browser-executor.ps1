[CmdletBinding()]
param(
    [string]$PythonPath = "",
    [string]$ChromePath = "",
    [string]$InitialTargetUrl = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtimeRoot = Join-Path $projectRoot "runtime_data"
$logDir = Join-Path $runtimeRoot "logs"
$extensionPath = Join-Path $projectRoot "browser_extension"
$backendUrl = "http://127.0.0.1:8787"

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

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
            & $candidate -c "import kt6_backend" 2>$null
            if ($LASTEXITCODE -eq 0) {
                return (Resolve-Path -LiteralPath $candidate).Path
            }
        }
    }
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }
    throw "Python was not found. Pass -PythonPath explicitly."
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
        (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw "Chrome was not found. Pass -ChromePath explicitly."
}

$targetUri = $null
$targetUrlText = $InitialTargetUrl.Trim()
if ($targetUrlText) {
    try {
        $targetUri = [Uri]$targetUrlText
    }
    catch {
        throw "InitialTargetUrl must be a valid HTTP/HTTPS URL."
    }
    if (-not $targetUri.IsAbsoluteUri -or $targetUri.Scheme -notin @("http", "https")) {
        throw "InitialTargetUrl must be a valid HTTP/HTTPS URL."
    }
}

$python = Resolve-PythonExecutable
& $python -c "import browser_harness" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Browser Harness is not installed for $python. Run: $python -m pip install -r requirements-browser-executor.txt"
}
$env:KT6_BROWSER_EXECUTION_DRIVER = "browser_harness"

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
        throw "KT6 backend did not become ready in 15 seconds. Check $stderrLog."
    }
}

$execution = Test-JsonEndpoint -Url "$backendUrl/api/execution/health"
if ($null -eq $execution) {
    throw "The backend has no execution health endpoint. Stop the old backend and rerun this script."
}
if (
    $null -eq $execution.PSObject.Properties["transport"] -or
    $execution.transport -ne "browser_harness"
) {
    throw "Port 8787 is serving an old browser runtime. Stop that backend and rerun this script."
}
if (-not $execution.configured -or -not $execution.planner_configured) {
    $executionJson = $execution | ConvertTo-Json -Compress -Depth 6
    throw "The execution runtime configuration is incomplete: $executionJson"
}

Write-Host "[ready] KT6 backend: $backendUrl"
Write-Host ""
Write-Host "KT6 Browser Harness runtime is ready for your daily Chrome."
Write-Host "Extension directory: $extensionPath"
Write-Host "1. Open chrome://extensions in your regular Chrome."
Write-Host "2. Enable Developer mode, choose Load unpacked, and select the extension directory above."
if ($null -ne $targetUri) {
    $browser = Resolve-ChromeExecutable
    Start-Process -FilePath $browser -ArgumentList @("--new-tab", $targetUri.AbsoluteUri) | Out-Null
    Write-Host "3. Opened a new tab in the existing Chrome: $($targetUri.AbsoluteUri)"
}
else {
    Write-Host "3. Open any HTTP/HTTPS target page in your existing Chrome."
}
Write-Host "4. Open chrome://inspect/#remote-debugging and enable Allow remote debugging for this browser instance."
Write-Host "5. Click KT6 Browser Agent in the target tab and enter the natural-language workflow."
Write-Host "6. On the first run, click Allow in Chrome when Browser Harness asks to attach."
Write-Host ""
Write-Host "The extension only selects the current tab; Browser Harness performs capture and actions."
Write-Host "No fixed remote-debugging port, dedicated Chrome profile, or new browser instance is used."
Write-Host "Planning uses the OpenAI-compatible KT6_MODEL_API_* settings from .env."
