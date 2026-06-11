@echo off
REM 리서치 실행 (thin wrapper). 사용: research.bat "질의" [옵션]
cd /d "%~dp0\.."

python tools\launch.py research %*
if errorlevel 1 goto FAIL
goto END

:FAIL
echo [ERROR] 리서치 실패. windows\doctor.bat 로 환경을 점검하세요.
exit /b 1

:END
