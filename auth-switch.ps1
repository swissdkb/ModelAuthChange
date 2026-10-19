$ErrorActionPreference = 'Stop'
$taskPython = Get-Command python -ErrorAction SilentlyContinue
if (-not $taskPython) {
    Write-Error 'Python 3.11+ is required. Install Python and run this script again.'
    exit 1
}
$taskOriginalPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = $PSScriptRoot + [IO.Path]::PathSeparator + $env:PYTHONPATH
    & $taskPython.Source -m authswitch @args
    $taskResult = $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $taskOriginalPath
}
exit $taskResult
