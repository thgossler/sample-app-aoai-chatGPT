@echo off

cd /D "%~dp0"

call build.cmd
if "%errorlevel%" neq "0" (
    echo Failed to build the project
    exit /B %errorlevel%
)

cd /D "%~dp0"

echo.    
echo Starting backend    
echo.    
dotenv
start http://127.0.0.1:8081
call python -m uvicorn app:app  --port 8081 --reload
if "%errorlevel%" neq "0" (    
    echo Failed to start backend    
    exit /B %errorlevel%    
) 
