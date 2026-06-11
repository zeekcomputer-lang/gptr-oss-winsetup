@echo off
REM BGE 임베딩 서버 기동 (포그라운드, thin wrapper)
cd /d "%~dp0\.."

python tools\launch.py bge
if errorlevel 1 goto FAIL
goto END

:FAIL
echo [ERROR] BGE 서버 기동 실패. 셋업 여부를 확인하세요: windows\setup.bat
exit /b 1

:END
