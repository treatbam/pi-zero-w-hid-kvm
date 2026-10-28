#Requires -Version 5.1
<#
.SYNOPSIS
  One-time pairing installer for Windows 10/11 target hosts (x64 & ARM64, no admin required).
.DESCRIPTION
  Writes ~/.kvm-helper/kvm_stub.ps1 (lightweight USB serial watcher stub that speaks TYPE_HELLO
  and streams SHA-256-verified kvm_helper.ps1 into RAM on every Zero boot) and registers a
  per-user Scheduled Task (KvmHelperStub) at logon, starting it immediately.
#>
$ErrorActionPreference = "Stop"

$DstDir = Join-Path $HOME ".kvm-helper"
New-Item -ItemType Directory -Force -Path $DstDir | Out-Null
$StubPath = Join-Path $DstDir "kvm_stub.ps1"

$StubCode = @'
#Requires -Version 5.1
$ErrorActionPreference = "SilentlyContinue"
$MAGIC = [byte[]](0xFE, 0xFD, 0x4B, 0x56)
$latin1 = [System.Text.Encoding]::GetEncoding(28591)
$probePfx = ([string][char]0x1E) + ([string][char]0x1F) + "KVMCTL:probe"
$bootPfx  = ([string][char]0x1E) + ([string][char]0x1F) + "KVMBOOT:windows:"

function Find-KvmPort {
    $dev = Get-CimInstance Win32_PnPEntity -Filter "PNPDeviceID LIKE '%VID_1D6B&PID_0104%'" |
           Where-Object { $_.Name -match '\((COM\d+)\)' } | Select-Object -First 1
    if ($dev -and $dev.Name -match '\((COM\d+)\)') { return $Matches[1] }
    $sp = Get-CimInstance Win32_SerialPort | Where-Object { $_.PNPDeviceID -match 'VID_1D6B&PID_0104' } | Select-Object -First 1
    if ($sp) { return $sp.DeviceID }
    return $null
}

function New-HelloPacket {
    $arch = if ($env:PROCESSOR_ARCHITECTURE) { $env:PROCESSOR_ARCHITECTURE.ToLowerInvariant() } else { "x64" }
    $json = "{`"v`":1,`"os`":`"windows`",`"arch`":`"$arch`",`"host`":`"$($env:COMPUTERNAME)`",`"want`":`"stream`"}"
    $body = [System.Text.Encoding]::UTF8.GetBytes($json)
    $pkt = New-Object byte[] (9 + $body.Length)
    [Array]::Copy($MAGIC, 0, $pkt, 0, 4)
    $pkt[4] = 10
    [Array]::Copy([BitConverter]::GetBytes([uint32]$body.Length), 0, $pkt, 5, 4)
    [Array]::Copy($body, 0, $pkt, 9, $body.Length)
    return ,$pkt
}

function Unpack-BootScript([string]$Rest) {
    $colon = $Rest.IndexOf(":")
    if ($colon -lt 64) { return $null }
    $wantSha = $Rest.Substring(0, $colon).ToLowerInvariant()
    $b64 = $Rest.Substring($colon + 1).Trim()
    $gzBytes = [Convert]::FromBase64String($b64)
    $msIn = New-Object System.IO.MemoryStream(,$gzBytes)
    $gz = New-Object System.IO.Compression.GZipStream($msIn, [System.IO.Compression.CompressionMode]::Decompress)
    $msOut = New-Object System.IO.MemoryStream
    $gz.CopyTo($msOut)
    $gz.Dispose(); $msIn.Dispose()
    $raw = $msOut.ToArray(); $msOut.Dispose()
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $gotSha = ([BitConverter]::ToString($sha.ComputeHash($raw)).Replace("-", "").ToLowerInvariant())
    if ($gotSha -ne $wantSha) { return $null }
    return [System.Text.Encoding]::UTF8.GetString($raw)
}

while ($true) {
    $com = Find-KvmPort
    if (-not $com) { Start-Sleep -Seconds 2; continue }
    $scriptText = $null
    $sp = New-Object System.IO.Ports.SerialPort($com, 115200, [System.IO.Ports.Parity]::None, 8, [System.IO.Ports.StopBits]::One)
    $sp.DtrEnable = $true; $sp.RtsEnable = $true; $sp.ReadTimeout = 200; $sp.WriteTimeout = 2000
    try {
        $sp.Open()
        $hello = New-HelloPacket
        $lastHello = [DateTime]::MinValue
        $buf = ""
        while ($sp.IsOpen) {
            if (([DateTime]::UtcNow - $lastHello).TotalSeconds -ge 2.5) {
                $sp.Write($hello, 0, $hello.Length)
                $lastHello = [DateTime]::UtcNow
            }
            $n = $sp.BytesToRead
            if ($n -gt 0) {
                $chunk = New-Object byte[] ([Math]::Min(16384, $n))
                $r = $sp.Read($chunk, 0, $chunk.Length)
                $buf += $latin1.GetString($chunk, 0, $r)
                if ($buf.Contains($probePfx)) {
                    $sp.Write($hello, 0, $hello.Length)
                    $lastHello = [DateTime]::UtcNow
                    $buf = $buf.Substring($buf.LastIndexOf($probePfx) + $probePfx.Length)
                }
                $bi = $buf.IndexOf($bootPfx)
                if ($bi -ge 0) {
                    $nl = $buf.IndexOf("`n", $bi)
                    if ($nl -gt $bi) {
                        $rest = $buf.Substring($bi + $bootPfx.Length, $nl - ($bi + $bootPfx.Length)).TrimEnd("`r")
                        $scriptText = Unpack-BootScript $rest
                        if ($scriptText) { break }
                        $buf = $buf.Substring($nl + 1)
                    }
                }
                if ($buf.Length -gt 524288) { $buf = $buf.Substring($buf.Length - 65536) }
            } else {
                Start-Sleep -Milliseconds 100
            }
        }
    } finally {
        if ($sp.IsOpen) { $sp.Close() }
        $sp.Dispose()
    }
    if ($scriptText) {
        $env:KVM_HOST_DEV = $com
        try {
            & ([scriptblock]::Create($scriptText)) -Port $com -Once
        } catch {}
    }
    Start-Sleep -Seconds 1
}
'@

Set-Content -Path $StubPath -Value $StubCode -Encoding UTF8

$TaskName = "KvmHelperStub"
$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$StubPath`""
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
try {
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Paired Windows host: Scheduled Task '$TaskName' active."
} catch {
    Start-Process powershell.exe -WindowStyle Hidden -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$StubPath`""
    Write-Host "Paired Windows host: started kvm_stub.ps1 in background."
}
