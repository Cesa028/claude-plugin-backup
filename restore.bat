@echo off
REM Trascina un file claude-plugins-backup-*.zip su questo file per ripristinarlo.
if "%~1"=="" (
  echo Trascina un file .zip di backup su questo file.
  pause
  exit /b 1
)
python "%~dp0claude_plugin_backup.py" restore "%~1"
echo.
pause
