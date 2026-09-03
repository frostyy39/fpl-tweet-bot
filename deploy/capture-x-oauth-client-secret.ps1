[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$OutputPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Test-SecureStringEqual {
    param(
        [Parameter(Mandatory = $true)]
        [Security.SecureString]$Left,
        [Parameter(Mandatory = $true)]
        [Security.SecureString]$Right
    )

    if ($Left.Length -ne $Right.Length) {
        return $false
    }
    $leftPointer = [IntPtr]::Zero
    $rightPointer = [IntPtr]::Zero
    try {
        $leftPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Left)
        $rightPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Right)
        for ($index = 0; $index -lt $Left.Length; $index++) {
            $offset = $index * 2
            if (
                [Runtime.InteropServices.Marshal]::ReadInt16($leftPointer, $offset) -ne
                [Runtime.InteropServices.Marshal]::ReadInt16($rightPointer, $offset)
            ) {
                return $false
            }
        }
        return $true
    }
    finally {
        if ($leftPointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($leftPointer)
        }
        if ($rightPointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($rightPointer)
        }
    }
}

$target = [IO.Path]::GetFullPath($OutputPath)
$parent = [IO.Path]::GetDirectoryName($target)
if (-not [IO.Directory]::Exists($parent)) {
    throw "The encrypted Client Secret destination directory does not exist."
}

$pending = Join-Path $parent (".{0}.pending-{1}" -f [IO.Path]::GetFileName($target), [guid]::NewGuid())
$secureSecret = Read-Host "Paste the newly generated OAuth 2.0 Client Secret" -AsSecureString
if ($secureSecret.Length -eq 0) {
    throw "The Client Secret was empty; the existing encrypted file was not changed."
}

try {
    $encrypted = ConvertFrom-SecureString -SecureString $secureSecret
    $roundTrip = ConvertTo-SecureString -String $encrypted
    if (-not (Test-SecureStringEqual -Left $secureSecret -Right $roundTrip)) {
        throw "The encrypted Client Secret failed local round-trip validation."
    }

    $encoding = [Text.UTF8Encoding]::new($false)
    $stream = [IO.File]::Open($pending, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $writer = [IO.StreamWriter]::new($stream, $encoding)
        try {
            $writer.Write($encrypted)
        }
        finally {
            $writer.Dispose()
        }
    }
    finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }

    if ([IO.File]::Exists($target)) {
        $timestamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
        $backup = "$target.backup-$timestamp"
        if ([IO.File]::Exists($backup)) {
            throw "A backup path already exists; the existing encrypted file was not changed."
        }
        [IO.File]::Replace($pending, $target, $backup, $true)
        Write-Output "Client Secret captured, validated, and atomically replaced; encrypted backup retained."
    }
    else {
        [IO.File]::Move($pending, $target)
        Write-Output "Client Secret captured, validated, and stored with current-user DPAPI."
    }
}
finally {
    if ([IO.File]::Exists($pending)) {
        [IO.File]::Delete($pending)
    }
    if ($null -ne $secureSecret) {
        $secureSecret.Dispose()
    }
}
