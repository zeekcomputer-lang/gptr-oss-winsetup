@echo off
REM 로컬 BGE 임베딩 서버 호환성 점검 (thin wrapper)
REM .env 의 EMBEDDING_BASE_URL / EMBEDDING 을 읽어 실제 POST 로 검증한다.
cd /d "%~dp0\.."
python tools\launch.py check-embedding %*
