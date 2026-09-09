param(
    [switch]$ValidateInputsOnly,
    [switch]$Build,
    [switch]$CoreStable,
    [string]$ReuseWorkFrom = ''
)
# 默认仅预检。主确认核心稳定后显式构建一次；不自动测试、运行EXE或安装依赖。
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($ValidateInputsOnly -and $Build) { throw '-ValidateInputsOnly and -Build are mutually exclusive.' }
if ($CoreStable -and -not $Build) { throw '-CoreStable requires -Build.' }
if ($Build -and -not $CoreStable) { throw 'Build requires explicit -CoreStable confirmation.' }
if ($ReuseWorkFrom -and (-not $Build -or $ReuseWorkFrom -notmatch '^\d{8}-\d{6}-[0-9a-f]{8}$')) {
    throw '-ReuseWorkFrom requires -Build and an existing packaging build ID, not an arbitrary path.'
}

$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).ProviderPath
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Project .venv Python is missing.' }
$id = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0,8)
$workRoot = Join-Path $root '.local\packaging'
$work = Join-Path $workRoot $id
New-Item -ItemType Directory -Path $work -Force | Out-Null
$intermediate = $work
if ($ReuseWorkFrom) {
    $intermediate = (Resolve-Path -LiteralPath (Join-Path $workRoot $ReuseWorkFrom)).ProviderPath
    $resolvedWorkRoot = (Resolve-Path -LiteralPath $workRoot).ProviderPath.TrimEnd('\')
    if (-not $intermediate.StartsWith($resolvedWorkRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Resolved reusable work directory is outside .local/packaging.'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $intermediate 'pyinstaller-work\opinion_capture\Analysis-00.toc') -PathType Leaf)) {
        throw 'The selected build has no Analysis intermediate to reuse.'
    }
}
$saved = @{}
foreach ($name in @('PYTHONPATH','PYTHONDONTWRITEBYTECODE','PYTHONIOENCODING','PYINSTALLER_CONFIG_DIR')) {
    $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$env:PYTHONPATH = (Join-Path $root 'src') + ';' + (Join-Path $root 'packaging')
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYINSTALLER_CONFIG_DIR = Join-Path $intermediate 'pyinstaller-cache'
$support = Join-Path $root 'packaging\build_support.py'

function Invoke-CheckedPython {
    param([string[]]$Arguments)
    & $python -B @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Python step failed ($LASTEXITCODE): $($Arguments -join ' ')" }
}

try {
    Invoke-CheckedPython -Arguments @($support,'preflight','--output',(Join-Path $work 'preflight.json'))
    Write-Output "Preflight evidence: $work"
    if (-not $Build) { return }

    # 全新dist子目录：即使重复执行也不覆盖旧包，无递归清理行为。
    $dist = Join-Path $root 'dist'
    New-Item -ItemType Directory -Path $dist -Force | Out-Null
    $resolvedDist = (Resolve-Path -LiteralPath $dist).ProviderPath.TrimEnd('\')
    if ($resolvedDist -ne [IO.Path]::GetFullPath($dist).TrimEnd('\')) { throw 'dist is redirected; stopped.' }
    $buildDist = Join-Path $dist ('candidate-' + $id)
    if (Test-Path -LiteralPath $buildDist) { throw 'Output already exists; stopped without overwriting.' }
    New-Item -ItemType Directory -Path $buildDist | Out-Null
    $resolvedBuild = (Resolve-Path -LiteralPath $buildDist).ProviderPath
    if (-not $resolvedBuild.StartsWith($resolvedDist + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Resolved output is outside dist.'
    }
    $before = Join-Path $work 'source-before.json'
    Invoke-CheckedPython -Arguments @($support,'fingerprint','--output',$before)
    [ordered]@{ reusedBuildId = $ReuseWorkFrom; intermediate = $intermediate; output = $buildDist } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $work 'build-invocation.json') -Encoding UTF8
    & $python -B -m PyInstaller --distpath $buildDist --workpath (Join-Path $intermediate 'pyinstaller-work') (Join-Path $root 'packaging\opinion_capture.spec') *> (Join-Path $work 'build.log')
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed; inspect $work\build.log" }
    $bundle = Join-Path $buildDist 'OpinionCapture-Portable'
    Invoke-CheckedPython -Arguments @($support,'audit','--target',$bundle,'--output',(Join-Path $work 'payload-audit.json'))

    # 最新交付约束：仅结构/哈希核对，不自动运行EXE、portable-check或烟测。
    $finishArgs = @($support,'finish','--before',$before,'--output',(Join-Path $work 'source-after.json'))
    & $python -B @finishArgs
    $fingerprintExit = $LASTEXITCODE
    $verdict = [ordered]@{
        bundle = $bundle; evidence = $work
        reusedBuildId = $ReuseWorkFrom
        structureAndHashesChecked = $true; sourceStable = ($fingerprintExit -eq 0)
        frozenRuntimeVerified = $false; runtimeCheckPolicy = 'Not executed per user instruction'
        finalDelivery = $false
        pending = @('Main integration approval','Frozen runtime remains unverified; no automatic tests')
    }
    $verdict | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $work 'build-result.json') -Encoding UTF8
    if ($fingerprintExit -ne 0) { throw "Source changed during build. Keep evidence and report to main; no automatic rebuild: $work" }
    Write-Output "Build, structure and hash checks passed. Bundle: $bundle"
    Write-Output "Frozen runtime NOT verified; not a final delivery claim. Evidence: $work"
} finally {
    foreach ($name in $saved.Keys) { [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process') }
}
