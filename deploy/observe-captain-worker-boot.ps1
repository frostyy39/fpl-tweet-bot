# Temporary read-only observer. Runs as SYSTEM, but never launches Chrome/the worker,
# accesses authentication files, or changes Task Scheduler/account/profile settings.
$ErrorActionPreference = 'Stop'
$taskName = 'Captain-Worker-NoPost'
$appRoot = 'C:\Users\captaintrial\AppData\Local\FPLBot\CaptainCloudWorker01'
$captainProfilePath = 'C:\Users\captaintrial\AppData\Local\FPLBot\FPLReviewCaptainProfile'
$expectedPython = 'C:\Users\captaintrial\AppData\Local\FPLBot\CaptainControlledTrial01\venv\Scripts\python.exe'
$expectedArguments = '-m fpl_bot.captain_worker_cli --config "' + $appRoot + '\worker-config.json"'
$boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
$utf8 = New-Object System.Text.UTF8Encoding($false, $true)
$maxChrome = 0
function Publish-SafeReport($report) {
    $json = $report | ConvertTo-Json -Depth 8 -Compress
    # Guest attributes carry diagnostic evidence only, never operational authority.
    Invoke-WebRequest -UseBasicParsing -Method Put -TimeoutSec 10 `
        -Uri 'http://metadata.google.internal/computeMetadata/v1/instance/guest-attributes/captain-rehearsal/boot-report' `
        -Headers @{'Metadata-Flavor'='Google'} -ContentType 'application/json' `
        -Body $utf8.GetBytes($json) | Out-Null
    Write-Output ('CAPTAIN_BOOT_OBSERVER ' + $json)
}
try {
    for ($poll = 0; $poll -lt 48; $poll++) {
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
        $info = $task | Get-ScheduledTaskInfo -ErrorAction Stop
        $chrome = @(Get-Process chrome -ErrorAction SilentlyContinue).Count
        $maxChrome = [Math]::Max($maxChrome, $chrome)
        $triggers = @($task.Triggers)
        $actions = @($task.Actions)
        $contractMatches = ($task.Principal.UserId.Split('\')[-1] -eq 'captaintrial' -and
            $task.Principal.LogonType -eq 'Password' -and $task.Principal.RunLevel -eq 'Limited' -and
            $triggers.Count -eq 1 -and $triggers[0].CimClass.CimClassName -eq 'MSFT_TaskBootTrigger' -and
            $triggers[0].Delay -eq 'PT30S' -and $actions.Count -eq 1 -and
            $actions[0].Execute -eq $expectedPython -and $actions[0].Arguments -eq $expectedArguments -and
            $actions[0].WorkingDirectory -eq $appRoot -and
            $task.Settings.ExecutionTimeLimit -eq 'PT25M' -and
            $task.Settings.MultipleInstances -eq 'IgnoreNew' -and
            $task.Settings.RestartCount -eq 0 -and -not $task.Settings.AllowDemandStart -and
            $task.Settings.RunOnlyIfNetworkAvailable)
        $audit = $null
        foreach ($file in @(Get-ChildItem -LiteralPath ($appRoot + '\audits') -Filter audit.json -Recurse -File)) {
            $candidate = [System.IO.File]::ReadAllText($file.FullName, $utf8) | ConvertFrom-Json
            if ([DateTimeOffset]::Parse($candidate.started_at_utc).UtcDateTime -ge $boot.ToUniversalTime()) {
                if ($null -ne $audit) { throw 'Multiple current-boot worker audits' }
                # Whitelist only non-secret fields. Never forward handoff/browser data.
                $audit = [ordered]@{
                    schema_version = $candidate.schema_version; no_post = $candidate.no_post
                    status = $candidate.status; exit_code = $candidate.exit_code
                    started_at_utc = $candidate.started_at_utc; ended_at_utc = $candidate.ended_at_utc
                    handoff_absent = ($null -eq $candidate.handoff)
                    payload_digest_absent = ($null -eq $candidate.payload_digest)
                    strict_utf8 = $true
                    sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
                }
            }
        }
        $markers = [ordered]@{}
        foreach ($marker in @('.captain-browser-owner','SingletonLock','SingletonSocket','SingletonCookie')) {
            $markers[$marker] = Test-Path -LiteralPath (Join-Path $captainProfilePath $marker)
        }
        $interactiveCount = 0
        $securityLog = Get-WinEvent -ListLog Security -ErrorAction Stop
        foreach ($event in @(Get-WinEvent -FilterHashtable @{LogName='Security';Id=4624;StartTime=$boot} -ErrorAction SilentlyContinue)) {
            $xml = [xml]$event.ToXml()
            $data = @{}
            foreach ($item in $xml.Event.EventData.Data) { $data[$item.Name] = $item.'#text' }
            if ($data.TargetUserName -eq 'captaintrial' -and $data.LogonType -in @('2','7','10','11')) {
                $interactiveCount++
            }
        }
        $taskEvents = @()
        foreach ($event in @(Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-TaskScheduler/Operational';Id=100,102,200,201;StartTime=$boot} -ErrorAction SilentlyContinue)) {
            $xml = [xml]$event.ToXml()
            $named = @($xml.Event.EventData.Data | Where-Object { $_.Name -eq 'TaskName' })
            if ($named.Count -eq 1 -and $named[0].'#text' -eq ('\' + $taskName)) {
                $taskEvents += @{event_id=$event.Id; utc=$event.TimeCreated.ToUniversalTime().ToString('o')}
            }
        }
        $report = [ordered]@{
            schema_version=1; postable=$false; observation='waiting'
            observed_at_utc=[DateTime]::UtcNow.ToString('o'); boot_utc=$boot.ToUniversalTime().ToString('o')
            task_contract_matches=$contractMatches
            principal=$task.Principal.UserId; logon_type=[string]$task.Principal.LogonType
            run_level=[string]$task.Principal.RunLevel; task_state=[string]$task.State
            last_run_utc=$info.LastRunTime.ToUniversalTime().ToString('o')
            last_result=$info.LastTaskResult; current_boot_interactive_logons=$interactiveCount
            security_log_enabled=$securityLog.IsEnabled; task_events=$taskEvents
            chrome_process_count=$chrome; max_observed_chrome_process_count=$maxChrome
            lifecycle_markers=$markers; audit=$audit
        }
        if (-not $contractMatches) { $report.observation='task_contract_mismatch'; Publish-SafeReport $report; exit 1 }
        if ($null -ne $audit -and $task.State -ne 'Running' -and $info.LastRunTime -ge $boot) {
            $report.observation='completed'; Publish-SafeReport $report; exit 0
        }
        if ($poll -eq 47) { $report.observation='bounded_observation_timeout' }
        Publish-SafeReport $report
        Start-Sleep -Seconds 10
    }
} catch {
    # Do not print arbitrary exception details or file content.
    Publish-SafeReport @{schema_version=1; postable=$false; observation='observer_failed_closed'; observed_at_utc=[DateTime]::UtcNow.ToString('o')}
    exit 1
}
