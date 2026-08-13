@echo off
setlocal EnableExtensions

for %%I in ("%~dp0.") do set "ROOT_DIR=%%~fI"
set "BUILD_DIR=%ROOT_DIR%\build\install-windows"
set "INSTALL_DIR=%LOCALAPPDATA%\Programs\Pomodorough"
set "BUILT_EXE=%BUILD_DIR%\dist\Pomodorough.exe"
set "INSTALLED_EXE=%INSTALL_DIR%\Pomodorough.exe"
set "PYINSTALLER_VERSION=pyinstaller>=6.14,<7"

if not defined LOCALAPPDATA (
    echo LOCALAPPDATA is not set. 1>&2
    exit /b 1
)

if defined POMODOROUGH_INSTALL_DIR set "INSTALL_DIR=%POMODOROUGH_INSTALL_DIR%"
set "INSTALLED_EXE=%INSTALL_DIR%\Pomodorough.exe"

if not exist "%BUILD_DIR%" mkdir "%BUILD_DIR%"
if errorlevel 1 exit /b 1

where uv >nul 2>&1
if not errorlevel 1 goto build_with_uv

set "VENV=%BUILD_DIR%\venv"
if exist "%VENV%\Scripts\python.exe" goto install_with_pip

if defined POMODOROUGH_PYTHON (
    "%POMODOROUGH_PYTHON%" -m venv "%VENV%"
) else (
    py -3.11 -m venv "%VENV%" 2>nul || python -m venv "%VENV%"
)
if errorlevel 1 exit /b 1

:install_with_pip
"%VENV%\Scripts\python.exe" -m pip install --upgrade "%ROOT_DIR%[iroh]" "%PYINSTALLER_VERSION%"
if errorlevel 1 exit /b 1
"%VENV%\Scripts\python.exe" -m PyInstaller --clean --noconfirm --onefile --windowed --name Pomodorough --collect-data pomodorough --hidden-import iroh --distpath "%BUILD_DIR%\dist" --workpath "%BUILD_DIR%\work" --specpath "%BUILD_DIR%" "%ROOT_DIR%\deploy\windows\launcher.py"
if errorlevel 1 exit /b 1
goto install

:build_with_uv
uv run --project "%ROOT_DIR%" --extra iroh --with "%PYINSTALLER_VERSION%" python -m PyInstaller --clean --noconfirm --onefile --windowed --name Pomodorough --collect-data pomodorough --hidden-import iroh --distpath "%BUILD_DIR%\dist" --workpath "%BUILD_DIR%\work" --specpath "%BUILD_DIR%" "%ROOT_DIR%\deploy\windows\launcher.py"
if errorlevel 1 exit /b 1

:install
if not exist "%BUILT_EXE%" (
    echo Built executable not found: "%BUILT_EXE%" 1>&2
    exit /b 1
)
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
if errorlevel 1 exit /b 1
copy /Y "%BUILT_EXE%" "%INSTALLED_EXE%" >nul
if errorlevel 1 exit /b 1

if defined APPDATA (
    set "SHORTCUT_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs"
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$shell = New-Object -ComObject WScript.Shell; $shortcut = $shell.CreateShortcut([IO.Path]::Combine($env:SHORTCUT_DIR, 'Pomodorough.lnk')); $shortcut.TargetPath = $env:INSTALLED_EXE; $shortcut.WorkingDirectory = $env:INSTALL_DIR; $shortcut.Save()"
    if errorlevel 1 exit /b 1
)

echo Installed Pomodorough to "%INSTALLED_EXE%"
