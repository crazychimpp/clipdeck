#PS_BEGIN
# Clipdeck setup and launcher for Windows. Runs from inside Clipdeck.cmd.
# 'Continue' so tools that print progress to stderr (pip, uv) don't abort setup;
# cmdlets that must succeed use -ErrorAction Stop.
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
# Windows PowerShell 5.1 may default to old TLS, which GitHub refuses.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$Version = '__VERSION__'
$Self  = $env:CLIPDECK_SELF
$Root  = Join-Path $env:LOCALAPPDATA 'Clipdeck'
$App   = Join-Path $Root 'app'
$Venv  = Join-Path $App '.venv'
$Py    = Join-Path $Venv 'Scripts\python.exe'
$Pyw   = Join-Path $Venv 'Scripts\pythonw.exe'
$Stamp = Join-Path $App 'version.txt'
$Url   = 'http://127.0.0.1:8765'

function Say($m) { Write-Host "  $m" -ForegroundColor Cyan }
function Test-Clipdeck {
  try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Headers @{ 'X-Clipdeck' = '1' } -Uri "$Url/api/status" | Out-Null; return $true }
  catch { return $false }
}
function Find-Uv {
  $c = Get-Command uv -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  foreach ($p in @((Join-Path $Root 'uv\uv.exe'), "$env:USERPROFILE\.local\bin\uv.exe", "$env:USERPROFILE\.cargo\bin\uv.exe", "$env:LOCALAPPDATA\Programs\uv\uv.exe")) {
    if (Test-Path $p) { return $p }
  }
  return $null
}

try {
  $installed = (Test-Path $Pyw) -and (Test-Path $Stamp) -and ((Get-Content $Stamp -Raw).Trim() -eq $Version)
  if (-not $installed) {
    Write-Host ''
    Write-Host '  Setting up Clipdeck. The first run downloads about 150 MB and takes a few minutes.' -ForegroundColor Yellow
    Write-Host ''
    if (Test-Clipdeck) {
      Say 'Closing the Clipdeck that is already running'
      try { Invoke-WebRequest -UseBasicParsing -Method Post -Headers @{ 'X-Clipdeck' = '1' } -Uri "$Url/api/shutdown" | Out-Null } catch {}
      Start-Sleep -Seconds 2
    }

    Say 'Unpacking Clipdeck'
    $text = [IO.File]::ReadAllText($Self)
    $marker = '::CLIPDECK' + '_PAYLOAD::'
    $at = $text.LastIndexOf($marker)
    if ($at -lt 0) { throw 'This setup file is damaged. Download it again.' }
    $b64 = $text.Substring($at + $marker.Length) -replace '\s', ''
    $zip = Join-Path $env:TEMP "clipdeck-$Version.zip"
    [IO.File]::WriteAllBytes($zip, [Convert]::FromBase64String($b64))
    New-Item -ItemType Directory -Force -Path $App -ErrorAction Stop | Out-Null
    Get-ChildItem -Path $App -Force | Where-Object { $_.Name -ne '.venv' } | Remove-Item -Recurse -Force -ErrorAction Stop
    Expand-Archive -Path $zip -DestinationPath $App -Force -ErrorAction Stop
    Remove-Item $zip -Force -ErrorAction SilentlyContinue

    if (-not (Test-Path $Py)) {
      $usePy = $false
      if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $usePy = $true }
      }
      if ($usePy) {
        Say 'Creating a private Python environment'
        & py -3 -m venv $Venv
      } else {
        $uv = Find-Uv
        if (-not $uv) {
          # Download uv straight into Clipdeck's folder. (Its official installer also edits
          # your PATH, and that step can hang on some PCs.)
          Say 'Downloading uv, which installs Python just for Clipdeck'
          $uvZip = Join-Path $env:TEMP 'clipdeck-uv.zip'
          try {
            Invoke-WebRequest -UseBasicParsing -ErrorAction Stop -OutFile $uvZip -Uri 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip'
            Expand-Archive -Path $uvZip -DestinationPath (Join-Path $Root 'uv') -Force -ErrorAction Stop
          } catch { throw "Couldn't download uv: $($_.Exception.Message)" }
          finally { Remove-Item $uvZip -Force -ErrorAction SilentlyContinue }
          $uv = Find-Uv
        }
        if (-not $uv) { throw 'Could not install uv. Check your internet connection and run Clipdeck.cmd again.' }
        Say 'Installing Python for Clipdeck'
        if (Test-Path $Venv) { Remove-Item $Venv -Recurse -Force -ErrorAction SilentlyContinue }
        & $uv venv --seed --python 3.12 $Venv
      }
      if (-not (Test-Path $Py)) { throw 'Setting up Python failed. Run Clipdeck.cmd again; if it keeps failing, install Python 3.12 from python.org and retry.' }
    }

    Say 'Installing Clipdeck parts: web server, yt-dlp and ffmpeg'
    & $Py -m pip install --disable-pip-version-check --no-warn-script-location --quiet --upgrade -r (Join-Path $App 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Installing Clipdeck parts failed. Check your internet connection and run Clipdeck.cmd again.' }

    if (-not (Test-Path $Pyw)) { $Pyw = $Py }  # no windowless Python: fall back to the regular one
    Say 'Adding Clipdeck to your desktop and Start menu'
    Copy-Item -Path $Self -Destination (Join-Path $Root 'Clipdeck setup.cmd') -Force -ErrorAction SilentlyContinue
    try {
      $shell = New-Object -ComObject WScript.Shell -ErrorAction Stop
      foreach ($dir in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
        if (-not $dir) { continue }
        $lnk = $shell.CreateShortcut((Join-Path $dir 'Clipdeck.lnk'))
        $lnk.TargetPath = $Pyw
        $lnk.Arguments = '"' + (Join-Path $App 'server.py') + '"'
        $lnk.WorkingDirectory = $App
        $lnk.IconLocation = (Join-Path $App 'clipdeck.ico') + ',0'
        $lnk.Description = 'Make ranking Shorts'
        $lnk.Save()
      }
    } catch {
      Write-Host "  Couldn't add the shortcuts ($($_.Exception.Message)). Open Clipdeck by running this file again." -ForegroundColor Yellow
    }
    Set-Content -Path $Stamp -Value $Version -Encoding ASCII
    Write-Host ''
    Say 'Clipdeck is installed. Next time, open it from the Clipdeck icon on your desktop.'
  }

  if (-not (Test-Path $Pyw)) { $Pyw = $Py }
  Say 'Opening Clipdeck in your browser'
  Start-Process -FilePath $Pyw -ArgumentList ('"' + (Join-Path $App 'server.py') + '"') -WorkingDirectory $App
  Start-Sleep -Seconds 3
  exit 0
} catch {
  Write-Host ''
  Write-Host "  Clipdeck setup stopped: $($_.Exception.Message)" -ForegroundColor Red
  Write-Host ''
  Read-Host '  Press Enter to close'
  exit 1
}
#PS_END
