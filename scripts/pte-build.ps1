[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidatePattern('^v[0-9]+(?:\.[0-9]+){2}(?:[-+][A-Za-z0-9.-]+)?$')]
    [string]$Tag
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$BuildRoot = Join-Path $RepoRoot '.build\pte'
$TempRoot = Join-Path $BuildRoot 'temp'

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Project Python is missing: $Python"
}

$PreviousTemp = $env:TEMP
$PreviousTmp = $env:TMP
New-Item -ItemType Directory -Path $TempRoot -Force | Out-Null
$env:TEMP = $TempRoot
$env:TMP = $TempRoot

Push-Location $RepoRoot
try {
    & $Python -m paper_trading_engine.release_cli build `
        --repo-root $RepoRoot `
        --build-root $BuildRoot `
        --release $Tag `
        --python $Python
    if ($LASTEXITCODE -ne 0) {
        throw "PTE build failed for $Tag with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
    $env:TEMP = $PreviousTemp
    $env:TMP = $PreviousTmp
}
