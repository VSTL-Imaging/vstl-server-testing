$ErrorActionPreference = "Continue"

$AppRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$DriverRoot = Join-Path $AppRoot "Drivers"
$AudioRoot = Join-Path $AppRoot "Audio"
$ReportRoot = Join-Path $AppRoot "VSTL-Windows-QC-Reports"
New-Item -ItemType Directory -Force -Path $ReportRoot | Out-Null

function Test-IsAdmin {
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = [Security.Principal.WindowsPrincipal]$Identity
    return $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Write-Title {
    Clear-Host
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host " VSTL Full Windows QC Agent - Experimental" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "Run this inside normal Windows, not WinPE." -ForegroundColor Yellow
    Write-Host
}

function Get-SafeValue($Value, $Fallback = "") {
    if ($null -eq $Value) { return $Fallback }
    $Text = [string]$Value
    if ([string]::IsNullOrWhiteSpace($Text)) { return $Fallback }
    return $Text.Trim()
}

function Get-HardwareInfo {
    $Bios = Get-CimInstance Win32_BIOS -ErrorAction SilentlyContinue
    $Csp = Get-CimInstance Win32_ComputerSystemProduct -ErrorAction SilentlyContinue
    $Cs = Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue
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
        cpu = Get-SafeValue $Cpu.Name
        ram_bytes = [int64]($RamBytes -as [int64])
        mac_addresses = @($Macs)
        os_environment = "Full Windows"
        is_admin = Test-IsAdmin
    }
}

function Get-DeviceGroups {
    $Devices = @(Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue |
        Select-Object Name, PNPClass, DeviceID, Manufacturer, Status, ConfigManagerErrorCode)
    [ordered]@{
        audio = @($Devices | Where-Object { $_.PNPClass -match "MEDIA|AudioEndpoint" -or $_.Name -match "Audio|Microphone|Realtek|Intel.*Sound|Smart Sound" })
        fingerprint = @($Devices | Where-Object { $_.PNPClass -match "Biometric" -or $_.Name -match "Finger|Biometric|Validity|Synaptics|Goodix|ELAN|FPC|AuthenTec|Broadcom" -or $_.DeviceID -match "VID_138A|VID_27C6|VID_06CB|VID_04F3|VID_10A5" })
        camera = @($Devices | Where-Object { $_.PNPClass -match "Camera|Image" -or $_.Name -match "Camera|Webcam|Integrated Webcam" })
        problem_devices = @($Devices | Where-Object { $_.ConfigManagerErrorCode -and $_.ConfigManagerErrorCode -ne 0 })
    }
}

function Install-Drivers {
    if (-not (Test-IsAdmin)) {
        return [ordered]@{ status = "FAIL"; message = "Run as administrator to install drivers." }
    }
    if (-not (Test-Path $DriverRoot)) {
        return [ordered]@{ status = "NA"; message = "Driver folder not found: $DriverRoot" }
    }
    $Infs = @(Get-ChildItem $DriverRoot -Recurse -Filter *.inf -ErrorAction SilentlyContinue)
    if ($Infs.Count -eq 0) {
        return [ordered]@{ status = "NA"; message = "No .inf drivers found."; count = 0 }
    }
    $Output = & pnputil.exe /add-driver "$DriverRoot\*.inf" /subdirs /install 2>&1
    [ordered]@{
        status = if ($LASTEXITCODE -eq 0) { "PASS" } else { "FAIL" }
        exit_code = $LASTEXITCODE
        count = $Infs.Count
        output = ($Output -join "`n")
    }
}

