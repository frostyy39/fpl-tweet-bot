[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ClientIdPath,
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$EncryptedClientSecretPath,
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[1-9][0-9]*$")]
    [string]$ExpectedUserId,
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$TokenOutputPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if ($Host.Name -eq "ConsoleHost") {
    $Host.UI.RawUI.WindowTitle = "FPL Bot - Test Account Authorization"
}

$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$python = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
if (-not [IO.File]::Exists($python)) {
    throw "The repository Python environment is unavailable; authorization was not started."
}
$priorPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $repositoryRoot "src"
    & $python -m fpl_bot.x_reauthorization_cli `
        --client-id-path ([IO.Path]::GetFullPath($ClientIdPath)) `
        --encrypted-client-secret-path ([IO.Path]::GetFullPath($EncryptedClientSecretPath)) `
        --expected-user-id $ExpectedUserId `
        --token-output-path ([IO.Path]::GetFullPath($TokenOutputPath)) `
        --repository-root $repositoryRoot
    if ($LASTEXITCODE -ne 0) {
        throw "The no-post test-account authorization did not complete successfully."
    }
}
finally {
    if ($null -eq $priorPythonPath) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONPATH = $priorPythonPath
    }
}
