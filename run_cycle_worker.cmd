@echo off
setlocal
cd /d "%~dp0"

set "PROVIDER_ARG="
if not "%~1"=="" set "PROVIDER_ARG=--provider %~1"

python main.py run-cycle %PROVIDER_ARG% 1>>runtime\web_run_stdout.log 2>>runtime\web_run_stderr.log
