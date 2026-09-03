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

$clientIdPath = [IO.Path]::GetFullPath($ClientIdPath)
$secretPath = [IO.Path]::GetFullPath($EncryptedClientSecretPath)
$tokenPath = [IO.Path]::GetFullPath($TokenOutputPath)
if ([IO.File]::Exists($tokenPath)) {
    throw "The new token-output file already exists; the prior handoff was not changed."
}

$clientId = [IO.File]::ReadAllText($clientIdPath).Trim()
if ([string]::IsNullOrWhiteSpace($clientId)) {
    throw "The OAuth 2.0 Client ID file is empty."
}
$encryptedSecret = [IO.File]::ReadAllText($secretPath).Trim()
if ([string]::IsNullOrWhiteSpace($encryptedSecret)) {
    throw "The encrypted OAuth 2.0 Client Secret file is empty."
}

$secureSecret = ConvertTo-SecureString -String $encryptedSecret
$secretPointer = [IntPtr]::Zero
try {
    $secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureSecret)
    $env:X_OAUTH_CLIENT_ID = $clientId
    $env:X_OAUTH_CLIENT_SECRET = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
    $env:X_OAUTH_TOKEN_OUTPUT_FILE = $tokenPath
    $env:X_EXPECTED_USER_ID = $ExpectedUserId
    & fpl-bot-x-authorize
    if ($LASTEXITCODE -ne 0) {
        throw "The no-post test-account authorization did not complete successfully."
    }
}
finally {
    Remove-Item Env:X_OAUTH_CLIENT_ID -ErrorAction SilentlyContinue
    Remove-Item Env:X_OAUTH_CLIENT_SECRET -ErrorAction SilentlyContinue
    Remove-Item Env:X_OAUTH_TOKEN_OUTPUT_FILE -ErrorAction SilentlyContinue
    Remove-Item Env:X_EXPECTED_USER_ID -ErrorAction SilentlyContinue
    if ($secretPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer)
    }
    $secureSecret.Dispose()
}
