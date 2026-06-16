@echo off
REM 용어사전(JSON) 점검/미리보기 (thin wrapper)
cd /d "%~dp0\.."
python tools\launch.py glossary %*
