@echo off
rem Personal Terminal - makes ONE .zip file that carries this installation to any other PC (the program, the bundled Python, the engines;
rem models only when asked for). It asks nothing but "Make the zip now?": by default it writes dist\PersonalTerminal-transport-<date>.zip
rem without models and with the image generator. Nothing in this folder is changed, moved or deleted; the zip is read back and checked
rem entry by entry before it gets its final name. Not included: data\ (your chats and settings), outputs\ (your images), design\, dist\, backups.
rem Options can be given on the command line (Make Transport Zip.bat "D:\Share\PT.zip" --models all --no-sd --yes);
rem see the top of tests\tools\make_transport_zip.py. Keep this file's line ends as CR+LF.
cd /d "%~dp0"
if not exist "%~dp0runtime\python\python.exe" (
  echo The bundled Python is not here: "%~dp0runtime\python\python.exe" is missing.
  echo This folder is not a complete installation, so there is nothing to pack.
  pause
  exit /b 1
)
if not exist "%~dp0tests\tools\make_transport_zip.py" (
  echo tests\tools\make_transport_zip.py is missing from this folder.
  pause
  exit /b 1
)
"%~dp0runtime\python\python.exe" -B "%~dp0tests\tools\make_transport_zip.py" %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
