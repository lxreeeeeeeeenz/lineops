@echo off
setlocal
title LineOps - Instalacao
cd /d "%~dp0"
echo.
echo  LineOps - preparar o ambiente
echo  ============================
echo  Nao precisa rodar a cada versao nova: o iniciar.bat ja faz isso sozinho,
echo  e so instala algo quando a versao nova trouxer pacote novo.
echo.
call "%~dp0_ambiente.bat" venv requirements.txt || goto :erro
"%VPY%" -c "import fastapi, uvicorn, lineops.app; print(' ok')" || goto :erro
echo.
echo  Pronto. De dois cliques em iniciar.bat.
echo.
pause
exit /b 0
:erro
echo.
echo  Falhou. Causas comuns: sem internet, antivirus bloqueando, pasta sem permissao.
pause
exit /b 1
