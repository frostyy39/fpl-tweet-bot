[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$OutputPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if ($Host.Name -eq "ConsoleHost") {
    $Host.UI.RawUI.WindowTitle = "FPL Bot - Secure Client Secret Capture"
}

$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$python = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
if (-not [IO.File]::Exists($python)) {
    throw "The repository Python environment is unavailable; the encrypted Client Secret was not changed."
}
$priorPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $repositoryRoot "src"
    & $python -m fpl_bot.x_client_secret_capture `
        --output-path ([IO.Path]::GetFullPath($OutputPath)) `
        --repository-root $repositoryRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Secure Client Secret capture did not complete; the prior encrypted file was preserved."
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
