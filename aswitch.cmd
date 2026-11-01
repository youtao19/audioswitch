@echo off
rem aswitch launcher: aswitch list / use headset / daemon ...
setlocal
pushd "%~dp0"
python -m aswitch %*
popd
