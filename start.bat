@echo off
setlocal enabledelayedexpansion
pushd "%~dp0"

REM --- вибір python (якщо є venv поруч, буде краще)
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

REM --- перевірка, що zstandard встановлений
%PY% -c "import zstandard" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python module "zstandard" not found.
  echo Run: %PY% -m pip install zstandard
  pause
  exit /b 1
)

REM --- папка для результатів
if not exist "dds_out" mkdir "dds_out"

echo Converting all .tgv in: %CD%
echo.

for %%F in (*.tgv) do (
  echo [%%F] -> dds_out\%%~nF.dds
  %PY% tgv_to_dds.py "%%F" "dds_out\%%~nF.dds"
)

echo.
echo Done! DDS files are in: dds_out
pause
popd
