@echo off
REM SSL 차단 환경용 tiktoken 오프라인 캐시 설치/점검 (thin wrapper)
REM   tiktoken.bat status                          캐시 상태/무결성 + 각 인코딩 다운로드 URL 출력
REM   tiktoken.bat install o200k_base.tiktoken cl100k_base.tiktoken   수동 다운로드 원본을 해시명으로 설치
REM   tiktoken.bat verify                          네트워크 차단 후 로드 → SSL 미접속 입증
cd /d "%~dp0\.."
python tools\launch.py tiktoken %*
