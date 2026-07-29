param(
    [switch] $ForwardStdin,
    [switch] $PrepareOnly
)

$ErrorActionPreference = 'Stop'
$env:PYTHONDONTWRITEBYTECODE = '1'
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
$PreparedTarget = Join-Path $DataRoot "portable-python-$Version"
$PreparedPython = Join-Path $PreparedTarget 'python.exe'
$LockPath = Join-Path $DataRoot '.portable-python.lock'
$Stage = Join-Path $DataRoot ('.portable-python-stage-' + [guid]::NewGuid().ToString('N'))

function Assert-DirectDirectory {
    param([Parameter(Mandatory = $true)][string] $Path)

    $FullPath = [IO.Path]::GetFullPath($Path)
    $Root = [IO.Path]::GetPathRoot($FullPath)
    $Current = $Root
    $Components = $FullPath.Substring($Root.Length).Split(
        [IO.Path]::DirectorySeparatorChar,
        [StringSplitOptions]::RemoveEmptyEntries
    )
    foreach ($Component in $Components) {
        $Current = Join-Path $Current $Component
        if (-not (Test-Path -LiteralPath $Current -PathType Container)) {
            throw "private runtime parent is unavailable: $Current"
        }
        $Item = Get-Item -LiteralPath $Current -Force
        if (
            -not $Item.PSIsContainer -or
            (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
        ) {
            throw "private runtime path is redirected: $Current"
        }
    }
}

function New-PrivateRootSecurity {
    $Sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $Inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit `
        -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit
    $Security = [Security.AccessControl.DirectorySecurity]::new()
    $Security.SetOwner($Sid)
    $Security.SetAccessRuleProtection($true, $false)
    $Rule = [Security.AccessControl.FileSystemAccessRule]::new(
        $Sid,
        [Security.AccessControl.FileSystemRights]::FullControl,
        $Inheritance,
        [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Allow
    )
    [void] $Security.AddAccessRule($Rule)
    return $Security
}

function Assert-PrivateRootSecurity {
    param([Parameter(Mandatory = $true)][string] $Path)

    $Sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $Inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit `
        -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit
    $Sections = [Security.AccessControl.AccessControlSections]::Owner `
        -bor [Security.AccessControl.AccessControlSections]::Access
    $Security = [IO.Directory]::GetAccessControl($Path, $Sections)
    $Rules = @($Security.GetAccessRules(
        $true,
        $true,
        [Security.Principal.SecurityIdentifier]
    ))
    if (
        -not $Security.AreAccessRulesProtected -or
        $Security.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $Sid.Value -or
        $Rules.Count -ne 1
    ) {
        throw "private runtime ACL verification failed: $Path"
    }
    $Actual = $Rules[0]
    if (
        $Actual.IdentityReference.Value -ne $Sid.Value -or
        $Actual.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
        $Actual.FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl -or
        $Actual.InheritanceFlags -ne $Inheritance -or
        $Actual.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None
    ) {
        throw "private runtime ACL verification failed: $Path"
    }
}

function Initialize-PrivateDataRoot {
    param([Parameter(Mandatory = $true)][string] $Path)

    Assert-DirectDirectory (Split-Path -Parent $Path)
    $Marker = Join-Path $Path '.private-root-v1'
    if (Test-Path -LiteralPath $Path) {
        Assert-DirectDirectory $Path
        if (-not (Test-Path -LiteralPath $Marker -PathType Leaf)) {
            throw "private runtime root requires migration: $Path"
        }
        $MarkerItem = Get-Item -LiteralPath $Marker -Force
        if (($MarkerItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "private runtime marker is redirected: $Marker"
        }
        Assert-PrivateRootSecurity $Path
        return
    }

    $Security = New-PrivateRootSecurity
    [void] [IO.Directory]::CreateDirectory($Path, $Security)
    Assert-DirectDirectory $Path
    Assert-PrivateRootSecurity $Path
    $Bytes = [Text.Encoding]::ASCII.GetBytes("private-root-v1`n")
    $Stream = [IO.File]::Open(
        $Marker,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        $Stream.Write($Bytes, 0, $Bytes.Length)
        $Stream.Flush($true)
    }
    finally {
        $Stream.Dispose()
    }
}

Initialize-PrivateDataRoot $DataRoot

if ((Test-Path -LiteralPath $PreparedTarget) -and -not (Test-Path -LiteralPath $PreparedPython -PathType Leaf)) {
    throw "portable runtime is incomplete: $PreparedTarget"
}
if (Test-Path -LiteralPath $PreparedPython -PathType Leaf) {
    $Python = $PreparedPython
}
else {
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
}

if ($PrepareOnly) {
    exit 0
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
    $StartInfo.Arguments = '-I -B -X utf8 "' + $PythonArgs[0] + '"'
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

& $Python -I -B -X utf8 @PythonArgs
exit $LASTEXITCODE
