@echo off
REM 시간순 이벤트 정리 모드(Mode 2) thin wrapper.
REM 전 문서를 누락 없이 map-reduce 요약 → 한글 비즈니스 보고서. 임베딩 불요.
REM 사용: research-chrono.bat "주제" [--max-input-kb 25]
cd /d "%~dp0\.."

if "%~1"=="" goto USAGE
python tools\launch.py research %* --mode chrono --source local
if errorlevel 1 goto FAIL
goto END

:USAGE
echo 사용법: research-chrono.bat "주제" [--max-input-kb 25]
echo   사전: prepare-data.bat 로 data\docs 생성(Office/PDF 는 win32 COM 변환)
exit /b 2

:FAIL
echo [ERROR] 실패. windows\doctor.bat 로 환경을 점검하세요.
exit /b 1

:END
