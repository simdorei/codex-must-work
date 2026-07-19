param(
    [switch] $ForwardStdin
)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$PythonArgs = [string[]] $args
$Version = '3.12.13+20260510'
$ExpectedHash = '24168aff2e7d93784c6a436124c4ebb79b076a4e289bde4902c08333507b71d0'

if ([string]::IsNullOrWhiteSpace($env:PLUGIN_DATA)) {
    throw 'PLUGIN_DATA is required for the Codex Must Work portable runtime'
}

$DataRoot = [IO.Path]::GetFullPath($env:PLUGIN_DATA)
$PluginRoot = Split-Path -Parent $PSScriptRoot
$Archive = Join-Path $PluginRoot "runtime\archives\cpython-$Version-windows-x64.tar.gz"
$Target = Join-Path $DataRoot "portable-python\$Version\windows-x64\python"
$Python = Join-Path $Target 'python.exe'
$LockPath = Join-Path $DataRoot '.portable-python.lock'
$Stage = Join-Path $DataRoot ('.portable-python-stage-' + [guid]::NewGuid().ToString('N'))

New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null
$Lock = $null
$Deadline = [DateTime]::UtcNow.AddSeconds(55)
while ($null -eq $Lock) {
    try {
        $Lock = [IO.File]::Open(
            $LockPath,
            [IO.FileMode]::OpenOrCreate,
            [IO.FileAccess]::ReadWrite,
            [IO.FileShare]::None
        )
    }
    catch [IO.IOException] {
        if ([DateTime]::UtcNow -ge $Deadline) {
            throw 'portable runtime bootstrap lock timed out'
        }
        Start-Sleep -Milliseconds 100
    }
}

try {
    if ((Test-Path -LiteralPath $Target) -and -not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw "portable runtime is incomplete: $Target"
    }
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        if (-not (Test-Path -LiteralPath $Archive -PathType Leaf)) {
            throw "portable runtime archive is missing: $Archive"
        }
        $ActualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Archive).Hash.ToLowerInvariant()
        if ($ActualHash -ne $ExpectedHash) {
            throw "portable runtime archive hash mismatch: $Archive"
        }
        New-Item -ItemType Directory -Path $Stage | Out-Null
        $Tar = Join-Path $env:SystemRoot 'System32\tar.exe'
        & $Tar -xzf $Archive -C $Stage
        if ($LASTEXITCODE -ne 0) {
            throw "portable runtime extraction failed with exit code $LASTEXITCODE"
        }
        $Extracted = Join-Path $Stage 'python'
        if (-not (Test-Path -LiteralPath (Join-Path $Extracted 'python.exe') -PathType Leaf)) {
            throw 'portable runtime archive has an unexpected layout'
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Target) | Out-Null
        Move-Item -LiteralPath $Extracted -Destination $Target
    }
}
finally {
    if (Test-Path -LiteralPath $Stage) {
        $ResolvedStage = [IO.Path]::GetFullPath($Stage)
        $Prefix = $DataRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
        if (-not $ResolvedStage.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "refusing to clean an unsafe staging path: $ResolvedStage"
        }
        Remove-Item -LiteralPath $ResolvedStage -Recurse -Force
    }
    $Lock.Dispose()
}

if ($ForwardStdin) {
    if ($PythonArgs.Count -ne 1 -or $PythonArgs[0].Contains('"')) {
        throw 'ForwardStdin requires exactly one safe Python script path'
    }
    $HookInput = [Console]::In.ReadToEnd()
    if ($HookInput.Length -eq 0) {
        throw 'ForwardStdin received no hook input'
    }
    if ($HookInput.Length -gt 0 -and $HookInput[0] -eq [char] 0xFEFF) {
        $HookInput = $HookInput.Substring(1)
    }
    $StartInfo = [Diagnostics.ProcessStartInfo]::new()
    $StartInfo.FileName = $Python
    $StartInfo.Arguments = '"' + $PythonArgs[0] + '"'
    $StartInfo.UseShellExecute = $false
    $StartInfo.CreateNoWindow = $true
    $StartInfo.RedirectStandardInput = $true
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $Child = [Diagnostics.Process]::new()
    $Child.StartInfo = $StartInfo
    $PreviousInputEncoding = [Console]::InputEncoding
    try {
        [Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
        if (-not $Child.Start()) {
            throw 'portable Python hook process did not start'
        }
        $StandardOutput = [Console]::OpenStandardOutput()
        $StandardError = [Console]::OpenStandardError()
        $StdoutCopy = $Child.StandardOutput.BaseStream.CopyToAsync($StandardOutput)
        $StderrCopy = $Child.StandardError.BaseStream.CopyToAsync($StandardError)
        $Child.StandardInput.Write($HookInput)
        $Child.StandardInput.Close()
        $Child.WaitForExit()
        [Threading.Tasks.Task]::WaitAll(
            [Threading.Tasks.Task[]] @($StdoutCopy, $StderrCopy)
        )
        $StandardOutput.Flush()
        $StandardError.Flush()
        $ChildExitCode = $Child.ExitCode
    }
    finally {
        [Console]::InputEncoding = $PreviousInputEncoding
        $Child.Dispose()
    }
    exit $ChildExitCode
}

& $Python @PythonArgs
exit $LASTEXITCODE
