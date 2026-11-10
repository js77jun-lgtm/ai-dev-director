$ErrorActionPreference = 'Stop'
$directorRoot = $PSScriptRoot
$directorPython = (Get-Command python).Source
try {
    $directorHealth = Invoke-RestMethod 'http://127.0.0.1:8081/ops/state' -TimeoutSec 3
    if ($directorHealth.solomon_url) { Write-Output 'AI DEV companion already running'; exit 0 }
} catch { }
Start-Process -FilePath $directorPython -ArgumentList 'companion_server.py' -WorkingDirectory $directorRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $directorRoot 'data/companion.log') -RedirectStandardError (Join-Path $directorRoot 'data/companion.err') | Out-Null
Write-Output 'AI DEV companion: http://127.0.0.1:8081'
