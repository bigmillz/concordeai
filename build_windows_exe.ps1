# Builds a real Windows app: MillenAI.exe, and an installer if Inno Setup is
# present. Run this ON Windows - PyInstaller cannot cross-compile, which is
# why this is the one build that can't be done from the Mac.
#
#   powershell -ExecutionPolicy Bypass -File build_windows_exe.ps1
#
# Produces:
#   dist\MillenAI\MillenAI.exe              the app (no Python needed to run)
#   ConcordeAI-<ver>-Setup-<arch>.exe       installer, if ISCC.exe is found
#   ConcordeAI-<ver>-Windows-exe-<arch>.zip otherwise, a zip of the folder
#
# PyInstaller bakes in whatever architecture built it, so the interpreter you
# run this with decides who can run the result:
#   x64 Python   -> x64 exe   : any normal PC, CUDA works. What a friend needs.
#   ARM64 Python -> ARM64 exe : Snapdragon/Surface/ARM VMs only, never CUDA.
# On an ARM box, an x64 Python runs fine under emulation and is the one to
# use for a shareable build. The script prefers x64 automatically when both
# are installed, and tells you which it picked.
#
# Both Pythons installed (an ARM VM, say)? -Arch picks one:
#   powershell -ExecutionPolicy Bypass -File build_windows_exe.ps1 -Arch arm64
# -Arch x64 or -Arch arm64 takes only an interpreter of that architecture
# and stops if there is none; with no -Arch it prefers x64, as before.
#
# KEEP THIS FILE PURE ASCII. Windows PowerShell 5.1 reads a .ps1 with no BOM
# as the ANSI codepage, not UTF-8. A UTF-8 em-dash arrives as three CP1252
# characters, the last of which is U+201D - a curly double quote, which
# PowerShell honours as a string delimiter. One dash in a comment silently
# desyncs the quoting for the rest of the file.
param(
  [ValidateSet("x64", "arm64")]
  [string]$Arch
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
# $Arch keeps its ValidateSet: never assign it (an assignment is validated
# too). PowerShell's -eq ignores case, so -Arch ARM64 works as well.

# ---------------------------------------------------------------- preflight
# Windows ships a stub python.exe under WindowsApps that only prints an advert
# for the Microsoft Store. It is on PATH even when Python is not installed, so
# resolve a real interpreter rather than trusting the name.
function Get-PyArch([string]$exe) {
  try {
    $a = & $exe -c "import platform;print(platform.machine())" 2>$null
    if ($LASTEXITCODE -eq 0) { return ("" + $a).Trim() }
  } catch { }
  return $null
}

# Collect every interpreter on the machine, then pick an x64 one. Testing
# only the first thing found is what went wrong before: on an ARM box the
# py.exe launcher is itself ARM64, so it answered for the whole machine and
# hid a perfectly good x64 install sitting next to it.
function Find-Python {
  $cands = New-Object System.Collections.Generic.List[string]

  foreach ($name in @("python", "python3")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -and $cmd.Source -notlike "*\WindowsApps\*") {
      $cands.Add($cmd.Source)
    }
  }
  # the launcher can enumerate every registered interpreter for us
  $launcher = Get-Command py -ErrorAction SilentlyContinue
  if ($launcher) {
    foreach ($line in (& $launcher.Source -0p 2>$null)) {
      if ("$line" -match '([A-Za-z]:\\[^\r\n]*python\.exe)') { $cands.Add($matches[1]) }
    }
  }
  # and the standard install locations, since a shell opened before the
  # installer ran captured the old PATH and will never see the new entry
  foreach ($g in @(
      "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
      "$env:ProgramFiles\Python3*\python.exe",
      "${env:ProgramFiles(x86)}\Python3*\python.exe",
      "C:\Python3*\python.exe")) {
    foreach ($f in (Get-ChildItem -Path $g -ErrorAction SilentlyContinue)) {
      $cands.Add($f.FullName)
    }
  }

  # Prefer x64 - it is the build an NVIDIA machine can actually run - but
  # fall back to ARM64 rather than claiming nothing is installed. With
  # -Arch, only that architecture will do (6b327).
  $seen = @{}
  $script:PyReport = @()
  $x64 = $null
  $arm = $null
  foreach ($exe in $cands) {
    if (-not (Test-Path $exe)) { continue }
    $key = $exe.ToLower()
    if ($seen.ContainsKey($key)) { continue }
    $seen[$key] = $true
    $arch = Get-PyArch $exe
    if (-not $arch) { continue }
    $script:PyReport += ("    {0,-8} {1}" -f $arch, $exe)
    if (-not $x64 -and $arch -match 'AMD64|x86_64') { $x64 = $exe }
    if (-not $arm -and $arch -match 'ARM64|aarch64') { $arm = $exe }
  }
  if ($Arch -eq "x64") { return $x64 }
  if ($Arch -eq "arm64") { return $arm }
  if ($x64) { return $x64 }
  return $arm
}

