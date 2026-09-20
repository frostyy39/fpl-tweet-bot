[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ClientIdPath,
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$EncryptedClientSecretPath,
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$TokenOutputPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if ($Host.Name -eq "ConsoleHost") {
    $Host.UI.RawUI.WindowTitle = "FPL Bot - Production Account Authorization (No Post)"
}

$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$python = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
if (-not [IO.File]::Exists($python)) {
    throw "The repository Python environment is unavailable; authorization was not started."
}
$priorPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $repositoryRoot "src"
    & $python -m fpl_bot.x_production_authorization_cli `
        --client-id-path ([IO.Path]::GetFullPath($ClientIdPath)) `
        --encrypted-client-secret-path ([IO.Path]::GetFullPath($EncryptedClientSecretPath)) `
        --token-output-path ([IO.Path]::GetFullPath($TokenOutputPath)) `
        --repository-root $repositoryRoot
    if ($LASTEXITCODE -ne 0) {
        throw "The no-post production-account authorization did not complete successfully."
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
