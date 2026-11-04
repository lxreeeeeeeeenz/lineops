@echo off
setlocal
title LineOps - gerar instalador
cd /d "%~dp0\.."
echo.
echo  LineOps - gerar o instalador (LineOps-Setup-X.Y.Z.exe)
echo  =====================================================
echo  Precisa: Python 3.10+ e, para o instalador, Inno Setup 6 (jrsoftware.org/isinfo.php).
echo.

echo [1/4] Ambiente de build em %LOCALAPPDATA%\LineOps\venv-build (reaproveitado entre versoes) ...
call "%~dp0..\_ambiente.bat" venv-build requirements.txt requirements-dev.txt packaging\requirements-build.txt || goto :erro

for /f "usebackq delims=" %%v in (`"%VPY%" packaging\set_version.py`) do set "VER=%%v"
if not defined VER goto :erro
echo        versao %VER%

echo [2/4] Testes ...
"%VPY%" -m pytest tests -q -p no:cacheprovider -rf || (echo  Testes falharam: o instalador NAO foi gerado. Mande a lista FAILED acima. & goto :erro)

echo [3/4] Empacotando (PyInstaller) ...
"%VPY%" -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\lineops.spec || goto :erro
"%VPY%" -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\acars.spec || goto :erro

echo [4/4] Instalador (Inno Setup) ...
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo  Inno Setup 6 nao encontrado. O app ja esta em dist\LineOps\LineOps.exe e roda sem instalar.
  echo  Para o instalador, instale o Inno Setup 6 e rode este arquivo de novo.
  pause & exit /b 0
)
"%ISCC%" /DAppVersion=%VER% packaging\installer.iss || goto :erro
echo.
echo  Pronto: dist\LineOps-Setup-%VER%.exe
echo  Sem assinatura digital, o Windows pode mostrar "O Windows protegeu o computador":
echo  clique em "Mais informacoes" e "Executar assim mesmo".
echo.
pause
exit /b 0

:erro
echo.
echo  Falhou. Leia a mensagem acima.
pause
exit /b 1
