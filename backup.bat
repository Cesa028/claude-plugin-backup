@echo off
REM Crea subito un backup dei plugin di Claude Code (senza interfaccia grafica).
python "%~dp0claude_plugin_backup.py" backup
echo.
pause
