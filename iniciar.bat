@echo off
setlocal
title LineOps - Servidor
cd /d "%~dp0"
rem Uso:  iniciar.bat           servidor + navegador
rem       iniciar.bat rede      libera o acesso pela rede de casa (PC do simulador, celular)
rem       iniciar.bat app       abre como app (janela propria + bandeja), igual ao instalado
call "%~dp0_ambiente.bat" venv requirements.txt || goto :erro
if not defined LINEOPS_PORT set "LINEOPS_PORT=8787"

if /i "%~1"=="app" (
  call "%~dp0_ambiente.bat" venv requirements.txt requirements-desktop.txt || goto :erro
  start "" "%VENV%\Scripts\pythonw.exe" -m lineops desktop --port %LINEOPS_PORT%
  exit /b 0
)

set "EXTRA="
if /i "%~1"=="rede" (
  set "EXTRA=--lan"
  echo  MODO REDE LOCAL: outros aparelhos da sua casa poderao acessar. Use uma senha forte.
  echo  O Windows pode pedir permissao no firewall: libere para Redes Privadas.
  echo.
)
echo  LineOps rodando em http://127.0.0.1:%LINEOPS_PORT%
echo  Dados em %LOCALAPPDATA%\LineOps\dados (os mesmos para todas as versoes).
echo  Feche esta janela (ou Ctrl+C) para parar o servidor.
echo.
rem o navegador abre sozinho quando a LineOps estiver no ar (se o GolOps antigo estiver aberto, ele e fechado antes)
"%VPY%" -m lineops serve --port %LINEOPS_PORT% --open %EXTRA%
echo.
echo  O servidor parou. Se foi erro, leia a mensagem acima
echo  (porta ocupada? o GolOps antigo aberto na bandeja? outra janela do LineOps?).
pause
exit /b 0
:erro
echo.
echo  Nao foi possivel preparar o ambiente. Leia a mensagem acima.
pause
exit /b 1
