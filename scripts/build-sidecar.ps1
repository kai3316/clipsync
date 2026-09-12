param(
    [switch]$SelfTest,
    [string]$Python = "python",
    [string]$Cargo = "cargo"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Push-Location $Root
try {
    & $Python -m PyInstaller --noconfirm clipsync-sidecar.spec
    if ($LASTEXITCODE -ne 0) { throw "Sidecar build failed" }
    $Target = (& $Cargo -vV | Select-String '^host: ').ToString().Substring(6).Trim()
    if (!$Target) { throw "Could not determine Rust host target" }
    $Extension = if ($IsWindows -or $env:OS -eq "Windows_NT") { ".exe" } else { "" }
    $Source = Join-Path $Root "dist/clipsync-sidecar$Extension"
    $Output = Join-Path $Root "desktop/src-tauri/binaries/clipsync-sidecar-$Target$Extension"
    Copy-Item -LiteralPath $Source -Destination $Output -Force
    if ($SelfTest) {
        & $Python scripts/smoke_sidecar.py $Output
        if ($LASTEXITCODE -ne 0) { throw "Packaged sidecar self-test failed" }
    }
    Write-Output $Output
}
finally { Pop-Location }
