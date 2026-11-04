@echo off
setlocal enabledelayedexpansion
title LineOps ACARS
cd /d "%~dp0"
echo.
echo  LineOps ACARS (simulacao - sem afiliacao com a GOL)
echo  ===================================================
echo.

set "PY="
where py >nul 2>&1
if not errorlevel 1 (
  py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
  where python >nul 2>&1
  if not errorlevel 1 (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
    if not errorlevel 1 set "PY=python"
  )
)

if not defined PY (
  echo  Python 3.10 ou mais novo nao foi encontrado neste PC.
  echo  Instale em https://www.python.org/downloads/ marcando "Add python.exe to PATH".
  start "" "https://www.python.org/downloads/"
  pause
  exit /b 1
)

set "SERVER=" & set "TOKEN=" & set "SIM="
if exist "acars.cfg" (
  for /f "usebackq tokens=1,* delims==" %%a in ("acars.cfg") do set "%%a=%%b"
)
if not defined SERVER goto :configurar
if not defined TOKEN goto :configurar
if not defined SIM goto :configurar
echo  Servidor: !SERVER!
echo  Simulador: !SIM!
echo.
set /p "MUDAR=Mudar configuracao? (s/N): "
if /i "!MUDAR!"=="s" goto :configurar
goto :voar

:configurar
echo.
echo  Endereco do servidor LineOps. Exemplo: http://192.168.0.10:8787
echo  (no PC do servidor, rode iniciar.bat rede; o IP aparece com o comando ipconfig)
set /p "SERVER=Servidor: "
echo.
echo  Token do ACARS: no LineOps, abra Ajustes e gere um token.
set /p "TOKEN=Token: "
echo.
echo  Simulador:  1 = X-Plane 12   2 = MSFS 2024   3 = Prepar3D v5
set /p "OP=Escolha (1/2/3): "
set "SIM=xplane"
if "!OP!"=="2" set "SIM=msfs"
if "!OP!"=="3" set "SIM=p3d"
> acars.cfg echo SERVER=!SERVER!
>> acars.cfg echo TOKEN=!TOKEN!
>> acars.cfg echo SIM=!SIM!
echo  Configuracao salva em acars.cfg (contem seu token: nao compartilhe).

:voar
echo.
echo  Voos disponiveis (reservados com aeronave):
echo.
%PY% -m lineops_acars --server "!SERVER!" --token "!TOKEN!" --sim !SIM! --list
if errorlevel 1 goto :fim
echo.
set /p "FID=ID do voo (ou A para abortar uma sessao aberta, ENTER para sair): "
if "!FID!"=="" goto :fim
if /i "!FID!"=="A" (
  set /p "FID=ID do voo a abortar: "
  %PY% -m lineops_acars --server "!SERVER!" --token "!TOKEN!" --sim !SIM! --flight !FID! --abort
  goto :fim
)
echo.
echo  Abra o simulador com o voo carregado. No X-Plane, deixe a rede liberada (porta UDP 49000).
echo  Ctrl+C sai e mantem a sessao aberta para retomar.
echo.
%PY% -m lineops_acars --server "!SERVER!" --token "!TOKEN!" --sim !SIM! --flight !FID!

:fim
echo.
pause
