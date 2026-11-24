@echo off
rem Personal Terminal - makes a complete, runnable COPY of this installation for another PC or drive.
rem It asks where to put it, whether to take the image generator and any models along, shows the sizes, and then copies.
rem Nothing in this folder is changed, moved or deleted; the copy is checked file by file against the original before it gets
rem its final name. Not copied: data\ (your chats and settings), outputs\ (your images), design\, dist\, backups.
rem Options can be given on the command line (Make Clean Copy.bat "D:\Personal Terminal" --models none --yes);
rem see the top of tests\tools\make_clean_copy.py. Keep this file's line ends as CR+LF.
cd /d "%~dp0"
if not exist "%~dp0runtime\python\python.exe" (
  echo The bundled Python is not here: "%~dp0runtime\python\python.exe" is missing.
  echo This folder is not a complete installation, so there is nothing to copy.
  pause
  exit /b 1
)
if not exist "%~dp0tests\tools\make_clean_copy.py" (
  echo tests\tools\make_clean_copy.py is missing from this folder.
  pause
  exit /b 1
)
"%~dp0runtime\python\python.exe" -B "%~dp0tests\tools\make_clean_copy.py" %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
