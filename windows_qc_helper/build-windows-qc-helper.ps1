param(
    [string]$WorkDir = "D:\win\vstl-windows-qc-helper-build",
    [string]$IsoPath = "D:\win\vstl-windows-qc-helper.iso",
    [string]$Arch = "amd64"
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = [Security.Principal.WindowsPrincipal]$Identity
    if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script from an Administrator PowerShell window. DISM must mount and edit boot.wim."
    }
}

function Find-FirstExistingPath([string[]]$Paths) {
    foreach ($Path in $Paths) {
        if (Test-Path $Path) { return $Path }
    }
    return $null
}

Assert-Admin

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$QcSource = Join-Path $ProjectRoot "qc"
$DriverSource = Join-Path $ProjectRoot "drivers"
$AudioSource = Join-Path (Split-Path -Parent $ProjectRoot) "imaging_server\bench-client\sounds\alsa"

$WinPeRoot = Find-FirstExistingPath @(
    "C:\Program Files (x86)\Windows Kits\10\Assessment and Deployment Kit\Windows Preinstallation Environment",
    "C:\Program Files (x86)\Windows Kits\11\Assessment and Deployment Kit\Windows Preinstallation Environment"
)
$DeployRoot = Find-FirstExistingPath @(
    "C:\Program Files (x86)\Windows Kits\10\Assessment and Deployment Kit\Deployment Tools",
    "C:\Program Files (x86)\Windows Kits\11\Assessment and Deployment Kit\Deployment Tools"
)
if (-not $WinPeRoot -or -not $DeployRoot) {
    throw "Windows ADK + WinPE add-on were not found. Install both before building."
}

$Copype = Join-Path $WinPeRoot "copype.cmd"
$MakeWinPeMedia = Join-Path $WinPeRoot "MakeWinPEMedia.cmd"
$AdkEnv = Join-Path $DeployRoot "DandISetEnv.bat"
$OcRoot = Join-Path $WinPeRoot "$Arch\WinPE_OCs"
$OcLangRoot = Join-Path $OcRoot "en-us"

if (-not (Test-Path $Copype)) { throw "Missing copype.cmd: $Copype" }
if (-not (Test-Path $MakeWinPeMedia)) { throw "Missing MakeWinPEMedia.cmd: $MakeWinPeMedia" }
if (-not (Test-Path $AdkEnv)) { throw "Missing ADK environment script: $AdkEnv" }
if (-not (Test-Path $OcRoot)) { throw "Missing WinPE optional components: $OcRoot" }

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $WorkDir), (Split-Path -Parent $IsoPath) | Out-Null
if (Test-Path $WorkDir) { Remove-Item -Recurse -Force $WorkDir }
if (Test-Path $IsoPath) { Remove-Item -Force $IsoPath }

Write-Host "Creating WinPE workspace: $WorkDir" -ForegroundColor Cyan
$CopypeCommand = "call `"$AdkEnv`" && `"$Copype`" $Arch `"$WorkDir`""
& cmd.exe /d /c $CopypeCommand
if ($LASTEXITCODE -ne 0) { throw "copype failed with exit code $LASTEXITCODE" }

$BootWim = Join-Path $WorkDir "media\sources\boot.wim"
$MountDir = Join-Path $WorkDir "mount"
New-Item -ItemType Directory -Force -Path $MountDir | Out-Null

Write-Host "Mounting boot.wim..." -ForegroundColor Cyan
& dism.exe /Mount-Image /ImageFile:$BootWim /Index:1 /MountDir:$MountDir
if ($LASTEXITCODE -ne 0) { throw "DISM mount failed with exit code $LASTEXITCODE" }

try {
    $Packages = @(
        "WinPE-WMI",
        "WinPE-NetFx",
        "WinPE-Scripting",
        "WinPE-PowerShell",
        "WinPE-StorageWMI",
        "WinPE-Dot3Svc",
        "WinPE-EnhancedStorage",
        "WinPE-RNDIS",
        "WinPE-GamingPeripherals"
    )

    foreach ($Package in $Packages) {
        $Cab = Join-Path $OcRoot "$Package.cab"
        if (Test-Path $Cab) {
            Write-Host "Adding package $Package" -ForegroundColor Gray
            & dism.exe /Image:$MountDir /Add-Package /PackagePath:$Cab
            if ($LASTEXITCODE -ne 0) { throw "Failed to add $Package" }
        }
        $LangCab = Join-Path $OcLangRoot "$Package`_en-us.cab"
        if (Test-Path $LangCab) {
            & dism.exe /Image:$MountDir /Add-Package /PackagePath:$LangCab
            if ($LASTEXITCODE -ne 0) { throw "Failed to add $Package language package" }
        }
    }

    $AppDir = Join-Path $MountDir "VSTL-QC"
    $AppAudio = Join-Path $AppDir "Audio"
    $AppDrivers = Join-Path $AppDir "Drivers"
    New-Item -ItemType Directory -Force -Path $AppDir, $AppAudio, $AppDrivers | Out-Null
    Copy-Item -Path (Join-Path $QcSource "*") -Destination $AppDir -Recurse -Force
    if (Test-Path $AudioSource) {
        Copy-Item -Path (Join-Path $AudioSource "*.wav") -Destination $AppAudio -Force
    }
    if (Test-Path $DriverSource) {
        Copy-Item -Path (Join-Path $DriverSource "*") -Destination $AppDrivers -Recurse -Force
        $DriverInfs = @(Get-ChildItem $DriverSource -Recurse -Filter *.inf -ErrorAction SilentlyContinue)
        if ($DriverInfs.Count -gt 0) {
            Write-Host "Injecting driver folder into WinPE: $DriverSource" -ForegroundColor Cyan
            & dism.exe /Image:$MountDir /Add-Driver /Driver:$DriverSource /Recurse
            if ($LASTEXITCODE -ne 0) { throw "DISM driver injection failed" }
        }
    }

    Copy-Item -Path (Join-Path $QcSource "startnet.cmd") -Destination (Join-Path $MountDir "Windows\System32\startnet.cmd") -Force

    Write-Host "Committing boot.wim..." -ForegroundColor Cyan
    & dism.exe /Unmount-Image /MountDir:$MountDir /Commit
    if ($LASTEXITCODE -ne 0) { throw "DISM unmount/commit failed with exit code $LASTEXITCODE" }
} catch {
    Write-Host "Build failed, discarding mounted image..." -ForegroundColor Red
    & dism.exe /Unmount-Image /MountDir:$MountDir /Discard | Out-Null
    throw
}

Write-Host "Building ISO: $IsoPath" -ForegroundColor Cyan
$MakeIsoCommand = "call `"$AdkEnv`" && `"$MakeWinPeMedia`" /ISO `"$WorkDir`" `"$IsoPath`""
& cmd.exe /d /c $MakeIsoCommand
if ($LASTEXITCODE -ne 0) { throw "MakeWinPEMedia failed with exit code $LASTEXITCODE" }

$Hash = Get-FileHash -Algorithm SHA256 $IsoPath
"$($Hash.Hash.ToLower())  $IsoPath" | Set-Content -Path "$IsoPath.sha256" -Encoding UTF8
Write-Host "Built: $IsoPath" -ForegroundColor Green
Write-Host "SHA256: $($Hash.Hash.ToLower())" -ForegroundColor Green
