@echo off
setlocal
title LineOps - Restaurar backup
cd /d "%~dp0"
rem Uso: arraste um backup .zip para cima deste arquivo, ou de dois cliques para restaurar o mais recente.
rem Feche a LineOps antes. O estado atual vira um backup "antes-de-restaurar" (da para voltar atras).
call "%~dp0_ambiente.bat" venv requirements.txt || (pause & exit /b 1)
"%VPY%" -m lineops restore %1
echo.
pause
