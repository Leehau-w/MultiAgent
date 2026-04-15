@echo off
setlocal

set ROOT_DIR=%~dp0
set BACKEND_DIR=%ROOT_DIR%backend
set FRONTEND_DIR=%ROOT_DIR%frontend

:: ---- Configurable ports (change here if default ports are taken) ----
if not defined BACKEND_PORT set BACKEND_PORT=8000
if not defined FRONTEND_PORT set FRONTEND_PORT=5173

echo.
echo  ============================================
echo   MultiAgent Studio
echo  ============================================
echo.

:: ---- Check Python ----
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.10+ and add it to PATH.
    pause
    exit /b 1
)

:: ---- Check Node ----
node --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Node.js not found. Please install Node.js 18+ and add it to PATH.
    pause
    exit /b 1
)

:: ---- Backend setup ----
echo [Backend] Setting up Python environment...
cd /d "%BACKEND_DIR%"

if not exist ".venv\Scripts\python.exe" (
    echo [Backend] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create venv.
        pause
        exit /b 1
    )
)

echo [Backend] Installing dependencies...
call .venv\Scripts\pip.exe install -q -r requirements.txt
if errorlevel 1 (
    echo [ERROR] pip install failed. See above for details.
    pause
    exit /b 1
)

:: ---- Frontend setup ----
echo [Frontend] Installing dependencies...
cd /d "%FRONTEND_DIR%"
call npm install --silent
if errorlevel 1 (
    echo [ERROR] npm install failed.
    pause
    exit /b 1
)

:: ---- Start services ----
:: ---- Kill leftover processes on these ports ----
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%BACKEND_PORT% " ^| findstr "LISTENING"') do (
    echo [Cleanup] Killing leftover process on port %BACKEND_PORT% (PID %%p)
    taskkill /pid %%p /f >nul 2>&1
)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%FRONTEND_PORT% " ^| findstr "LISTENING"') do (
    echo [Cleanup] Killing leftover process on port %FRONTEND_PORT% (PID %%p)
    taskkill /pid %%p /f >nul 2>&1
)

echo.
echo [Starting] Backend on http://localhost:%BACKEND_PORT% ...
cd /d "%BACKEND_DIR%"
start "MultiAgent-Backend" cmd /k ".venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port %BACKEND_PORT% --reload"

echo [Starting] Frontend on http://localhost:%FRONTEND_PORT% ...
cd /d "%FRONTEND_DIR%"
start "MultiAgent-Frontend" cmd /k "npx vite --port %FRONTEND_PORT%"

:: ---- Wait a moment then open browser ----
timeout /t 3 /nobreak >nul
start http://localhost:%FRONTEND_PORT%

echo.
echo  ============================================
echo   Frontend: http://localhost:%FRONTEND_PORT%
echo   Backend:  http://localhost:%BACKEND_PORT%
echo   API Docs: http://localhost:%BACKEND_PORT%/docs
echo  ============================================
echo.
echo   Press any key to stop all services...
pause >nul

for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%BACKEND_PORT% " ^| findstr "LISTENING"') do taskkill /pid %%p /f >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%FRONTEND_PORT% " ^| findstr "LISTENING"') do taskkill /pid %%p /f >nul 2>&1
echo [MultiAgent Studio] Stopped.
pause
