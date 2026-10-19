# Install for the current Windows user only. No administrator privileges needed.
[CmdletBinding()]
param([switch]$Preview)
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'setup\ProfileTools.psm1') -Force
$taskInstallBin = Join-Path $PSScriptRoot 'bin'
$taskInstallLoader = Join-Path $PSScriptRoot 'enable-auth.ps1'
$taskInstallPython = Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1
& $taskInstallPython.Source -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required.' }
$taskInstallOther = Get-Command auth -ErrorAction SilentlyContinue
$taskInstallOwnCmd = Join-Path $taskInstallBin 'auth.cmd'
if ($taskInstallOther -and -not (
    ($taskInstallOther.CommandType -eq 'Application' -and $taskInstallOther.Source -eq $taskInstallOwnCmd) -or
    ($taskInstallOther.CommandType -eq 'Function' -and $taskInstallOther.Definition -like '*-m authswitch*')
)) { throw 'An unrelated auth command already exists. It was not replaced.' }

$taskInstallOldPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$taskInstallNewPath = Get-AuthUserPath -Original $taskInstallOldPath -BinPath $taskInstallBin
$taskInstallDocuments = [Environment]::GetFolderPath('MyDocuments')
if ([string]::IsNullOrWhiteSpace($taskInstallDocuments)) { throw 'Cannot locate the Documents directory.' }
$taskInstallProfiles = @(
    (Join-Path $taskInstallDocuments 'PowerShell\profile.ps1'),
    (Join-Path $taskInstallDocuments 'WindowsPowerShell\profile.ps1')
)
$taskInstallPlans = @()
foreach ($taskInstallProfile in $taskInstallProfiles) {
    $taskInstallExists = Test-Path -LiteralPath $taskInstallProfile
    $taskInstallOriginal = if ($taskInstallExists) { [IO.File]::ReadAllText($taskInstallProfile) } else { '' }
    $taskInstallUpdated = Get-AuthProfileContent -Original $taskInstallOriginal -LoaderPath $taskInstallLoader
    $taskInstallPlans += [pscustomobject]@{ Path=$taskInstallProfile; Original=$taskInstallOriginal; Updated=$taskInstallUpdated; Exists=$taskInstallExists }
}
Write-Host ('User PATH entry: ' + $taskInstallBin)
foreach ($taskInstallPlan in $taskInstallPlans) { Write-Host ('PowerShell startup: ' + $taskInstallPlan.Path) }
if ($Preview) {
    Write-Host 'Preview only. No environment variables or profile files were changed.'
    return
}

# Make all backups before changing profiles or user PATH. Existing text is retained.
$taskInstallStamp = [DateTime]::UtcNow.ToString('yyyyMMddHHmmssfff') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
foreach ($taskInstallPlan in $taskInstallPlans) {
    if ($taskInstallPlan.Original -ceq $taskInstallPlan.Updated) { continue }
    if ($taskInstallPlan.Exists) {
        $taskInstallBackup = $taskInstallPlan.Path + '.authswitch-' + $taskInstallStamp + '.bak'
        Copy-Item -LiteralPath $taskInstallPlan.Path -Destination $taskInstallBackup
        Write-Host ('Profile backup: ' + $taskInstallBackup)
    }
}
foreach ($taskInstallPlan in $taskInstallPlans) {
    if ($taskInstallPlan.Original -ceq $taskInstallPlan.Updated) { continue }
    $taskInstallCurrent = if (Test-Path -LiteralPath $taskInstallPlan.Path) { [IO.File]::ReadAllText($taskInstallPlan.Path) } else { '' }
    if ($taskInstallCurrent -cne $taskInstallPlan.Original) { throw 'Profile changed during installation. Re-run installation.' }
    [IO.Directory]::CreateDirectory((Split-Path -Parent $taskInstallPlan.Path)) | Out-Null
    # BOM keeps non-ASCII pre-existing profile content readable in Windows PowerShell 5.1.
    [IO.File]::WriteAllText($taskInstallPlan.Path, $taskInstallPlan.Updated, [Text.UTF8Encoding]::new($true))
}
if ($taskInstallNewPath -cne $taskInstallOldPath) {
    [Environment]::SetEnvironmentVariable('Path', $taskInstallNewPath, 'User')
}
$env:Path = Get-AuthUserPath -Original $env:Path -BinPath $taskInstallBin
. $taskInstallLoader -Quiet
Write-Host 'Installed. Run auth list from any directory. New PowerShell windows load auth and Tab completion automatically.'
Write-Host 'Restart your terminal application if a new cmd window still inherits an old PATH. Keep this project at its current location.'
