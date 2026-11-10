# Windows built-in OCR (Windows.Media.Ocr) over one or more screen regions.
# No third-party OCR libs, no network. Windows PowerShell 5.1 compatible.
# NOTE: keep this file ASCII-only (PS 5.1 reads .ps1 as ANSI without a BOM).
#
#   powershell -ExecutionPolicy Bypass -File screen-ocr.ps1 -X 0 -Y 0 -W 1200 -H 300 -AsJson
#   powershell -ExecutionPolicy Bypass -File screen-ocr.ps1 -Regions "100,20,420,120,4|2060,20,470,60,3" -AsJson
#   powershell -ExecutionPolicy Bypass -File screen-ocr.ps1 -File shot.jpg -Regions "100,20,420,120,4" -AsJson
param(
  [int]$X = 0,
  [int]$Y = 0,
  [int]$W = 0,
  [int]$H = 0,
  [string]$File = "",
  [string]$Lang = "zh-Hans-CN",
  [switch]$AsJson,
  [int]$Scale = 1,
  [string]$Regions = ""
)

$ErrorActionPreference = "Stop"
# Force UTF-8 on stdout. Without this a console-less parent (python started from
# node with no console window) makes powershell emit GBK bytes, and the python
# side decodes them as UTF-8 -> every Chinese line turns into mojibake, which
# silently breaks all keyword matching (phase/开始行动/小队分配...).
try {
  [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
  $OutputEncoding = [Console]::OutputEncoding
} catch { }
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Runtime.WindowsRuntime

# DPI awareness: a DPI-unaware parent (python.exe) makes powershell capture a
# virtualized, downscaled desktop, so regions near the right/bottom edge read
# as empty. Force physical pixels (2560x1600) so the calibrated regions line up.
$script:DpiError = ""
try {
  Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class DpiFix {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern int GetSystemMetrics(int i);
  public static int ScreenWidth() { return GetSystemMetrics(0); }
  public static int ScreenHeight() { return GetSystemMetrics(1); }
}
"@
  $null = [DpiFix]::SetProcessDPIAware()
} catch { $script:DpiError = "$_" }

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
  })[0]

function Await($op, $resultType) {
  $asTask = $asTaskGeneric.MakeGenericMethod($resultType)
  $netTask = $asTask.Invoke($null, @($op))
  $netTask.Wait(-1) | Out-Null
  return $netTask.Result
}

$null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.DataWriter, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]

$language = New-Object Windows.Globalization.Language $Lang
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if ($null -eq $engine) { throw "OCR engine unavailable" }

$src = $null
if ($File -ne "") { $src = [System.Drawing.Bitmap]::FromFile($File) }

function Get-SourceBitmap([int]$rx, [int]$ry, [int]$rw, [int]$rh) {
  if ($null -ne $script:src) {
    $rect = New-Object System.Drawing.Rectangle $rx, $ry, $rw, $rh
    return $script:src.Clone($rect, $script:src.PixelFormat)
  }
  $b = New-Object System.Drawing.Bitmap $rw, $rh
  $g = [System.Drawing.Graphics]::FromImage($b)
  $g.CopyFromScreen($rx, $ry, 0, 0, (New-Object System.Drawing.Size $rw, $rh))
  $g.Dispose()
  return $b
}

