param(
    [switch]$CheckOnly,
    [ValidateRange(0, 65535)]
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"
$Root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$Desktop = Join-Path $Root "desktop"
$OriginalPath = $env:PATH
$OriginalPython = $env:CLIPSYNC_PYTHON
$OriginalConfig = $env:CLIPSYNC_CONFIG_DIR
$Mutex = $null
$OwnsMutex = $false
$ConfigPath = $null

function Find-Executable([string[]]$Candidates, [string]$Label) {
    foreach ($Candidate in $Candidates) {
        if ([string]::IsNullOrWhiteSpace($Candidate)) { continue }
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $Candidate).Path
        }
        $Command = Get-Command $Candidate -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($Command) { return $Command.Source }
    }
    throw "$Label was not found. Install it, then double-click Start-ClipSync.bat again."
}

function Get-AvailablePort([int]$Requested) {
    $Candidates = if ($Requested) { @($Requested) } else { 1420..1470 }
    foreach ($Candidate in $Candidates) {
        $Listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Candidate)
        try {
            $Listener.Start()
            return $Candidate
        }
        catch [Net.Sockets.SocketException] {
            if ($Requested) { throw "Port $Requested is in use. Close its owner or omit -Port." }
        }
        finally { $Listener.Stop() }
    }
    throw "No free local port was found between 1420 and 1470."
}

