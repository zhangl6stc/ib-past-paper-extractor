# Runs shards through run_shard.cmd, max $MaxConcurrent at a time.
# Usage:  powershell -File scripts\scheduler.ps1 [-Shards a,b,c] [-MaxConcurrent 5]
param(
    [string[]]$Shards = @('cs_a','cs_b','cs_c','econ_a','econ_b','phys_a','phys_b','mathhl_a','mathhl_b','aahl','aahl_b'),
    [int]$MaxConcurrent = 5
)
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$logDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$log = Join-Path $logDir 'scheduler.log'

$queue = New-Object System.Collections.Generic.Queue[string]
$Shards | ForEach-Object { $queue.Enqueue($_) }
$running = @{}
"$(Get-Date -Format o) scheduler start (max $MaxConcurrent)" | Out-File $log -Append -Encoding utf8
while ($queue.Count -gt 0 -or $running.Count -gt 0) {
    $done = @()
    foreach ($procId in @($running.Keys)) {
        if (-not (Get-Process -Id $procId -ErrorAction SilentlyContinue)) {
            $done += $procId
        }
    }
    foreach ($procId in $done) {
        "$(Get-Date -Format o) finished $($running[$procId]) (pid $procId)" | Out-File $log -Append -Encoding utf8
        $running.Remove($procId)
    }
    while ($queue.Count -gt 0 -and $running.Count -lt $MaxConcurrent) {
        $s = $queue.Dequeue()
        $p = Start-Process -FilePath "cmd.exe" -ArgumentList "/c scripts\run_shard.cmd $s" -WorkingDirectory $root -WindowStyle Hidden -PassThru
        $running[$p.Id] = $s
        "$(Get-Date -Format o) started $s (pid $($p.Id))" | Out-File $log -Append -Encoding utf8
        Start-Sleep -Seconds 5
    }
    Start-Sleep -Seconds 30
}
"$(Get-Date -Format o) ALL SHARDS DONE" | Out-File $log -Append -Encoding utf8
