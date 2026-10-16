# Apartment Planner - Windows installer logic (run by ApartmentPlanner-Setup.exe or install-windows.bat).
# Installs per user, no admin rights needed:
#   1. finds Python 3.11-3.13, or installs Python 3.12 (winget, else python.org installer)
#   2. creates a private environment in %LOCALAPPDATA%\ApartmentPlanner\venv
#   3. installs the planner engine and all its dependencies (pip)
#   4. runs "planner doctor"
#   5. creates Desktop + Start Menu shortcuts and links Claude Desktop if it is installed
param(
    [string]$InstallDir = "$env:LOCALAPPDATA\ApartmentPlanner",
    [string]$Source = "",
    [switch]$NoClaude,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$PyVersion = "3.12.10"
$PyUrl = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-amd64.exe"

if (-not $Source) { $Source = Split-Path -Parent $PSScriptRoot }   # repo root (folder that holds engine\)
$Log = Join-Path $env:TEMP "ApartmentPlanner-install.log"
Start-Transcript -Path $Log -Force | Out-Null

function Step($n, $text) { Write-Host ""; Write-Host "[$n/6] $text" -ForegroundColor Cyan }
function Fail($text) {
    Write-Host ""; Write-Host "Installation failed: $text" -ForegroundColor Red
    Write-Host "Log: $Log"
    Stop-Transcript | Out-Null
    if (-not $Quiet) { Read-Host "Press Enter to close" | Out-Null }
    exit 1
}

function Test-Python($exe) {
    try {
        $v = & $exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $v) { return $null }
        $parts = $v.Trim().Split(".")
        if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 11 -and [int]$parts[1] -le 13) {
            return (& $exe -c "import sys; print(sys.executable)").Trim()
        }
    } catch { }
    return $null
}

function Find-Python {
    $candidates = @()
    foreach ($v in @("3.12", "3.13", "3.11")) {
        try { $p = & py "-$v" -c "import sys; print(sys.executable)" 2>$null; if ($LASTEXITCODE -eq 0 -and $p) { $candidates += $p.Trim() } } catch { }
    }
    foreach ($d in @("Python312", "Python313", "Python311")) {
        $candidates += Join-Path $env:LOCALAPPDATA "Programs\Python\$d\python.exe"
        $candidates += Join-Path $env:ProgramFiles "$d\python.exe"
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notlike "*WindowsApps*") { $candidates += $cmd.Source }
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { $ok = Test-Python $c; if ($ok) { return $ok } }
    }
    return $null
}

Write-Host "Apartment Planner - installing to $InstallDir" -ForegroundColor White
if (-not (Test-Path (Join-Path $Source "engine\pyproject.toml"))) { Fail "engine\pyproject.toml not found under $Source" }

Step 1 "Looking for Python 3.11-3.13"
$Python = Find-Python
if (-not $Python) {
    Write-Host "  Python not found - installing Python $PyVersion (per user)."
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        & winget install --id Python.Python.3.12 --exact --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host
        $Python = Find-Python
    }
    if (-not $Python) {
        $exe = Join-Path $env:TEMP "python-$PyVersion-amd64.exe"
        Write-Host "  Downloading $PyUrl"
        try { Invoke-WebRequest -Uri $PyUrl -OutFile $exe -UseBasicParsing } catch { Fail "could not download Python ($($_.Exception.Message)). Check the internet connection." }
        $p = Start-Process -FilePath $exe -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0 Shortcuts=0" -Wait -PassThru
        if ($p.ExitCode -ne 0) { Fail "the Python installer exited with code $($p.ExitCode)" }
        $Python = Find-Python
    }
    if (-not $Python) { Fail "Python was installed but could not be found. Restart Windows and run the installer again." }
}
Write-Host "  Using $Python"

Step 2 "Creating the app environment"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$Venv = Join-Path $InstallDir "venv"
if (Test-Path (Join-Path $Venv "Scripts\python.exe")) {
    $ok = Test-Python (Join-Path $Venv "Scripts\python.exe")
    if (-not $ok) { Remove-Item -Recurse -Force $Venv }
}
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
    & $Python -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Fail "could not create the environment" }
}
$VPy = Join-Path $Venv "Scripts\python.exe"
$VPyw = Join-Path $Venv "Scripts\pythonw.exe"

