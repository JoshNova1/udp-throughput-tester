# ===========================================================================
#  fetch-binaries.ps1
#
#  One-time helper. Downloads redistributable Windows builds of ffmpeg and
#  iperf3 from upstream releases and drops them into .\bin\ so the installer
#  build can bundle them.
#
#  srt-live-transmit is not commonly distributed for Windows -- see notes
#  below if you want the full SRT stats display on Windows.
#
#  Run from the repository root:
#      pwsh deploy\fetch-binaries.ps1
# ===========================================================================
[CmdletBinding()]
param(
    [string]$BinDir = "bin",
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # disable Invoke-WebRequest progress bar (much faster)

# Force TLS 1.2 -- Windows PowerShell 5.1 defaults to TLS 1.0/1.1 which many
# modern HTTPS servers reject.
[Net.ServicePointManager]::SecurityProtocol =
    [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls11

# --- Resolve project root --------------------------------------------------
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RepoRoot   = Split-Path -Parent $ScriptRoot
Set-Location $RepoRoot

if (-not (Test-Path $BinDir)) { New-Item -Type Directory -Path $BinDir | Out-Null }

function Get-Asset {
    param(
        [string]$Name,
        [string]$Url,
        [string]$ArchiveType,        # 'zip' or '7z'
        [string]$InnerPath,          # relative path of binary inside the archive
        [string]$DestBin,            # final dest path under .\bin\
        [string[]]$Companions = @()  # extra files (by leaf name) to also copy
                                     # out of the archive if present; missing
                                     # ones are silently ignored. Used for
                                     # runtime DLLs like cygwin1.dll that ship
                                     # alongside the main exe.
    )

    $finalPath = Join-Path $BinDir $DestBin
    $companionPaths = $Companions | ForEach-Object { Join-Path $BinDir $_ }
    $allPresent = (Test-Path $finalPath) -and
                  (-not ($companionPaths | Where-Object { -not (Test-Path $_) }))
    if ($allPresent -and (-not $Force)) {
        Write-Host "  [skip] $Name already present: $finalPath"
        return
    }
    Write-Host "==> $Name"
    Write-Host "    downloading $Url"
    $tmp = New-TemporaryFile
    $tmp = "$($tmp.FullName).$ArchiveType"
    Invoke-WebRequest -Uri $Url -OutFile $tmp -UseBasicParsing

    Write-Host "    extracting"
    $stage = Join-Path ([System.IO.Path]::GetTempPath()) ("tput-" + [guid]::NewGuid().ToString('N'))
    New-Item -Type Directory -Path $stage | Out-Null

    if ($ArchiveType -eq 'zip') {
        Expand-Archive -Path $tmp -DestinationPath $stage -Force
    } elseif ($ArchiveType -eq '7z') {
        # 7z support is not built-in; require 7-Zip on PATH.
        if (-not (Get-Command 7z -ErrorAction SilentlyContinue)) {
            throw "7z.exe not on PATH. Install 7-Zip (https://www.7-zip.org/) so this script can extract .7z archives."
        }
        & 7z x $tmp "-o$stage" -y | Out-Null
    } else {
        throw "Unsupported archive type: $ArchiveType"
    }

    # The inner path may have a wildcard for the versioned folder. Resolve it.
    $resolved = Get-ChildItem -Path $stage -Recurse -Filter (Split-Path $InnerPath -Leaf) |
                Select-Object -First 1
    if (-not $resolved) {
        throw "Could not locate '$InnerPath' inside the downloaded archive."
    }
    Copy-Item $resolved.FullName -Destination $finalPath -Force
    Write-Host "    -> $finalPath"

    foreach ($companion in $Companions) {
        $found = Get-ChildItem -Path $stage -Recurse -Filter $companion -ErrorAction SilentlyContinue |
                 Select-Object -First 1
        if ($found) {
            $dest = Join-Path $BinDir $companion
            Copy-Item $found.FullName -Destination $dest -Force
            Write-Host "    -> $dest  (companion of $Name)"
        } else {
            Write-Host "    [no $companion in archive -- skipping companion]" -ForegroundColor DarkGray
        }
    }

    Remove-Item -Recurse -Force $tmp, $stage -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Fetching Windows binaries into $BinDir\"
Write-Host "----------------------------------------------------------"

# -- ffmpeg -- gyan.dev "ffmpeg-release-essentials" build is small, LGPL-safe,
#    statically-linked enough that we just need ffmpeg.exe.
$ffmpegUrl = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
Get-Asset -Name "ffmpeg" `
          -Url $ffmpegUrl `
          -ArchiveType 'zip' `
          -InnerPath  'bin/ffmpeg.exe' `
          -DestBin    'ffmpeg.exe'

# -- iperf3 -- try a few known-good Windows builds. iperf3 is OPTIONAL: SRT mode
#    and ffmpeg UDP mode both work fine without it; you only lose the standalone
#    iperf3 test mode. So if every URL 404s, we warn and move on.
# The iperf.fr builds are Cygwin-based and need cygwin1.dll alongside the exe.
# The ar51an/* GitHub builds are statically linked, so no companion needed.
$iperfCandidates = @(
    @{ Url = 'https://iperf.fr/download/windows/iperf-3.17_64.zip';                                       Inner = 'iperf3.exe'; Companions = @('cygwin1.dll') },
    @{ Url = 'https://iperf.fr/download/windows/iperf-3.1.3-win64.zip';                                   Inner = 'iperf3.exe'; Companions = @('cygwin1.dll') },
    @{ Url = 'https://github.com/ar51an/iperf3-win-builds/releases/latest/download/iperf3.17.1_64.zip';   Inner = 'iperf3.exe'; Companions = @() },
    @{ Url = 'https://github.com/ar51an/iperf3-win-builds/releases/download/3.17.1/iperf3.17.1_64.zip';   Inner = 'iperf3.exe'; Companions = @() }
)
$iperfGot = $false
foreach ($c in $iperfCandidates) {
    try {
        Get-Asset -Name "iperf3" `
                  -Url $c.Url `
                  -ArchiveType 'zip' `
                  -InnerPath  $c.Inner `
                  -DestBin    'iperf3.exe' `
                  -Companions $c.Companions
        $iperfGot = $true
        break
    } catch {
        Write-Host "    [miss] $($c.Url)" -ForegroundColor DarkGray
        Write-Host "           $($_.Exception.Message)" -ForegroundColor DarkGray
    }
}
if (-not $iperfGot) {
    Write-Host ""
    Write-Host "NOTE: iperf3 download failed from every candidate URL." -ForegroundColor Yellow
    Write-Host "      The build will continue without it. SRT and ffmpeg UDP modes" -ForegroundColor Yellow
    Write-Host "      still work; only the iperf3 test mode is unavailable." -ForegroundColor Yellow
    Write-Host "      To add iperf3 later: download a Windows build from" -ForegroundColor Yellow
    Write-Host "      https://iperf.fr/iperf-download.php and drop iperf3.exe" -ForegroundColor Yellow
    Write-Host "      into .\bin\, then rebuild." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "----------------------------------------------------------"
Write-Host "Done."
Write-Host ""
Write-Host "Contents of $BinDir\:"
Get-ChildItem $BinDir | ForEach-Object {
    "{0,-30} {1,10:N0} bytes" -f $_.Name, $_.Length
}

Write-Host ""
Write-Host "NOTE: srt-live-transmit is NOT included."
Write-Host "      The libsrt project doesn't publish Windows binaries directly."
Write-Host "      Options:"
Write-Host "      1) Build from source: https://github.com/Haivision/srt (CMake + vcpkg, ~20 min)"
Write-Host "      2) Use the desktop app without it -- SRT mode falls back to"
Write-Host "         ffmpeg's native SRT support. You lose the detailed RTT/retrans"
Write-Host "         per-second stats but the basic throughput test still works."
Write-Host ""
Write-Host "      Drop srt-live-transmit.exe into .\bin\ if/when you have it."
