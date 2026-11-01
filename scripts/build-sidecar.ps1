param(
    [switch]$SelfTest,
    [switch]$OneDir,
    [string]$Python = "python",
    [string]$Cargo = "cargo"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Push-Location $Root
try {
    # Two shapes come out of the spec (see its docstring): the onefile
    # executable, and the directory that costs 191ms a launch instead of 1004ms
    # but needs a packager able to ship a tree.  -OneDir asks for the second;
    # macOS always builds it, because that platform re-validates every binary
    # the onefile writes to a fresh temp path on every launch.
    if ($OneDir) { $env:CLIPSYNC_SIDECAR_ONEDIR = "1" }
    & $Python -m PyInstaller --noconfirm clipsync-sidecar.spec
    if ($LASTEXITCODE -ne 0) { throw "Sidecar build failed" }
    $Extension = if ($IsWindows -or $env:OS -eq "Windows_NT") { ".exe" } else { "" }

    if ($OneDir) {
        # Staged as a bundle *resource*: `externalBin` takes a single executable
        # and the tree has nowhere to put its `_internal` directory beside it.
        # The host looks under `sidecar/` first -- see `pick_sidecar` in
        # desktop/src-tauri/src/bridge.rs.
        $Source = Join-Path $Root "dist/clipsync-sidecar"
        if (!(Test-Path -LiteralPath $Source)) { throw "Onedir sidecar not found at $Source" }
        $Output = Join-Path $Root "desktop/src-tauri/sidecar"
        if (Test-Path -LiteralPath $Output) { Remove-Item -LiteralPath $Output -Recurse -Force }
        Copy-Item -LiteralPath $Source -Destination $Output -Recurse -Force
        $Staged = Join-Path $Output "clipsync-sidecar$Extension"
    }
    else {
        $Target = (& $Cargo -vV | Select-String '^host: ').ToString().Substring(6).Trim()
        if (!$Target) { throw "Could not determine Rust host target" }
        $Source = Join-Path $Root "dist/clipsync-sidecar$Extension"
        $Output = Join-Path $Root "desktop/src-tauri/binaries/clipsync-sidecar-$Target$Extension"
        Copy-Item -LiteralPath $Source -Destination $Output -Force
        $Staged = $Output
    }

    if ($SelfTest) {
        & $Python scripts/smoke_sidecar.py $Staged
        if ($LASTEXITCODE -ne 0) { throw "Packaged sidecar self-test failed" }
    }
    Write-Output $Staged
}
finally {
    Pop-Location
    if ($OneDir) { Remove-Item Env:CLIPSYNC_SIDECAR_ONEDIR -ErrorAction SilentlyContinue }
}
