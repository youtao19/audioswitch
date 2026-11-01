@echo off
rem audioswitch console: shows current status then drops you into a shell.
rem Keep this file ASCII-only (cmd.exe reads .cmd as the OEM codepage).
setlocal
pushd "%~dp0"
title audioswitch
python -m aswitch status
echo.
python -m aswitch help
cmd /k
