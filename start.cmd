@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if exist ".venv\Scripts\python.exe" if exist ".venv\.dependencies-ready" goto ready
if exist ".venv\Scripts\python.exe" goto install
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.10+ is required. Install it from https://www.python.org/downloads/
  pause
  exit /b 1
)
python -m venv .venv
if errorlevel 1 goto failed
:install
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo ready>".venv\.dependencies-ready"
:ready
".venv\Scripts\python.exe" convert.py %*
if errorlevel 1 goto failed
if not "%~1"=="" pause
exit /b 0
:failed
echo Operation failed. See the error above.
pause
exit /b 1
