# Internal plugin launcher. No profile, package installation, or network probes.
# Match the bootstrap's UTF-8 pipes, including in Windows PowerShell 5.1.
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) } catch { }
$finderArguments = @($args)
# Windows PowerShell 5.1 drops empty arguments and embedded quotes when invoking
# native programs. Transport the argument array as data instead of re-quoting it.
$finderArgumentsJson = ConvertTo-Json -InputObject $finderArguments -Compress
$finderArgumentsEncoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($finderArgumentsJson))
$finderEntryPoint = Join-Path $PSScriptRoot 'skill_finder.py'
if (-not (Test-Path -LiteralPath $finderEntryPoint -PathType Leaf)) {
    [Console]::Error.WriteLine('Universal Skill Finder cannot start: the plugin is incomplete. Reinstall the plugin. No searches were run.')
    exit 4
}

$finderCandidates = @(
    @{ Name = 'py'; Prefix = @('-3') },
    @{ Name = 'python3'; Prefix = @() },
    @{ Name = 'python'; Prefix = @() },
    @{ Name = 'py'; Prefix = @('-3.14') },
    @{ Name = 'py'; Prefix = @('-3.13') },
    @{ Name = 'py'; Prefix = @('-3.12') },
    @{ Name = 'py'; Prefix = @('-3.11') },
    @{ Name = 'py'; Prefix = @('-3.10') }
)
foreach ($finderCandidate in $finderCandidates) {
    $finderCommand = Get-Command -Name $finderCandidate.Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $finderCommand) { continue }
    $finderPrefix = $finderCandidate.Prefix
    try {
        & $finderCommand.Source @finderPrefix $finderEntryPoint --check *> $null
        if ($LASTEXITCODE -ne 0) { continue }
    } catch {
        continue
    }
    try {
        & $finderCommand.Source @finderPrefix $finderEntryPoint --launcher-args $finderArgumentsEncoded
        exit $LASTEXITCODE
    } catch {
        [Console]::Error.WriteLine('Universal Skill Finder cannot start: the Python runtime could not be launched. Check the runtime and retry. No searches were run.')
        exit 4
    }
}

[Console]::Error.WriteLine('Universal Skill Finder cannot start: Python 3.10 or later with SSL support is required, but no compatible interpreter was found on PATH. Make a supported Python runtime available, restart the coding assistant, and retry. No searches were run.')
exit 4
