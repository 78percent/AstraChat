param(
    [ValidateSet('snowluma', 'napcat')]
    [string]$Protocol = 'snowluma'
)
$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    $projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $projectPython)) {
        throw '请先按 LOCAL-SETUP.md 创建项目 Python 3.12 环境。'
    }
    if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'runtime')) {
        & $projectPython -X utf8 scripts/check_runtime.py
        if ($LASTEXITCODE -ne 0) { throw '已有部署检查失败；保留数据，未启动容器。' }
    } else {
        throw '缺少运行资料，请先按 LOCAL-SETUP.md 完成首次准备；此脚本不自动导入人设。'
    }
    docker compose config --quiet
    if ($LASTEXITCODE -ne 0) { throw 'Compose 配置校验失败。' }
    if ($Protocol -eq 'snowluma') {
        & $projectPython -X utf8 scripts/check_snowluma.py
        if ($LASTEXITCODE -ne 0) { throw 'SnowLuma 配置检查失败；未切换协议端。' }
        docker compose stop napcat
        if ($LASTEXITCODE -ne 0) { throw 'NapCat 停止失败，不能启动同号另一端。' }
        $runningOld = docker compose ps --status running --quiet napcat
        if ($LASTEXITCODE -ne 0 -or $runningOld) { throw '尚未确认 NapCat 停止。' }
        docker compose up -d --no-deps astrbot
        if ($LASTEXITCODE -ne 0) { throw 'AstrBot 启动失败。' }
        docker compose -f compose.snowluma.yaml up -d
        if ($LASTEXITCODE -ne 0) { throw 'SnowLuma 启动失败；NapCat 保持停止，数据保留。' }
        Write-Host 'AstrBot：http://127.0.0.1:6185；SnowLuma：http://127.0.0.1:5099；QQ 登录：http://127.0.0.1:6081/vnc.html'
        Write-Host 'SnowLuma 登录资料见 runtime/snowluma/access.local.json。'
    } else {
        if (Test-Path -LiteralPath 'runtime/snowluma.env.local') {
            docker compose -f compose.snowluma.yaml stop snowluma
            if ($LASTEXITCODE -ne 0) { throw 'SnowLuma 停止失败，不能启动同号另一端。' }
            $runningOld = docker compose -f compose.snowluma.yaml ps --status running --quiet snowluma
            if ($LASTEXITCODE -ne 0 -or $runningOld) { throw '尚未确认 SnowLuma 停止。' }
        }
        docker compose up -d --no-deps astrbot napcat
        if ($LASTEXITCODE -ne 0) { throw 'NapCat 回退启动未完成，数据保留。' }
        Write-Host 'AstrBot：http://127.0.0.1:6185；NapCat：http://127.0.0.1:6099'
        Write-Host 'NapCat 登录资料见 runtime/access.local.json。'
    }
    Write-Host '容器启动不等于 QQ 登录或真实消息已送达；同账号不要同时登录电脑版 QQ。'
} finally {
    Pop-Location
}
