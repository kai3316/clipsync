<#
.SYNOPSIS
    One-click build: package ClipSync into a single Windows .exe.

.DESCRIPTION
    Mirrors what .github/workflows/build.yml does on windows-latest:
    installs the dependencies, runs `pyinstaller clipsync.spec`, then smoke-tests
    the resulting onefile binary to confirm the static web UI really got bundled
    (see the long comment in clipsync.spec — that bundling fails *silently*).

    Output: dist\clipsync.exe

.EXAMPLE
    .\scripts\build_exe.ps1
    Build with whatever is already installed.

.EXAMPLE
    .\scripts\build_exe.ps1 -Deps -Zip
    Refresh dependencies first, and also produce dist\clipsync-windows.zip.
#>
[CmdletBinding()]
param(
    # Install/upgrade requirements.txt + pyinstaller before building.
    # Missing packages are installed automatically anyway; use this to force a refresh.
    [switch]$Deps,

    # Also produce dist\clipsync-windows.zip (the shape CI attaches to a release).
    [switch]$Zip,

    # Launch the .exe once the build succeeds.
    [switch]$Run,

    # Keep the previous PyInstaller cache. Faster, but stale bundles are a real risk.
    [switch]$NoClean,

    # Keep PyInstaller's build\clipsync work directory (~hundreds of MB).
    # By default it is deleted once the .exe is verified.
    [switch]$KeepBuild,

    # Kill a running clipsync.exe instead of aborting (it would lock the output file).
    [switch]$StopRunning
)

$ErrorActionPreference = 'Stop'

# --- output helpers -----------------------------------------------------------
# Deliberately ASCII-only: this script is usually launched by double-clicking
# build.bat, where the console codepage is whatever Windows picked (often 936).
$script:StepNo = 0
function Write-Step($msg) {
    $script:StepNo++
    Write-Host ""
    Write-Host "==> [$script:StepNo] $msg" -ForegroundColor Cyan
}
function Write-Info($msg) { Write-Host "    $msg" -ForegroundColor Gray }
function Write-Ok($msg)   { Write-Host "    OK: $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "    WARNING: $msg" -ForegroundColor Yellow }
function Die($msg) {
    Write-Host ""
    Write-Host "BUILD FAILED: $msg" -ForegroundColor Red
    Write-Host ""
    exit 1
}

$started = Get-Date

# --- locate the project -------------------------------------------------------
$Root = Split-Path -Parent $PSScriptRoot
$Spec = Join-Path $Root 'clipsync.spec'
$Dist = Join-Path $Root 'dist'
$Work = Join-Path $Root 'build\clipsync'
$ExePath = Join-Path $Dist 'clipsync.exe'

if (-not (Test-Path $Spec)) {
    Die "clipsync.spec not found at $Spec (is scripts\ still inside the repo?)"
}

Write-Host ""
Write-Host "ClipSync - Windows .exe build" -ForegroundColor White
Write-Host "Project: $Root" -ForegroundColor Gray

# --- 1. pick an interpreter ---------------------------------------------------
Write-Step "Locating Python"

$Py = $null
foreach ($candidate in @('.venv\Scripts\python.exe', 'venv\Scripts\python.exe')) {
    $full = Join-Path $Root $candidate
    if (Test-Path $full) {
        $Py = $full
        Write-Info "Using project virtualenv: $candidate"
        break
    }
}
if (-not $Py) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) {
        $cmd = Get-Command py -ErrorAction SilentlyContinue
    }
    if (-not $cmd) {
        Die "No Python found. Install Python 3.12+ and make sure it is on PATH."
    }
    $Py = $cmd.Source
    Write-Info "Using Python from PATH (no .venv in the project)"
}

$pyVer = & $Py -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
if ($LASTEXITCODE -ne 0) { Die "Could not run '$Py'." }
Write-Info "$Py  (Python $pyVer)"

# pyproject.toml declares requires-python = ">=3.12".
$verParts = $pyVer.Split('.')
$major = [int]$verParts[0]
$minor = [int]$verParts[1]
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 12)) {
    Die "Python $pyVer is too old. ClipSync requires 3.12+ (see pyproject.toml)."
}
Write-Ok "Interpreter is suitable"

# --- 2. dependencies ----------------------------------------------------------
Write-Step "Checking dependencies"

# tkinter is stdlib but ships separately (and conda//python.org builds can miss
# the tcl/tk bits). customtkinter and pystray both hard-depend on it, so a
# missing tkinter must not be reported as "pip install will fix it".
& $Py -c "import tkinter" 2>$null
if ($LASTEXITCODE -ne 0) {
    Die "This Python has no working 'tkinter'. Reinstall Python with the tcl/tk option enabled (ClipSync's tray + native UI need it)."
}