Step 3 "Installing the planner and its dependencies (a few minutes the first time)"
$AppSrc = Join-Path $InstallDir "app"
if ((Resolve-Path $Source).Path -ne (Resolve-Path $InstallDir -ErrorAction SilentlyContinue).Path) {
    if (Test-Path $AppSrc) { Remove-Item -Recurse -Force $AppSrc }
    New-Item -ItemType Directory -Force -Path $AppSrc | Out-Null
    Copy-Item -Recurse -Force (Join-Path $Source "engine") $AppSrc
    Get-ChildItem -Path (Join-Path $AppSrc "engine") -Include ".venv", "__pycache__", "*.egg-info", "build", "dist" -Recurse -Directory -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}
& $VPy -m pip install --upgrade pip --disable-pip-version-check | Out-Host
if ($LASTEXITCODE -ne 0) { Fail "pip could not be upgraded (check the internet connection)" }
& $VPy -m pip install --disable-pip-version-check (Join-Path $AppSrc "engine") | Out-Host
if ($LASTEXITCODE -ne 0) { Fail "the planner could not be installed (see the log)" }

Step 4 "Checking the installation"
& $VPy -m planner_engine.cli doctor | Out-Host
if ($LASTEXITCODE -ne 0) { Fail "planner doctor reported a problem (see above)" }

Step 5 "Creating shortcuts"
$Launcher = Join-Path $InstallDir "launch.pyw"
Copy-Item -Force (Join-Path $PSScriptRoot "launch.pyw") $Launcher
$Icon = Join-Path $InstallDir "planner.ico"
$IconSrc = Join-Path $PSScriptRoot "planner.ico"
if (Test-Path $IconSrc) { Copy-Item -Force $IconSrc $Icon }
$Shell = New-Object -ComObject WScript.Shell
$Targets = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "Apartment Planner.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "Apartment Planner.lnk")
)
foreach ($t in $Targets) {
    $s = $Shell.CreateShortcut($t)
    $s.TargetPath = $VPyw
    $s.Arguments = "`"$Launcher`""
    $s.WorkingDirectory = $InstallDir
    $s.Description = "Pick a plot, get a checked apartment floor plan"
    if (Test-Path $Icon) { $s.IconLocation = "$Icon,0" }
    $s.Save()
}
# command-line helper: %LOCALAPPDATA%\ApartmentPlanner\planner.cmd doctor | demo | check file.dxf ...
Set-Content -Path (Join-Path $InstallDir "planner.cmd") -Value "@`"$VPy`" -m planner_engine.cli %*" -Encoding ASCII

Step 6 "Linking Claude Desktop"
if ($NoClaude) {
    Write-Host "  Skipped (-NoClaude)."
} else {
    $claudeDirs = @((Join-Path $env:APPDATA "Claude"), (Join-Path $env:LOCALAPPDATA "Packages\Claude_pzs8sxrjxfjjc"), (Join-Path $env:LOCALAPPDATA "AnthropicClaude"))
    if ($claudeDirs | Where-Object { Test-Path $_ }) {
        $env:PLANNER_MCP_COMMAND = $VPy
        # the bridge entry runs: <venv python> -m planner_engine.mcp_server --stdio (config is backed up first)
        & $VPy -c "import json,sys; from planner_engine import claude_link as c; e={'command': sys.executable, 'args': ['-m','planner_engine.mcp_server','--stdio']}; c.bridge_command=lambda: e; r=c.link(confirm=True); print('  ' + r.get('message', r.get('error','')))" | Out-Host
        Write-Host "  Quit Claude fully (tray icon) and reopen it to see the planner tools."
    } else {
        Write-Host "  Claude Desktop not found - skipped. Link it later from the app (Claude badge, top right)."
    }
}

Stop-Transcript | Out-Null
Write-Host ""
Write-Host "Apartment Planner is installed." -ForegroundColor Green
Write-Host "Open it from the Desktop or Start Menu shortcut 'Apartment Planner'."
if (-not $Quiet) {
    $go = Read-Host "Open it now? [Y/n]"
    if ($go -ne "n" -and $go -ne "N") { Start-Process -FilePath $VPyw -ArgumentList "`"$Launcher`"" -WorkingDirectory $InstallDir }
}
exit 0
