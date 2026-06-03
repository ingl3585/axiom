@echo off
setlocal

set "AXIOM_ROOT=%~dp0"
set "BUNDLED_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if exist "%BUNDLED_PYTHON%" (
  set "PYTHON_EXE=%BUNDLED_PYTHON%"
) else (
  set "PYTHON_EXE=python"
)

if defined PYTHONPATH (
  set "PYTHONPATH=%AXIOM_ROOT%src;%PYTHONPATH%"
) else (
  set "PYTHONPATH=%AXIOM_ROOT%src"
)

"%PYTHON_EXE%" -m axiom %*
exit /b %ERRORLEVEL%

