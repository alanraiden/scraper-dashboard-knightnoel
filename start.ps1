# start.ps1 -- One-click start for the Knight Novel Scraper Dashboard
# Run from PowerShell in the project root:
#   Set-ExecutionPolicy -Scope Process Bypass ; .\start.ps1
# ----------------------------------------------------------------

Set-Location $PSScriptRoot

Write-Host ""
Write-Host "  Knight Novel Scraper Dashboard" -ForegroundColor Cyan
Write-Host ""

# -- Check Python -----------------------------------------------
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "  [ERROR] python not found. Install Python 3 from python.org" -ForegroundColor Red
    exit 1
}

# -- Check Node -------------------------------------------------
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Host "  [ERROR] node not found. Install Node.js from nodejs.org" -ForegroundColor Red
    exit 1
}

# -- Install node deps if missing -------------------------------
if (-not (Test-Path "node_modules")) {
    Write-Host "  Installing node dependencies..." -ForegroundColor Yellow
    & cmd /c "npm install"
}

# -- Install Python deps if missing ----------------------------
$pyCheck = & python -c "import flask, flask_cors, requests, bs4" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Installing Python dependencies..." -ForegroundColor Yellow
    & pip install flask flask-cors requests beautifulsoup4 lxml -q
}

# -- Start Python scraper server (port 7832) -------------------
Write-Host "  [1/2] Starting Python scraper server on port 7832..." -ForegroundColor Green
$pythonProc = Start-Process -FilePath "python" `
    -ArgumentList "scraper-server\scraper_server.py" `
    -PassThru `
    -WindowStyle Normal

Start-Sleep -Seconds 2

# -- Start Vite dashboard (port 5174) --------------------------
Write-Host "  [2/2] Starting Vite dashboard on port 5174..." -ForegroundColor Green
$viteProc = Start-Process -FilePath "cmd" `
    -ArgumentList "/c npm run dev" `
    -PassThru `
    -WindowStyle Normal

Write-Host ""
Write-Host "  Both servers are starting up." -ForegroundColor Green
Write-Host "    Dashboard  ->  http://localhost:5174" -ForegroundColor Cyan
Write-Host "    Scraper    ->  http://localhost:7832" -ForegroundColor Cyan
Write-Host "    KN site    ->  http://localhost:3000  (start separately)" -ForegroundColor DarkCyan
Write-Host ""
Write-Host "  Press ENTER to stop both servers." -ForegroundColor Yellow
Read-Host | Out-Null

# -- Cleanup ---------------------------------------------------
Write-Host "  Stopping servers..." -ForegroundColor Yellow

if ($pythonProc -and -not $pythonProc.HasExited) {
    Stop-Process -Id $pythonProc.Id -Force -ErrorAction SilentlyContinue
}
if ($viteProc -and -not $viteProc.HasExited) {
    Stop-Process -Id $viteProc.Id -Force -ErrorAction SilentlyContinue
}

# Kill any stray processes still holding port 7832
$port7832 = netstat -ano 2>$null | Select-String ":7832\s"
foreach ($line in $port7832) {
    $parts = ($line.ToString().Trim() -split "\s+")
    $procId = $parts[-1]
    if ($procId -match "^\d+$" -and $procId -ne "0") {
        Stop-Process -Id ([int]$procId) -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "  Done. Goodbye!" -ForegroundColor Green
