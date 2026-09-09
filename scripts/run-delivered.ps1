param(
    [ValidateSet('gui','preview','run','watch','history','devices')]
    [string]$Action = 'gui'
)
# 本机交付入口：复用本地计划和已登录专属会话；交付包不包含账号数据。
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$pointer = Join-Path $root '.local\delivery\latest.json'
if (-not (Test-Path -LiteralPath $pointer -PathType Leaf)) { throw '尚未登记交付包，请先使用 scripts/run.ps1。' }
$release = Get-Content -LiteralPath $pointer -Raw -Encoding UTF8 | ConvertFrom-Json
$bundle = [IO.Path]::GetFullPath($release.bundle)
$dist = [IO.Path]::GetFullPath((Join-Path $root 'dist')).TrimEnd('\')
if (-not $bundle.StartsWith($dist + '\', [StringComparison]::OrdinalIgnoreCase)) { throw '交付包路径不在项目dist内。' }
$name = if ($Action -eq 'gui') { 'OpinionCapture.exe' } else { 'OpinionCapture-CLI.exe' }
$exe = Join-Path $bundle $name
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw '交付文件缺失，请按交付说明恢复整个便携目录。' }
$config = Join-Path $root '.local\plan.json'
if ($Action -eq 'gui') {
    # 此脚本由用户主动启动GUI；不自动开启采集或定时任务。
    Start-Process -FilePath $exe -ArgumentList @('gui','--config',('"' + $config + '"')) -WorkingDirectory $bundle
} else {
    & $exe $Action --config $config
    exit $LASTEXITCODE
}
