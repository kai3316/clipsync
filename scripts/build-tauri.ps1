param(
    [switch]$VerifyPackage,
    # Ship the sidecar as a directory resource instead of the onefile
    # `externalBin`.  Worth about 800ms on every launch (see
    # clipsync-sidecar.spec), at the cost of the installer carrying a directory
    # rather than a single file.  macOS always does this; the switch is here so
    # Windows and Linux can too, and so the two shapes stay buildable while the
    # change is being rolled out.
    [switch]$OneDirSidecar,
    [string]$Python = "python",
    [string]$Cargo = "cargo"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if ($OneDirSidecar) {
    & "$PSScriptRoot/build-sidecar.ps1" -SelfTest -OneDir -Python $Python -Cargo $Cargo
} else {
    & "$PSScriptRoot/build-sidecar.ps1" -SelfTest -Python $Python -Cargo $Cargo
}
if ($LASTEXITCODE -ne 0) { throw "Sidecar staging failed" }
$OriginalPath = $env:PATH
$CargoDirectory = Split-Path -Parent (Get-Command $Cargo -ErrorAction Stop).Source
$env:PATH = $CargoDirectory + [IO.Path]::PathSeparator + $env:PATH
Push-Location (Join-Path $Root "desktop")
try {
    & npm run build
    if ($LASTEXITCODE -ne 0) { throw "Frontend build failed" }
    & npm test
    if ($LASTEXITCODE -ne 0) { throw "Frontend tests failed" }
    & $Cargo test --manifest-path src-tauri/Cargo.toml --locked
    if ($LASTEXITCODE -ne 0) { throw "Rust tests failed" }
    $Version = (& $Python -c "import sys; sys.path.insert(0, '..'); from internal.version import __version__; print(__version__)").Trim()
    New-Item -ItemType Directory -Force -Path (Join-Path $Root "build") | Out-Null
    $Config = Join-Path $Root "build/tauri-package.json"
    # externalBin is resolved against src-tauri, not against this file, so the
    # path is "binaries/..." -- spelling it "../desktop/src-tauri/binaries/..."
    # made every build fail with "resource path ... doesn't exist".
    # WriteAllText rather than Set-Content -Encoding utf8: under Windows
    # PowerShell 5.1 that writes a BOM, and Tauri will not parse the config.
    # `createUpdaterArtifacts` is asked for only when a signing key is actually
    # in reach.  tauri.conf.json commits the public key, so the bundler treats
    # "sign these" with no private key available as an error rather than a
    # no-op; CI gates the same flag on the tag, because a pull request from a
    # fork gets no secrets.  Without it a local build still produces every
    # installer, just none of the updater payloads or their .sig files.
    #
    # The directory shape is a bundle *resource* rather than an `externalBin`,
    # because that field takes one executable and the tree needs room for the
    # `_internal` directory beside it.  Which shape was staged is what decides
    # this, and both are configured the way the host's own lookup expects --
    # see `pick_sidecar` in src-tauri/src/bridge.rs.
    if ($OneDirSidecar) {
        # The array form, as the macOS job has always written it: the staged
        # directory lands beside the host, which is where `pick_sidecar` looks.
        $Bundle = @{ resources = @("sidecar") }
    } else {
        $Bundle = @{ externalBin = @("binaries/clipsync-sidecar") }
    }
    if ($env:TAURI_SIGNING_PRIVATE_KEY) { $Bundle.createUpdaterArtifacts = $true }
    $Override = @{ version = $Version; bundle = $Bundle } | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText($Config, $Override)
    & npm exec tauri build -- --config $Config
    if ($LASTEXITCODE -ne 0) { throw "Tauri build failed" }
    if ($VerifyPackage) {
        # This verifies the staged companion executable, not signing or installation.
        $Extension = if ($IsWindows -or $env:OS -eq "Windows_NT") { ".exe" } else { "" }
        if ($OneDirSidecar) {
            $Staged = Join-Path $Root "desktop/src-tauri/sidecar/clipsync-sidecar$Extension"
        } else {
            $Target = (& $Cargo -vV | Select-String '^host: ').ToString().Substring(6).Trim()
            $Staged = Join-Path $Root "desktop/src-tauri/binaries/clipsync-sidecar-$Target$Extension"
        }
        & $Python "$PSScriptRoot/smoke_sidecar.py" $Staged
        if ($LASTEXITCODE -ne 0) { throw "Staged sidecar verification failed" }
    }
}
finally {
    Pop-Location
    $env:PATH = $OriginalPath
}
