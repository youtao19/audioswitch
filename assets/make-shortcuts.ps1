# Create desktop shortcuts for audioswitch (ASCII-only: PS 5.1 reads non-BOM UTF-8 as ANSI)
Add-Type -AssemblyName System.Drawing
$root = 'C:\Users\youtao\audioswitch'
$desktop = [Environment]::GetFolderPath('Desktop')
$ws = New-Object -ComObject WScript.Shell
$ico = Join-Path $root 'assets\aswitch.ico'
$pythonw = 'C:\Users\youtao\AppData\Local\Programs\Python\Python313\pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = 'pythonw.exe' }

# 1) one-click toggle (no console window)
$lnk = $ws.CreateShortcut((Join-Path $desktop 'Switch Audio.lnk'))
$lnk.TargetPath = $pythonw
$lnk.Arguments = '-m aswitch toggle'
$lnk.WorkingDirectory = $root
if (Test-Path $ico) { $lnk.IconLocation = "$ico,0" }
$lnk.Description = 'Switch default audio device (headset <-> speaker)'
$lnk.Save()

# 2) command line window with live status
$lnk = $ws.CreateShortcut((Join-Path $desktop 'audioswitch Console.lnk'))
$lnk.TargetPath = Join-Path $root 'aswitch-shell.cmd'
$lnk.WorkingDirectory = $root
if (Test-Path $ico) { $lnk.IconLocation = "$ico,0" }
$lnk.Description = 'audioswitch command line'
$lnk.Save()

Get-ChildItem $desktop -Filter '*.lnk' | Where-Object { $_.Name -match 'Audio|audioswitch' } |
    Select-Object Name, FullName | Format-Table -AutoSize
