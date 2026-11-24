@echo off
rem Personal Terminal - writes a diagnostics report (data\diagnostics-<stamp>.txt) for when the app does not start on this PC.
rem Changes nothing on the PC. Takes a few minutes: it also starts the model server once, on a spare port, to see whether it
rem works here. Add --no-start-test to skip that part, or --quick for the file, Python and process checks only.
cd /d "%~dp0"
if not exist "%~dp0runtime\python\python.exe" (
  echo The bundled Python is not here: "%~dp0runtime\python\python.exe" is missing.
  echo This folder is not a complete installation. It needs the runtime, bin and models folders of a working copy -
  echo an update package on its own ^(app code only^) cannot run.
  pause
  exit /b 1
)
"%~dp0runtime\python\python.exe" -B "%~dp0app\diagnose.py" %*
echo.
echo Done. Send the report named above ^(data\diagnostics-...txt^).
pause
