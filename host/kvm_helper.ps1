#Requires -Version 5.1
<#
.SYNOPSIS
  Self-contained Windows 10/11 helper for Pi Zero W HID KVM (PowerShell 5.1/7+ & .NET, x64 & ARM64).
.DESCRIPTION
  Zero Python or third-party dependencies required on the Windows target PC.
  Auto-discovers the composite USB CDC ACM serial port (VID_1D6B&PID_0104), sends PC
  stats (TYPE_STATS, TYPE_NET, TYPE_TRIAGE, TYPE_EXT), GEEK 240x135 RGB565 stills
  (TYPE_FRAME, full or 1:1 cursor loupe), changed 64x64 JPEG tiles for the web preview
  (TYPE_PREVIEW + TYPE_QUALITY), clipboard sync (TYPE_CLIP), and interactive PowerShell
  serial mode (shell/helper) over the USB OTG cable.
#>
param(
    [string]$Port = $env:KVM_HOST_DEV,
    [switch]$Once
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

try { Add-Type -AssemblyName System.Drawing -ErrorAction SilentlyContinue } catch {}
try { Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue } catch {}

if (-not ("KvmFastPix" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
public static class KvmFastPix {
    public static byte[] BgraToRgb565(byte[] bgra, int stride, int w, int h) {
        byte[] outBuf = new byte[w * h * 2];
        int o = 0;
        for (int y = 0; y < h; y++) {
            int row = y * stride;
            for (int x = 0; x < w; x++) {
                int b = bgra[row + x * 4];
                int g = bgra[row + x * 4 + 1];
                int r = bgra[row + x * 4 + 2];
                ushort v = (ushort)(((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3));
                outBuf[o++] = (byte)(v & 0xFF);
                outBuf[o++] = (byte)(v >> 8);
            }
        }
        return outBuf;
    }
    public static uint TileFnv1a(byte[] bgra, int stride, int tx, int ty, int tw, int th) {
        uint h = 2166136261u;
        for (int y = 0; y < th; y++) {
            int row = (ty + y) * stride + tx * 4;
            for (int i = 0; i < tw * 4; i++) {
                h ^= bgra[row + i];
                h *= 16777619u;
            }
        }
        return h;
    }
}
"@
}

$MAGIC = [byte[]](0xFE, 0xFD, 0x4B, 0x56)

function New-Envelope([byte]$Type, [byte[]]$Payload) {
    $out = New-Object byte[] (9 + $Payload.Length)
    [Array]::Copy($MAGIC, 0, $out, 0, 4)
    $out[4] = $Type
    $lenBytes = [BitConverter]::GetBytes([uint32]$Payload.Length)
    [Array]::Copy($lenBytes, 0, $out, 5, 4)
    if ($Payload.Length -gt 0) {
        [Array]::Copy($Payload, 0, $out, 9, $Payload.Length)
    }
    return ,$out
}

function Find-KvmPort([string]$Explicit) {
    if ($Explicit) { return $Explicit }
    try {
        $dev = Get-CimInstance Win32_PnPEntity -Filter "PNPDeviceID LIKE '%VID_1D6B&PID_0104%'" -ErrorAction SilentlyContinue |
               Where-Object { $_.Name -match '\((COM\d+)\)' } | Select-Object -First 1
        if ($dev -and $dev.Name -match '\((COM\d+)\)') { return $Matches[1] }
    } catch {}
    try {
        $sp = Get-CimInstance Win32_SerialPort -ErrorAction SilentlyContinue |
              Where-Object { $_.PNPDeviceID -match 'VID_1D6B&PID_0104' } | Select-Object -First 1
        if ($sp -and $sp.DeviceID) { return $sp.DeviceID }
    } catch {}
    return $null
}

function Get-HostMachineId {
    try {
        $g = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Cryptography" -Name MachineGuid -ErrorAction Stop).MachineGuid
        $sha = [System.Security.Cryptography.SHA256]::Create()
        $hb = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($g))
        return ([BitConverter]::ToString($hb).Replace("-", "").ToLowerInvariant().Substring(0, 12))
    } catch { return "" }
}

function Get-CpuTempC {
    try {
        $tz = Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop | Select-Object -First 1
        if ($tz -and $tz.CurrentTemperature -gt 2732) {
            return [Math]::Round(($tz.CurrentTemperature / 10.0) - 273.15, 1)
        }
    } catch {}
    return 0.0
}

function Get-LanIpAndIface {
    try {
        $ip = [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() |
              Where-Object { $_.OperationalStatus -eq 'Up' -and $_.NetworkInterfaceType -ne 'Loopback' } |
              Select-Object -First 1
        if ($ip) {
            $uni = $ip.GetIPProperties().UnicastAddresses |
                   Where-Object { $_.Address.AddressFamily -eq 'InterNetwork' } | Select-Object -First 1
            $addr = if ($uni) { $uni.Address.IPAddressToString } else { "" }
            return @($ip.Name, $addr, $ip.GetIPv4Statistics())
        }
    } catch {}
    return @("", "", $null)
}

function Capture-GeekFrame([string]$CropMode) {
    try {
        $scr = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
        if ($scr.Width -le 0 -or $scr.Height -le 0) { return $null }
        $bmp = New-Object System.Drawing.Bitmap(240, 135, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::Bilinear
        if ($CropMode -eq "loupe") {
            $cur = [System.Windows.Forms.Cursor]::Position
            $sx = [Math]::Max(0, [Math]::Min($scr.Width - 240, $cur.X - 120))
            $sy = [Math]::Max(0, [Math]::Min($scr.Height - 135, $cur.Y - 67))
            $g.CopyFromScreen($scr.X + $sx, $scr.Y + $sy, 0, 0, (New-Object System.Drawing.Size(240, 135)))
            $cx = $cur.X - $sx; $cy = $cur.Y - $sy
            $pen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(220, 62, 232, 181), 1)
            $g.DrawLine($pen, $cx - 8, $cy, $cx + 8, $cy)
            $g.DrawLine($pen, $cx, $cy - 8, $cx, $cy + 8)
            $pen.Dispose()
        } else {
            $srcBmp = New-Object System.Drawing.Bitmap($scr.Width, $scr.Height, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
            $sg = [System.Drawing.Graphics]::FromImage($srcBmp)
            $sg.CopyFromScreen($scr.Location, [System.Drawing.Point]::Empty, $scr.Size)
            $sg.Dispose()
            $g.DrawImage($srcBmp, 0, 0, 240, 135)
            $srcBmp.Dispose()
        }
        $g.Dispose()
        $rect = New-Object System.Drawing.Rectangle(0, 0, 240, 135)
        $bd = $bmp.LockBits($rect, [System.Drawing.Imaging.ImageLockMode]::ReadOnly, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        $raw = New-Object byte[] ($bd.Stride * 135)
        [System.Runtime.InteropServices.Marshal]::Copy($bd.Scan0, $raw, 0, $raw.Length)
        $stride = $bd.Stride
        $bmp.UnlockBits($bd)
        $bmp.Dispose()
        $rgb565 = [KvmFastPix]::BgraToRgb565($raw, $stride, 240, 135)
        return ,(New-Envelope 2 $rgb565)
    } catch { return $null }
}

$JpegCodec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq 'image/jpeg' } | Select-Object -First 1

function Capture-PreviewPacket([string]$Level, [hashtable]$TileHashes, [bool]$ForceKey) {
    if (-not $JpegCodec) { return $null }
    $cfg = switch ($Level) {
        "low"  { @{ W = 640;  H = 360; QId = 0; JpgQ = 48L } }
        "high" { @{ W = 1280; H = 720; QId = 2; JpgQ = 74L } }
        default { @{ W = 960;  H = 540; QId = 1; JpgQ = 62L } }
    }
    try {
        $scr = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
        if ($scr.Width -le 0 -or $scr.Height -le 0) { return $null }
        $srcBmp = New-Object System.Drawing.Bitmap($scr.Width, $scr.Height, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        $sg = [System.Drawing.Graphics]::FromImage($srcBmp)
        $sg.CopyFromScreen($scr.Location, [System.Drawing.Point]::Empty, $scr.Size)
        $sg.Dispose()

        $w = $cfg.W; $h = $cfg.H
        $scaled = New-Object System.Drawing.Bitmap($w, $h, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        $g = [System.Drawing.Graphics]::FromImage($scaled)
        $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::Bilinear
        $g.DrawImage($srcBmp, 0, 0, $w, $h)
        $g.Dispose()
        $srcBmp.Dispose()

        $rect = New-Object System.Drawing.Rectangle(0, 0, $w, $h)
        $bd = $scaled.LockBits($rect, [System.Drawing.Imaging.ImageLockMode]::ReadOnly, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        $raw = New-Object byte[] ($bd.Stride * $h)
        [System.Runtime.InteropServices.Marshal]::Copy($bd.Scan0, $raw, 0, $raw.Length)
        $stride = $bd.Stride
        $scaled.UnlockBits($bd)

        if ($ForceKey) { $TileHashes.Clear() }
        $ep = New-Object System.Drawing.Imaging.EncoderParameters(1)
        $ep.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter([System.Drawing.Imaging.Encoder]::Quality, $cfg.JpgQ)

        $tiles = New-Object System.Collections.Generic.List[object]
        for ($ty = 0; $ty -lt $h; $ty += 64) {
            $th = [Math]::Min(64, $h - $ty)
            for ($tx = 0; $tx -lt $w; $tx += 64) {
                $tw = [Math]::Min(64, $w - $tx)
                $hash = [KvmFastPix]::TileFnv1a($raw, $stride, $tx, $ty, $tw, $th)
                $key = "$tx,$ty"
                if (-not $ForceKey -and $TileHashes.ContainsKey($key) -and $TileHashes[$key] -eq $hash) { continue }
                $TileHashes[$key] = $hash
                $tb = $scaled.Clone((New-Object System.Drawing.Rectangle($tx, $ty, $tw, $th)), [System.Drawing.Imaging.PixelFormat]::Format24bppRgb)
                $ms = New-Object System.IO.MemoryStream
                $tb.Save($ms, $JpegCodec, $ep)
                $tb.Dispose()
                $jb = $ms.ToArray()
                $ms.Dispose()
                $tiles.Add(@{ X = [uint16]$tx; Y = [uint16]$ty; W = [uint16]$tw; H = [uint16]$th; Data = $jb })
            }
        }
        $ep.Dispose()
        $scaled.Dispose()

        $n = $tiles.Count
        if ($n -eq 0 -and -not $ForceKey) { return ,([byte[]]@()) }
        $msOut = New-Object System.IO.MemoryStream
        $bw = New-Object System.IO.BinaryWriter($msOut)
        $bw.Write([byte]1)
        $bw.Write([byte]$(if ($ForceKey) { 1 } else { 0 }))
        $bw.Write([byte]$cfg.QId)
        $bw.Write([uint16]$w)
        $bw.Write([uint16]$h)
        $bw.Write([uint16]$n)
        foreach ($t in $tiles) {
            $bw.Write([uint16]$t.X)
            $bw.Write([uint16]$t.Y)
            $bw.Write([uint16]$t.W)
            $bw.Write([uint16]$t.H)
            $bw.Write([uint32]$t.Data.Length)
        }
        foreach ($t in $tiles) { $bw.Write($t.Data) }
        $bw.Flush()
        $payload = $msOut.ToArray()
        $bw.Dispose(); $msOut.Dispose()
        return ,(New-Envelope 7 $payload)
    } catch { return $null }
}

function Pack-Mode([byte]$m) { return ,(New-Envelope 3 ([byte[]]@($m))) }
function Pack-Quality([string]$q) {
    $v = switch ($q) { "low" { 0 } "med" { 1 } "high" { 2 } default { 3 } }
    return ,(New-Envelope 9 ([byte[]]@([byte]$v)))
}

function Run-Session([string]$ComName) {
    $sp = New-Object System.IO.Ports.SerialPort($ComName, 115200, [System.IO.Ports.Parity]::None, 8, [System.IO.Ports.StopBits]::One)
    $sp.Handshake = [System.IO.Ports.Handshake]::None
    $sp.DtrEnable = $true
    $sp.RtsEnable = $true
    $sp.ReadTimeout = 50
    $sp.WriteTimeout = 2000
    $sp.Open()
    Write-Host "kvm_helper.ps1 connected on $ComName"

    $mode = "helper"
    $statsOn = $false
    $quality = "off"
    $cropMode = "full"
    $frameUntil = [DateTime]::UtcNow.AddSeconds(8)
    $nextTick = [DateTime]::UtcNow
    $nextPrev = [DateTime]::UtcNow
    $nextKey = [DateTime]::UtcNow
    $tileHashes = @{}
    $pcId = Get-HostMachineId
    $pcHost = $env:COMPUTERNAME
    $ncpu = [float][Environment]::ProcessorCount
    $prevRx = -1L; $prevTx = -1L; $prevAt = [DateTime]::UtcNow
    $ctlBuf = New-Object System.Collections.Generic.List[byte]
    $shellBuf = ""

    try {
        $ack = Pack-Mode 0
        $sp.Write($ack, 0, $ack.Length)

        while ($sp.IsOpen) {
            $now = [DateTime]::UtcNow
            if ($mode -eq "helper" -and $now -ge $nextTick) {
                $ms = New-Object System.IO.MemoryStream
                $bw = New-Object System.IO.BinaryWriter($ms)

                $os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
                $cpuLoad = (Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue | Measure-Object -Property LoadPercentage -Average).Average
                if ($null -eq $cpuLoad) { $cpuLoad = 0.0 }
                $memPct = 0.0; $memUsed = 0L; $memTotal = 0L
                if ($os -and $os.TotalVisibleMemorySize -gt 0) {
                    $memTotal = [int64]$os.TotalVisibleMemorySize * 1024L
                    $memFree = [int64]$os.FreePhysicalMemory * 1024L
                    $memUsed = $memTotal - $memFree
                    $memPct = 100.0 * ($memUsed / [double]$memTotal)
                }
                $tempC = Get-CpuTempC
                $load1 = [Math]::Round(($cpuLoad / 100.0) * $ncpu, 2)

                $stPay = New-Object byte[] 12
                [Array]::Copy([BitConverter]::GetBytes([single]$tempC), 0, $stPay, 0, 4)
                [Array]::Copy([BitConverter]::GetBytes([single]$load1), 0, $stPay, 4, 4)
                [Array]::Copy([BitConverter]::GetBytes([single]$memPct), 0, $stPay, 8, 4)
                $pkt = New-Envelope 1 $stPay; $bw.Write($pkt)

                $netInfo = Get-LanIpAndIface
                $iface = $netInfo[0]; $lanIp = $netInfo[1]; $v4 = $netInfo[2]
                $rxBps = -1.0; $txBps = -1.0
                if ($v4) {
                    $dt = ($now - $prevAt).TotalSeconds
                    if ($prevRx -ge 0 -and $dt -gt 0.2) {
                        $rxBps = [Math]::Max(0.0, ($v4.BytesReceived - $prevRx) / $dt)
                        $txBps = [Math]::Max(0.0, ($v4.BytesSent - $prevTx) / $dt)
                    }
                    $prevRx = $v4.BytesReceived; $prevTx = $v4.BytesSent; $prevAt = $now
                }
                $netPay = New-Object byte[] 36
                [Array]::Copy([BitConverter]::GetBytes([single]$rxBps), 0, $netPay, 0, 4)
                [Array]::Copy([BitConverter]::GetBytes([single]$txBps), 0, $netPay, 4, 4)
                [Array]::Copy([BitConverter]::GetBytes([single]$ncpu), 0, $netPay, 8, 4)
                $cb = [byte][Math]::Min(100, [Math]::Max(0, [int][Math]::Round($cpuLoad)))
                for ($ci = 0; $ci -lt 8; $ci++) { $netPay[12 + $ci] = $(if ($ci -lt $ncpu) { $cb } else { 255 }) }
                $ifBytes = [System.Text.Encoding]::UTF8.GetBytes(($iface + ""))
                [Array]::Copy($ifBytes, 0, $netPay, 20, [Math]::Min(15, $ifBytes.Length))
                $bw.Write((New-Envelope 4 $netPay))

                $drv = Get-PSDrive C -ErrorAction SilentlyContinue
                $diskPct = -1.0; $diskUsed = 0L; $diskTotal = 0L
                if ($drv -and ($drv.Used + $drv.Free) -gt 0) {
                    $diskUsed = [int64]$drv.Used
                    $diskTotal = [int64]($drv.Used + $drv.Free)
                    $diskPct = 100.0 * ($diskUsed / [double]$diskTotal)
                }
                $topP = Get-Process -ErrorAction SilentlyContinue | Sort-Object CPU -Descending | Select-Object -First 1
                $topName = if ($topP) { $topP.ProcessName } else { "" }
                $triPay = New-Object byte[] 40
                [Array]::Copy([BitConverter]::GetBytes([single]$diskPct), 0, $triPay, 0, 4)
                [Array]::Copy([BitConverter]::GetBytes([single]$cpuLoad), 0, $triPay, 4, 4)
                $ipB = [System.Text.Encoding]::UTF8.GetBytes($lanIp)
                [Array]::Copy($ipB, 0, $triPay, 8, [Math]::Min(15, $ipB.Length))
                $prB = [System.Text.Encoding]::UTF8.GetBytes($topName)
                [Array]::Copy($prB, 0, $triPay, 24, [Math]::Min(15, $prB.Length))
                $bw.Write((New-Envelope 5 $triPay))

                if ($now -le $frameUntil) {
                    $fp = Capture-GeekFrame $cropMode
                    if ($fp) { $bw.Write($fp) }
                }

                $bw.Write((Pack-Mode 0))

                if ($statsOn -or ($quality -ne "off")) {
                    $uptimeS = [int]([Environment]::TickCount64 / 1000L)
                    $extObj = @{
                        v = 1
                        uptime_s = $uptimeS
                        mem = @($memUsed, $memTotal)
                        disk = @($diskUsed, $diskTotal, "C:")
                        pc_host = $pcHost
                    }
                    if ($pcId) { $extObj["pc_id"] = $pcId }
                    if ($lanIp) { $extObj["net_ip"] = $lanIp }
                    if ($tempC -gt 0) { $extObj["temps"] = @{ cpu = $tempC } }
                    $extBytes = [System.Text.Encoding]::UTF8.GetBytes(($extObj | ConvertTo-Json -Compress))
                    $bw.Write((New-Envelope 8 $extBytes))
                }

                $bw.Flush()
                $blob = $ms.ToArray()
                $bw.Dispose(); $ms.Dispose()
                $sp.Write($blob, 0, $blob.Length)
                $nextTick = [DateTime]::UtcNow.AddSeconds(2.0)
            }

            if ($mode -eq "helper" -and $quality -in @("low", "med", "high") -and $now -ge $nextPrev) {
                $forceKey = ($now -ge $nextKey)
                if ($forceKey) { $nextKey = $now.AddSeconds(15.0) }
                $pvPkt = Capture-PreviewPacket $quality $tileHashes $forceKey
                if ($pvPkt -and $pvPkt.Length -gt 0) {
                    $sp.Write($pvPkt, 0, $pvPkt.Length)
                }
                $ivMs = switch ($quality) { "low" { 500 } "high" { 200 } default { 330 } }
                $nextPrev = [DateTime]::UtcNow.AddMilliseconds($ivMs)
            }

            $avail = $sp.BytesToRead
            if ($avail -gt 0) {
                $inBuf = New-Object byte[] ([Math]::Min(4096, $avail))
                $nRead = $sp.Read($inBuf, 0, $inBuf.Length)
                for ($i = 0; $i -lt $nRead; $i++) { $ctlBuf.Add($inBuf[$i]) }

                $latin1 = [System.Text.Encoding]::GetEncoding(28591)
                $ctlPfx = ([string][char]0x1E) + ([string][char]0x1F) + "KVMCTL:"
                while ($ctlBuf.Count -gt 0) {
                    $arr = $ctlBuf.ToArray()
                    $sTxt = $latin1.GetString($arr)
                    $idx = $sTxt.IndexOf($ctlPfx)
                    if ($idx -lt 0) {
                        if ($mode -eq "shell") {
                            $shellBuf += [System.Text.Encoding]::UTF8.GetString($arr)
                            while ($shellBuf.Contains("`n") -or $shellBuf.Contains("`r")) {
                                $parts = $shellBuf -split "\r?\n", 2
                                $cmdLine = $parts[0]
                                $shellBuf = if ($parts.Length -gt 1) { $parts[1] } else { "" }
                                if ($cmdLine.Trim() -eq "exit") {
                                    $mode = "helper"
                                    $ack = Pack-Mode 0; $sp.Write($ack, 0, $ack.Length)
                                    $nextTick = [DateTime]::UtcNow
                                } elseif ($cmdLine.Length -gt 0) {
                                    try {
                                        $res = (Invoke-Expression $cmdLine 2>&1 | Out-String)
                                    } catch { $res = "ERR: $($_.Exception.Message)`r`n" }
                                    $rb = [System.Text.Encoding]::UTF8.GetBytes($res + "PS> ")
                                    $sp.Write($rb, 0, $rb.Length)
                                } else {
                                    $rb = [System.Text.Encoding]::UTF8.GetBytes("PS> ")
                                    $sp.Write($rb, 0, $rb.Length)
                                }
                            }
                        }
                        $ctlBuf.Clear()
                        break
                    }
                    if ($idx -gt 0) { $ctlBuf.RemoveRange(0, $idx); continue }
                    $nlIdx = $sTxt.IndexOf("`n", 9)
                    if ($nlIdx -lt 0) {
                        if ($ctlBuf.Count -gt 2048) { $ctlBuf.Clear() }
                        break
                    }
                    $cmd = $sTxt.Substring(9, $nlIdx - 9).TrimEnd("`r")
                    $ctlBuf.RemoveRange(0, $nlIdx + 1)

                    switch -Wildcard ($cmd) {
                        "probe" {
                            $ack = Pack-Mode $(if ($mode -eq "shell") { 1 } else { 0 })
                            $sp.Write($ack, 0, $ack.Length)
                            $nextTick = [DateTime]::UtcNow
                        }
                        "stats:on"  { $statsOn = $true;  $nextTick = [DateTime]::UtcNow }
                        "stats:off" { $statsOn = $false; $nextTick = [DateTime]::UtcNow }
                        "q:*" {
                            $q = $cmd.Substring(2)
                            if ($q -in @("low", "med", "high", "off")) {
                                $quality = $q
                                $tileHashes.Clear()
                                $nextPrev = [DateTime]::UtcNow
                                $nextKey = [DateTime]::UtcNow
                                $qp = Pack-Quality $quality
                                $sp.Write($qp, 0, $qp.Length)
                            }
                        }
                        "frame" { $cropMode = "full";  $frameUntil = [DateTime]::UtcNow.AddSeconds(8) }
                        "loupe" { $cropMode = "loupe"; $frameUntil = [DateTime]::UtcNow.AddSeconds(8) }
                        "cget" {
                            try { $ct = Get-Clipboard -Raw -ErrorAction SilentlyContinue } catch { $ct = "" }
                            $cb = [System.Text.Encoding]::UTF8.GetBytes(($ct + ""))
                            if ($cb.Length -gt 1024) { $cb = $cb[0..1023] }
                            $cp = New-Envelope 6 $cb
                            $sp.Write($cp, 0, $cp.Length)
                        }
                        "cset:*" {
                            try {
                                $raw = [Convert]::FromBase64String($cmd.Substring(5))
                                $txt = [System.Text.Encoding]::UTF8.GetString($raw)
                                Set-Clipboard -Value $txt -ErrorAction SilentlyContinue
                                $cp = New-Envelope 6 $raw
                                $sp.Write($cp, 0, $cp.Length)
                            } catch {}
                        }
                        "shell" {
                            $mode = "shell"
                            $ack = Pack-Mode 1; $sp.Write($ack, 0, $ack.Length)
                            $pb = [System.Text.Encoding]::UTF8.GetBytes("PowerShell serial session (`exit` returns to helper)`r`nPS> ")
                            $sp.Write($pb, 0, $pb.Length)
                        }
                        "helper" {
                            $mode = "helper"
                            $ack = Pack-Mode 0; $sp.Write($ack, 0, $ack.Length)
                            $nextTick = [DateTime]::UtcNow; $nextPrev = [DateTime]::UtcNow
                        }
                    }
                }
            } else {
                Start-Sleep -Milliseconds 25
            }
        }
    } finally {
        if ($sp.IsOpen) { $sp.Close() }
        $sp.Dispose()
    }
}

while ($true) {
    $com = Find-KvmPort $Port
    $connected = $false
    if ($com) {
        try {
            $connected = $true
            Run-Session $com
        } catch {
            Write-Host "kvm_helper.ps1 ($com): $($_.Exception.Message)"
        }
    }
    if ($Once -and $connected) { break }
    Start-Sleep -Seconds 2
}
