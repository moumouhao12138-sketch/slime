@echo off
setlocal
for %%I in ("%~dp0..") do set "SLIME_PROJECT_ROOT=%%~fI"
if defined PYTHONPATH (
    set "PYTHONPATH=%SLIME_PROJECT_ROOT%\src;%PYTHONPATH%"
) else (
    set "PYTHONPATH=%SLIME_PROJECT_ROOT%\src"
)
if defined SLIME_PYTHON (
    "%SLIME_PYTHON%" -m slime_cairn.cli %*
) else (
    python -m slime_cairn.cli %*
)
set "SLIME_EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %SLIME_EXIT_CODE%
