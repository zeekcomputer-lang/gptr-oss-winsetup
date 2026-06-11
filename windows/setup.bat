@echo off
REM gptr-oss-winsetup - 1회성 셋업 (thin wrapper)
REM Python 런처에 모든 로직 위임 (LESSONS L-005 회피: 단순 위임만)

cd /d "%~dp0\.."

where python >nul 2>nul
if errorlevel 1 goto NOPYTHON

python tools\setup.py %*
if errorlevel 1 goto FAIL

echo.
echo [OK] 셋업 완료. 다음: windows\start-bge.bat  그리고  windows\research.bat "질의"
goto END

:NOPYTHON
echo [ERROR] python 을 찾을 수 없습니다. Python 3.11 을 설치하고 PATH 에 추가하세요.
exit /b 1

:FAIL
echo [ERROR] 셋업 실패. 위 로그를 확인하세요.
exit /b 1

:END
