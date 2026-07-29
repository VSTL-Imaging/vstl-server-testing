$ErrorActionPreference = "Continue"

$AppRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ReportRoot = Join-Path $AppRoot "Reports"
$DriverRoot = Join-Path $AppRoot "Drivers"
$AudioRoot = Join-Path $AppRoot "Audio"
New-Item -ItemType Directory -Force -Path $ReportRoot | Out-Null

function Write-Title {
    Clear-Host
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host " VSTL Windows QC Helper - Experimental" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "This test USB does not modify the Linux VSTL server." -ForegroundColor Yellow
    Write-Host
}

function Get-SafeValue($Value, $Fallback = "") {
    if ($null -eq $Value) { return $Fallback }
    $Text = [string]$Value
    if ([string]::IsNullOrWhiteSpace($Text)) { return $Fallback }
    return $Text.Trim()
}

function Get-ReportDirectory {
    $Removable = Get-CimInstance Win32_LogicalDisk -ErrorAction SilentlyContinue |
        Where-Object { $_.DriveType -eq 2 -and $_.DeviceID -ne "X:" } |
        Select-Object -First 1
    if ($Removable) {
        $Path = Join-Path $Removable.DeviceID "VSTL-Windows-QC-Reports"
        New-Item -ItemType Directory -Force -Path $Path | Out-Null
        return $Path
    }
    return $ReportRoot
}

function Get-HardwareInfo {
    $Bios = Get-CimInstance Win32_BIOS -ErrorAction SilentlyContinue
    $Csp = Get-CimInstance Win32_ComputerSystemProduct -ErrorAction SilentlyContinue
    $Cs = Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue
    $Base = Get-CimInstance Win32_BaseBoard -ErrorAction SilentlyContinue
    $Cpu = Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue | Select-Object -First 1
    $RamBytes = (Get-CimInstance Win32_PhysicalMemory -ErrorAction SilentlyContinue | Measure-Object Capacity -Sum).Sum
    $Macs = Get-CimInstance Win32_NetworkAdapter -ErrorAction SilentlyContinue |
        Where-Object { $_.MACAddress -and $_.PhysicalAdapter } |
        Select-Object -ExpandProperty MACAddress -Unique

    [ordered]@{
        timestamp_utc = (Get-Date).ToUniversalTime().ToString("o")
        serial_number = Get-SafeValue $Bios.SerialNumber
        manufacturer = Get-SafeValue $Cs.Manufacturer
        model = Get-SafeValue $Cs.Model
        product_name = Get-SafeValue $Csp.Name
        sku = Get-SafeValue $Csp.SKUNumber
        uuid = Get-SafeValue $Csp.UUID
        bios_version = (($Bios.SMBIOSBIOSVersion, $Bios.Version) | Where-Object { $_ } | Select-Object -First 1)
        baseboard_product = Get-SafeValue $Base.Product
        baseboard_serial = Get-SafeValue $Base.SerialNumber
        cpu = Get-SafeValue $Cpu.Name
        ram_bytes = [int64]($RamBytes -as [int64])
        mac_addresses = @($Macs)
        os_environment = "WinPE / Windows QC Helper"
    }
}

function Get-PnpDevices {
    Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue |
        Select-Object Name, PNPClass, DeviceID, Manufacturer, Status, ConfigManagerErrorCode
}

function Get-DeviceGroups {
    $Devices = @(Get-PnpDevices)
    [ordered]@{
        audio = @($Devices | Where-Object { $_.PNPClass -match "MEDIA|AudioEndpoint" -or $_.Name -match "Audio|Microphone|Realtek|Intel.*Sound|Smart Sound" })
        fingerprint = @($Devices | Where-Object { $_.PNPClass -match "Biometric" -or $_.Name -match "Finger|Biometric|Validity|Synaptics|Goodix|ELAN|FPC|AuthenTec|Broadcom" -or $_.DeviceID -match "VID_138A|VID_27C6|VID_06CB|VID_04F3|VID_10A5" })
        camera = @($Devices | Where-Object { $_.PNPClass -match "Camera|Image" -or $_.Name -match "Camera|Webcam|Integrated Webcam" })
        usb_c_related = @($Devices | Where-Object { $_.Name -match "USB.*Type|USB.*C|Thunderbolt|UCSI|Billboard|USB4" -or $_.PNPClass -match "USB" })
        problem_devices = @($Devices | Where-Object { $_.ConfigManagerErrorCode -and $_.ConfigManagerErrorCode -ne 0 })
    }
}

