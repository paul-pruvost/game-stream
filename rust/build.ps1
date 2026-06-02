# Build the GameStream Rust components behind the Netskope proxy.
# Sets up the CA bundle, an out-of-tree target dir (avoids AV/OneDrive file
# locks), and puts the full MinGW-w64 (WinLibs) on PATH for dlltool/as.

$ErrorActionPreference = "Stop"

# 1. Netskope CA so cargo can fetch crates.io
$ca = "C:\Apps\Netskope\certifi_plus_netskope.pem"
if (Test-Path $ca) { $env:CARGO_HTTP_CAINFO = $ca }

# 2. Out-of-tree build dir (project lives under Documents; AV locks fresh exes)
$env:CARGO_TARGET_DIR = "$env:USERPROFILE\.gamestream-target"

# 3. Full MinGW-w64 (WinLibs) on PATH — rustup's self-contained dlltool is incomplete
$winlibs = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Directory `
    -Filter "BrechtSanders.WinLibs*" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($winlibs) {
    $bin = Get-ChildItem $winlibs.FullName -Recurse -Filter "dlltool.exe" -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty DirectoryName
    if ($bin) { $env:PATH = "$bin;$env:PATH" }
}

$cargo = "$env:USERPROFILE\.cargo\bin\cargo.exe"
$manifest = Join-Path $PSScriptRoot "Cargo.toml"
& $cargo build --release --manifest-path $manifest
if ($LASTEXITCODE -eq 0) {
    Write-Output "`nOK -> $env:CARGO_TARGET_DIR\release\gamestream-relay.exe"
}
exit $LASTEXITCODE
