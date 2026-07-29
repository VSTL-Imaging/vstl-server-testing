param(
    [string]$Message = "",
    [switch]$MainServer,
    [switch]$TestingServer,
    [string]$Remote = ""
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
    if ([string]::IsNullOrWhiteSpace($Remote)) { $Remote = "original" }
    git push $Remote HEAD:main
} elseif ($TestingServer) {
    if ([string]::IsNullOrWhiteSpace($Remote)) { $Remote = "testing-remote" }
    git push $Remote HEAD:testing
} else {
    if (-not [string]::IsNullOrWhiteSpace($Remote)) {
        git push $Remote
        exit 0
    }
    git push
}
