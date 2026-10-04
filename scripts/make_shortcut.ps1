$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $dir "open.bat"))) {
  $dir = $PSScriptRoot
  if (-not (Test-Path -LiteralPath (Join-Path $dir "open.bat"))) {
    $dir = Split-Path -Parent $PSScriptRoot
  }
}
# scripts/ -> project root
$dir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$lnkPath = Join-Path $dir "LocalPluginManager.lnk"
$target = Join-Path $dir "open.bat"
$ico = Join-Path $dir "resources\icon.ico"
$w = New-Object -ComObject WScript.Shell
$s = $w.CreateShortcut($lnkPath)
$s.TargetPath = $target
$s.WorkingDirectory = $dir
if (Test-Path -LiteralPath $ico) {
  $s.IconLocation = $ico
}
$s.Description = "Local Plugin Manager"
$s.Save()
Write-Output "Created: $lnkPath"
