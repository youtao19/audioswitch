' 静默启动 audioswitch 守护进程（无控制台窗口）
' 用法：双击运行，或把本文件的快捷方式放进 shell:startup 实现开机自启
Dim fso, sh, dir
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir

' 显式指定 pythonw.exe（不依赖 PATH）
Dim pythonw
pythonw = "C:\Users\youtao\AppData\Local\Programs\Python\Python313\pythonw.exe"
If Not fso.FileExists(pythonw) Then pythonw = "pythonw.exe"

sh.Run """" & pythonw & """ -m aswitch daemon", 0, False
