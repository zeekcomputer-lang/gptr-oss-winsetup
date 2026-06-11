@echo off
REM 환경 점검 (thin wrapper)
cd /d "%~dp0\.."
python tools\launch.py doctor
