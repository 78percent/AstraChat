$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    docker compose stop
    if ($LASTEXITCODE -ne 0) { throw '容器停止失败，请检查 Docker 状态。' }
    if (Test-Path -LiteralPath 'runtime/snowluma.env.local') {
        docker compose -f compose.snowluma.yaml stop snowluma
        if ($LASTEXITCODE -ne 0) { throw 'SnowLuma 停止失败，请检查 Docker 状态。' }
    }
    Write-Host '本项目容器已停止，runtime 数据保留。这不是云服务器停费操作。'
} finally {
    Pop-Location
}
