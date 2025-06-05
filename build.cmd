@echo off

cd /D "%~dp0"

call build_backend.cmd
if "%errorlevel%" neq "0" (
    echo Failed to build the project
    exit /B %errorlevel%
)

call build_frontend.cmd
if "%errorlevel%" neq "0" (
    echo Failed to build the project
    exit /B %errorlevel%
)