function Play-WavFile($Path) {
    if (-not (Test-Path $Path)) { return $false }
    try {
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
    Write-Host "You should hear Front Left, then Front Right."
    Write-Host
    $LeftOk = Play-WavFile (Join-Path $AudioRoot "Front_Left.wav")
    Start-Sleep -Milliseconds 500
    $RightOk = Play-WavFile (Join-Path $AudioRoot "Front_Right.wav")
    $Choice = Read-Host "Did you hear correct left/right audio? P=Pass, F=Fail, N=NA"
    $Status = switch -Regex ($Choice) {
        "^[Pp]" { "PASS"; break }
        "^[Ff]" { "FAIL"; break }
        default { "NA" }
    }
    [ordered]@{
        status = $Status
        left_file_played = $LeftOk
        right_file_played = $RightOk
        remarks = if ($Status -eq "FAIL") { Read-Host "Remarks" } else { "" }
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
    Write-Host "Speak clearly. Recording duration: 5 seconds."
    $Wav = Join-Path $ReportRoot ("mic-test-{0}.wav" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
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
    $Choice = Read-Host "Did playback contain your real voice? P=Pass, F=Fail, N=NA"
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
        remarks = if ($Status -eq "FAIL") { Read-Host "Remarks" } else { "" }
    }
}

Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Threading.Tasks;

public class VstlWinBio {
    const uint WINBIO_TYPE_FINGERPRINT = 0x00000008;
    const uint WINBIO_POOL_SYSTEM = 1;
    const uint WINBIO_FLAG_DEFAULT = 0;
    const byte WINBIO_PURPOSE_VERIFY = 1;
    const byte WINBIO_DATA_FLAG_RAW = 1;

    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    struct WINBIO_UNIT_SCHEMA {
        public uint UnitId;
        public uint PoolType;
        public uint BiometricFactor;
        public uint SensorSubType;
        public uint Capabilities;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=256)] public string DeviceInstanceId;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=256)] public string Description;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=256)] public string Manufacturer;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=256)] public string Model;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=256)] public string SerialNumber;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst=256)] public string FirmwareVersion;
    }

    [DllImport("winbio.dll")]
    static extern int WinBioEnumBiometricUnits(uint Factor, out IntPtr UnitSchemaArray, out UIntPtr UnitCount);
    [DllImport("winbio.dll")]
    static extern void WinBioFree(IntPtr Address);
    [DllImport("winbio.dll")]
    static extern int WinBioOpenSession(uint Factor, uint PoolType, uint Flags, IntPtr UnitArray, UIntPtr UnitCount, IntPtr DatabaseId, out IntPtr SessionHandle);
    [DllImport("winbio.dll")]
    static extern int WinBioCloseSession(IntPtr SessionHandle);
    [DllImport("winbio.dll")]
    static extern int WinBioCaptureSample(IntPtr SessionHandle, byte Purpose, byte Flags, out uint UnitId, out IntPtr Sample, out UIntPtr SampleSize, out uint RejectDetail);

    public static string EnumUnits() {
        IntPtr ptr;
        UIntPtr countPtr;
        int hr = WinBioEnumBiometricUnits(WINBIO_TYPE_FINGERPRINT, out ptr, out countPtr);
        ulong count = countPtr.ToUInt64();
        if (hr != 0) return "ERROR hr=0x" + hr.ToString("X8");
        if (count == 0) return "NONE";
        int size = Marshal.SizeOf(typeof(WINBIO_UNIT_SCHEMA));
        string result = "";
        for (ulong i = 0; i < count; i++) {
            IntPtr item = new IntPtr(ptr.ToInt64() + (long)(i * (ulong)size));
            WINBIO_UNIT_SCHEMA schema = (WINBIO_UNIT_SCHEMA)Marshal.PtrToStructure(item, typeof(WINBIO_UNIT_SCHEMA));
            result += "UnitId=" + schema.UnitId + "; " + schema.Manufacturer + " " + schema.Model + "; " + schema.Description + "; " + schema.DeviceInstanceId + "\\n";
        }
        WinBioFree(ptr);
        return result.Trim();
    }

    public static string CaptureTouch(int timeoutMs) {
        return Task.Run(() => {
            IntPtr session;
            int hr = WinBioOpenSession(WINBIO_TYPE_FINGERPRINT, WINBIO_POOL_SYSTEM, WINBIO_FLAG_DEFAULT, IntPtr.Zero, UIntPtr.Zero, IntPtr.Zero, out session);
            if (hr != 0) return "OPEN_ERROR hr=0x" + hr.ToString("X8");
            try {
                uint unitId, rejectDetail;
                IntPtr sample;
                UIntPtr sampleSize;
                hr = WinBioCaptureSample(session, WINBIO_PURPOSE_VERIFY, WINBIO_DATA_FLAG_RAW, out unitId, out sample, out sampleSize, out rejectDetail);
                if (sample != IntPtr.Zero) WinBioFree(sample);
                if (hr == 0) return "TOUCH_DETECTED unit=" + unitId + " bytes=" + sampleSize.ToUInt64();
                return "CAPTURE_ERROR hr=0x" + hr.ToString("X8") + " reject=" + rejectDetail;
            } finally {
                WinBioCloseSession(session);
            }
        }).Result;
    }
}
"@ -ErrorAction SilentlyContinue | Out-Null

