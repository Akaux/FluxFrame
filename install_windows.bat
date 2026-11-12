@echo off
setlocal
cd /d "%~dp0"
echo === FluxFrame Windows build ===

where py >nul 2>&1
if %errorlevel%==0 (set PY=py -3) else (set PY=python)

if not exist .venv (
    echo [1/4] criando venv...
    %PY% -m venv .venv || goto :err
)
call .venv\Scripts\activate.bat

echo [2/4] aplicando patch Windows...
python windows_patch.py || goto :err

echo [3/4] instalando dependencias...
python -m pip install --upgrade pip || goto :err
pip install -r requirements_windows.txt || goto :err

echo [4/4] gerando exe...
python build_exe.py || goto :err

echo.
echo ================================================
echo  PRONTO: dist\framegen.exe
echo ================================================
pause
exit /b 0

:err
echo.
echo ERRO. Veja as mensagens acima.
pause
exit /b 1