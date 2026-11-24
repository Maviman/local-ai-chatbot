@echo off
rem Force-stops the chatbot if it is stuck. Normal shutdown is the power
rem button inside the app. Only kills processes running from this folder.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like '%~dp0*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Personal Terminal stopped.
pause