Push-Location -LiteralPath $Root
try {
    $Node = Find-Executable @(
        "node.exe",
        (Join-Path $env:ProgramFiles "nodejs\node.exe")
    ) "Node.js 22.12 or newer"
    $CargoCandidates = @("cargo.exe", (Join-Path $env:USERPROFILE ".cargo\bin\cargo.exe"))
    if ($env:CARGO_HOME) { $CargoCandidates += (Join-Path $env:CARGO_HOME "bin\cargo.exe") }
    $Cargo = Find-Executable $CargoCandidates "Rust/Cargo"
    $PythonCandidates = @(
        (Join-Path $Root ".venv\Scripts\python.exe"),
        (Join-Path $Root "venv\Scripts\python.exe"),
        (Join-Path $env:USERPROFILE "miniconda3\python.exe"),
        (Join-Path $env:USERPROFILE "anaconda3\python.exe"),
        "python.exe"
    )
    if ($env:CONDA_PREFIX) {
        $PythonCandidates = @((Join-Path $env:CONDA_PREFIX "python.exe")) + $PythonCandidates
    }
    if ($env:CLIPSYNC_PYTHON) { $PythonCandidates = @($env:CLIPSYNC_PYTHON) }
    $Python = Find-Executable $PythonCandidates "Python 3.12 or newer"
    $env:PATH = (Split-Path -Parent $Node) + [IO.Path]::PathSeparator +
        (Split-Path -Parent $Cargo) + [IO.Path]::PathSeparator + $env:PATH
    $env:CLIPSYNC_PYTHON = $Python
    if (!$env:CLIPSYNC_CONFIG_DIR) {
        $env:CLIPSYNC_CONFIG_DIR = Join-Path $Root ".tauri-dev-data"
    }
    if (![IO.Path]::IsPathRooted($env:CLIPSYNC_CONFIG_DIR)) {
        throw "CLIPSYNC_CONFIG_DIR must be an absolute directory path."
    }
    & $Node -e "if (Number(process.versions.node.split('.')[0]) < 22 || (Number(process.versions.node.split('.')[0]) === 22 && Number(process.versions.node.split('.')[1]) < 12)) process.exit(1)"
    if ($LASTEXITCODE -ne 0) { throw "Node.js 22.12 or newer is required." }
    & $Python -c "import sys; assert sys.version_info >= (3, 12), 'Python 3.12+ required'; import internal.application.bootstrap; import internal.infrastructure.runtime.lan"
    if ($LASTEXITCODE -ne 0) {
        throw "Python dependencies are missing. In this project run: & '$Python' -m pip install -e ."
    }
    & $Cargo --version
    if ($LASTEXITCODE -ne 0) { throw "Rust/Cargo is not ready. Install the stable MSVC toolchain." }
    $Npm = Find-Executable @("npm.cmd") "npm"
    $SelectedPort = Get-AvailablePort $Port
    Write-Host "Python: $Python"
    Write-Host "Data:   $env:CLIPSYNC_CONFIG_DIR"
    Write-Host "Port:   $SelectedPort"
    Write-Host "Mode:   ClipSync new Tauri desktop (old desktop entry points are not launched)"
    if ($CheckOnly) {
        Write-Host "Startup checks passed. No application or service was started."
        exit 0
    }

    $Hash = [Security.Cryptography.SHA256]::Create()
    try {
        $RootKey = [BitConverter]::ToString(
            $Hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($Root.ToLowerInvariant()))
        ).Replace("-", "").Substring(0, 24)
    }
    finally { $Hash.Dispose() }
    $Mutex = [Threading.Mutex]::new($false, "Local\ClipSync-Launcher-$RootKey")
    try { $OwnsMutex = $Mutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $OwnsMutex = $true }
    if (!$OwnsMutex) {
        Write-Host "This desktop is already starting or running."
        $Native = Join-Path $Desktop "src-tauri\target\debug\clipsync-desktop.exe"
        if (Test-Path -LiteralPath $Native -PathType Leaf) {
            $Existing = Get-Process -Name "clipsync-desktop" -ErrorAction SilentlyContinue |
                Where-Object { $_.Path -eq $Native }
            if ($Existing) {
                # The user requested the interactive desktop. Tauri forwards focus.
                Start-Process -FilePath $Native -WorkingDirectory $Root
            }
        }
        exit 0
    }

    Push-Location -LiteralPath $Desktop
    try {
        $Cli = Join-Path $Desktop "node_modules\@tauri-apps\cli\tauri.js"
        $InstalledLock = Join-Path $Desktop "node_modules\.package-lock.json"
        $Lock = Get-Item -LiteralPath (Join-Path $Desktop "package-lock.json")
        $NeedsInstall = !(Test-Path -LiteralPath $Cli) -or !(Test-Path -LiteralPath $InstalledLock)
        if (!$NeedsInstall) {
            $NeedsInstall = $Lock.LastWriteTimeUtc -gt
                (Get-Item -LiteralPath $InstalledLock).LastWriteTimeUtc
        }
        if ($NeedsInstall) {
            Write-Host "Installing locked frontend dependencies..."
            & $Npm ci --no-audit --no-fund
            if ($LASTEXITCODE -ne 0) { throw "Frontend dependency installation failed." }
        }
        $BaseConfig = Get-Content -LiteralPath "src-tauri\tauri.conf.json" -Raw |
            ConvertFrom-Json
        $DevCsp = $BaseConfig.app.security.devCsp.Replace(
            "ws://127.0.0.1:1420", "ws://127.0.0.1:$SelectedPort"
        )
        $Override = @{
            build = @{
                beforeDevCommand = "node node_modules/vite/bin/vite.js --host 127.0.0.1 --port $SelectedPort --strictPort"
                devUrl = "http://127.0.0.1:$SelectedPort"
            }
            app = @{ security = @{ devCsp = $DevCsp } }
        }
        $BuildDir = Join-Path $Root "build"
        [IO.Directory]::CreateDirectory($BuildDir) | Out-Null
        $ConfigPath = Join-Path $BuildDir "tauri-launch-$PID.json"
        [IO.File]::WriteAllText(
            $ConfigPath, ($Override | ConvertTo-Json -Depth 8),
            [Text.UTF8Encoding]::new($false)
        )
        Write-Host "Building and opening ClipSync. First launch can take several minutes."
        Write-Host "Closing the window keeps ClipSync running in the system tray."
        Write-Host "Choose Exit ClipSync from the tray menu to stop the app and its local server."
        & $Node $Cli dev --no-watch --config $ConfigPath
        if ($LASTEXITCODE -ne 0) {
            throw "Tauri startup failed. Check the build output above (including MSVC/WebView2 prerequisites)."
        }
    }
    finally { Pop-Location }
}
catch {
    Write-Host ""
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
finally {
    if ($ConfigPath -and (Test-Path -LiteralPath $ConfigPath)) {
        Remove-Item -LiteralPath $ConfigPath -Force
    }
    if ($OwnsMutex) { $Mutex.ReleaseMutex() }
    if ($Mutex) { $Mutex.Dispose() }
    $env:PATH = $OriginalPath
    $env:CLIPSYNC_PYTHON = $OriginalPython
    $env:CLIPSYNC_CONFIG_DIR = $OriginalConfig
    Pop-Location
}
