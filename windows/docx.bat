@echo off
REM 마크다운 보고서 → 비즈니스 DOCX(표 지원) 변환 (thin wrapper)
REM 사용:  docx.bat outputs\report.md            (report.docx 생성)
REM        docx.bat a.md b.md -o combined.docx   (병합)
cd /d "%~dp0\.."
python tools\launch.py docx %*
