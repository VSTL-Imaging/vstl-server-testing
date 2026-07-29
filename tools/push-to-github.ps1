param(
    [string]$Message = "",
    [switch]$MainServer,
    [switch]$TestingServer
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path ".git")) {
    throw "This folder is not a Git repository. Run git init first."
}

if ([string]::IsNullOrWhiteSpace($Message)) {
    $Message = "Update VSTL server project $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
}

git status --short
git add -A

$pending = git diff --cached --name-only
if (-not $pending) {
    Write-Host "No Git changes to commit."
    exit 0
}

git commit -m $Message

if ($MainServer) {
    git push origin HEAD:main
} elseif ($TestingServer) {
    git push origin HEAD:testing
} else {
    git push
}
