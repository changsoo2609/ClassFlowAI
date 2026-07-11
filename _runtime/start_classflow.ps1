$ErrorActionPreference = "Stop"
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

function Show-StartupProgress {
    param(
        [int]$Percent,
        [string]$Message
    )

    $Percent = [Math]::Max(0, [Math]::Min(100, $Percent))
    $BarWidth = 28
    $Filled = [Math]::Floor($BarWidth * $Percent / 100)
    $Empty = $BarWidth - $Filled

    $Bar = ("#" * $Filled) + ("-" * $Empty)
    $Line = "[{0}] {1,3}%  {2}" -f $Bar, $Percent, $Message

    Write-Host ("`r" + $Line.PadRight(78)) -NoNewline
}

Write-Host ""
Write-Host "ClassFlowAI를 실행 중입니다. 잠시만 기다려 주세요."
Show-StartupProgress -Percent 5 -Message "실행 환경을 준비하는 중"

$RuntimeDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogFile = Join-Path $RuntimeDir "ClassFlowAI_startup.log"
$ReadyFile = Join-Path $RuntimeDir "APP_STARTED.flag"
$ErrorFile = Join-Path $RuntimeDir "STARTUP_ERROR.log"
$AppFile = Join-Path $RuntimeDir "app.py"

Remove-Item $ReadyFile -Force -ErrorAction SilentlyContinue
Remove-Item $ErrorFile -Force -ErrorAction SilentlyContinue

@(
    "============================================"
    "ClassFlowAI startup log"
    "Date: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    "Folder: $RuntimeDir"
    "============================================"
) | Set-Content -Path $LogFile -Encoding UTF8

function Test-PythonCandidate {
    param(
        [string]$Command,
        [string[]]$PrefixArgs
    )

    try {
        $args = @()
        $args += $PrefixArgs
        $args += @("-c", "import sys, tkinter; print(sys.executable)")
        $output = & $Command @args 2>$null
        if ($LASTEXITCODE -ne 0) {
            return $null
        }

        $exePath = ($output | Select-Object -Last 1).Trim()
        if (-not $exePath -or -not (Test-Path $exePath)) {
            return $null
        }

        $verifyArgs = @()
        $verifyArgs += $PrefixArgs
        $verifyArgs += @("-c", "import PIL, pynput, requests, tkinter; print('OK')")
        $verify = & $Command @verifyArgs 2>$null
        if ($LASTEXITCODE -ne 0 -or (($verify | Select-Object -Last 1).Trim() -ne "OK")) {
            return @{
                Command = $Command
                PrefixArgs = $PrefixArgs
                ExePath = $exePath
                PackagesOk = $false
            }
        }

        return @{
            Command = $Command
            PrefixArgs = $PrefixArgs
            ExePath = $exePath
            PackagesOk = $true
        }
    }
    catch {
        return $null
    }
}

$candidates = @(
    @{ Command = "py"; PrefixArgs = @("-3.12") },
    @{ Command = "py"; PrefixArgs = @("-3.11") },
    @{ Command = "py"; PrefixArgs = @("-3.10") },
    @{ Command = "python"; PrefixArgs = @() }
)

$selected = $null
$packageMissing = $null

Show-StartupProgress -Percent 15 -Message "Python을 확인하는 중"

foreach ($candidate in $candidates) {
    $tested = Test-PythonCandidate -Command $candidate.Command -PrefixArgs $candidate.PrefixArgs
    if ($null -eq $tested) {
        continue
    }

    if (-not $tested.PackagesOk) {
        if ($null -eq $packageMissing) {
            $packageMissing = $tested
        }
        continue
    }

    $selected = $tested
    break
}

if ($null -eq $selected) {
    if ($null -ne $packageMissing) {
        Write-Host "[ERROR] Python exists, but required packages are missing."
        Write-Host "Run INSTALL_FIRST.bat."
        Add-Content -Path $LogFile -Value "[ERROR] Required packages are missing."
    }
    else {
        Write-Host "[ERROR] Python 3.10-3.12 with tkinter was not found."
        Write-Host "Run INSTALL_FIRST.bat."
        Add-Content -Path $LogFile -Value "[ERROR] No usable Python runtime was found."
    }
    exit 1
}

Show-StartupProgress -Percent 40 -Message "Python 확인 완료"

$PythonExe = $selected.ExePath
$PythonDir = Split-Path -Parent $PythonExe
$PythonW = Join-Path $PythonDir "pythonw.exe"

if (-not (Test-Path $PythonW)) {
    Write-Host "[ERROR] pythonw.exe was not found."
    Write-Host "Reinstall standard Python desktop components."
    Add-Content -Path $LogFile -Value "[ERROR] pythonw.exe was not found: $PythonW"
    exit 1
}

Add-Content -Path $LogFile -Value "[OK] Python: $PythonExe"
Add-Content -Path $LogFile -Value "[INFO] Starting ClassFlowAI."

Show-StartupProgress -Percent 60 -Message "필수 패키지 확인 완료"
Show-StartupProgress -Percent 72 -Message "프로그램 창을 여는 중"

$process = Start-Process `
    -FilePath $PythonW `
    -ArgumentList @("`"$AppFile`"") `
    -WorkingDirectory $RuntimeDir `
    -PassThru

$deadline = (Get-Date).AddSeconds(12)
$waitStarted = Get-Date

while ((Get-Date) -lt $deadline) {
    if (Test-Path $ReadyFile) {
        Show-StartupProgress -Percent 100 -Message "실행 완료"
        Write-Host ""
        Start-Sleep -Milliseconds 350
        exit 0
    }

    if ($process.HasExited) {
        Write-Host ""
        Write-Host "[ERROR] ClassFlowAI closed during startup."
        if (Test-Path $ErrorFile) {
            Get-Content $ErrorFile
        }
        else {
            Get-Content $LogFile
        }
        exit 1
    }

    $elapsedMs = ((Get-Date) - $waitStarted).TotalMilliseconds
    $dynamicPercent = 72 + [Math]::Floor([Math]::Min(23, $elapsedMs / 500))
    Show-StartupProgress -Percent $dynamicPercent -Message "창이 준비될 때까지 기다리는 중"
    Start-Sleep -Milliseconds 250
}

Write-Host ""
Write-Host "[ERROR] ClassFlowAI did not confirm startup within 12 seconds."
if (Test-Path $ErrorFile) {
    Get-Content $ErrorFile
}
else {
    Get-Content $LogFile
}
exit 1
