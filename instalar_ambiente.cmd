@echo off
setlocal EnableExtensions DisableDelayedExpansion
title SAP Explorer - Preparar ambiente

rem Pode ser executado por duplo clique ou a partir de qualquer pasta.
if /i "%~1"=="--no-pause" set "SAP_EXPLORER_NO_PAUSE=1"
pushd "%~dp0"
if errorlevel 1 goto :erro_pasta
set "VENV_PYTHON=%CD%\venv\Scripts\python.exe"

echo [1/4] Verificando Python 3.10 ou superior...
if exist "%VENV_PYTHON%" goto :validar_venv
if exist "venv\" goto :erro_venv

py -3 -c "import sys, venv, tkinter; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 goto :usar_py
python -c "import sys, venv, tkinter; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 goto :usar_python
python3 -c "import sys, venv, tkinter; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 goto :usar_python3
echo ERRO: Python 3.10+ com venv e Tcl/Tk nao foi encontrado.
echo Instale Python para Windows em https://www.python.org/downloads/windows/
echo Inclua o componente Tcl/Tk e a opcao Add python.exe to PATH.
goto :falha

:usar_py
set "BASE_PYTHON=py -3"
goto :criar_venv
:usar_python
set "BASE_PYTHON=python"
goto :criar_venv
:usar_python3
set "BASE_PYTHON=python3"

:criar_venv
echo [2/4] Criando o ambiente virtual em venv...
%BASE_PYTHON% -m venv "venv"
if errorlevel 1 goto :erro_venv
goto :validar_python

:validar_venv
echo [2/4] Reutilizando o ambiente virtual existente...
:validar_python
"%VENV_PYTHON%" -c "import sys, tkinter; assert sys.version_info >= (3, 10), 'Python 3.10+ necessario'; assert sys.prefix != sys.base_prefix, 'Python fora de um venv'; print('Python:', sys.version.split()[0]); print('Executavel:', sys.executable)"
if errorlevel 1 goto :erro_venv
if not exist "requirements.txt" goto :erro_requirements

echo [3/4] Instalando as dependencias no ambiente virtual...
"%VENV_PYTHON%" -m pip --version >nul 2>&1
if not errorlevel 1 goto :instalar_libs
"%VENV_PYTHON%" -m ensurepip --upgrade
if errorlevel 1 goto :erro_instalacao
:instalar_libs
"%VENV_PYTHON%" -m pip install --disable-pip-version-check -r "requirements.txt"
if errorlevel 1 goto :erro_instalacao

echo [4/4] Conferindo as dependencias...
"%VENV_PYTHON%" -m pip check
if errorlevel 1 goto :erro_instalacao
"%VENV_PYTHON%" -c "import tkinter, pythoncom, win32com.client; print('Tkinter e SAP COM disponiveis.')"
if errorlevel 1 goto :erro_instalacao
echo.
echo Ambiente preparado. Execute iniciar_aplicacao.cmd para abrir o gravador.
popd
if not defined SAP_EXPLORER_NO_PAUSE pause
exit /b 0

:erro_requirements
echo ERRO: requirements.txt nao foi encontrado junto deste arquivo.
goto :falha
:erro_venv
echo ERRO: nao foi possivel criar ou validar o ambiente venv.
echo Confira o Python instalado e as permissoes desta pasta.
echo Se venv estiver danificado ou tiver sido copiado de outra maquina,
echo renomeie essa pasta e execute este instalador novamente.
goto :falha
:erro_instalacao
echo ERRO: falha na instalacao ou verificacao das dependencias.
echo Confira as mensagens acima e o acesso ao indice de pacotes configurado no pip.
goto :falha
:erro_pasta
echo ERRO: nao foi possivel acessar a pasta do SAP Explorer.
if not defined SAP_EXPLORER_NO_PAUSE pause
exit /b 1
:falha
popd
if not defined SAP_EXPLORER_NO_PAUSE pause
exit /b 1
