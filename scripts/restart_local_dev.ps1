[CmdletBinding()]
param(
    [switch]$SkipBuild,
    [int]$WebPort = 5173,
    [int]$GatewayPort = 19000
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$python = (Resolve-Path -LiteralPath (Join-Path $repo ".venv\Scripts\python.exe")).Path
$frontend = Join-Path $repo "jiuwenswarm\channels\web\frontend"
$distIndex = Join-Path $frontend "dist\index.html"
$logDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Stop-JiuwenPortProcess {
    param(
        [int]$Port,
        [string]$ExpectedModule
    )

    $listeners = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
        if (-not $process.CommandLine -or $process.CommandLine -notmatch [regex]::Escape($ExpectedModule)) {
            throw "Port $Port is owned by an unexpected process: $($process.CommandLine)"
        }
        Stop-Process -Id $listener.OwningProcess
    }
}

function Wait-LocalPort {
    param(
        [int]$Port,
        [int]$TimeoutSeconds = 30
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
            return
        }
        Start-Sleep -Milliseconds 300
    }
    throw "Port $Port did not become ready within $TimeoutSeconds seconds"
}

if (-not $SkipBuild) {
    Push-Location $frontend
    try {
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) {
            throw "Frontend build failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

if (-not (Test-Path -LiteralPath $distIndex)) {
    throw "Frontend dist is missing: $distIndex"
}

Stop-JiuwenPortProcess -Port $WebPort -ExpectedModule "jiuwenswarm.channels.web.app_web"
Stop-JiuwenPortProcess -Port $GatewayPort -ExpectedModule "jiuwenswarm.gateway.app_gateway"

Start-Process -FilePath $python `
    -ArgumentList "-m", "jiuwenswarm.gateway.app_gateway" `
    -WorkingDirectory $repo `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir "gateway-local.out.log") `
    -RedirectStandardError (Join-Path $logDir "gateway-local.err.log")

Start-Process -FilePath $python `
    -ArgumentList "-m", "jiuwenswarm.channels.web.app_web" `
    -WorkingDirectory $repo `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir "web-local.out.log") `
    -RedirectStandardError (Join-Path $logDir "web-local.err.log")

Wait-LocalPort -Port $GatewayPort
Wait-LocalPort -Port $WebPort

$distHtml = Get-Content -LiteralPath $distIndex -Raw
$expectedAsset = [regex]::Match($distHtml, 'src="([^"]+\.js)"').Groups[1].Value
$servedHtml = (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$WebPort/").Content
$servedAsset = [regex]::Match($servedHtml, 'src="([^"]+\.js)"').Groups[1].Value
if (-not $expectedAsset -or $servedAsset -ne $expectedAsset) {
    throw "Frontend version mismatch: built=$expectedAsset served=$servedAsset"
}

Write-Output "Jiuwen local services are ready."
Write-Output "Web:     http://127.0.0.1:$WebPort ($servedAsset)"
Write-Output "Gateway: ws://127.0.0.1:$GatewayPort/ws"
