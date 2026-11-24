@echo off
rem Personal Terminal launcher - starts the model + native UI.
rem Every path below is absolute (built from this file's own folder), so this also works from a network path
rem (\\server\share\...), where cmd cannot make the folder current: that step is then simply skipped, quietly.
cd /d "%~dp0" >nul 2>&1
start "Personal Terminal" "%~dp0runtime\python\pythonw.exe" "%~dp0app\desktop_main.py"
