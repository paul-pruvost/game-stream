# Build the GameStream Rust components behind the Netskope proxy.
# Sets up the CA bundle, an out-of-tree target dir (avoids AV/OneDrive file
# locks), and puts the full MinGW-w64 (WinLibs) on PATH for dlltool/as/g++.
# After building, copies the MinGW C++ runtime DLLs next to the client binaries
# (openh264 is C++; the relay is pure Rust and needs no DLLs).

$ErrorActionPreference = "Stop"

# 1. Netskope CA so cargo can fetch crates.io
$ca = "C:\Apps\Netskope\certifi_plus_netskope.pem"
if (Test-Path $ca) { $env:CARGO_HTTP_CAINFO = $ca }

# 2. Out-of-tree build dir (project lives under Documents; AV locks fresh exes)
$env:CARGO_TARGET_DIR = "$env:USERPROFILE\.gamestream-target"

# 3. Full MinGW-w64 (WinLibs) on PATH — rustup's self-contained dlltool is incomplete
$winlibs = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Directory `
    -Filter "BrechtSanders.WinLibs*" -ErrorAction SilentlyContinue | Select-Object -First 1
$mingwBin = $null
if ($winlibs) {
    $mingwBin = Get-ChildItem $winlibs.FullName -Recurse -Filter "dlltool.exe" -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty DirectoryName
    if ($mingwBin) { $env:PATH = "$mingwBin;$env:PATH" }
}

$cargo = "$env:USERPROFILE\.cargo\bin\cargo.exe"
$manifest = Join-Path $PSScriptRoot "Cargo.toml"
& $cargo build --release --manifest-path $manifest
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# 4. Bundle the MinGW C++ runtime next to the client binaries (openh264 is C++).
$rel = "$env:CARGO_TARGET_DIR\release"
if ($mingwBin) {
    foreach ($d in @("libstdc++-6.dll", "libwinpthread-1.dll", "libgcc_s_seh-1.dll")) {
        Copy-Item "$mingwBin\$d" $rel -Force -ErrorAction SilentlyContinue
    }
}

Write-Output "`nOK -> $rel"
Write-Output "  gamestream-relay.exe     (standalone single binary)"
Write-Output "  gamestream-headless.exe  (+ libstdc++-6 / libwinpthread-1 / libgcc_s_seh-1 .dll)"
