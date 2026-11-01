# Dump selected frames of an .ico as upscaled PNG previews (nearest neighbor).
# Usage: powershell -ExecutionPolicy Bypass -File preview-ico.ps1 -Ico aswitch.ico -Scale 16
param(
    [Parameter(Mandatory = $true)][string]$Ico,
    [int]$Target = 256
)
Add-Type -AssemblyName System.Drawing

[byte[]]$bytes = [System.IO.File]::ReadAllBytes((Resolve-Path $Ico))
[int]$count = [BitConverter]::ToUInt16($bytes, 4)
Write-Output ("frames: {0}" -f $count)

for ([int]$i = 0; $i -lt $count; $i++) {
    [int]$o = 6 + 16 * $i
    [int]$size = $bytes[$o]
    if ($size -eq 0) { $size = 256 }
    [int]$len = [BitConverter]::ToUInt32($bytes, $o + 8)
    [int]$off = [BitConverter]::ToUInt32($bytes, $o + 12)
    [byte[]]$slice = New-Object byte[] $len
    [Array]::Copy($bytes, $off, $slice, 0, $len)
    $ms = New-Object System.IO.MemoryStream(, $slice)
    $img = [System.Drawing.Image]::FromStream($ms)
    [int]$px = $Target
    [int]$scale = [Math]::Max(1, [int]($Target / $size))
    $big = New-Object System.Drawing.Bitmap($px, $px, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($big)
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::NearestNeighbor
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::Half
    # checkerboard so transparency is visible
    $light = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 240, 240, 240))
    $dark = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 205, 205, 205))
    [int]$cell = [Math]::Max(8, [int]($px / 8))
    for ([int]$yy = 0; $yy -lt $px; $yy += $cell) {
        for ([int]$xx = 0; $xx -lt $px; $xx += $cell) {
            $c = if ((($xx / $cell) + ($yy / $cell)) % 2 -eq 0) { $light } else { $dark }
            $g.FillRectangle($c, $xx, $yy, $cell, $cell)
        }
    }
    $g.DrawImage($img, 0, 0, $px, $px)
    $g.Dispose()
    $out = Join-Path (Get-Location) ("preview_{0}.png" -f $size)
    $big.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
    $big.Dispose(); $img.Dispose(); $ms.Dispose()
    Write-Output ("wrote {0} (from {1}x{1} frame)" -f $out, $size)
}
