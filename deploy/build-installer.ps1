# ===========================================================================
#  build-installer.ps1
#
#  End-to-end Windows build pipeline -- auto-bootstrapping.
#
#  On first run, this script will:
#    1. Detect Python; install Python 3.12 if missing (winget first, falling
#       back to the python.org installer). User-scope install, no admin.
#    2. Detect Inno Setup; install if missing (winget first, falling back
#       to the jrsoftware.org installer). Inno Setup requires admin elevation
#       -- Windows will prompt with UAC.
#    3. Create / activate a Python venv.
#    4. Install Python dependencies into the venv.
#    5. Download ffmpeg + iperf3 Windows binaries into .\bin\ if absent.
#    6. Run PyInstaller -> dist\UDPThroughputTester\
#    7. Run Inno Setup -> dist\ThroughputTester-Setup-X.Y.Z.exe
#
#  Subsequent runs skip steps 1-2 (and step 5 if .\bin\ is populated).
#
#  Run from the repository root:
#      pwsh deploy\build-installer.ps1
#  Or simply double-click deploy\build-windows.bat which delegates here.
#
#  Flags:
#    -NoAutoInstall    Don't auto-install missing prereqs (fail instead).
#    -SkipBinaries     Don't fetch ffmpeg/iperf3 (use whatever's in .\bin\).
#    -SkipInstaller    Stop after PyInstaller -- produce portable folder only.
#    -Clean            Wipe build\ and dist\ before starting.
# ===========================================================================
[CmdletBinding()]
param(
    [switch]$NoAutoInstall,
    [switch]$SkipBinaries,
    [switch]$SkipInstaller,
    [switch]$Clean,
    # Versioning + auto-update wiring. Defaults give a "dev" build that
    # disables in-app update. CI overrides these (see .github/workflows).
    [string]$Version = "0.0.0-dev",
    [string]$Repo    = "REPLACE_ME/REPLACE_ME"
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

# --- Locate repo root regardless of where script was invoked from --------
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RepoRoot   = Split-Path -Parent $ScriptRoot
Set-Location $RepoRoot

function H1 { Write-Host ""; Write-Host "==> $args" -ForegroundColor Cyan }
function H2 { Write-Host "    $args" -ForegroundColor DarkGray }
function HOK { Write-Host "    $args" -ForegroundColor Green }
function HWarn { Write-Host "    $args" -ForegroundColor Yellow }
function Err { Write-Host "ERROR: $args" -ForegroundColor Red }

# ===========================================================================
#  Helpers -- PATH refresh, dependency detection, dependency installation
# ===========================================================================
function Update-PathFromRegistry {
    # Picks up PATH changes made by silent installers without needing a logout.
    $machine = [System.Environment]::GetEnvironmentVariable("PATH", "Machine")
    $user    = [System.Environment]::GetEnvironmentVariable("PATH", "User")
    $env:PATH = "$machine;$user"
}

function Test-HasWinget {
    $cmd = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $cmd) { return $false }
    # winget exists but might be a Store stub on a stripped-down install.
    try {
        & winget --version 2>&1 | Out-Null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

# ----- Python detection / install -----------------------------------------
function Get-PythonExe {
    # Prefer `python` on PATH (rejecting the Microsoft Store app-execution-
    # alias redirector that lives under WindowsApps). This is what
    # actions/setup-python on GitHub Actions exposes, and it's also what
    # most user installs put on PATH first.
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python -and ($python.Source -notlike '*\WindowsApps\*')) {
        return $python.Source
    }
    # Fall back to the 'py' launcher -- but be aware that `py -3` picks the
    # *highest* installed 3.x, which on the GHA runner image is 3.14 and
    # breaks pythonnet (no wheels yet). Pin to 3.12 explicitly when we use
    # the launcher; if 3.12 isn't installed it returns nothing and we fall
    # through to the auto-install path.
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        foreach ($spec in @("-3.12", "-3.13", "-3.11", "-3")) {
            try {
                $path = (& py $spec -c "import sys; print(sys.executable)" 2>&1).Trim()
                if ($path -and (Test-Path $path)) { return $path }
            } catch {}
        }
    }
    return $null
}

