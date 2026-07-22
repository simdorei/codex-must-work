$ErrorActionPreference = 'Stop'

$BootstrapPrefix = 'cmw-installer-bootstrap.'
$Bootstrap = $null
$ExitCode = 1
$FailureMessage = $null
$CleanupMessage = $null
$PreviousCodexHome = $env:CODEX_HOME
$PreviousPluginData = $env:PLUGIN_DATA
$PreviousPythonPath = $env:PYTHONPATH

try {
    $Architecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
    if ($Architecture -ne 'X64') {
        throw "unsupported installer target: Windows $Architecture"
    }

    $SourceRoot = [IO.Path]::GetFullPath($PSScriptRoot)
    $Launcher = Join-Path $SourceRoot 'runtime\launch-python.ps1'
    $Installer = Join-Path $SourceRoot 'scripts\install_plugin.py'
    if (-not (Test-Path -LiteralPath $Launcher -PathType Leaf)) {
        throw "portable runtime launcher is missing: $Launcher"
    }
    if (-not (Test-Path -LiteralPath $Installer -PathType Leaf)) {
        throw "installer script is missing: $Installer"
    }

    $PowerShellExecutable = [IO.Path]::GetFullPath((Get-Process -Id $PID).Path)
    if (-not (Test-Path -LiteralPath $PowerShellExecutable -PathType Leaf)) {
        throw "current PowerShell executable is missing: $PowerShellExecutable"
    }

    $DefaultHome = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex'
    $CodexHomeInput = if ([string]::IsNullOrWhiteSpace($env:CODEX_HOME)) {
        $DefaultHome
    }
    else {
        $env:CODEX_HOME
    }
    $CodexHome = [IO.Path]::GetFullPath($CodexHomeInput)
    $TempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $Bootstrap = Join-Path $TempRoot ($BootstrapPrefix + [guid]::NewGuid().ToString('N'))
    $Bootstrap = (New-Item -ItemType Directory -Path $Bootstrap -ErrorAction Stop).FullName

    $env:CODEX_HOME = $CodexHome
    $env:PLUGIN_DATA = $Bootstrap
    $env:PYTHONPATH = $SourceRoot
    & $PowerShellExecutable -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File $Launcher $Installer $CodexHome $SourceRoot
    $ExitCode = $LASTEXITCODE
}
catch {
    $FailureMessage = $_.Exception.Message
    $ExitCode = 1
}
finally {
    if ($null -eq $PreviousCodexHome) {
        Remove-Item Env:CODEX_HOME -ErrorAction SilentlyContinue
    }
    else {
        $env:CODEX_HOME = $PreviousCodexHome
    }
    if ($null -eq $PreviousPluginData) {
        Remove-Item Env:PLUGIN_DATA -ErrorAction SilentlyContinue
    }
    else {
        $env:PLUGIN_DATA = $PreviousPluginData
    }
    if ($null -eq $PreviousPythonPath) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONPATH = $PreviousPythonPath
    }

    if ($null -ne $Bootstrap) {
        try {
            $ResolvedBootstrap = [IO.Path]::GetFullPath($Bootstrap)
            $ResolvedParent = [IO.Path]::GetDirectoryName($ResolvedBootstrap)
            $ResolvedName = [IO.Path]::GetFileName($ResolvedBootstrap)
            $ExpectedPrefix = $TempRoot.TrimEnd(
                [IO.Path]::DirectorySeparatorChar,
                [IO.Path]::AltDirectorySeparatorChar
            )
            if (-not $ResolvedParent.Equals($ExpectedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw "bootstrap is not a direct child of the temporary root: $ResolvedBootstrap"
            }
            if (-not $ResolvedName.StartsWith($BootstrapPrefix, [StringComparison]::Ordinal) -or
                $ResolvedName.Length -le $BootstrapPrefix.Length) {
                throw "bootstrap name does not have the required prefix: $ResolvedBootstrap"
            }
            if (Test-Path -LiteralPath $ResolvedBootstrap) {
                $BootstrapItem = Get-Item -LiteralPath $ResolvedBootstrap -Force
                if (-not $BootstrapItem.PSIsContainer) {
                    throw "bootstrap path was replaced with a non-directory: $ResolvedBootstrap"
                }
                if (($BootstrapItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                    throw "bootstrap path was replaced with a reparse point: $ResolvedBootstrap"
                }
                Remove-Item -LiteralPath $ResolvedBootstrap -Recurse -Force -ErrorAction Stop
                if (Test-Path -LiteralPath $ResolvedBootstrap) {
                    throw "bootstrap directory remains after cleanup: $ResolvedBootstrap"
                }
            }
        }
        catch {
            $CleanupMessage = $_.Exception.Message
            $ExitCode = 70
        }
    }
}

if ($null -ne $FailureMessage) {
    [Console]::Error.WriteLine("installer entrypoint failed: $FailureMessage")
}
if ($null -ne $CleanupMessage) {
    [Console]::Error.WriteLine("installer bootstrap cleanup failed: $CleanupMessage")
}
exit $ExitCode
