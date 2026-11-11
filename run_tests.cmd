@echo off
cd /d "%~dp0"
py -m pytest -q
pause
