$logFile = "$env:USERPROFILE\Desktop\system_monitor_log.txt"
$startTime = Get-Date
$endTime = $startTime.AddHours(72)

function Get-SystemReport {
    $report = @()
    $report += "=" * 60
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $report += "System Monitor Report - $ts"
    $report += "=" * 60

    $os = Get-CimInstance Win32_OperatingSystem
    $totalMemGB = [math]::Round($os.TotalVisibleMemorySize / 1MB, 2)
    $freeMemGB = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
    $usedMemGB = [math]::Round($totalMemGB - $freeMemGB, 2)
    $memPercent = [math]::Round(($usedMemGB / $totalMemGB) * 100, 1)

    $report += ""
    $report += "[Memory Overview]"
    $report += "  Total: $totalMemGB GB"
    $report += "  Used: $usedMemGB GB / Free: $freeMemGB GB"
    $report += "  Usage: $memPercent%"

    $cpuLoad = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
    $report += ""
    $report += "[CPU Overview]"
    $report += "  CPU Usage: $cpuLoad%"

    $report += ""
    $report += "[Top 10 Memory Processes]"
    $topMem = Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 10
    $header = "{0,-30} {1,10} {2,10}" -f "Name", "PID", "Mem(MB)"
    $report += $header
    $report += "-" * 55
    foreach ($p in $topMem) {
        $memMB = [math]::Round($p.WorkingSet64 / 1MB, 1)
        $pn = $p.ProcessName
        if ($pn.Length -gt 28) { $pn = $pn.Substring(0, 28) }
        $line = "{0,-30} {1,10} {2,10}" -f $pn, $p.Id, $memMB
        $report += $line
    }

    $report += ""
    $report += "[Top 10 CPU Processes]"
    $topCpu = Get-Process | Sort-Object CPU -Descending | Select-Object -First 10
    $header2 = "{0,-30} {1,10} {2,12}" -f "Name", "PID", "CPU(s)"
    $report += $header2
    $report += "-" * 55
    foreach ($p in $topCpu) {
        $cpuSec = [math]::Round($p.CPU, 1)
        $pn = $p.ProcessName
        if ($pn.Length -gt 28) { $pn = $pn.Substring(0, 28) }
        $line = "{0,-30} {1,10} {2,12}" -f $pn, $p.Id, $cpuSec
        $report += $line
    }

    $report += ""
    $report += "[High Usage Alerts]"
    $alerts = @()

    $highMem = Get-Process | Where-Object { $_.WorkingSet64 -gt 2GB }
    foreach ($p in $highMem) {
        $memMB = [math]::Round($p.WorkingSet64 / 1MB, 0)
        $alerts += "  [ALERT-MEM] $($p.ProcessName) (PID: $($p.Id)) - $memMB MB"
    }

    $highCpu = Get-Process | Where-Object { $_.CPU -gt 50 }
    foreach ($p in $highCpu) {
        $cpuSec = [math]::Round($p.CPU, 1)
        $alerts += "  [ALERT-CPU] $($p.ProcessName) (PID: $($p.Id)) - $cpuSec s"
    }

    if ($memPercent -gt 80) {
        $alerts += "  [ALERT-SYS] Memory usage $memPercent% exceeds 80% threshold!"
    }

    if ($cpuLoad -gt 80) {
        $alerts += "  [ALERT-SYS] CPU usage $cpuLoad% exceeds 80% threshold!"
    }

    if ($alerts.Count -eq 0) {
        $report += "  All normal. No high-usage processes detected."
    } else {
        $report += $alerts
    }

    $endTs = $endTime.ToString('yyyy-MM-dd HH:mm:ss')
    $report += ""
    $report += "Monitoring until: $endTs"
    $report += "=" * 60
    $report += ""

    return $report
}

$headLine = "Monitor started - will run 72h (until $($endTime.ToString('yyyy-MM-dd HH:mm:ss')))"
$headLine | Out-File -FilePath $logFile -Encoding utf8
"" | Out-File -FilePath $logFile -Append -Encoding utf8

while ((Get-Date) -lt $endTime) {
    $report = Get-SystemReport
    $report | Out-File -FilePath $logFile -Append -Encoding utf8
    $report | Write-Host

    $alertLines = $report | Where-Object { $_ -match "ALERT" }
    if ($alertLines) {
        Add-Type -AssemblyName System.Windows.Forms
        $alertText = ($alertLines -join "`n")
        [System.Windows.Forms.MessageBox]::Show(
            "High usage detected!`n`n$alertText`n`nSee log: system_monitor_log.txt on Desktop",
            "System Monitor Alert",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Warning
        )
    }

    Start-Sleep -Seconds 1200
}

"Monitor ended - $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" | Out-File -FilePath $logFile -Append -Encoding utf8
Write-Host "Monitoring complete. Log: $logFile"
