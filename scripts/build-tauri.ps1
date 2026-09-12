param(
    [switch]$VerifyPackage,
    [string]$Python = "python",
    [string]$Cargo = "cargo"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
& "$PSScriptRoot/build-sidecar.ps1" -SelfTest -Python $Python -Cargo $Cargo
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
    $Override = @{ version = $Version; bundle = @{ externalBin = @("binaries/clipsync-sidecar") } } |
        ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText($Config, $Override)
    & npm exec tauri build -- --config $Config
    if ($LASTEXITCODE -ne 0) { throw "Tauri build failed" }
    if ($VerifyPackage) {
        # This verifies the staged companion executable, not signing or installation.
        $Extension = if ($IsWindows -or $env:OS -eq "Windows_NT") { ".exe" } else { "" }
        $Target = (& $Cargo -vV | Select-String '^host: ').ToString().Substring(6).Trim()
        $Staged = Join-Path $Root "desktop/src-tauri/binaries/clipsync-sidecar-$Target$Extension"
        & $Python "$PSScriptRoot/smoke_sidecar.py" $Staged
        if ($LASTEXITCODE -ne 0) { throw "Staged sidecar verification failed" }
    }
}
finally {
    Pop-Location
    $env:PATH = $OriginalPath
}
