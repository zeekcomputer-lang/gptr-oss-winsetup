"""
win32_convert — Office/PDF → 텍스트 변환 (Windows COM 기반, DRM 대응)

원칙(요구사항):
  - docx/doc/pdf/ppt(x) 는 **반드시 win32 COM(설치된 Word/PowerPoint 애플리케이션)** 으로 연다.
  - python-docx / python-pptx 처럼 **XML 을 직접 파싱하지 않는다**.
    (사내 DRM 문서는 XML 직접 접근이 차단·복호화 불가 → COM 자동화는 앱이 복호화 후 텍스트 제공)
  - Windows + 해당 Office 앱 설치 필수. 비-Windows/COM 부재 시 명시적 에러+안내.

대상 확장자:
  - .docx .doc .rtf .pdf  → Word.Application  (Word 2013+ 는 PDF 열기 지원)
  - .pptx .ppt            → PowerPoint.Application

반환:
  추출 텍스트(str). 실패 시 RuntimeError.

CLI:
  python tools/win32_convert.py <입력파일> [--out <txt>]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

WORD_EXTS = {".docx", ".doc", ".rtf", ".pdf"}
PPT_EXTS = {".pptx", ".ppt"}
SUPPORTED_EXTS = WORD_EXTS | PPT_EXTS


def is_supported(path: str | os.PathLike) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTS


def _require_windows_com():
    """win32com(pywin32) 로딩. 비-Windows/미설치면 친절한 에러."""
    if os.name != "nt":
        raise RuntimeError(
            "win32 COM 변환은 Windows 에서만 가능합니다(설치된 Office 앱 필요). "
            "현재 OS 는 비-Windows 입니다. Windows 호스트에서 prepare 를 실행하세요."
        )
    try:
        import win32com.client  # type: ignore
        import pythoncom  # type: ignore
        return win32com.client, pythoncom
    except Exception as e:  # pragma: no cover (Windows 전용)
        raise RuntimeError(
            f"pywin32(win32com) 로딩 실패: {e}. "
            "'pip install -r requirements-windows.txt' 로 pywin32 를 설치하세요."
        )


def _word_to_text(path: Path) -> str:  # pragma: no cover (Windows 전용)
    win32com, pythoncom = _require_windows_com()
    pythoncom.CoInitialize()
    word = None
    doc = None
    try:
        word = win32com.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0  # wdAlertsNone
        # ReadOnly + AddToRecentFiles=False. PDF 는 자동으로 변환 다이얼로그가 뜰 수 있어
        # DisplayAlerts=0 + Visible=False 로 억제. ConfirmConversions=False.
        doc = word.Documents.Open(
            str(path), ReadOnly=True, AddToRecentFiles=False, ConfirmConversions=False,
        )
        text = doc.Content.Text or ""
        return text.replace("\r", "\n")
    finally:
        try:
            if doc is not None:
                doc.Close(False)
        except Exception:
            pass
        try:
            if word is not None:
                word.Quit()
        except Exception:
            pass
        pythoncom.CoUninitialize()


def _ppt_to_text(path: Path) -> str:  # pragma: no cover (Windows 전용)
    win32com, pythoncom = _require_windows_com()
    pythoncom.CoInitialize()
    ppt = None
    pres = None
    try:
        ppt = win32com.DispatchEx("PowerPoint.Application")
        # PowerPoint 는 Visible=False 를 거부하는 버전이 있어 최소화로 우회.
        try:
            ppt.Visible = False
        except Exception:
            pass
        # WithWindow=False 로 창 없이 오픈, ReadOnly
        pres = ppt.Presentations.Open(
            str(path), ReadOnly=True, Untitled=False, WithWindow=False,
        )
        lines: list[str] = []
        for idx, slide in enumerate(pres.Slides, 1):
            lines.append(f"[Slide {idx}]")
            for shape in slide.Shapes:
                try:
                    if shape.HasTextFrame and shape.TextFrame.HasText:
                        lines.append(shape.TextFrame.TextRange.Text.replace("\r", "\n"))
                except Exception:
                    continue
            lines.append("")
        return "\n".join(lines)
    finally:
        try:
            if pres is not None:
                pres.Close()
        except Exception:
            pass
        try:
            if ppt is not None:
                ppt.Quit()
        except Exception:
            pass
        pythoncom.CoUninitialize()


def convert_to_text(path: str | os.PathLike) -> str:
    """확장자에 따라 Word/PowerPoint COM 으로 텍스트 추출."""
    p = Path(path)
    ext = p.suffix.lower()
    if not p.exists():
        raise RuntimeError(f"입력 없음: {p}")
    if ext in WORD_EXTS:
        return _word_to_text(p)
    if ext in PPT_EXTS:
        return _ppt_to_text(p)
    raise RuntimeError(f"미지원 확장자: {ext} (지원: {sorted(SUPPORTED_EXTS)})")


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Office/PDF → 텍스트 (Windows COM)")
    ap.add_argument("path")
    ap.add_argument("--out", default=None, help="출력 .txt 경로(미지정 시 stdout)")
    args = ap.parse_args()

    try:
        text = convert_to_text(args.path)
    except RuntimeError as e:
        print(f"[win32_convert][ERROR] {e}", file=sys.stderr)
        return 1
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"[win32_convert] 완료 → {args.out} ({len(text)} chars)")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
