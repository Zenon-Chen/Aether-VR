$os = Get-CimInstance Win32_OperatingSystem
$total = [math]::Round($os.TotalVisibleMemorySize / 1MB, 2)
$free = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
$used = [math]::Round($total - $free, 2)
$pct = [math]::Round(($used / $total) * 100, 1)
Write-Host "=== Memory Overview ==="
Write-Host "Total: $total GB"
Write-Host "Used: $used GB"
Write-Host "Free: $free GB"
Write-Host "Usage: $pct%"

$cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
Write-Host ""
Write-Host "=== CPU Overview ==="
Write-Host "CPU Usage: $cpu%"

Write-Host ""
Write-Host "=== Top 10 Memory Processes ==="
Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 10 | ForEach-Object {
    $memMB = [math]::Round($_.WorkingSet64 / 1MB, 1)
    Write-Host ("{0,-35} PID:{1,-8} {2} MB" -f $_.ProcessName, $_.Id, $memMB)
}

Write-Host ""
Write-Host "=== Top 10 CPU Processes ==="
Get-Process | Sort-Object CPU -Descending | Select-Object -First 10 | ForEach-Object {
    $cpuSec = [math]::Round($_.CPU, 1)
    Write-Host ("{0,-35} PID:{1,-8} {2} s" -f $_.ProcessName, $_.Id, $cpuSec)
}

Write-Host ""
Write-Host "=== High Usage Alerts ==="
$found = $false
Get-Process | Where-Object { $_.WorkingSet64 -gt 2GB } | ForEach-Object {
    $memMB = [math]::Round($_.WorkingSet64 / 1MB, 0)
    Write-Host "[ALERT-MEM] $($_.ProcessName) (PID: $($_.Id)) - $memMB MB"
    $found = $true
}
Get-Process | Where-Object { $_.CPU -gt 50 } | ForEach-Object {
    $cpuSec = [math]::Round($_.CPU, 1)
    Write-Host "[ALERT-CPU] $($_.ProcessName) (PID: $($_.Id)) - $cpuSec s"
    $found = $true
}
if ($pct -gt 80) { Write-Host "[ALERT-SYS] Memory usage $pct% exceeds 80%"; $found = $true }
if ($cpu -gt 80) { Write-Host "[ALERT-SYS] CPU usage $cpu% exceeds 80%"; $found = $true }
if (-not $found) { Write-Host "All normal. No high-usage processes detected." }
