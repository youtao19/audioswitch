# Build a multi-size Windows .ico from a PNG (no Pillow / ImageMagick needed).
# Usage: powershell -ExecutionPolicy Bypass -File make-ico.ps1 -Source ..\aswitch.png -Output .\aswitch.ico
# NOTE: keep this file ASCII-only; Windows PowerShell reads non-BOM UTF-8 as ANSI.
param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Output,
    [int[]]$Sizes = @(16, 20, 24, 32, 40, 48, 64, 128, 256),
    [double]$Margin = 0.04
)

Add-Type -AssemblyName System.Drawing

$src = [System.Drawing.Image]::FromFile((Resolve-Path $Source))
Write-Output ("source: {0}x{1} {2}" -f $src.Width, $src.Height, $src.PixelFormat)

# ---- 1. compute content bounding box from the alpha channel ----
$full = New-Object System.Drawing.Bitmap($src)
$rect = New-Object System.Drawing.Rectangle 0, 0, $full.Width, $full.Height
$data = $full.LockBits($rect, [System.Drawing.Imaging.ImageLockMode]::ReadOnly,
                       [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
$bytes = New-Object byte[] ($data.Stride * $full.Height)
[System.Runtime.InteropServices.Marshal]::Copy($data.Scan0, $bytes, 0, $bytes.Length)
$full.UnlockBits($data)

$minX = $full.Width; $minY = $full.Height; $maxX = -1; $maxY = -1
$stride = $data.Stride
for ($y = 0; $y -lt $full.Height; $y++) {
    $row = $y * $stride
    for ($x = 0; $x -lt $full.Width; $x++) {
        $a = $bytes[$row + ($x * 4) + 3]
        if ($a -gt 24) {
            if ($x -lt $minX) { $minX = $x }
            if ($x -gt $maxX) { $maxX = $x }
            if ($y -lt $minY) { $minY = $y }
            if ($y -gt $maxY) { $maxY = $y }
        }
    }
}
$full.Dispose()
if ($maxX -lt 0) { throw "image is fully transparent" }

$cw = $maxX - $minX + 1
$ch = $maxY - $minY + 1
Write-Output ("content box: {0}x{1} at ({2},{3})" -f $cw, $ch, $minX, $minY)

# square crop, centered, small margin, clipped to the source image
$side = [Math]::Max($cw, $ch) * (1 + 2 * $Margin)
$cx = ($minX + $maxX) / 2.0
$cy = ($minY + $maxY) / 2.0
$sx = $cx - $side / 2.0
$sy = $cy - $side / 2.0
if ($sx -lt 0) { $sx = 0 }
if ($sy -lt 0) { $sy = 0 }
if (($sx + $side) -gt $src.Width) { $sx = $src.Width - $side }
if (($sy + $side) -gt $src.Height) { $sy = $src.Height - $side }
$srcRect = New-Object System.Drawing.RectangleF([float]$sx, [float]$sy, [float]$side, [float]$side)

# ---- 2. resize each size and encode as PNG ----
$entries = @()
foreach ($s in $Sizes) {
    $bmp = New-Object System.Drawing.Bitmap($s, $s, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
    $g.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceCopy
    $g.Clear([System.Drawing.Color]::Transparent)
    $dst = New-Object System.Drawing.RectangleF([float]0, [float]0, [float]$s, [float]$s)
    $g.DrawImage($src, $dst, $srcRect, [System.Drawing.GraphicsUnit]::Pixel)
    $g.Dispose()
    $ms = New-Object System.IO.MemoryStream
    $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    $entries += [pscustomobject]@{ Size = $s; Bytes = $ms.ToArray() }
    $ms.Dispose()
}
$src.Dispose()

# ---- 3. assemble the ICO container (PNG-compressed entries, Vista+) ----
$outPath = [System.IO.Path]::GetFullPath($Output)
$fs = [System.IO.File]::Create($outPath)
$bw = New-Object System.IO.BinaryWriter($fs)
$bw.Write([uint16]0)
$bw.Write([uint16]1)
$bw.Write([uint16]$entries.Count)
$offset = 6 + 16 * $entries.Count
foreach ($e in $entries) {
    if ($e.Size -ge 256) { $dim = 0 } else { $dim = $e.Size }
    $bw.Write([byte]$dim); $bw.Write([byte]$dim)
    $bw.Write([byte]0); $bw.Write([byte]0)
    $bw.Write([uint16]1)
    $bw.Write([uint16]32)
    $bw.Write([uint32]$e.Bytes.Length)
    $bw.Write([uint32]$offset)
    $offset += $e.Bytes.Length
}
foreach ($e in $entries) { $bw.Write($e.Bytes) }
$bw.Close(); $fs.Close()

Write-Output ("written: {0}  ({1} bytes, {2} sizes: {3})" -f `
    $outPath, (Get-Item $outPath).Length, $entries.Count, ($Sizes -join '/'))
