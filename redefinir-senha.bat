@echo off
setlocal
title LineOps - Redefinir senha
cd /d "%~dp0"
rem Troca a senha esquecida de um usuario desta instalacao. Nada da carreira e apagado.
rem No app instalado, use "Esqueci a senha" na tela de entrada (mesmo efeito, sem precisar deste arquivo).
call "%~dp0_ambiente.bat" venv requirements.txt || (pause & exit /b 1)
"%VPY%" -m lineops redefinir-senha %*
echo.
pause
