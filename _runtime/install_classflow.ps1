$ErrorActionPreference = "Stop"

$RuntimeDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Requirements = Join-Path $RuntimeDir "requirements.txt"

function Find-Python {
    $candidates = @(
        @{ Command = "py"; PrefixArgs = @("-3.12") },
        @{ Command = "py"; PrefixArgs = @("-3.11") },
        @{ Command = "py"; PrefixArgs = @("-3.10") },
        @{ Command = "python"; PrefixArgs = @() }
    )

    foreach ($candidate in $candidates) {
        try {
            $args = @()
            $args += $candidate.PrefixArgs
            $args += @("-c", "import sys, tkinter; print(sys.executable)")
            $output = & $candidate.Command @args 2>$null
            if ($LASTEXITCODE -eq 0) {
                $exePath = ($output | Select-Object -Last 1).Trim()
                if ($exePath -and (Test-Path $exePath)) {
                    return $candidate
                }
            }
        }
        catch {
        }
    }

    return $null
}

$python = Find-Python

if ($null -eq $python) {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($null -eq $winget) {
        Write-Host "[ERROR] Python was not found and winget is unavailable."
        Write-Host "Install Python 3.12 manually, then run this file again."
        exit 1
    }

    Write-Host "[INFO] Installing Python 3.12..."
    & winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] Python installation failed."
        exit 1
    }

    Write-Host "[OK] Python installation finished."
    Write-Host "Close this window and run INSTALL_FIRST.bat again."
    exit 0
}

$prefix = @()
$prefix += $python.PrefixArgs

Write-Host "[1/3] Upgrade pip"
& $python.Command @prefix -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] pip upgrade failed."
    exit 1
}

Write-Host "[2/3] Install packages"
& $python.Command @prefix -m pip install --prefer-binary --upgrade -r $Requirements
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Package installation failed."
    exit 1
}

Write-Host "[3/3] Verify runtime"
& $python.Command @prefix -c "import PIL, pynput, requests, tkinter; print('VERIFY_OK')"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Runtime verification failed."
    exit 1
}

Write-Host "[OK] Installation completed."
Write-Host "Run START_HERE.bat."
exit 0
