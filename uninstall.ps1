param(
    [switch] $PurgeData
)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$BootstrapPrefix = 'cmw-uninstaller-bootstrap.'
$Bootstrap = $null
$ExitCode = 1
$PreviousCodexHome = $env:CODEX_HOME
$PreviousPluginData = $env:PLUGIN_DATA
$PreviousPythonPath = $env:PYTHONPATH

try {
    $SourceRoot = [IO.Path]::GetFullPath($PSScriptRoot)
    $Launcher = Join-Path $SourceRoot 'runtime\launch-python.ps1'
    $Uninstaller = Join-Path $SourceRoot 'scripts\uninstall_plugin.py'
    if (-not (Test-Path -LiteralPath $Launcher -PathType Leaf)) {
        throw "portable runtime launcher is missing: $Launcher"
    }
    if (-not (Test-Path -LiteralPath $Uninstaller -PathType Leaf)) {
        throw "uninstaller script is missing: $Uninstaller"
    }
    $DefaultHome = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex'
    $HomeInput = if ([string]::IsNullOrWhiteSpace($env:CODEX_HOME)) {
        $DefaultHome
    }
    else {
        $env:CODEX_HOME
    }
    $CodexHome = [IO.Path]::GetFullPath($HomeInput)
    $TempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $Bootstrap = Join-Path $TempRoot ($BootstrapPrefix + [guid]::NewGuid().ToString('N'))
    $env:CODEX_HOME = $CodexHome
    $env:PLUGIN_DATA = $Bootstrap
    $env:PYTHONPATH = $SourceRoot
    $Arguments = @($Uninstaller, $CodexHome, $SourceRoot)
    if ($PurgeData) {
        $Arguments += '--purge-data'
    }
    & $Launcher @Arguments
    $ExitCode = $LASTEXITCODE
}
catch {
    [Console]::Error.WriteLine("uninstaller entrypoint failed: $($_.Exception.Message)")
    $ExitCode = 1
}
finally {
    if ($null -eq $PreviousCodexHome) {
        Remove-Item -LiteralPath Env:CODEX_HOME -ErrorAction SilentlyContinue
    }
    else {
        $env:CODEX_HOME = $PreviousCodexHome
    }
    if ($null -eq $PreviousPluginData) {
        Remove-Item -LiteralPath Env:PLUGIN_DATA -ErrorAction SilentlyContinue
    }
    else {
        $env:PLUGIN_DATA = $PreviousPluginData
    }
    if ($null -eq $PreviousPythonPath) {
        Remove-Item -LiteralPath Env:PYTHONPATH -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONPATH = $PreviousPythonPath
    }
    if ($null -ne $Bootstrap) {
        try {
            $ResolvedBootstrap = [IO.Path]::GetFullPath($Bootstrap)
            $ExpectedParent = [IO.Path]::GetDirectoryName($ResolvedBootstrap)
            $ExpectedName = [IO.Path]::GetFileName($ResolvedBootstrap)
            if (-not $ExpectedParent.Equals($TempRoot, [StringComparison]::OrdinalIgnoreCase)) {
                throw "bootstrap is not a direct child of the temporary root: $ResolvedBootstrap"
            }
            if (-not $ExpectedName.StartsWith($BootstrapPrefix, [StringComparison]::Ordinal) -or
                $ExpectedName.Length -le $BootstrapPrefix.Length) {
                throw "bootstrap name does not have the required prefix: $ResolvedBootstrap"
            }
            if (Test-Path -LiteralPath $ResolvedBootstrap) {
                $Item = Get-Item -LiteralPath $ResolvedBootstrap -Force
                if (-not $Item.PSIsContainer -or
                    (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
                    throw "bootstrap path was replaced with an unsafe object: $ResolvedBootstrap"
                }
                Remove-Item -LiteralPath $ResolvedBootstrap -Recurse -Force -ErrorAction Stop
            }
        }
        catch {
            [Console]::Error.WriteLine(
                "uninstaller bootstrap cleanup failed: $($_.Exception.Message)"
            )
            $ExitCode = 70
        }
    }
}

exit $ExitCode
