@echo off
setlocal EnableExtensions DisableDelayedExpansion
title SAP Explorer - Gravador contextual

pushd "%~dp0"
if errorlevel 1 goto :erro_pasta
set "VENV_PYTHON=%CD%\venv\Scripts\python.exe"
if not exist "%VENV_PYTHON%" goto :erro_ambiente
if not exist "main.py" goto :erro_main
"%VENV_PYTHON%" -c "import sys, tkinter, pythoncom, win32com.client; assert sys.version_info >= (3, 10); assert sys.prefix != sys.base_prefix" >nul 2>&1
if errorlevel 1 goto :erro_ambiente

rem Sem argumentos, main.py abre a janela de selecao da sessao SAP.
rem Argumentos opcionais sao repassados ao CLI, por exemplo --help.
"%VENV_PYTHON%" "%CD%\main.py" %*
set "APP_EXIT_CODE=%ERRORLEVEL%"
popd
if "%APP_EXIT_CODE%"=="0" exit /b 0
echo.
echo A aplicacao encerrou com erro. Confira as mensagens acima.
if not defined SAP_EXPLORER_NO_PAUSE pause
exit /b %APP_EXIT_CODE%

:erro_ambiente
echo ERRO: ambiente virtual ausente, incompleto ou incompativel.
echo Execute instalar_ambiente.cmd nesta pasta antes de iniciar.
goto :falha
:erro_main
echo ERRO: main.py nao foi encontrado junto deste arquivo.
goto :falha
:erro_pasta
echo ERRO: nao foi possivel acessar a pasta do SAP Explorer.
if not defined SAP_EXPLORER_NO_PAUSE pause
exit /b 1
:falha
popd
if not defined SAP_EXPLORER_NO_PAUSE pause
exit /b 1
