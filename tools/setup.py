"""
setup — 1회성 셋업 (무거움)

수행:
  1) .venv 생성
  2) gpt-researcher repo vendoring (vendor/gpt-researcher)
  3) 의존성 설치 (gpt-researcher + BGE 서버 + 런처)
  4) .env 부트스트랩 (.env.example → .env, 없을 때만)
  5) 검증 (import gpt_researcher / sentence_transformers)

사용:
  python tools/setup.py
  python tools/setup.py --skip-bge     # BGE(torch) 설치 생략(외부 임베딩 사용 시)
  python tools/setup.py --cpu          # torch CPU 휠 강제
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    ROOT, VENDOR_DIR, VENV_DIR, OUTPUTS_DIR, GPTR_REPO_URL, GPTR_PIN,
    venv_python, venv_exists, vendor_exists, run, py_exe, section, is_windows,
)


def make_venv() -> None:
    section("1/5 venv 생성")
    if venv_exists():
        print("  이미 존재 — 건너뜀")
        return
    run([py_exe(), "-m", "venv", str(VENV_DIR)])


def vendor_repo() -> None:
    section("2/5 gpt-researcher vendoring")
    if vendor_exists():
        print(f"  이미 존재 — 건너뜀 ({VENDOR_DIR})")
        return
    VENDOR_DIR.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("git") is None:
        raise SystemExit("git 이 필요합니다. Git for Windows 를 설치하세요.")
    args = ["git", "clone", "--depth", "1"]
    if GPTR_PIN:
        args += ["--branch", GPTR_PIN]
    args += [GPTR_REPO_URL, str(VENDOR_DIR)]
    run(args)


def pip_install(skip_bge: bool, cpu: bool) -> None:
    section("3/5 의존성 설치")
    vpy = str(venv_python())
    run([vpy, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])

    # gpt-researcher 런타임 의존성
    req = VENDOR_DIR / "requirements.txt"
    if req.exists():
        run([vpy, "-m", "pip", "install", "-r", str(req)])

    # 런처/공통 (간단 .env 로더는 자체 구현이라 dotenv 불요)
    run([vpy, "-m", "pip", "install", "duckduckgo-search"])

    if not skip_bge:
        if cpu:
            run([vpy, "-m", "pip", "install", "torch",
                 "--index-url", "https://download.pytorch.org/whl/cpu"])
        run([vpy, "-m", "pip", "install",
             "fastapi", "uvicorn", "sentence-transformers"])
    else:
        print("  --skip-bge: torch/sentence-transformers 설치 생략")


def bootstrap_env() -> None:
    section("4/5 .env 부트스트랩")
    env = ROOT / ".env"
    example = ROOT / ".env.example"
    if env.exists():
        print("  .env 이미 존재 — 보존")
        return
    shutil.copyfile(example, env)
    print(f"  생성: {env}  (값을 채우세요: OPENAI_BASE_URL, 모델명 등)")


def verify(skip_bge: bool) -> None:
    section("5/5 검증")
    vpy = str(venv_python())
    # gpt_researcher import (vendor 경로 주입)
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "import gpt_researcher; print('gpt_researcher OK', gpt_researcher.__file__)"
        % str(VENDOR_DIR)
    )
    rc = run([vpy, "-c", code], check=False)
    if rc != 0:
        raise SystemExit("검증 실패: gpt_researcher import 불가")
    if not skip_bge:
        rc2 = run([vpy, "-c", "import sentence_transformers, fastapi, uvicorn; print('BGE deps OK')"],
                  check=False)
        if rc2 != 0:
            raise SystemExit("검증 실패: BGE 의존성 import 불가")
    OUTPUTS_DIR.mkdir(exist_ok=True)
    print("  outputs/ 준비 완료")


def main() -> int:
    ap = argparse.ArgumentParser(description="gptr-oss-winsetup 셋업")
    ap.add_argument("--skip-bge", action="store_true")
    ap.add_argument("--cpu", action="store_true")
    args = ap.parse_args()

    print(f"[setup] ROOT={ROOT}")
    print(f"[setup] platform={'Windows' if is_windows() else 'POSIX'}")

    make_venv()
    vendor_repo()
    pip_install(args.skip_bge, args.cpu)
    bootstrap_env()
    verify(args.skip_bge)

    section("셋업 완료")
    print("다음 단계:")
    print("  1) .env 편집 — OPENAI_BASE_URL / 모델명 / (선택)OPENAI_EXTRA_HEADERS")
    print("  2) 임베딩 서버:  python tools/launch.py bge")
    print("  3) 리서치 실행:  python tools/launch.py research \"질의\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
