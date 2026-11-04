param(
    [ValidateSet('Weekly', 'Annual')]
    [string]$Report = 'Weekly',
    [string]$PythonPath = 'python',
    [string]$WeeklyTime = '07:00',
    [string]$AnnualTime = '07:00'
)

$ErrorActionPreference = 'Stop'
$reportRoot = $PSScriptRoot
$resolvedPython = (Get-Command $PythonPath -ErrorAction Stop).Source
$reportScript = Join-Path $reportRoot 'automate_report.py'

# Validate locally before registering a task. No credentials are put in task arguments.
& $resolvedPython $reportScript check
if ($LASTEXITCODE -ne 0) { throw 'Complete the Zoho connection setup before registering a task.' }

if ($Report -eq 'Weekly') {
    $taskName = 'TWC Zoho Weekly Report'
    $taskArguments = '"' + $reportScript + '" weekly --publish'
    $taskTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At $WeeklyTime
} else {
    $taskName = 'TWC Zoho Annual Benchmark'
    # January 1 builds the target year from the calendar year that has just closed.
    $taskArguments = '"' + (Join-Path $reportRoot 'run_annual_report.py') + '"'
    $taskTrigger = New-ScheduledTaskTrigger -Daily -At $AnnualTime
}

$taskAction = New-ScheduledTaskAction -Execute $resolvedPython -Argument $taskArguments -WorkingDirectory $reportRoot
$taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 12)
$taskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $taskTrigger -Settings $taskSettings -Principal $taskPrincipal -Force | Select-Object TaskName, State
Write-Host 'Task registered for this Windows user. The computer must be running and this user signed in.'
Write-Host 'Task times use Windows local time. Set Windows to South Africa time for the requested schedule.'
