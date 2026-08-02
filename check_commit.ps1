Write-Host "=== Commit Charge Overview ==="
$os = Get-CimInstance Win32_OperatingSystem
$commitTotalGB = [math]::Round($os.TotalVirtualMemorySize / 1MB, 2)
$commitFreeGB = [math]::Round($os.FreeVirtualMemory / 1MB, 2)
$commitUsedGB = [math]::Round($commitTotalGB - $commitFreeGB, 2)
Write-Host "Commit Total: $commitTotalGB GB"
Write-Host "Commit Used: $commitUsedGB GB"
Write-Host "Commit Free: $commitFreeGB GB"

Write-Host ""
Write-Host "=== Top 20 Processes by Committed Memory (Private Bytes) ==="
$procs = Get-Process | Select-Object Id, ProcessName, WorkingSet64, @{Name='PrivateBytes';Expression={$_.PrivateMemorySize64}}, @{Name='VirtualMem';Expression={$_.VirtualMemorySize64}}
$topCommit = $procs | Sort-Object PrivateBytes -Descending | Select-Object -First 20
foreach ($p in $topCommit) {
    $privMB = [math]::Round($p.PrivateBytes / 1MB, 1)
    $wsMB = [math]::Round($p.WorkingSet64 / 1MB, 1)
    $virtMB = [math]::Round($p.VirtualMem / 1MB, 1)
    Write-Host ("{0,-30} PID:{1,-8} Private:{2,10} MB  WorkingSet:{3,10} MB" -f $p.ProcessName, $p.Id, $privMB, $wsMB)
}

Write-Host ""
Write-Host "=== Processes with PrivateBytes > 1GB ==="
$bigCommit = $procs | Where-Object { $_.PrivateBytes -gt 1GB } | Sort-Object PrivateBytes -Descending
if ($bigCommit) {
    foreach ($p in $bigCommit) {
        $privMB = [math]::Round($p.PrivateBytes / 1MB, 0)
        Write-Host "[ALERT] $($p.ProcessName) (PID: $($p.Id)) - Private: $privMB MB"
    }
} else {
    Write-Host "No processes with PrivateBytes > 1GB"
}