function Install-PythonViaWinget {
    H2 "via winget: Python.Python.3.12 (user scope)"
    & winget install --id Python.Python.3.12 --silent --scope user `
        --accept-source-agreements --accept-package-agreements 2>&1 |
        Where-Object { $_ -match '\S' } | ForEach-Object { H2 $_ }
    return ($LASTEXITCODE -eq 0)
}

function Install-PythonViaDirect {
    $url = "https://www.python.org/ftp/python/3.12.6/python-3.12.6-amd64.exe"
    $exe = Join-Path $env:TEMP "python-3.12.6-amd64.exe"
    H2 "downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
    H2 "running silent install (user scope, no admin needed)"
    $proc = Start-Process -Wait -PassThru -FilePath $exe -ArgumentList @(
        "/quiet",
        "InstallAllUsers=0",
        "PrependPath=1",
        "Include_test=0",
        "Include_pip=1",
        "Include_launcher=1",
        "AssociateFiles=0",
        "Shortcuts=0"
    )
    Remove-Item $exe -Force -ErrorAction SilentlyContinue
    return ($proc.ExitCode -eq 0)
}

function Ensure-Python {
    $existing = Get-PythonExe
    if ($existing) {
        $ver = (& $existing --version 2>&1).Trim()
        HOK "Python: $ver"
        H2  "       $existing"
        return $existing
    }
    if ($NoAutoInstall) {
        Err "Python not found and -NoAutoInstall was specified."
        Err "Install Python 3.11+ manually from https://python.org and re-run."
        exit 1
    }
    H1 "Python not found -- installing automatically"
    $installed = $false
    if (Test-HasWinget) { $installed = Install-PythonViaWinget }
    if (-not $installed) {
        HWarn "winget install failed or unavailable -- falling back to direct download."
        $installed = Install-PythonViaDirect
    }
    Update-PathFromRegistry
    $existing = Get-PythonExe
    if (-not $existing) {
        Err "Python install completed but py.exe / python.exe is still not on PATH."
        Err "Open a new PowerShell window and re-run the build."
        exit 1
    }
    $ver = (& $existing --version 2>&1).Trim()
    HOK "Python installed: $ver"
    H2  "                  $existing"
    return $existing
}

# ----- Inno Setup detection / install -------------------------------------
function Get-InnoSetupExe {
    # 1) Known install locations. winget can land it in any of these
    #    depending on whether the install is machine-scope or user-scope.
    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
        "${env:LOCALAPPDATA}\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 5\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 5\ISCC.exe",
        "${env:LOCALAPPDATA}\Programs\Inno Setup 5\ISCC.exe"
    )
    foreach ($p in $candidates) {
        if ($p -and (Test-Path $p)) { return $p }
    }

    # 2) PATH lookup (covers any install that updated the user/system PATH).
    $cmd = Get-Command iscc -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -and (Test-Path $cmd.Source)) { return $cmd.Source }

    # 3) Registry InstallLocation under the standard Uninstall keys.
    #    Inno Setup writes "Inno Setup <ver>_is1" with InstallLocation set.
    $regRoots = @(
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    )
    foreach ($root in $regRoots) {
        try {
            $keys = Get-ChildItem $root -ErrorAction SilentlyContinue |
                    Where-Object { $_.PSChildName -like "Inno Setup*_is1" }
            foreach ($k in $keys) {
                $loc = (Get-ItemProperty $k.PSPath -ErrorAction SilentlyContinue).InstallLocation
                if ($loc) {
                    $candidate = Join-Path $loc "ISCC.exe"
                    if (Test-Path $candidate) { return $candidate }
                }
            }
        } catch {}
    }

    # 4) Last resort: ask Windows to search.
    try {
        $found = & where.exe ISCC.exe 2>$null | Select-Object -First 1
        if ($LASTEXITCODE -eq 0 -and $found -and (Test-Path $found)) {
            return $found
        }
    } catch {}

    return $null
}

function Install-InnoSetupViaWinget {
    H2 "via winget: JRSoftware.InnoSetup (may prompt for UAC)"
    & winget install --id JRSoftware.InnoSetup --silent `
        --accept-source-agreements --accept-package-agreements 2>&1 |
        Where-Object { $_ -match '\S' } | ForEach-Object { H2 $_ }
    return ($LASTEXITCODE -eq 0)
}

function Install-InnoSetupViaDirect {
    # jrsoftware.org's /download.php/is.exe redirects to the current stable.
    $url = "https://jrsoftware.org/download.php/is.exe"
    $exe = Join-Path $env:TEMP "innosetup-installer.exe"
    H2 "downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
    H2 "running silent install (this WILL prompt for UAC elevation)"
    $proc = Start-Process -Wait -PassThru -FilePath $exe -ArgumentList @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/SP-", "/NORESTART"
    )
    Remove-Item $exe -Force -ErrorAction SilentlyContinue
    return ($proc.ExitCode -eq 0)
}

