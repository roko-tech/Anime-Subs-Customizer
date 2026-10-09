<#
.SYNOPSIS
  One-command Windows build: tests -> freeze -> self-test -> notices -> installer -> portable ZIP -> checksums.

.DESCRIPTION
  Creates/uses a hash-locked build environment (.venv-build, from requirements-lock.txt),
  freezes the app with PyInstaller (AnimeSubsCustomizer.spec), runs the frozen exe's
  --self-test, bundles FFmpeg (a folder you pass with -FFmpegDir), writes
  THIRD-PARTY-NOTICES.txt, compiles the Inno Setup installer and the portable ZIP,
  and writes SHA256SUMS.txt. Everything lands in build\out\.

.EXAMPLE
  pwsh scripts/build.ps1 -FFmpegDir C:\path\to\ffmpeg-minimal\bin
.EXAMPLE
  pwsh scripts/build.ps1 -SkipTests -NoFFmpeg
#>
param(
  [string]$Python = "",        # interpreter for the build venv (default: py -3.13)
  [string]$FFmpegDir = "",     # folder with ffmpeg.exe, ffprobe.exe, their DLLs and licence texts
  [switch]$NoFFmpeg,           # build without FFmpeg (extraction/real-frame preview then need it on PATH)
  [switch]$SkipTests,
  [switch]$NoInstaller,
  [string]$Iscc = ""           # path to ISCC.exe (default: Inno Setup 7 per-user or per-machine install)
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Build = Join-Path $Root "build"
$Out = Join-Path $Build "out"
$Venv = Join-Path $Root ".venv-build"
$Py = Join-Path $Venv "Scripts\python.exe"

function Step($text) { Write-Host "`n=== $text" -ForegroundColor Cyan }
function Check($ok, $why) { if (-not $ok) { throw $why } }

$version = (Select-String -Path (Join-Path $Root "anime_subs\runtime.py") -Pattern '^APP_VERSION = "(\d+\.\d+\.\d+)"').Matches[0].Groups[1].Value
Write-Host "Anime Subs Customizer $version"

Step "Build environment (requirements-lock.txt, hash-checked)"
if (-not (Test-Path $Py)) {
  if ($Python) { & $Python -m venv $Venv } else { & py -3.13 -m venv $Venv }
  Check ($LASTEXITCODE -eq 0) "could not create $Venv"
}
& $Py -m pip install --disable-pip-version-check -q --require-hashes -r (Join-Path $Root "requirements-lock.txt")
Check ($LASTEXITCODE -eq 0) "dependency install failed"

if (-not $SkipTests) {
  Step "Tests"
  Push-Location $Root
  try { & $Py -m pytest tests -q -p no:cacheprovider; Check ($LASTEXITCODE -eq 0) "tests failed" } finally { Pop-Location }
}

Step "Freeze (PyInstaller one-folder, no UPX)"
Remove-Item (Join-Path $Build "dist"), (Join-Path $Build "work") -Recurse -Force -ErrorAction SilentlyContinue
Push-Location $Root
try {
  & $Py -m PyInstaller --noconfirm --clean --log-level WARN --distpath (Join-Path $Build "dist") --workpath (Join-Path $Build "work") AnimeSubsCustomizer.spec
  Check ($LASTEXITCODE -eq 0) "PyInstaller failed"
} finally { Pop-Location }
$Dist = Join-Path $Build "dist\AnimeSubsCustomizer"
$Exe = Join-Path $Dist "AnimeSubsCustomizer.exe"

Step "Self-test of the frozen exe"
$report = Join-Path $Build "selftest-frozen.json"
$p = Start-Process $Exe -ArgumentList "--self-test", "--self-test-out=`"$report`"" -PassThru -Wait
$json = Get-Content $report -Raw | ConvertFrom-Json
$json.checks.PSObject.Properties | ForEach-Object { "{0,-22} {1}" -f $_.Name, $_.Value.ok }
Check ($p.ExitCode -eq 0 -and $json.ok) "frozen self-test failed (exit $($p.ExitCode)); see $report"

$ffBin = $null
if (-not $NoFFmpeg) {
  Step "FFmpeg"
  Check ($FFmpegDir -and (Test-Path (Join-Path $FFmpegDir "ffmpeg.exe"))) "pass -FFmpegDir <folder with ffmpeg.exe> or -NoFFmpeg"
  $ffBin = (Resolve-Path $FFmpegDir).Path
  $cfg = (& (Join-Path $ffBin "ffmpeg.exe") -hide_banner -version 2>&1 | Select-String "configuration:").Line
  Write-Host $cfg
  # ADR-004: never ship a GPL or nonfree FFmpeg.
  Check (-not ($cfg -match "--enable-(gpl|nonfree)")) "this FFmpeg is GPL or nonfree; refusing to bundle it"
  Check (Test-Path (Join-Path $ffBin "ffprobe.exe")) "ffprobe.exe missing in $ffBin"
}

Step "Third-party notices"
$notices = Join-Path $Build "notices\THIRD-PARTY-NOTICES.txt"
$noticeArgs = @("scripts\notices.py", "--out", $notices)
if ($ffBin) { $noticeArgs += @("--ffmpeg-dir", $ffBin) }
Push-Location $Root
try { & $Py @noticeArgs; Check ($LASTEXITCODE -eq 0) "notices failed" } finally { Pop-Location }
Copy-Item $notices $Dist
Copy-Item (Join-Path $Root "LICENSE") (Join-Path $Dist "LICENSE.txt")

Remove-Item $Out -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $Out | Out-Null

if (-not $NoInstaller) {
  Step "Installer (Inno Setup)"
  if (-not $Iscc) {
    $Iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 7\ISCC.exe",
              "$env:ProgramFiles\Inno Setup 7\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
  }
  Check ($Iscc -and (Test-Path $Iscc)) "ISCC.exe (Inno Setup 7) not found; pass -Iscc"
  $isccArgs = @("/Q", "/DAppVersion=$version", "/DSourceDir=$Dist", "/DOutputDir=$Out")
  if ($ffBin) { $isccArgs += "/DFFmpegDir=$ffBin" }
  & $Iscc @isccArgs (Join-Path $Root "installer\AnimeSubsCustomizer.iss")
  Check ($LASTEXITCODE -eq 0) "ISCC failed"
}

Step "Portable ZIP"
$stage = Join-Path $Build "portable\AnimeSubsCustomizer"
Remove-Item (Join-Path $Build "portable") -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item $Dist $stage -Recurse
# A portable copy keeps its data beside the exe (ADR-003).
Set-Content (Join-Path $stage "portable.marker") "This copy keeps its settings in this folder." -Encoding utf8
if ($ffBin) { Copy-Item $ffBin (Join-Path $stage "bin") -Recurse }
Compress-Archive -Path $stage -DestinationPath (Join-Path $Out "AnimeSubsCustomizer-portable.zip") -CompressionLevel Optimal
Copy-Item $notices $Out

Step "Checksums"
Push-Location $Out
try {
  Get-ChildItem -File | Where-Object Name -ne "SHA256SUMS.txt" | Sort-Object Name | ForEach-Object {
    "{0}  {1}" -f (Get-FileHash $_.Name -Algorithm SHA256).Hash.ToLower(), $_.Name
  } | Set-Content "SHA256SUMS.txt" -Encoding ascii
  Get-Content "SHA256SUMS.txt"
} finally { Pop-Location }

Get-ChildItem $Out | Select-Object Name, @{n = "MB"; e = { [math]::Round($_.Length / 1MB, 1) } } | Format-Table -AutoSize
Write-Host "Done: $Out" -ForegroundColor Green
