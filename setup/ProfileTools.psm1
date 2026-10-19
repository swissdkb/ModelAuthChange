function Get-AuthProfileContent {
    param([AllowEmptyString()][string]$Original, [string]$LoaderPath)
    $taskStart = '# >>> AuthSwitch managed >>>'
    $taskEnd = '# <<< AuthSwitch managed <<<'
    $taskPattern = '(?ms)^' + [regex]::Escape($taskStart) + '\r?\n.*?^' + [regex]::Escape($taskEnd) + '(?:\r?\n)?'
    $taskStartCount = [regex]::Matches($Original, '(?m)^' + [regex]::Escape($taskStart) + '\r?$').Count
    $taskEndCount = [regex]::Matches($Original, '(?m)^' + [regex]::Escape($taskEnd) + '\r?$').Count
    $taskMatches = [regex]::Matches($Original, $taskPattern)
    if ($taskStartCount -ne $taskEndCount -or $taskMatches.Count -ne $taskStartCount) {
        throw 'Incomplete AuthSwitch profile markers. No profile was changed.'
    }
    $taskQuoted = $LoaderPath.Replace("'", "''")
    $taskBlock = $taskStart + "`r`n" + "if (Test-Path -LiteralPath '$taskQuoted') { . '$taskQuoted' -Quiet }" + "`r`n" + $taskEnd + "`r`n"
    if ($taskMatches.Count -gt 0) {
        if ($taskMatches.Count -ne 1) { throw 'Multiple AuthSwitch profile blocks found. No profile was changed.' }
        $taskEvaluator = [System.Text.RegularExpressions.MatchEvaluator]{ param($match) $taskBlock }.GetNewClosure()
        return [regex]::Replace($Original, $taskPattern, $taskEvaluator)
    }
    $taskSeparator = if ($Original.Length -gt 0 -and -not $Original.EndsWith("`n")) { "`r`n" } else { '' }
    return $Original + $taskSeparator + $taskBlock
}

function Get-AuthUserPath {
    param([AllowEmptyString()][string]$Original, [string]$BinPath)
    foreach ($taskEntry in ($Original -split ';')) {
        $taskExpanded = [Environment]::ExpandEnvironmentVariables($taskEntry.Trim().Trim('"')).TrimEnd('\')
        if ($taskExpanded -ieq $BinPath.TrimEnd('\')) { return $Original }
    }
    if ([string]::IsNullOrWhiteSpace($Original)) { return $BinPath }
    $taskSeparator = if ($Original.EndsWith(';')) { '' } else { ';' }
    return $Original + $taskSeparator + $BinPath
}

Export-ModuleMember -Function Get-AuthProfileContent, Get-AuthUserPath
