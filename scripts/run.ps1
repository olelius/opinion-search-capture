param(
    [ValidateSet('gui','preview','run','watch','history','init-config','devices')]
    [string]$Action = 'gui',
    [string]$Config = '',
    [string]$ResumeBatch = ''
)
# 使用仓库独立虚拟环境；启动器不自动安装依赖或运行采集。
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw '请先建立项目 .venv 环境。' }
$env:PYTHONPATH = Join-Path $root 'src'
$env:PYTHONIOENCODING = 'utf-8'
if (-not $Config) { $Config = Join-Path $root '.local\plan.json' }
Push-Location -LiteralPath $root
try {
    $arguments = @('-m','opinion_capture',$Action,'--config',$Config)
    if ($ResumeBatch) { $arguments += @('--resume-batch',$ResumeBatch) }
    & $python @arguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
