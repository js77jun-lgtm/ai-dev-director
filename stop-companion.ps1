$ErrorActionPreference = 'Stop'
$directorListeners = Get-NetTCPConnection -LocalPort 8081 -State Listen -ErrorAction SilentlyContinue
foreach ($directorListener in $directorListeners) {
    $directorProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($directorListener.OwningProcess)"
    if ($directorProcess.CommandLine -match 'companion_server\.py') {
        Stop-Process -Id $directorListener.OwningProcess
        Write-Output 'AI DEV companion stopped; records and worktrees preserved.'
    } else { throw 'Port 8081 belongs to another process; not stopped.' }
}
