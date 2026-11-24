@echo off
rem Rebuilds app_icon.ico and "Local AI ChatBot.exe" from source.
rem Uses only tools already on Windows (bundled tkinter + the .NET Framework's
rem csc.exe) - no PyInstaller, no pip installs, nothing downloaded.
cd /d "%~dp0"

set CSC="%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist %CSC% set CSC="%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"

echo Regenerating icon...
"..\runtime\python\python.exe" make_ico.py
if errorlevel 1 goto :error

echo Compiling launcher...
%CSC% /nologo /target:winexe /platform:anycpu ^
    /out:"..\Local AI ChatBot.exe" ^
    /win32icon:"..\app\ui\images\app_icon.ico" ^
    /reference:System.Windows.Forms.dll ^
    Launcher.cs
if errorlevel 1 goto :error

echo Done - "Local AI ChatBot.exe" is in the project root.
goto :eof

:error
echo Build failed.
exit /b 1
