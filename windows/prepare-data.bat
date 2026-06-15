@echo off
REM 로컬 데이터(jsonl/csv/json) -> data\docs(.md) 변환 (thin wrapper)
REM 사용: prepare-data.bat "data\raw\corpus.jsonl" [--content-field text] [--clean]
cd /d "%~dp0\.."

python tools\launch.py prepare %*
if errorlevel 1 goto FAIL
echo.
echo [OK] 변환 완료. 다음: start-bge.bat 기동 후  research.bat "질의" --source local
goto END

:FAIL
echo [ERROR] 변환 실패. 입력 경로/필드 매핑(--content-field 등)을 확인하세요.
exit /b 1

:END
