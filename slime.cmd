@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" goto help
if /i "%~1"=="help" goto help
if /i "%~1"=="-h" goto help
if /i "%~1"=="--help" goto help
if /i "%~1"=="up" goto up
if /i "%~1"=="down" goto compose
if /i "%~1"=="restart" goto compose
if /i "%~1"=="logs" goto compose
if /i "%~1"=="ps" goto compose
if /i "%~1"=="pull" goto image
if /i "%~1"=="build" goto image
if /i "%~1"=="ui" goto ui
if /i "%~1"=="shell" goto shell

docker compose exec -T api slime %*
exit /b %ERRORLEVEL%

:up
if not exist ".env" (
  copy /y ".env.example" ".env" >nul
  echo Created .env. Fill in the model provider values, then run slime up again.
  exit /b 2
)
docker compose up -d --remove-orphans
exit /b %ERRORLEVEL%

:compose
docker compose %*
exit /b %ERRORLEVEL%

:image
docker compose %*
exit /b %ERRORLEVEL%

:ui
if not defined SLIME_BIND_ADDRESS set "SLIME_BIND_ADDRESS=127.0.0.1"
if not defined SLIME_PORT set "SLIME_PORT=8000"
start "" "http://%SLIME_BIND_ADDRESS%:%SLIME_PORT%/"
exit /b 0

:shell
docker compose exec api sh
exit /b %ERRORLEVEL%

:help
echo Usage: slime COMMAND [ARGS...]
echo.
echo Compose commands:
echo   up       Start API and dispatcher in the background
echo   down     Stop services and keep persistent data
echo   restart  Restart running services
echo   pull     Pull application and Worker images
echo   build    Build application and Worker images locally
echo   logs     Show service logs
echo   ps       Show service status
echo   ui       Open the local web interface
echo   shell    Open a shell in the API container
echo.
echo All other commands are passed to the Slime CLI in the API container.
exit /b 0
