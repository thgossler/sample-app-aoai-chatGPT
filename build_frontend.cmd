@echo off

pushd "%~dp0"

echo.
echo Restoring frontend npm packages
echo.
cd frontend
call npm install
if "%errorlevel%" neq "0" (
    echo Failed to restore frontend npm packages
    popd
    exit /B %errorlevel%
)

echo.
echo Building frontend
echo.
call npm run build
if "%errorlevel%" neq "0" (
    echo Failed to build frontend
    popd
    exit /B %errorlevel%
)

popd
