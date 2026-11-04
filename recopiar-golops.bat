@echo off
setlocal
title LineOps - Trazer os dados do GolOps de novo
cd /d "%~dp0"
rem Use se voce registrou voos no GolOps antigo depois que a LineOps copiou os dados dele.
rem Feche a LineOps antes. A pasta atual da LineOps nao e apagada (vai para dados-substituidos-... ao lado).
call "%~dp0_ambiente.bat" venv requirements.txt || (pause & exit /b 1)
"%VPY%" -m lineops recopiar-golops
echo.
pause
