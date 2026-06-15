"""
_common — gptr-oss-winsetup 공유 유틸 (setup.py / launch.py 공용)

LESSONS(code-2char-system) 차용: 셋업(무겁고 1회) / 실행(가벼운 반복) 분리.
이 모듈은 경로/플랫폼/venv 해석만 담당하고 부수효과는 호출측에서.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = ROOT / "vendor" / "gpt-researcher"
VENV_DIR = ROOT / ".venv"
OUTPUTS_DIR = ROOT / "outputs"

# 로컬 데이터 파이프라인 경로
#   DATA_RAW_DIR : 원본 jsonl/csv/json 투입 위치
#   DOCS_DIR     : 변환 산출물(.md). gpt-researcher 의 DOC_PATH 기본값과 일치시킨다.
DATA_DIR = ROOT / "data"
DATA_RAW_DIR = DATA_DIR / "raw"
DOCS_DIR = DATA_DIR / "docs"

GPTR_REPO_URL = "https://github.com/assafelovic/gpt-researcher.git"
GPTR_PIN = os.getenv("GPTR_PIN", "")  # 비우면 기본 브랜치 최신


def is_windows() -> bool:
    return platform.system() == "Windows"


def venv_python() -> Path:
    if is_windows():
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def venv_exists() -> bool:
    return venv_python().exists()


def vendor_exists() -> bool:
    return (VENDOR_DIR / "gpt_researcher" / "__init__.py").exists()


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> int:
    """서브프로세스 실행. 실패 시 check=True 면 예외."""
    printable = " ".join(str(c) for c in cmd)
    print(f"  $ {printable}")
    proc = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None)
    if check and proc.returncode != 0:
        raise SystemExit(f"명령 실패(rc={proc.returncode}): {printable}")
    return proc.returncode


def py_exe() -> str:
    """현재 인터프리터 (setup 부트스트랩용)."""
    return sys.executable


def section(title: str) -> None:
    print()
    print("=" * 64)
    print(f"  {title}")
    print("=" * 64)
