param(
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = "C:\Users\x1462\AppData\Local\Programs\Python\Python312\python.exe"
$url = "http://127.0.0.1:$Port/"
$healthUrl = "${url}api/health"
$launcherLog = Join-Path $projectDir "runtime\launcher.log"
$serverOutputLog = Join-Path $projectDir "runtime\server-output.log"
$serverErrorLog = Join-Path $projectDir "runtime\server-error.log"

function Write-LauncherLog([string]$Message) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $launcherLog -Value $line -Encoding UTF8
}

function Test-BackendHealth {
    try {
        $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

New-Item -ItemType Directory -Path (Join-Path $projectDir "runtime") -Force | Out-Null

if (-not (Test-Path -LiteralPath $python)) {
    Write-LauncherLog "Python executable not found: $python"
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show("Python was not found. Please contact the maintainer.", "Flight Price Tracker") | Out-Null
    exit 1
}

if (-not (Test-BackendHealth)) {
    Write-LauncherLog "Starting backend on port $Port"
    $arguments = @(
        "main.py",
        "serve-ui",
        "--host", "127.0.0.1",
        "--port", "$Port"
    )
    Start-Process `
        -FilePath $python `
        -ArgumentList $arguments `
        -WorkingDirectory $projectDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $serverOutputLog `
        -RedirectStandardError $serverErrorLog | Out-Null

    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Seconds 1
        if (Test-BackendHealth) {
            $ready = $true
            break
        }
    }

    if (-not $ready) {
        Write-LauncherLog "Backend did not become ready within 30 seconds"
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show(
            "The backend did not start within 30 seconds. Please check runtime\server-error.log.",
            "Flight Price Tracker"
        ) | Out-Null
        exit 1
    }
    Write-LauncherLog "Backend is ready"
}
else {
    Write-LauncherLog "Backend already running"
}

Start-Process $url
