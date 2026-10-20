@echo off
cd /d "%~dp0"
python -m label_porter
if errorlevel 1 pause
