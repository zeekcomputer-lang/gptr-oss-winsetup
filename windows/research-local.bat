@echo off
REM 로컬 데이터 기반 리서치 (thin wrapper). 웹 미접속, BGE 임베딩 유사도만 사용.
REM 사용: research-local.bat "우리 데이터 핵심 요약" [--report-type detailed_report]
cd /d "%~dp0\.."

if "%~1"=="" goto USAGE
python tools\launch.py research %* --source local
if errorlevel 1 goto FAIL
goto END

:USAGE
echo 사용법: research-local.bat "질의" [옵션]
echo   사전: prepare-data.bat 로 data\docs 생성 + start-bge.bat 로 BGE 기동
exit /b 2

:FAIL
echo [ERROR] 리서치 실패. windows\doctor.bat 로 환경을 점검하세요.
exit /b 1

:END
