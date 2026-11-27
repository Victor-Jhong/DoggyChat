@echo off
setlocal
cd /d "%~dp0"

set "BUILD_ROOT=%TEMP%\DoggyChatBuild-%RANDOM%-%RANDOM%"
set "PYTHON_HOME=%BUILD_ROOT%\python"
set "PYTHON_ARCHIVE=%BUILD_ROOT%\python-3.10.15-windows-x64.tar.gz"
set "BUILD_ROOT_CREATED="

echo Setting up a temporary Python 3.10 build environment...
mkdir "%BUILD_ROOT%"
if errorlevel 1 goto failed
set "BUILD_ROOT_CREATED=1"

echo Downloading portable Python 3.10.15 from GitHub...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference = 'SilentlyContinue'; Invoke-WebRequest -Uri 'https://github.com/astral-sh/python-build-standalone/releases/download/20241016/cpython-3.10.15%2B20241016-x86_64-pc-windows-msvc-install_only.tar.gz' -OutFile '%PYTHON_ARCHIVE%'"
if errorlevel 1 goto failed

tar -xzf "%PYTHON_ARCHIVE%" -C "%BUILD_ROOT%"
if errorlevel 1 goto failed

echo Installing DoggyChat dependencies and PyInstaller...
"%PYTHON_HOME%\python.exe" -m pip install --no-warn-script-location "pyinstaller==6.22.3" -r requirements.txt
if errorlevel 1 goto failed

echo Building DoggyChat.exe...
"%PYTHON_HOME%\python.exe" -m PyInstaller --clean --noconfirm --onefile --console --name DoggyChat --distpath dist --workpath "%BUILD_ROOT%\work" --specpath "%BUILD_ROOT%" launcher.py
if errorlevel 1 goto failed

rmdir /s /q "%BUILD_ROOT%"
echo.
echo Build complete: dist\DoggyChat.exe
pause
exit /b 0

:failed
echo.
echo Build failed. Check GitHub access, PowerShell, and the Windows tar command.
if defined BUILD_ROOT_CREATED rmdir /s /q "%BUILD_ROOT%" 2>nul
pause
exit /b 1
