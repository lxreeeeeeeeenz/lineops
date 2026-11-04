@echo off
rem Ambiente Python compartilhado por todas as versoes do LineOps, fora da pasta do projeto:
rem   %LOCALAPPDATA%\LineOps\<nome>   (venv para rodar, venv-build para gerar o instalador)
rem Extrair uma versao nova numa pasta nova NAO reinstala nada: so instala o que os requirements novos pedirem.
rem Uso: call "%~dp0_ambiente.bat" NOME req1.txt [req2.txt] [req3.txt]   ->  define VPY
set "VENV=%LOCALAPPDATA%\LineOps\%~1"
set "VPY=%VENV%\Scripts\python.exe"
if exist "%VPY%" goto :deps

set "PY="
where py >nul 2>&1 && py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1 && set "PY=python"
if not defined PY (
  echo  Python 3.10 ou mais novo nao foi encontrado.
  echo  Instale em https://www.python.org/downloads/ marcando "Add python.exe to PATH".
  start "" "https://www.python.org/downloads/"
  exit /b 1
)
echo  Criando o ambiente em %VENV% (so na primeira vez) ...
%PY% -m venv "%VENV%" || exit /b 1
"%VPY%" -m pip install --upgrade pip --disable-pip-version-check -q

:deps
"%VPY%" "%~dp0tools\ensure_env.py" %2 %3 %4 || exit /b 1
exit /b 0
