# Deliberately no param block: Windows PowerShell 5.1's advanced parameter
# binder can consume unknown --flags before ValueFromRemainingArguments sees
# them. Parse only launcher options; forward every engine argument verbatim.
$ErrorActionPreference = 'Stop'
$rawArguments = @($args)
if ($rawArguments.Count -eq 0) { throw 'Usage: workflow.ps1 ACTION [-Config FILE] [workflow options]; run setup first.' }
$Action = [string]$rawArguments[0]
$Config = Join-Path $PSScriptRoot 'config/local.json'
$RuntimeRoot = Join-Path $env:LOCALAPPDATA 'NguyenHuuThinhWorkflow/runtime-1.1.0'
$PythonPath = ''
$forward = New-Object 'System.Collections.Generic.List[string]'
for ($index = 1; $index -lt $rawArguments.Count; $index++) {
    $token = [string]$rawArguments[$index]
    $launcherOption = $token.ToLowerInvariant()
    if ($launcherOption -in @('-config', '--config', '-runtimeroot', '--runtime-root', '-pythonpath', '--python-path')) {
        if ($index + 1 -ge $rawArguments.Count) { throw "Missing value for $token" }
        $index++
        $value = [string]$rawArguments[$index]
        switch ($launcherOption) {
            { $_ -in '-config', '--config' } { $Config = $value }
            { $_ -in '-runtimeroot', '--runtime-root' } { $RuntimeRoot = $value }
            { $_ -in '-pythonpath', '--python-path' } { $PythonPath = $value }
        }
    }
    else { $forward.Add($token) }
}
$engineArguments = $forward.ToArray()
if ($Action -eq 'setup') {
    if ($engineArguments.Count -ne 0) { throw ('Unsupported setup argument: ' + $engineArguments[0]) }
    & (Join-Path $PSScriptRoot 'setup.ps1') -RuntimeRoot $RuntimeRoot -Config $Config -PythonPath $PythonPath
    exit $LASTEXITCODE
}
if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) { throw 'Configuration missing; run workflow.ps1 setup first.' }
$settings = Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json
$controller = $settings.env.controller_python
if (-not (Test-Path -LiteralPath $controller -PathType Leaf)) { throw 'Configured controller Python missing; run setup with a new external runtime root.' }
& $controller -B -X utf8 (Join-Path $PSScriptRoot 'tools/season.py') $Action --config $Config @engineArguments
exit $LASTEXITCODE
