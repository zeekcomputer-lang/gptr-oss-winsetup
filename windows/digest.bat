@echo off
REM 시간순 이벤트 다이제스트 단독 생성(Mode 2 Stage1) thin wrapper.
REM 사용: digest.bat --query "주제" [--max-input-kb 25] [--doc-path data\docs]
cd /d "%~dp0\.."

python tools\launch.py digest %*
if errorlevel 1 goto FAIL
goto END

:FAIL
echo [ERROR] 다이제스트 생성 실패. windows\doctor.bat 로 환경을 점검하세요.
exit /b 1

:END