# find_spec avoids actually importing pystray/customtkinter, which can be slow or
# probe for a display. All names below are top-level, so find_spec cannot raise.
#
# The module list travels as argv, and the snippet itself contains no quotes:
# PowerShell 5.1 strips inner double quotes when handing -c to a native exe, so
# an inlined Python list literal would arrive as bare (undefined) identifiers.
$mods = @('zeroconf', 'cryptography', 'PIL', 'pystray', 'customtkinter', 'qrcode', 'paho', 'certifi', 'PyInstaller')
$probe = 'import importlib.util as u, sys; print(*[m for m in sys.argv[1:] if u.find_spec(m) is None])'
$missingRaw = & $Py -c $probe @mods
if ($LASTEXITCODE -ne 0) { Die "Dependency probe failed." }

$missing = @()
if ($missingRaw -and $missingRaw.Trim()) {
    $missing = $missingRaw.Trim() -split '\s+'
}

if ($Deps) {
    Write-Info "-Deps given: refreshing all dependencies"
} elseif ($missing.Count -gt 0) {
    Write-Info ("Missing: " + ($missing -join ', '))
} else {
    Write-Ok "All build dependencies present"
}

if ($Deps -or $missing.Count -gt 0) {
    Write-Info "Installing (this can take a few minutes the first time)..."

    & $Py -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { Write-Warn "Could not upgrade pip; continuing." }

    & $Py -m pip install -r (Join-Path $Root 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { Die "pip install -r requirements.txt failed." }

    & $Py -m pip install "pyinstaller>=6.0"
    if ($LASTEXITCODE -ne 0) { Die "pip install pyinstaller failed." }

    Write-Ok "Dependencies installed"
}

# --- 3. read the version ------------------------------------------------------
Write-Step "Reading version"
Push-Location $Root
try {
    $Version = & $Py -c "from internal.version import __version__; print(__version__)"
    if ($LASTEXITCODE -ne 0) { Die "Could not import internal.version." }
} finally {
    Pop-Location
}
$Version = $Version.Trim()
Write-Ok "internal/version.py -> $Version"

# --- 4. make sure the target isn't locked -------------------------------------
Write-Step "Checking for a running ClipSync"
$running = Get-Process -Name 'clipsync' -ErrorAction SilentlyContinue
if ($running) {
    if ($StopRunning) {
        Write-Info "Stopping $($running.Count) running clipsync process(es)"
        $running | Stop-Process -Force
        Start-Sleep -Milliseconds 600
        Write-Ok "Stopped"
    } else {
        Die "clipsync.exe is running and would lock dist\clipsync.exe. Close it (tray icon -> Quit), or re-run with -StopRunning."
    }
} else {
    Write-Ok "Nothing to unlock"
}

# --- 5. clean ----------------------------------------------------------------
Write-Step "Cleaning previous output"
if ($NoClean) {
    Write-Info "-NoClean given: reusing the PyInstaller cache"
} else {
    # Only our own artifacts: build\clipsync is PyInstaller's workdir for this
    # spec. build\lib and build\bdist.* belong to setuptools -- leave them alone.
    if (Test-Path $Work) {
        Remove-Item -Recurse -Force $Work
        Write-Info "Removed build\clipsync"
    }
    if (Test-Path $ExePath) {
        Remove-Item -Force $ExePath
        Write-Info "Removed dist\clipsync.exe"
    }
    Write-Ok "Clean"
}

# --- 6. build ----------------------------------------------------------------
Write-Step "Running PyInstaller (a few minutes; lots of output is normal)"
Push-Location $Root
try {
    # The spec resolves 'internal', 'src/main.py' and the asset dirs relative to
    # SPECPATH, so the working directory must be the project root.
    $pyiArgs = @('-m', 'PyInstaller', '--noconfirm')
    if (-not $NoClean) { $pyiArgs += '--clean' }
    $pyiArgs += 'clipsync.spec'

    & $Py @pyiArgs
    $rc = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($rc -ne 0) { Die "PyInstaller exited with code $rc (scroll up for the cause)." }
Write-Ok "PyInstaller finished"

# --- 7. verify ---------------------------------------------------------------
Write-Step "Verifying the bundle"

if (-not (Test-Path $ExePath)) {
    Die "Expected dist\clipsync.exe, but it was not produced."
}
$exeItem = Get-Item $ExePath
$sizeMB = [math]::Round($exeItem.Length / 1MB, 1)
Write-Ok "dist\clipsync.exe ($sizeMB MB)"

if ($exeItem.Length -lt 5MB) {
    Write-Warn "That is suspiciously small for a bundled Python app - check the PyInstaller log above."
}

# The Windows build is ONEFILE, so the web UI lives inside the .exe's archive
# rather than on disk. clipsync.spec warns that this bundling can fail silently
# and leave the app serving only its minimal fallback page -- so assert it.
#
# -b (brief) prints bare entry names. Without it, archive_viewer formats each
# name through Python repr(), which on Windows renders separators as escaped
# double backslashes and defeats naive matching.
$tocRaw = & $Py -m PyInstaller.utils.cliutils.archive_viewer -l -b $ExePath 2>$null
$tocOk = ($LASTEXITCODE -eq 0) -and ($tocRaw) -and ($tocRaw.Count -ge 10)

if (-not $tocOk) {
    # Never fail the build on a broken inspector -- the .exe itself is fine.
    Write-Warn "Could not read the archive listing; skipped the bundled-data check."
} else {
    # Collapse any mix of / and \ (and doubled separators) to a single /.
    $toc = ($tocRaw -join "`n") -replace '[\\/]+', '/'

    $required = @(
        @{ Path = 'internal/web/static/index.html'; What = 'web UI (desktop)' },
        @{ Path = 'internal/web/static/mobile.html'; What = 'web UI (mobile)' },
        @{ Path = 'assets/themes/clipsync.json';    What = 'CTk aurora theme' }
    )
    $bad = @()
    foreach ($r in $required) {
        if ($toc -like "*$($r.Path)*") {
            Write-Ok "bundled: $($r.Path)"
        } else {
            $bad += $r
        }
    }
    if ($bad.Count -gt 0) {
        foreach ($b in $bad) {
            Write-Host "    MISSING: $($b.Path)  <- $($b.What)" -ForegroundColor Red
        }
        Die "The build produced an .exe, but required data files are missing from it. Check the 'datas' list in clipsync.spec."
    }
}

# --- 8. optional zip ---------------------------------------------------------
if ($Zip) {
    Write-Step "Packaging zip"
    $zipPath = Join-Path $Dist 'clipsync-windows.zip'
    if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
    Compress-Archive -Path $ExePath -DestinationPath $zipPath
    $zipMB = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
    Write-Ok "dist\clipsync-windows.zip ($zipMB MB)"
}

# --- 9. drop the intermediates ------------------------------------------------
# Everything needed is now inside the single .exe, so PyInstaller's work
# directory (the collected DLLs, the object cache, the .toc files -- easily a few
# hundred MB) is dead weight. Runs last, so a failed build keeps its evidence.
Write-Step "Removing intermediate files"
if ($KeepBuild) {
    Write-Info "-KeepBuild given: leaving build\clipsync in place"
} else {
    if (Test-Path $Work) {
        $workMB = [math]::Round(
            ((Get-ChildItem -Recurse -Force -File $Work | Measure-Object -Property Length -Sum).Sum / 1MB), 1)
        Remove-Item -Recurse -Force $Work
        Write-Info "Removed build\clipsync ($workMB MB reclaimed)"
    }

    # __pycache__ created at the project root while the spec imported internal.*
    Get-ChildItem -Path $Root -Directory -Recurse -Filter '__pycache__' -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-Item -Recurse -Force $_.FullName -ErrorAction SilentlyContinue }

    # Prune build\ itself, but only if nothing else lives there. build\lib and
    # build\bdist.* are setuptools' output, not ours -- never delete those.
    $buildRoot = Join-Path $Root 'build'
    if (Test-Path $buildRoot) {
        $leftovers = Get-ChildItem -Force $buildRoot -ErrorAction SilentlyContinue
        if (-not $leftovers) {
            Remove-Item -Recurse -Force $buildRoot
            Write-Info "Removed the now-empty build\"
        } else {
            Write-Info ("Kept build\ (not ours: " + (($leftovers | Select-Object -First 4 | ForEach-Object { $_.Name }) -join ', ') + ")")
        }
    }
    Write-Ok "Only dist\ remains"
}

# --- done --------------------------------------------------------------------
$elapsed = (Get-Date) - $started
Write-Host ""
Write-Host "-------------------------------------------------------" -ForegroundColor Green
Write-Host " BUILD OK - ClipSync $Version" -ForegroundColor Green
Write-Host " Output : $ExePath" -ForegroundColor Green
Write-Host " Size   : $sizeMB MB" -ForegroundColor Green
Write-Host (" Took   : {0:mm}m {0:ss}s" -f $elapsed) -ForegroundColor Green
Write-Host "-------------------------------------------------------" -ForegroundColor Green
Write-Host ""
Write-Host "This is a single self-contained .exe - copy it anywhere." -ForegroundColor Gray
Write-Host "Note: it starts with no window; look for the tray icon." -ForegroundColor Gray

if ($Run) {
    Write-Host ""
    Write-Info "Launching..."
    Start-Process -FilePath $ExePath
}

exit 0
