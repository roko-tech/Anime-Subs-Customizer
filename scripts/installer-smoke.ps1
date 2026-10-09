<#
.SYNOPSIS
  Installer smoke test: silent install -> installed --self-test (+ desktop screenshot)
  -> silent upgrade over a RUNNING copy -> silent uninstall that keeps settings.
  Run on a clean machine (GitHub runner, Windows Sandbox); it installs per-user.

.EXAMPLE
  pwsh scripts/installer-smoke.ps1 -Setup build/out/AnimeSubsCustomizer-Setup.exe -Evidence build/smoke
#>
param(
  [Parameter(Mandatory)] [string]$Setup,
  [string]$Evidence = "smoke-evidence"
)
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force $Evidence | Out-Null
$Evidence = (Resolve-Path $Evidence).Path
$Setup = (Resolve-Path $Setup).Path
$App = Join-Path $env:LOCALAPPDATA "Programs\Anime Subs Customizer"
$Exe = Join-Path $App "AnimeSubsCustomizer.exe"
$Data = Join-Path $env:LOCALAPPDATA "Anime Subs Customizer"
$Mutex = "ASS-Subs-Changer-Singleton"

function Step($t) { Write-Host "`n=== $t" -ForegroundColor Cyan }
function Check($ok, $why) { if (-not $ok) { throw "FAILED: $why" } ; Write-Host "  ok: $why" }
function MutexHeld { $m = $null; $held = [System.Threading.Mutex]::TryOpenExisting($Mutex, [ref]$m); if ($m) { $m.Dispose() }; $held }
function WaitFor($cond, $seconds) { $t0 = Get-Date; while (-not (& $cond)) { if (((Get-Date) - $t0).TotalSeconds -gt $seconds) { return $false }; Start-Sleep -Milliseconds 300 }; $true }
function Shot($path) {
  Add-Type -AssemblyName System.Windows.Forms, System.Drawing
  $b = [System.Windows.Forms.SystemInformation]::VirtualScreen
  $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  try { $g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size); $bmp.Save($path) } catch { Write-Host "  (no screenshot: $_)" } finally { $g.Dispose(); $bmp.Dispose() }
}
function RunSetup($log, [string[]]$extra = @()) {
  $args = @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=`"$log`"") + $extra
  # Wait for Setup itself only: Start-Process -Wait also waits for the
  # process tree, which after an upgrade includes the relaunched tray app.
  $p = Start-Process $Setup -ArgumentList $args -PassThru
  $p.WaitForExit()
  $p.ExitCode
}

Step "Silent install (per-user, no administrator)"
Check ((RunSetup (Join-Path $Evidence "install.log")) -eq 0) "installer exit code 0"
Check (Test-Path $Exe) "app installed to $App"
Check (Test-Path (Join-Path $App "THIRD-PARTY-NOTICES.txt")) "notices installed"
Check (Test-Path (Join-Path $App "bin\ffmpeg.exe")) "FFmpeg installed"
$lang = (Get-ItemProperty "HKCU:\Software\Anime Subs Customizer" -ErrorAction SilentlyContinue).InstallerLanguage
Check ($lang -in @("en", "ar")) "installer language recorded ($lang)"

Step "Installed --self-test"
$report = Join-Path $Evidence "installed-selftest.json"
$p = Start-Process $Exe -ArgumentList "--self-test", "--self-test-out=`"$report`"" -PassThru
Start-Sleep -Seconds 4
Shot (Join-Path $Evidence "desktop-during-selftest.png")
Check ($p.WaitForExit(180000)) "self-test finished"
$json = Get-Content $report -Raw | ConvertFrom-Json
$json.checks.PSObject.Properties | ForEach-Object { "  {0,-22} ok={1} {2}" -f $_.Name, $_.Value.ok, ($_.Value.detail | ConvertTo-Json -Compress -Depth 2) }
Check ($p.ExitCode -eq 0 -and $json.ok) "installed self-test passed"
Check ([string]$json.checks.ffmpeg.detail -like "*\bin\ffmpeg.exe") "bundled FFmpeg found"

Step "Silent upgrade over a running copy"
Start-Process $Exe -ArgumentList "--minimized" | Out-Null
Check (WaitFor { MutexHeld } 30) "app running (mutex held)"
Start-Sleep -Seconds 3
Check ((RunSetup (Join-Path $Evidence "upgrade.log")) -eq 0) "upgrade succeeded while the app was running"
Check (WaitFor { MutexHeld } 30) "app relaunched after the silent upgrade"
Check (Test-Path (Join-Path $Data "config.json")) "settings exist in $Data"

Step "--quit"
$q = Start-Process $Exe -ArgumentList "--quit" -PassThru -Wait
Check ($q.ExitCode -eq 0 -and -not (MutexHeld)) "--quit closed the running app"

Step "Silent uninstall keeps settings"
$uninst = Join-Path $App "unins000.exe"
Start-Process $uninst -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=`"$(Join-Path $Evidence 'uninstall.log')`"" -Wait
Check (WaitFor { -not (Test-Path $Exe) } 60) "app removed"
Check (Test-Path (Join-Path $Data "config.json")) "settings kept"
Check (-not (Test-Path "HKCU:\Software\Anime Subs Customizer")) "installer language key removed"

Write-Host "`nSmoke test passed." -ForegroundColor Green
