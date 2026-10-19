# Load once in a PowerShell window: . .\enable-auth.ps1
# Registers a session-only command; does not edit PATH or your PowerShell profile.
param([switch]$Quiet)
$taskAuthManagedLauncher = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'bin\auth.cmd'))
$taskAuthExisting = Get-Command auth -ErrorAction SilentlyContinue
$taskAuthIsLauncher = $taskAuthExisting -and $taskAuthExisting.CommandType -eq 'Application' -and $taskAuthExisting.Source -eq $taskAuthManagedLauncher
if ($taskAuthExisting -and -not $taskAuthIsLauncher -and ($taskAuthExisting.CommandType -ne 'Function' -or $taskAuthExisting.Definition -notlike '*-m authswitch*')) {
    throw 'An auth command already exists. It was not replaced.'
}
$taskAuthPythonCommand = Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1
$taskAuthPython = $taskAuthPythonCommand.Source
$taskAuthRoot = $PSScriptRoot
$taskAuthFunction = {
    $taskAuthPreviousPath = $env:PYTHONPATH
    $taskAuthCode = 1
    try {
        $env:PYTHONPATH = $taskAuthRoot + [IO.Path]::PathSeparator + $taskAuthPreviousPath
        & $taskAuthPython -m authswitch @args
        $taskAuthCode = $LASTEXITCODE
    } finally {
        $env:PYTHONPATH = $taskAuthPreviousPath
    }
    $global:LASTEXITCODE = $taskAuthCode
}.GetNewClosure()
Set-Item -LiteralPath Function:\global:auth -Value $taskAuthFunction
$taskAuthCompleter = {
    param($wordToComplete, $commandAst, $cursorPosition)
    $taskAuthParts = @($commandAst.CommandElements | ForEach-Object { $_.ToString() })
    if ($taskAuthParts.Count -lt 2) { return }
    $taskAuthService = 'codex'
    $taskAuthLast = $taskAuthParts[-1].Trim("'", '"')
    if ($taskAuthLast -in @('codex', 'claude')) {
        $taskAuthService = $taskAuthLast
        $taskAuthParts = @($taskAuthParts[0..($taskAuthParts.Count - 2)])
    }
    # Cursor on the final service argument: complete service names, not accounts.
    $taskAuthCurrentElement = @($commandAst.CommandElements | Where-Object {
        $_.Extent.StartOffset -lt $cursorPosition -and $_.Extent.EndOffset -ge $cursorPosition
    } | Select-Object -Last 1)
    $taskAuthServicePosition = $commandAst.CommandElements.Count -ge 4 -or (
        $commandAst.CommandElements.Count -ge 3 -and $taskAuthParts[1] -in @('list', 'doctor', '--help', '-h'))
    if ($taskAuthCurrentElement.Count -and $taskAuthCurrentElement[0] -eq $commandAst.CommandElements[-1] -and
        $taskAuthServicePosition) {
        foreach ($taskAuthCandidate in @('codex', 'claude')) {
            if ($taskAuthCandidate.StartsWith($wordToComplete, [StringComparison]::OrdinalIgnoreCase)) {
                [System.Management.Automation.CompletionResult]::new($taskAuthCandidate, $taskAuthCandidate, 'ParameterValue', $taskAuthCandidate)
            }
        }
        if ($taskAuthParts[1] -ne 'order' -or $taskAuthLast -in @('codex', 'claude')) { return }
    }
    if ($taskAuthParts[1] -notin @('use', 'usage', 'refresh', 'rename', 'order')) { return }
    if ($taskAuthParts.Count -gt 3 -and $taskAuthParts[1] -ne 'order') { return }
    $taskAuthCompletionPath = $env:PYTHONPATH
    $taskAuthCompletionExit = $global:LASTEXITCODE
    try {
        $env:PYTHONPATH = $taskAuthRoot + [IO.Path]::PathSeparator + $taskAuthCompletionPath
        # Completion must stay local: no quota query, login refresh or auth writes.
        $taskAuthRaw = & $taskAuthPython -m authswitch list --json --cached $taskAuthService 2>$null
        if ($LASTEXITCODE -ne 0) { return }
        $taskAuthAccounts = ($taskAuthRaw -join "`n" | ConvertFrom-Json).accounts
        $taskAuthPrefix = $wordToComplete.Trim("'", '"')
        foreach ($taskAuthAccount in $taskAuthAccounts) {
            $taskAuthName = [string]$taskAuthAccount.name
            if ($taskAuthName.StartsWith($taskAuthPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                $taskAuthQuoted = "'" + $taskAuthName.Replace("'", "''") + "'"
                [System.Management.Automation.CompletionResult]::new(
                    $taskAuthQuoted, $taskAuthName, 'ParameterValue', $taskAuthName)
            }
        }
    } catch {
        # A locked/unavailable vault should simply yield no completions.
    } finally {
        $env:PYTHONPATH = $taskAuthCompletionPath
        $global:LASTEXITCODE = $taskAuthCompletionExit
    }
}.GetNewClosure()
Register-ArgumentCompleter -Native -CommandName auth -ScriptBlock $taskAuthCompleter
if (-not $Quiet) {
    Write-Host 'Ready in all directories of this window: auth | auth list | auth use m (Tab)'
}