$py = Find-Python
if (-not $py -and $Arch) {
  Write-Host ""
  Write-Host "  No $Arch Python found. Interpreters found:" -ForegroundColor Yellow
  if ($script:PyReport) { $script:PyReport | ForEach-Object { Write-Host $_ } }
  else { Write-Host "    (none)" }
  Write-Host "  Install the $Arch build from https://www.python.org/downloads/windows/"
  Write-Host "  (for arm64, Python 3.11 or newer: 3.10 has no ARM64 cffi wheel)."
  throw "no $Arch Python interpreter found"
}
if (-not $py) {
  Write-Host ""
  Write-Host "  Python is not installed." -ForegroundColor Yellow
  Write-Host ""
  Write-Host "  Get it from  https://www.python.org/downloads/windows/"
  Write-Host "  Choose 'Windows installer (64-bit)' - the x64 one - if you"
  Write-Host "  want a build an NVIDIA machine can run. ARM64 also works, but"
  Write-Host "  the exe it makes runs only on ARM machines and has no CUDA."
  Write-Host ""
  Write-Host "  Tick 'Add python.exe to PATH' in the installer."
  Write-Host ""
  Write-Host "  ALREADY INSTALLED IT? Close this window and open a new one."
  Write-Host "  A shell captures PATH when it starts, so one opened before the"
  Write-Host "  installer ran will never see Python no matter how often you retry."
  Write-Host ""
  Write-Host "  If it still is not found afterwards, turn off the Store stubs:"
  Write-Host "  Settings > Apps > Advanced app settings > App execution aliases"
  Write-Host "  -> switch off python.exe and python3.exe."
  throw "no Python interpreter found"
}
Write-Host "python: $py"
if ($script:PyReport -and $script:PyReport.Count -gt 1) {
  Write-Host "  interpreters found:"
  $script:PyReport | ForEach-Object { Write-Host $_ }
}

$arch = & $py -c "import platform;print(platform.machine())"
Write-Host "python architecture: $arch"
Write-Host ("-> building with: {0} ({1}){2}" -f $py, $arch,
            $(if ($Arch) { " as asked by -Arch $Arch" } else { "" }))
$isArm = $arch -match 'ARM64|aarch64'
if ($isArm) {
  Write-Host ""
  Write-Host "  Building an ARM64 exe." -ForegroundColor Yellow
  Write-Host "  Runs on: this VM, Snapdragon and Surface ARM machines."
  Write-Host "  Does NOT run on an x64 PC - x64 Windows cannot execute ARM64"
  Write-Host "  binaries; emulation only works the other way round. There is"
  Write-Host "  also no CUDA on Windows-on-ARM, so this build is CPU-only."
  Write-Host "  For an NVIDIA machine, rerun this under the x64 python.org build."
  Write-Host ""
} elseif ($arch -notmatch 'AMD64|x86_64') {
  throw "unrecognised architecture: $arch"
}

$ver = (Select-String -Path millenai.py -Pattern 'APP_VERSION = "([^"]+)"').Matches[0].Groups[1].Value
Write-Host "version: $ver"

