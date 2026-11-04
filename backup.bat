@echo off
setlocal
title LineOps - Backup
cd /d "%~dp0"
call "%~dp0_ambiente.bat" venv requirements.txt || (pause & exit /b 1)
"%VPY%" -m lineops backup
echo.
echo  Backup criado em %LOCALAPPDATA%\LineOps\dados\backups. Copie para outro lugar (pen drive, nuvem).
echo.
pause