function Import-VstlDrivers {
    $Loaded = @()
    if (-not (Test-Path $DriverRoot)) {
        return [ordered]@{ status = "NO_DRIVER_FOLDER"; loaded = @(); message = "No X:\VSTL-QC\Drivers folder found." }
    }
    $Infs = @(Get-ChildItem -Path $DriverRoot -Recurse -Filter *.inf -ErrorAction SilentlyContinue)
    foreach ($Inf in $Infs) {
        Write-Host "Loading driver: $($Inf.FullName)" -ForegroundColor Gray
        $Output = & drvload.exe $Inf.FullName 2>&1
        $Loaded += [ordered]@{
            inf = $Inf.FullName
            output = ($Output -join "`n")
            exit_code = $LASTEXITCODE
        }
    }
    [ordered]@{ status = "DONE"; loaded = $Loaded; count = $Loaded.Count }
}

function Play-WavFile($Path) {
    if (-not (Test-Path $Path)) { return $false }
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue | Out-Null
        Add-Type -AssemblyName System.Drawing -ErrorAction SilentlyContinue | Out-Null
        $Player = New-Object System.Media.SoundPlayer $Path
        $Player.Load()
        $Player.PlaySync()
        return $true
    } catch {
        Write-Host "Playback failed: $($_.Exception.Message)" -ForegroundColor Red
        return $false
    }
}

function Invoke-SpeakerHeadphoneTest {
    Write-Title
    Write-Host "Speaker / Headphone Test" -ForegroundColor Green
    Write-Host "Connect headphones if testing the audio jack."
    Write-Host "You should hear: Front Left, then Front Right."
    Write-Host
    $Left = Join-Path $AudioRoot "Front_Left.wav"
    $Right = Join-Path $AudioRoot "Front_Right.wav"
    $LeftOk = Play-WavFile $Left
    Start-Sleep -Milliseconds 500
    $RightOk = Play-WavFile $Right
    Write-Host
    $Choice = Read-Host "Did you hear correct left/right audio? Type P=Pass, F=Fail, N=Not tested"
    $Status = switch -Regex ($Choice) {
        "^[Pp]" { "PASS"; break }
        "^[Ff]" { "FAIL"; break }
        default { "NA" }
    }
    [ordered]@{
        status = $Status
        left_file_played = $LeftOk
        right_file_played = $RightOk
        remarks = if ($Status -eq "FAIL") { Read-Host "Enter remarks" } else { "" }
    }
}

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class VstlMci {
    [DllImport("winmm.dll", CharSet = CharSet.Auto)]
    public static extern int mciSendString(string command, string buffer, int bufferSize, IntPtr hwndCallback);
}
"@ -ErrorAction SilentlyContinue | Out-Null