function Invoke-OcrOnBitmap([System.Drawing.Bitmap]$bmp, [int]$scale) {
  $work = $bmp
  if ($scale -gt 1) {
    $work = New-Object System.Drawing.Bitmap ($bmp.Width * $scale), ($bmp.Height * $scale)
    $g = [System.Drawing.Graphics]::FromImage($work)
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.DrawImage($bmp, 0, 0, $work.Width, $work.Height)
    $g.Dispose()
  }

  $ms = New-Object System.IO.MemoryStream
  $work.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
  $bytes = $ms.ToArray()
  $ms.Dispose()

  $ras = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
  $dw = New-Object Windows.Storage.Streams.DataWriter $ras
  $dw.WriteBytes($bytes)
  $null = Await ($dw.StoreAsync()) ([uint32])
  $null = $dw.DetachStream()
  $ras.Seek(0)

  $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($ras)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $sb = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $res = Await ($engine.RecognizeAsync($sb)) ([Windows.Media.Ocr.OcrResult])

  $out = @()
  foreach ($ln in $res.Lines) {
    $xs = @($ln.Words.BoundingRect.X)
    $ys = @($ln.Words.BoundingRect.Y)
    $ws = @($ln.Words.BoundingRect.Width)
    $hs = @($ln.Words.BoundingRect.Height)
    if ($xs.Count -le 0) { continue }
    $minX = ($xs | Measure-Object -Minimum).Minimum
    $minY = ($ys | Measure-Object -Minimum).Minimum
    $maxR = 0.0; $maxB = 0.0
    for ($i = 0; $i -lt $xs.Count; $i++) {
      $r = $xs[$i] + $ws[$i]
      $b = $ys[$i] + $hs[$i]
      if ($r -gt $maxR) { $maxR = $r }
      if ($b -gt $maxB) { $maxB = $b }
    }
    # map coordinates back to the original image (divide by the upscale factor)
    $out += [pscustomobject]@{
      text = $ln.Text
      x    = [int]($minX / $scale)
      y    = [int]($minY / $scale)
      w    = [int](($maxR - $minX) / $scale)
      h    = [int](($maxB - $minY) / $scale)
    }
  }
  if ($scale -gt 1) { $work.Dispose() }
  return $out
}

$results = @()
try {
  if ($Regions -ne "") {
    $parts = $Regions.Split("|")
    $idx = 0
    foreach ($p in $parts) {
      $f = $p.Split(",")
      if ($f.Count -lt 4) { continue }
      $rx = [int]$f[0]; $ry = [int]$f[1]; $rw = [int]$f[2]; $rh = [int]$f[3]
      $sc = if ($f.Count -ge 5) { [int]$f[4] } else { $Scale }
      $bmp = Get-SourceBitmap $rx $ry $rw $rh
      try {
        $lines = Invoke-OcrOnBitmap $bmp $sc
      } finally { $bmp.Dispose() }
      $results += [pscustomobject]@{ index = $idx; x = $rx; y = $ry; w = $rw; h = $rh; scale = $sc; lines = $lines }
      $idx++
    }
  } else {
    if ($null -ne $src) {
      $bmp = $src
    } else {
      if ($W -le 0 -or $H -le 0) { throw "need -W/-H when -File is empty" }
      $bmp = Get-SourceBitmap $X $Y $W $H
    }
    $lines = Invoke-OcrOnBitmap $bmp $Scale
    if ($null -eq $src) { $bmp.Dispose() }
    $results += [pscustomobject]@{ index = 0; x = $X; y = $Y; w = 0; h = 0; scale = $Scale; lines = $lines }
  }

  # foreground window title: cheap "is the game focused" signal, works even when OCR fails
  $fg = ""
  try {
    if (-not ("FgWin" -as [type])) {
      Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class FgWin {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  public static string Title() { var sb = new StringBuilder(512); GetWindowText(GetForegroundWindow(), sb, 512); return sb.ToString(); }
}
"@
    }
    $fg = [FgWin]::Title()
  } catch { $fg = "" }

  if ($AsJson) {
    $sw = 0; $sh = 0
    try { $sw = [DpiFix]::ScreenWidth(); $sh = [DpiFix]::ScreenHeight() } catch { }
    [pscustomobject]@{ ok = $true; fg = $fg; screen = "$($sw)x$($sh)"; dpiErr = $script:DpiError; results = $results } | ConvertTo-Json -Depth 6 -Compress
  } else {
    foreach ($r in $results) {
      [Console]::Out.WriteLine("--- region $($r.index) ($($r.x),$($r.y) $($r.w)x$($r.h) x$($r.scale))")
      foreach ($l in $r.lines) {
        [Console]::Out.WriteLine(("{0,5},{1,5}  {2}" -f $l.x, $l.y, $l.text))
      }
    }
  }
} finally {
  if ($null -ne $src) { $src.Dispose() }
}
