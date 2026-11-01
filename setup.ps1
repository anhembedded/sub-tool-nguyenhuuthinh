param(
    [string]$RuntimeRoot = (Join-Path $env:LOCALAPPDATA 'NguyenHuuThinhWorkflow/runtime-1.1.0'),
    [string]$Config = (Join-Path $PSScriptRoot 'config/local.json'),
    [string]$PythonPath = ''
)
$ErrorActionPreference = 'Stop'
if (-not $PythonPath) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $PythonPath = (& py -3.12 -c 'import sys; print(sys.executable)' | Select-Object -Last 1)
    }
    elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $PythonPath = (Get-Command python).Source
    }
}
if (-not $PythonPath -or -not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw 'Install CPython 3.12 x64 from https://www.python.org/downloads/windows/ and rerun setup.ps1 -PythonPath FULL_PATH_TO_python.exe. A compatible NVIDIA driver and CapCut desktop are required.'
}
& $PythonPath -B -X utf8 (Join-Path $PSScriptRoot 'tools/runtime_setup.py') --runtime-root $RuntimeRoot --config-output $Config
exit $LASTEXITCODE
