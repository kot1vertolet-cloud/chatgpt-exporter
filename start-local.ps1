param(
    [Parameter(Mandatory)] [string] $OpenWebUIRoot,
    [string] $StateDir = (Join-Path $PSScriptRoot 'data'),
    [string] $WebUIDataDir,
    [ValidateRange(1, 2048)] [int] $UploadLimitMiB = 256,
    [ValidateRange(1, 2048)] [int] $ArchiveLimitMiB = 1024
)

$ErrorActionPreference = 'Stop'
$webuiRoot = (Resolve-Path -LiteralPath $OpenWebUIRoot).Path
$webuiExe = (Resolve-Path -LiteralPath (Join-Path $webuiRoot '.venv\Scripts\open-webui.exe')).Path
$pythonExe = (Resolve-Path -LiteralPath (Join-Path $webuiRoot '.venv\Scripts\python.exe')).Path
$nodeExe = (Get-Command node.exe -CommandType Application | Select-Object -First 1).Source
$importer = Join-Path $PSScriptRoot 'browser_import.py'
$proxy = Join-Path $PSScriptRoot 'local_proxy.js'
if (-not $WebUIDataDir) { $WebUIDataDir = Join-Path $webuiRoot 'data' }
$WebUIDataDir = (Resolve-Path -LiteralPath $WebUIDataDir).Path
New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
$state = (Resolve-Path -LiteralPath $StateDir).Path

function Get-ListenerProcess([int] $Port) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($listener) { Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" }
}

function Assert-Listener([int] $Port, $Process, [string] $Script) {
    if ($Process -and $Process.CommandLine.IndexOf($Script, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw "Port $Port is occupied by another application."
    }
}

function Wait-Listener([int] $Port, [string] $Script) {
    $deadline = (Get-Date).AddSeconds(120)
    do {
        $listener = Get-ListenerProcess $Port
        Assert-Listener $Port $listener $Script
        if ($listener) { return }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw "Port $Port did not become ready. See the logs in $state."
}

function Start-LocalProcess([string] $Program, [string] $Arguments, [string] $Directory, [string] $LogName) {
    Start-Process -FilePath $Program -ArgumentList $Arguments -WorkingDirectory $Directory -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $state "$LogName.stdout.log") `
        -RedirectStandardError (Join-Path $state "$LogName.stderr.log") | Out-Null
}

$front = Get-ListenerProcess 3000
$backend = Get-ListenerProcess 3001
$oldImporter = Get-ListenerProcess 3012
$legacy = $front -and $front.CommandLine.IndexOf($webuiExe, [StringComparison]::OrdinalIgnoreCase) -ge 0
if (-not $legacy) { Assert-Listener 3000 $front $proxy }
Assert-Listener 3001 $backend $webuiExe
Assert-Listener 3012 $oldImporter $importer
if ($legacy -and $backend) { throw 'Open WebUI is already running on both ports.' }

$previousDataDir = $env:DATA_DIR
try {
    $env:DATA_DIR = $WebUIDataDir
    if ($legacy) {
        Stop-Process -Id $front.ProcessId
        Wait-Process -Id $front.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
        $front = $null
    }
    if (-not $backend) {
        Start-LocalProcess $webuiExe 'serve --host 127.0.0.1 --port 3001' $webuiRoot 'open-webui'
        Wait-Listener 3001 $webuiExe
    }
    if ($oldImporter) {
        Stop-Process -Id $oldImporter.ProcessId
        Wait-Process -Id $oldImporter.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
    }
    $importArgs = '-B "' + $importer + '" --upstream http://127.0.0.1:3001 --public-origin http://127.0.0.1:3000' +
        ' --data-dir "' + $state + '" --upload-limit-mib ' + $UploadLimitMiB + ' --archive-limit-mib ' + $ArchiveLimitMiB
    Start-LocalProcess $pythonExe $importArgs $state 'importer'
    Wait-Listener 3012 $importer
    if (-not $front) {
        Start-LocalProcess $nodeExe ('"' + $proxy + '"') $state 'proxy'
        Wait-Listener 3000 $proxy
    }
    Write-Output 'Open WebUI: http://127.0.0.1:3000/'
    Write-Output 'ChatGPT import: http://127.0.0.1:3000/chatgpt-import/'
} finally {
    $env:DATA_DIR = $previousDataDir
}
