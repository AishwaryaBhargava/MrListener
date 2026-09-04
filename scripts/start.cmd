@echo off
REM ---------------------------------------------------------------------------
REM  MrListener - double-click launcher.
REM
REM  Builds the frontend, then runs the one production process: uvicorn on 8000
REM  serving both the API and the built UI. The browser opens on its own.
REM  Close this window (or press Ctrl+C) to stop the app.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0.."

if not exist "backend\.venv\Scripts\python.exe" (
    echo Backend virtual environment not found.
    echo Run  npm run setup  in this folder first.
    pause
    exit /b 1
)
if not exist "frontend\node_modules" (
    echo Frontend dependencies not found.
    echo Run  npm run setup  in this folder first.
    pause
    exit /b 1
)

echo Building the frontend...
call npm run build
if errorlevel 1 (
    echo.
    echo The build failed. See the messages above.
    pause
    exit /b 1
)

echo.
echo MrListener is starting on http://localhost:8000
echo Close this window to stop it.
echo.

start "" http://localhost:8000
cd backend
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000

endlocal