# ------------------------------------------------------------------- venv
$bv = ".build-venv"
# the build venv is kept between builds; one made by another Python (the
# other architecture, after -Arch, or another version) would freeze the
# wrong interpreter in, so it is made again (6b327)
$pyId = & $py -c "import platform,sys;print(platform.machine(), sys.version.split()[0])"
if (Test-Path "$bv\Scripts\python.exe") {
  $bvId = & "$bv\Scripts\python.exe" -c "import platform,sys;print(platform.machine(), sys.version.split()[0])"
  if ("$bvId" -ne "$pyId") {
    Write-Host "-> $bv was made by $bvId, not $pyId; making it again"
    Remove-Item -Recurse -Force $bv
  }
}
if (-not (Test-Path "$bv\Scripts\python.exe")) {
  Write-Host "-> creating build venv"
  & $py -m venv $bv
}
$bpy = "$bv\Scripts\python.exe"
Write-Host "-> installing build dependencies (a few minutes the first time)"
& $bpy -m pip install --upgrade pip | Out-Null
# PyNaCl for accounts (6b327): the hash-pinned wheels from CRYPTO_REQS in
# millenai.py (cffi and pycparser with it), in their own pip call before
# the rest, so the pinned cffi is the one that gets frozen in. Both x64
# and ARM64 have wheels. A build without them stops here: accounts would
# be off for everyone who installed it. A release from before 6b327 has
# no pins (exit 3) and builds as it did.
$crypto = $true
& $bpy packaging\crypto_reqs.py millenai.py crypto-requirements.txt
if ($LASTEXITCODE -eq 3) {
  Write-Host "  no CRYPTO_REQS in millenai.py (a release from before 6b327): no PyNaCl"
  $crypto = $false
} elseif ($LASTEXITCODE -ne 0) {
  throw "could not read CRYPTO_REQS from millenai.py"
} else {
  # --force-reinstall: --require-hashes checks only what it downloads, and
  # .build-venv lives between builds, so the pins are fetched and checked
  # every time rather than trusted from the last build
  & $bpy -m pip install --only-binary=:all: --require-hashes --no-deps --force-reinstall -r crypto-requirements.txt
  if ($LASTEXITCODE -ne 0) { throw "PyNaCl did not install (hash-pinned wheels, see above)" }
}
# tzdata: Windows has no time-zone database of its own. The four export
# libraries can't be pip-installed on demand inside a frozen app, so they
# ship in it (6b317, from the Windows sweep).
# pywebview is pinned (6b321): the app guards its js_api bridge, which
# was written and tested against this version
$deps = @("pyinstaller", "pywebview==6.2.1", "ddgs", "psutil", "huggingface_hub",
          "tzdata", "reportlab", "openpyxl", "python-docx", "python-pptx")
if ($isArm) {
  # pythonnet - pywebview's default Windows backend - has no ARM64 wheel, so
  # drive the Qt backend instead; PySide6 and QtWebEngine do ship one.
  # ctranslate2 has no ARM64 wheel either, so voice input is left out and the
  # app degrades to "voice unavailable" rather than failing to start.
  $deps += "pyside6"
} else {
  $deps += "faster-whisper"
}
& $bpy -m pip install @deps | Out-Null
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# ------------------------------------------------------------------- build
if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
if (Test-Path "dist")  { Remove-Item -Recurse -Force "dist"  }

# onedir, not onefile: pywebview loads native WebView2 pieces at runtime and
# is markedly more reliable unpacked. The installer hides the folder anyway.
# NB: not named $args - that is an automatic variable in PowerShell.
$pyiArgs = @(
  "--noconfirm", "--clean", "--windowed", "--onedir",
  "--name", "MillenAI",
  "--icon", "MillenAI.ico",
  "--collect-all", "webview",
  "--collect-data", "tzdata"
)
# PyNaCl's libsodium module loads _cffi_backend from C, where PyInstaller
# cannot see the import (6b327): named here for x64 and ARM64 alike, or
# the exe imports nacl and fails its self-test below
if ($crypto) {
  $pyiArgs += @("--hidden-import", "_cffi_backend", "--collect-submodules", "nacl")
}
# pywebview resolves its backend dynamically, so PyInstaller cannot see it
if ($isArm) {
  $pyiArgs += @("--hidden-import", "webview.platforms.qt", "--collect-all", "PySide6")
} else {
  $pyiArgs += @("--hidden-import", "webview.platforms.edgechromium", "--hidden-import", "clr")
}
$pyiArgs += "millenai.py"
Write-Host "-> running PyInstaller"
& $bpy -m PyInstaller @pyiArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
if (-not (Test-Path "dist\MillenAI\MillenAI.exe")) { throw "no exe was produced" }
Write-Host "-> built dist\MillenAI\MillenAI.exe"

