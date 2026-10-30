param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$previewUrl = 'http://127.0.0.1:5178/'
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'console-ui/dist/index.html'))) {
    throw '请先在 console-ui 目录运行 npm run build。'
}
function Test-Preview {
    try { return (Invoke-RestMethod -Uri ($previewUrl + '__preview_health') -TimeoutSec 2).app -eq 'mitsuha-console' }
    catch { return $false }
}
if (-not (Test-Preview)) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort 5178 -ErrorAction SilentlyContinue
    if ($listener) { throw '端口 5178 被其他程序占用；未停止或替换该程序。' }
    $previewPython = Join-Path $projectRoot '.venv/Scripts/pythonw.exe'
    if (-not (Test-Path -LiteralPath $previewPython)) { throw '找不到项目 Python 环境。' }
    $serverScript = Join-Path $PSScriptRoot 'serve_console_preview.py'
    Start-Process -FilePath $previewPython -ArgumentList @('"' + $serverScript + '"') -WorkingDirectory $projectRoot -WindowStyle Hidden | Out-Null
    $ready = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if (Test-Preview) { $ready = $true; break }
        Start-Sleep -Milliseconds 250
    }
    if (-not $ready) { throw '本地控制台预览服务未能启动。' }
}
if (-not $NoBrowser) { Start-Process $previewUrl }
Write-Output $previewUrl
