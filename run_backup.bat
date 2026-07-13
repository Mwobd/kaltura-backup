@echo off
setlocal
set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"
if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
) else (
    echo Virtual environment not found at .venv\Scripts\activate.bat
    exit /b 1
)
python -m kaltura_backup.cli --config config.ini
exit /b %ERRORLEVEL%