function Ensure-InnoSetup {
    $existing = Get-InnoSetupExe
    if ($existing) {
        HOK "Inno Setup: $existing"
        return $existing
    }
    if ($NoAutoInstall) {
        Err "Inno Setup not found and -NoAutoInstall was specified."
        Err "Install it manually from https://jrsoftware.org/isdl.php and re-run."
        exit 1
    }
    H1 "Inno Setup not found -- installing automatically"
    $installed = $false
    if (Test-HasWinget) { $installed = Install-InnoSetupViaWinget }
    if (-not $installed) {
        HWarn "winget install failed or unavailable -- falling back to direct download."
        $installed = Install-InnoSetupViaDirect
    }
    $existing = Get-InnoSetupExe
    if (-not $existing) {
        Err "Inno Setup install completed but ISCC.exe was not found in expected locations."
        Err "If Defender / antivirus blocked the install, install manually:"
        Err "  https://jrsoftware.org/isdl.php"
        exit 1
    }
    HOK "Inno Setup installed: $existing"
    return $existing
}

# ===========================================================================
#  Main pipeline
# ===========================================================================
H1 "Checking prerequisites"
$pythonExe = Ensure-Python
$iscc = $null
if (-not $SkipInstaller) {
    $iscc = Ensure-InnoSetup
}

if ($Clean) {
    H1 "Cleaning previous build"
    Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
}

# ----- venv ---------------------------------------------------------------
if (-not (Test-Path ".venv")) {
    H1 "Creating Python virtual environment"
    & $pythonExe -m venv .venv
    if ($LASTEXITCODE -ne 0) { Err "venv creation failed."; exit 1 }
}

H1 "Activating venv"
$activate = Join-Path $RepoRoot ".venv\Scripts\Activate.ps1"
. $activate

H1 "Installing Python dependencies into venv"
& python -m pip install --quiet --upgrade pip
& python -m pip install --quiet -r requirements.txt -r requirements-desktop.txt
if ($LASTEXITCODE -ne 0) { Err "pip install failed."; exit 1 }

# ----- fetch ffmpeg / iperf3 ----------------------------------------------
if (-not $SkipBinaries) {
    $binFiles = @(Get-ChildItem -Path "bin" -ErrorAction SilentlyContinue)
    if ($binFiles.Count -lt 2) {
        H1 "Fetching ffmpeg + iperf3 (a couple of minutes on a fresh build)"
        & "$ScriptRoot\fetch-binaries.ps1"
    } else {
        H1 "Bundled binaries already present in .\bin\ (skipping)"
        $binFiles | ForEach-Object { H2 ("$($_.Name) -- {0:N1} MB" -f ($_.Length / 1MB)) }
    }
}

# ----- Stamp version + repo into _buildinfo.py before PyInstaller freezes
H1 "Stamping build info"
H2 "  version: $Version"
H2 "  repo:    $Repo"
Set-Content -Path "$RepoRoot\_buildinfo.py" -Encoding ASCII -Value @"
# Auto-generated by deploy/build-installer.ps1. Do not edit by hand.
APP_VERSION = "$Version"
GITHUB_REPO = "$Repo"
"@

# ----- PyInstaller --------------------------------------------------------
H1 "Running PyInstaller"
& python -m PyInstaller --clean --noconfirm desktop.spec
if ($LASTEXITCODE -ne 0) { Err "PyInstaller failed."; exit 1 }
$portable = "$RepoRoot\dist\UDPThroughputTester"
if (-not (Test-Path "$portable\UDPThroughputTester.exe")) {
    Err "PyInstaller didn't produce the expected .exe."
    exit 1
}
HOK "PyInstaller output: $portable"

# ----- Inno Setup ---------------------------------------------------------
if (-not $SkipInstaller) {
    H1 "Building installer with Inno Setup"
    # Pass the version through to installer.iss via /D so it's baked into
    # both the Setup-X.Y.Z.exe filename and the Apps & Features entry.
    & $iscc "$ScriptRoot\installer.iss" "/DAppVersion=$Version" /Q
    if ($LASTEXITCODE -ne 0) { Err "Inno Setup compilation failed."; exit 1 }
    $setup = Get-ChildItem "$RepoRoot\dist\ThroughputTester-Setup-*.exe" |
             Select-Object -First 1
    if ($setup) {
        HOK "Installer: $($setup.FullName)"
        H2  ("Size:      {0:N1} MB" -f ($setup.Length / 1MB))
    }
}

# ===========================================================================
Write-Host ""
Write-Host "==================================================================" -ForegroundColor Green
Write-Host " Build complete." -ForegroundColor Green
Write-Host "==================================================================" -ForegroundColor Green
if ($SkipInstaller) {
    Write-Host " Portable folder: $portable"
    Write-Host " Distribute the whole folder, or zip it."
} else {
    Write-Host " Installer:       dist\ThroughputTester-Setup-*.exe"
    Write-Host " Distribute the single .exe -- users double-click to install."
}
Write-Host ""
