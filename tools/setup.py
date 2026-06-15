"""
setup — 1회성 셋업 (무거움)

수행:
  1) .venv 생성
  2) gpt-researcher repo vendoring (vendor/gpt-researcher)
  3) 의존성 설치 (gpt-researcher 라이브러리 + 검색 retriever)
  4) .env 부트스트랩 (.env.example → .env, 없을 때만)
  5) 검증 (import gpt_researcher)

※ 임베딩(BGE) 서버는 이 repo 가 설치/구동하지 않는다. torch/sentence-transformers
   같은 무거운 의존성은 설치하지 않으며, 사용자가 별도로 띄운 임베딩
   엔드포인트(EMBEDDING_BASE_URL)에 접속만 한다.

사용:
  python tools/setup.py
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    ROOT, VENDOR_DIR, VENV_DIR, OUTPUTS_DIR, DATA_RAW_DIR, DOCS_DIR,
    GPTR_REPO_URL, GPTR_PIN,
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


def pip_install() -> None:
    section("3/5 의존성 설치")
    vpy = str(venv_python())
    run([vpy, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])

    # gpt-researcher 런타임 의존성 (라이브러리 사용용)
    req = VENDOR_DIR / "requirements.txt"
    if req.exists():
        run([vpy, "-m", "pip", "install", "-r", str(req)])

    # 검색 retriever (web/hybrid 모드용, 무키). local 전용이면 사실상 불필이나 고정메뉴상 설치.
    run([vpy, "-m", "pip", "install", "duckduckgo-search"])
    # ※ 임베딩 서버는 별도 운영 — torch/sentence-transformers 미설치.


def bootstrap_env() -> None:
    section("4/5 .env 부트스트랩")
    env = ROOT / ".env"
    example = ROOT / ".env.example"
    if env.exists():
        print("  .env 이미 존재 — 보존")
        return
    shutil.copyfile(example, env)
    print(f"  생성: {env}  (값을 채우세요: OPENAI_BASE_URL, 모델명 등)")


def verify() -> None:
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
    OUTPUTS_DIR.mkdir(exist_ok=True)
    DATA_RAW_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    print("  outputs/ , data/raw/ , data/docs/ 준비 완료")


def main() -> int:
    ap = argparse.ArgumentParser(description="gptr-oss-winsetup 셋업")
    ap.parse_args()

    print(f"[setup] ROOT={ROOT}")
    print(f"[setup] platform={'Windows' if is_windows() else 'POSIX'}")

    make_venv()
    vendor_repo()
    pip_install()
    bootstrap_env()
    verify()

    section("셋업 완료")
    print("다음 단계:")
    print("  1) .env 편집 — OPENAI_BASE_URL / 모델명 / EMBEDDING_BASE_URL / (선택)OPENAI_EXTRA_HEADERS")
    print("  2) 별도 운영 중인 BGE 엔드포인트 확인:  python tools/launch.py check-embedding")
    print("  3-a) 웹 리서치 :  python tools/launch.py research \"질의\"")
    print("  3-b) 로컬 데이터:  python tools/launch.py prepare data/raw/<파일>.jsonl")
    print("                → python tools/launch.py research \"질의\" --source local")
    print("  자세한 시나리오는 MANUAL.md 참조")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