function Invoke-MicrophoneTest {
    Write-Title
    Write-Host "Microphone Test" -ForegroundColor Green
    Write-Host "Speak clearly after recording starts. Recording duration: 5 seconds."
    Write-Host
    $OutDir = Get-ReportDirectory
    $Wav = Join-Path $OutDir ("mic-test-{0}.wav" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
    try {
        [void][VstlMci]::mciSendString("close vstlrec", $null, 0, [IntPtr]::Zero)
        $Open = [VstlMci]::mciSendString("open new type waveaudio alias vstlrec", $null, 0, [IntPtr]::Zero)
        [void][VstlMci]::mciSendString("set vstlrec time format ms bitspersample 16 channels 1 samplespersec 44100", $null, 0, [IntPtr]::Zero)
        $Rec = [VstlMci]::mciSendString("record vstlrec", $null, 0, [IntPtr]::Zero)
        Write-Host "Recording..."
        Start-Sleep -Seconds 5
        $Save = [VstlMci]::mciSendString("save vstlrec `"$Wav`"", $null, 0, [IntPtr]::Zero)
        [void][VstlMci]::mciSendString("close vstlrec", $null, 0, [IntPtr]::Zero)
        Write-Host "Playing recorded audio..."
        $Played = Play-WavFile $Wav
        $Choice = Read-Host "Did playback contain your real voice? Type P=Pass, F=Fail, N=Not tested"
        $Status = switch -Regex ($Choice) {
            "^[Pp]" { "PASS"; break }
            "^[Ff]" { "FAIL"; break }
            default { "NA" }
        }
        [ordered]@{
            status = $Status
            wav_path = $Wav
            open_code = $Open
            record_code = $Rec
            save_code = $Save
            playback_attempted = $Played
            remarks = if ($Status -eq "FAIL") { Read-Host "Enter remarks" } else { "" }
        }
    } catch {
        [ordered]@{
            status = "ERROR"
            wav_path = $Wav
            error = $_.Exception.Message
        }
    }
}

function Invoke-FingerprintCheck($Groups) {
    Write-Title
    Write-Host "Fingerprint Sensor Check" -ForegroundColor Green
    $Sensors = @($Groups.fingerprint)
    if ($Sensors.Count -eq 0) {
        Write-Host "No fingerprint/biometric sensor detected."
        return [ordered]@{ status = "NA"; detected = $false; devices = @(); remarks = "No sensor detected by Windows PnP." }
    }
    Write-Host "Detected fingerprint/biometric device(s):" -ForegroundColor Yellow
    $Sensors | Format-Table Name, Manufacturer, Status, ConfigManagerErrorCode -AutoSize
    Write-Host
    Write-Host "WinPE may detect the sensor but may not support full touch authentication."
    $Choice = Read-Host "If touch/light/driver behavior looks correct type P=Pass, F=Fail, N=NA"
    $Status = switch -Regex ($Choice) {
        "^[Pp]" { "PASS"; break }
        "^[Ff]" { "FAIL"; break }
        default { "NA" }
    }
    [ordered]@{
        status = $Status
        detected = $true
        devices = @($Sensors)
        remarks = if ($Status -eq "FAIL") { Read-Host "Enter remarks" } else { "" }
    }
}

function Save-Result($Result) {
    $OutDir = Get-ReportDirectory
    $Serial = Get-SafeValue $Result.hardware.serial_number "UNKNOWN"
    $Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $JsonPath = Join-Path $OutDir ("vstl-windows-qc-{0}-{1}.json" -f $Serial, $Stamp)
    $TxtPath = Join-Path $OutDir ("vstl-windows-qc-{0}-{1}.txt" -f $Serial, $Stamp)
    $Result | ConvertTo-Json -Depth 8 | Set-Content -Path $JsonPath -Encoding UTF8
    @(
        "VSTL Windows QC Helper"
        "Serial: $($Result.hardware.serial_number)"
        "Model: $($Result.hardware.model)"
        "SKU: $($Result.hardware.sku)"
        "Timestamp UTC: $($Result.hardware.timestamp_utc)"
        ""
        "Speaker/headphone: $($Result.tests.speaker_headphone.status)"
        "Microphone: $($Result.tests.microphone.status)"
        "Fingerprint: $($Result.tests.fingerprint.status)"
        ""
        "JSON: $JsonPath"
    ) | Set-Content -Path $TxtPath -Encoding UTF8
    return [ordered]@{ json = $JsonPath; text = $TxtPath }
}

$Result = [ordered]@{
    helper_version = "2026-06-28-test1"
    hardware = Get-HardwareInfo
    driver_load = $null
    devices = $null
    tests = [ordered]@{
        speaker_headphone = [ordered]@{ status = "NOT_RUN" }
        microphone = [ordered]@{ status = "NOT_RUN" }
        fingerprint = [ordered]@{ status = "NOT_RUN" }
    }
}

$Running = $true
while ($Running) {
    $Groups = Get-DeviceGroups
    $Result.devices = $Groups
    Write-Title
    Write-Host "Serial : $($Result.hardware.serial_number)"
    Write-Host "Model  : $($Result.hardware.manufacturer) $($Result.hardware.model)"
    Write-Host "SKU    : $($Result.hardware.sku)"
    Write-Host
    Write-Host "1. Load drivers from X:\VSTL-QC\Drivers"
    Write-Host "2. Speaker / headphone left-right audio test"
    Write-Host "3. Microphone 5-second record/playback test"
    Write-Host "4. Fingerprint sensor check"
    Write-Host "5. Save report"
    Write-Host "6. Show detected devices"
    Write-Host "Q. Quit"
    Write-Host
    $Key = Read-Host "Select"
    switch -Regex ($Key) {
        "^1$" {
            $Result.driver_load = Import-VstlDrivers
            Write-Host "Driver load complete. Press ENTER." -ForegroundColor Green
            Read-Host | Out-Null
        }
        "^2$" { $Result.tests.speaker_headphone = Invoke-SpeakerHeadphoneTest }
        "^3$" { $Result.tests.microphone = Invoke-MicrophoneTest }
        "^4$" { $Result.tests.fingerprint = Invoke-FingerprintCheck $Groups }
        "^5$" {
            $Saved = Save-Result $Result
            Write-Host "Saved JSON: $($Saved.json)" -ForegroundColor Green
            Write-Host "Saved TXT : $($Saved.text)" -ForegroundColor Green
            Read-Host "Press ENTER" | Out-Null
        }
        "^6$" {
            Write-Title
            $Groups.GetEnumerator() | ForEach-Object {
                Write-Host "[$($_.Key)]" -ForegroundColor Cyan
                @($_.Value) | Format-Table Name, PNPClass, Manufacturer, Status, ConfigManagerErrorCode -AutoSize
                Write-Host
            }
            Read-Host "Press ENTER" | Out-Null
        }
        "^[Qq]$" {
            $Saved = Save-Result $Result
            Write-Host "Final report saved: $($Saved.json)" -ForegroundColor Green
            $Running = $false
        }
    }
}