# THE CRYPTO SELF-TEST (0a 5.9, 6b327): the exe runs cai_crypto's
# known-answer test and exits 0 only when libsodium answered right. It
# runs before any window, port or data folder. An exe that fails it is
# never packaged.
if ($crypto) {
  $st = Join-Path (Get-Location) "crypto-selftest.json"
  if (Test-Path $st) { Remove-Item $st }
  $p = Start-Process -FilePath "dist\MillenAI\MillenAI.exe" `
         -ArgumentList @("--crypto-selftest", ('"' + $st + '"')) -Wait -PassThru
  if (Test-Path $st) { Write-Host ("crypto self-test: " + (Get-Content -Raw $st)) }
  if ($p.ExitCode -ne 0) { throw "the exe failed its crypto self-test (exit $($p.ExitCode)): accounts would be off" }
}

# --------------------------------------------------------------- installer
if ($env:MILLENAI_NO_PACKAGE -eq "1") {
  Write-Host "MILLENAI_NO_PACKAGE=1 - dist\MillenAI is the product; skipping zip/installer"
  exit 0
}
$iscc = @(
  "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
  "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
  # a per-user Inno install (winget without admin) lands here (6b327)
  "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

$suffix   = if ($isArm) { "arm64" } else { "x64" }
$archMode = if ($isArm) { "arm64" } else { "x64compatible" }

if ($iscc) {
  Write-Host "-> found Inno Setup, building the installer"
  $iss = @"
[Setup]
AppName=ConcordeAI
; identity stays pinned to the original name (Inno's default AppId was
; the old AppName) so a ConcordeAI installer upgrades a MillenAI-era
; install in place instead of installing beside it
AppId=MillenAI
AppVersion=$ver
DefaultDirName={autopf}\MillenAI
DefaultGroupName=ConcordeAI
OutputBaseFilename=ConcordeAI-$ver-Setup-$suffix
OutputDir=.
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=$archMode
SetupIconFile=MillenAI.ico
UninstallDisplayIcon={app}\MillenAI.exe
PrivilegesRequired=lowest

[Files]
Source: "dist\MillenAI\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\ConcordeAI"; Filename: "{app}\MillenAI.exe"
Name: "{autodesktop}\ConcordeAI"; Filename: "{app}\MillenAI.exe"

[Run]
Filename: "{app}\MillenAI.exe"; Description: "Launch ConcordeAI"; Flags: nowait postinstall skipifsilent
"@
  Set-Content -Encoding UTF8 "MillenAI.iss" $iss
  & $iscc "MillenAI.iss"
  if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
  Write-Host ""
  Write-Host "built ConcordeAI-$ver-Setup-$suffix.exe" -ForegroundColor Green
} else {
  $zip = "ConcordeAI-$ver-Windows-exe-$suffix.zip"
  if (Test-Path $zip) { Remove-Item $zip }
  # the unzipped folder should carry the brand people downloaded, not
  # the internal name - stage a renamed copy just for the archive
  if (Test-Path "dist\ConcordeAI") { Remove-Item -Recurse -Force "dist\ConcordeAI" }
  Copy-Item -Recurse "dist\MillenAI" "dist\ConcordeAI"
  Compress-Archive -Path "dist\ConcordeAI" -DestinationPath $zip
  Remove-Item -Recurse -Force "dist\ConcordeAI"
  Write-Host ""
  Write-Host "built $zip" -ForegroundColor Green
  Write-Host "(install Inno Setup 6 for a single-file installer instead:"
  Write-Host "  winget install JRSoftware.InnoSetup )"
}

Write-Host ""
Write-Host "Models are NOT bundled - the app downloads them on first run,"
Write-Host "and pulls Ollama's CUDA build automatically on an NVIDIA machine."