function Invoke-FingerprintTest($Groups) {
    Write-Title
    Write-Host "Fingerprint Test" -ForegroundColor Green
    $Svc = Get-Service WinBioSrvc -ErrorAction SilentlyContinue
    if ($Svc -and $Svc.Status -ne "Running") {
        try { Start-Service WinBioSrvc -ErrorAction SilentlyContinue } catch {}
    }
    $Pnp = @($Groups.fingerprint)
    $Enum = ""
    try { $Enum = [VstlWinBio]::EnumUnits() } catch { $Enum = "ERROR $($_.Exception.Message)" }
    Write-Host "PnP fingerprint devices: $($Pnp.Count)"
    Write-Host "WinBio units: $Enum"
    Write-Host
    $DoTouch = Read-Host "Touch sensor test? Y/N"
    $Touch = "NOT_RUN"
    if ($DoTouch -match "^[Yy]") {
        Write-Host "Touch the fingerprint sensor now..."
        try { $Touch = [VstlWinBio]::CaptureTouch(15000) } catch { $Touch = "ERROR $($_.Exception.Message)" }
        Write-Host $Touch
    }
    $Choice = Read-Host "Fingerprint result P=Pass, F=Fail, N=NA"
    $Status = switch -Regex ($Choice) {
        "^[Pp]" { "PASS"; break }
        "^[Ff]" { "FAIL"; break }
        default { "NA" }
    }
    [ordered]@{
        status = $Status
        pnp_devices = @($Pnp)
        winbio_units = $Enum
        touch_result = $Touch
        remarks = if ($Status -eq "FAIL") { Read-Host "Remarks" } else { "" }
    }
}

function Save-Result($Result) {
    $Serial = Get-SafeValue $Result.hardware.serial_number "UNKNOWN"
    $Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $JsonPath = Join-Path $ReportRoot ("vstl-full-windows-qc-{0}-{1}.json" -f $Serial, $Stamp)
    $TxtPath = Join-Path $ReportRoot ("vstl-full-windows-qc-{0}-{1}.txt" -f $Serial, $Stamp)
    $Result | ConvertTo-Json -Depth 10 | Set-Content -Path $JsonPath -Encoding UTF8
    @(
        "VSTL Full Windows QC Agent"
        "Serial: $($Result.hardware.serial_number)"
        "Model: $($Result.hardware.manufacturer) $($Result.hardware.model)"
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
    agent_version = "2026-06-28-full-windows-test1"
    hardware = Get-HardwareInfo
    driver_install = $null
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
    Write-Host "Admin  : $($Result.hardware.is_admin)"
    Write-Host
    Write-Host "1. Install drivers from Drivers folder"
    Write-Host "2. Speaker / headphone left-right audio test"
    Write-Host "3. Microphone 5-second record/playback test"
    Write-Host "4. Fingerprint WinBio touch test"
    Write-Host "5. Save report"
    Write-Host "6. Show detected devices"
    Write-Host "Q. Quit"
    Write-Host
    $Key = Read-Host "Select"
    switch -Regex ($Key) {
        "^1$" { $Result.driver_install = Install-Drivers; $Result.driver_install | Format-List; Read-Host "Press ENTER" | Out-Null }
        "^2$" { $Result.tests.speaker_headphone = Invoke-SpeakerHeadphoneTest }
        "^3$" { $Result.tests.microphone = Invoke-MicrophoneTest }
        "^4$" { $Result.tests.fingerprint = Invoke-FingerprintTest $Groups }
        "^5$" { $Saved = Save-Result $Result; Write-Host "Saved: $($Saved.json)" -ForegroundColor Green; Read-Host "Press ENTER" | Out-Null }
        "^6$" {
            Write-Title
            $Groups.GetEnumerator() | ForEach-Object {
                Write-Host "[$($_.Key)]" -ForegroundColor Cyan
                @($_.Value) | Format-Table Name, PNPClass, Manufacturer, Status, ConfigManagerErrorCode -AutoSize
                Write-Host
            }
            Read-Host "Press ENTER" | Out-Null
        }
        "^[Qq]$" { $Saved = Save-Result $Result; Write-Host "Final report saved: $($Saved.json)" -ForegroundColor Green; $Running = $false }
    }
}
