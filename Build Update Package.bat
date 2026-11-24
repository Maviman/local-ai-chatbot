@echo off
rem Builds a distributable update package (a .zip) containing this app's
rem code - never data\ (your conversations/settings), models\ (huge GGUF
rem files), runtime\ (the interpreter), or the engines in bin\ (engine
rem binaries and CUDA libraries, a separate, much rarer upgrade path).
rem The one exception is a handful of small Microsoft Visual C++ runtime
rem DLLs from bin\ and bin\sd\ (about 1 MB): a PC without that runtime cannot
rem start the model server or the image server at all - Windows ends the
rem program without a word - so those travel with updates.
rem Copy the resulting zip to another installation of this same app and
rem extract it over that folder (allow overwrite) to update it - no git
rem needed (this project has none).
rem
rem When tests\check_update_package.py is here, it checks the finished zip
rem against this folder (complete, identical, nothing extra, the code compiles
rem and starts) and a package that fails is deleted. Keep this file's line
rem ends as CR+LF: with bare LF, cmd can lose the :labels below whenever an
rem edit moves them across a 512-byte boundary.
cd /d "%~dp0"

echo Rebuilding launcher...
call "launcher_src\build.bat"
if errorlevel 1 goto :error
cd /d "%~dp0"

if not exist dist mkdir dist

set "STAGE=%TEMP%\PersonalTerminal-package-stage"
if exist "%STAGE%" rmdir /s /q "%STAGE%"
mkdir "%STAGE%"

echo Recording the known-good file set (app\diagnose_manifest.json, what Diagnose.bat checks a copy against)...
"runtime\python\python.exe" -B "app\diagnose.py" --make-manifest
if errorlevel 1 goto :error

echo Staging app code...
robocopy app "%STAGE%\app" /E /XD __pycache__ /NFL /NDL /NJH /NJS /NP
if %ERRORLEVEL% GEQ 8 goto :error

robocopy launcher_src "%STAGE%\launcher_src" /E /XD __pycache__ /XF "_icon_tmp_*.png" /NFL /NDL /NJH /NJS /NP
if %ERRORLEVEL% GEQ 8 goto :error

rem The small Visual C++ runtime DLLs (see the top). Only these, and only the ones that are here.
if exist "bin\vcruntime140.dll" robocopy bin "%STAGE%\bin" vcruntime140.dll vcruntime140_1.dll msvcp140.dll /NFL /NDL /NJH /NJS /NP
if %ERRORLEVEL% GEQ 8 goto :error
if exist "bin\sd\vcruntime140.dll" robocopy bin\sd "%STAGE%\bin\sd" vcruntime140.dll vcruntime140_1.dll msvcp140.dll vcomp140.dll msvcp140_codecvt_ids.dll /NFL /NDL /NJH /NJS /NP
if %ERRORLEVEL% GEQ 8 goto :error

copy /y "Local AI ChatBot.exe" "%STAGE%\" >nul
if errorlevel 1 goto :error
copy /y "Start Chatbot.bat" "%STAGE%\" >nul
if errorlevel 1 goto :error
copy /y "Stop Chatbot.bat" "%STAGE%\" >nul
if errorlevel 1 goto :error
copy /y "Diagnose.bat" "%STAGE%\" >nul
if errorlevel 1 goto :error
copy /y "README.md" "%STAGE%\" >nul
if errorlevel 1 goto :error

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmm"') do set "STAMP=%%i"

(
echo This is an update package for Personal Terminal ^(formerly Local AI ChatBot^).
echo Built from a snapshot taken %STAMP%.
echo.
echo To apply: close the target installation first ^(run its Stop Chatbot.bat,
echo or use the in-app power button^), then extract this zip's contents
echo directly into the target installation's folder, allowing overwrite.
echo To be able to go back, copy the target's app folder somewhere first.
echo.
echo This touches app code ^(app\, launcher_src\, the .exe, the three .bat files,
echo and README.md^) and, under bin\ and bin\sd\, a few small Microsoft Visual
echo C++ runtime DLLs ^(vcruntime140, vcruntime140_1, msvcp140, vcomp140,
echo msvcp140_codecvt_ids^) that a PC without that runtime needs before the
echo model server or the image server can start at all. It never includes or
echo touches data\, models\ or runtime\, or the engines in bin\ ^(llama-server,
echo sd-server, the CUDA libraries^), so conversations, settings, downloaded
echo models, and engine binaries are untouched either way.
echo.
echo An older GPU, such as a Tesla P40, needs the CUDA 12 engine build. It is
echo NOT in this package ^(it is 1.4 GB^): copy the folder bin\engines\cuda12 from
echo the PC this package was built on into the target's bin\engines\ by hand.
echo.
echo If the app does not start afterwards, run Diagnose.bat in the target folder and
echo send the report it writes ^(data\diagnostics-...txt^). It checks every file of the
echo installation against the copy this package was built from, so a file that was
echo not copied, cut short or damaged is named.
) > "%STAGE%\HOW_TO_UPDATE.txt"

set "ZIPNAME=dist\PersonalTerminal-update-%STAMP%.zip"
if exist "%ZIPNAME%" del /f /q "%ZIPNAME%"

echo Compressing...
powershell -NoProfile -Command "Compress-Archive -Path '%STAGE%\*' -DestinationPath '%ZIPNAME%'"
if errorlevel 1 goto :error

rmdir /s /q "%STAGE%"

if exist "tests\check_update_package.py" goto :verify
echo tests\check_update_package.py is not here - the package was NOT verified.
goto :verified

:verify
echo Verifying the package...
"runtime\python\python.exe" -B "tests\check_update_package.py" "%ZIPNAME%"
if errorlevel 1 goto :rejected

:verified
echo.
echo Done: %ZIPNAME%
for %%f in ("%ZIPNAME%") do echo Size: %%~zf bytes
goto :success

:rejected
del /f /q "%ZIPNAME%"
echo.
echo The package failed its check and was deleted - see the list above.
goto :error

:error
echo.
echo Build failed.
pause
exit /b 1

:success
pause
