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
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    ROOT, VENDOR_DIR, VENV_DIR, OUTPUTS_DIR, DATA_RAW_DIR, DOCS_DIR,
    OFFLINE_DIR, TIKTOKEN_CACHE_DIR, NLTK_DATA_DIR,
    GPTR_REPO_URL, GPTR_PIN,
    venv_python, venv_exists, vendor_exists, run, py_exe, section, is_windows,
)

BUILD_REQUIREMENTS = ROOT / ".gptr-build-requirements.txt"


def make_venv() -> None:
    section("1/6 venv 생성")
    if venv_exists():
        print("  이미 존재 — 건너뜀")
        return
    run([py_exe(), "-m", "venv", str(VENV_DIR)])


def vendor_repo() -> None:
    section("2/6 gpt-researcher vendoring")
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


def _venv_pyver() -> tuple[int, int]:
    """venv 의 파이썬 버전(major, minor) 를 조회."""
    try:
        out = subprocess.run(
            [str(venv_python()), "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        major, minor = (int(x) for x in out.split(".")[:2])
        return (major, minor)
    except Exception:
        return sys.version_info[:2]


def _materialize_requirements(src: Path, pyver: tuple[int, int]) -> Path:
    """vendor requirements.txt 를 읽어 파생 설치 목록을 만든다(원본 무수정).

    Python 3.14+ 에서는 'numpy>=2.0.0,<2.3.0' 의 상한이 cp314 휠 버전(2.3.x+)을
    막아 소스 빌드를 유발하므로, numpy 상한만 완화해 휠 설치가 가능하게 한다.
    원본 vendor 파일은 건드리지 않고 별도 파일(.gptr-build-requirements.txt)에 기록.
    """
    lines_out = []
    relaxed = False
    for raw in src.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if pyver >= (3, 14) and re.match(r"^numpy\b", s):
            lines_out.append("numpy>=2.3.0  # py3.14: cp314 wheel (orig: %s)" % s)
            relaxed = True
        else:
            lines_out.append(raw)
    BUILD_REQUIREMENTS.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
    if relaxed:
        print(f"  [py{pyver[0]}.{pyver[1]}] numpy 상한 완화: <2.3.0 -> >=2.3.0 (cp314 휠 사용, 원본 무수정)")
    return BUILD_REQUIREMENTS


def pip_install() -> None:
    section("3/6 의존성 설치")
    vpy = str(venv_python())
    run([vpy, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])

    pyver = _venv_pyver()
    print(f"  venv Python = {pyver[0]}.{pyver[1]}")

    # gpt-researcher 런타임 의존성 (라이브러리 사용용)
    req = VENDOR_DIR / "requirements.txt"
    if req.exists():
        # Python 3.14+: numpy 를 cp314 휠로 먼저 확보해 소스 빌드를 차단 (best effort)
        if pyver >= (3, 14):
            run([vpy, "-m", "pip", "install", "--only-binary=:all:", "numpy>=2.3.0"], check=False)
        build_req = _materialize_requirements(req, pyver)
        # --prefer-binary: 휠이 있는 버전을 우선해 소스 빌드(컴파일러 필요)를 피한다
        run([vpy, "-m", "pip", "install", "--prefer-binary", "-r", str(build_req)])

    # 검색 retriever (web/hybrid 모드용, 무키). local 전용이면 사실상 불필이나 고정메뉴상 설치.
    run([vpy, "-m", "pip", "install", "--prefer-binary", "duckduckgo-search"])
    # NLTK: unstructured 계열 로더(.md/.docx 등)가 문장분할에 사용. 명시 설치(오프라인 프로비저닝 대상).
    run([vpy, "-m", "pip", "install", "--prefer-binary", "nltk"], check=False)
    # python-docx: 마크다운 보고서 → 비즈니스 DOCX(표 지원) 내보내기(tools/md_to_docx.py). 순수 파이썬 휠.
    run([vpy, "-m", "pip", "install", "--prefer-binary", "python-docx"], check=False)
    # 임베딩 서버는 별도 운영 - torch/sentence-transformers 미설치.
    # cp314 휠이 없는 패키지가 있으면 --prefer-binary 가 휠 있는 버전을 선택한다.


# 오프라인 런타임에 필요한 외부 리소스 — setup(온라인) 시점에 미리 받아 offline/ 에 고정한다.
#   tiktoken: 비용산정 토크나이저 BPE 블록(매 LLM 호출에서 사용 → 필수)
#   nltk    : unstructured 계열 로더(.md/.docx/.pptx 등)의 문장분할(punkt_tab 등)
_NLTK_PACKAGES = [
    "punkt", "punkt_tab",
    "averaged_perceptron_tagger", "averaged_perceptron_tagger_eng",
    "stopwords",
]


def provision_offline() -> None:
    """오프라인 구동용 외부 리소스를 venv 안에서 미리 내려받아 offline/ 에 고정.

    - setup 은 pip(PyPI) 때문에 어차피 온라인이다. 이 시점에 tiktoken/nltk 리소스를
      받아두면 이후 research 는 네트워크 없이(LLM·임베딩 API 호출만) 완결한다.
    - 실패해도 치명적이지 않게 best-effort(check=False). 단, 미완료 시 명확히 안내.
    """
    section("4/6 오프라인 리소스 프로비저닝 (tiktoken·NLTK)")
    vpy = str(venv_python())
    OFFLINE_DIR.mkdir(parents=True, exist_ok=True)
    TIKTOKEN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    NLTK_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 0) 수동 드롭 폴더(offline/manual/*.tiktoken)가 있으면 먼저 올바른 해시명으로 설치
    #    (SSL 차단망: openaipublic 접근 불가 → 허용 PC에서 받은 원본을 여기 두면 자동 편입)
    manual_dir = OFFLINE_DIR / "manual"
    if manual_dir.is_dir():
        drops = sorted(str(p) for p in manual_dir.glob("*.tiktoken"))
        if drops:
            print(f"  offline/manual 에서 수동 드롭 {len(drops)}건 발견 → 자동 설치 시도")
            run([vpy, str(ROOT / "tools" / "tiktoken_offline.py"), "install", *drops], check=False)

    # 1) tiktoken BPE 블록 사전 캐시 — TIKTOKEN_CACHE_DIR 지정 후 encode 1회로 다운로드 유발
    #    (온라인이면 성공, SSL 차단망이면 실패 → 수동 install 로 보완)
    tk_code = (
        "import os, tiktoken;"
        "os.environ['TIKTOKEN_CACHE_DIR']=r'%s';"
        "[tiktoken.get_encoding(n).encode('warmup') for n in ('o200k_base','cl100k_base')];"
        "tiktoken.encoding_for_model('text-embedding-3-small').encode('x');"
        "print('tiktoken cache OK ->', os.environ['TIKTOKEN_CACHE_DIR'])"
        % str(TIKTOKEN_CACHE_DIR)
    )
    rc_tk = run([vpy, "-c", tk_code], check=False)
    # 온라인 다운로드가 실패해도 수동 설치분이 있으면 status 가 OK 일 수 있음 → 재판정
    if rc_tk != 0:
        rc_tk = run([vpy, str(ROOT / "tools" / "tiktoken_offline.py"), "status"], check=False)

    # 2) NLTK 데이터 사전 다운로드 → offline/nltk_data
    nltk_pkgs = ",".join(repr(p) for p in _NLTK_PACKAGES)
    nltk_code = (
        "import nltk;"
        "d=r'%s';"
        "ok=all([nltk.download(p, download_dir=d, quiet=True) for p in [%s]]);"
        "print('nltk data OK ->', d) if ok else print('nltk data PARTIAL ->', d)"
        % (str(NLTK_DATA_DIR), nltk_pkgs)
    )
    rc_nltk = run([vpy, "-c", nltk_code], check=False)

    if rc_tk != 0 or rc_nltk != 0:
        print("  [WARN] 오프라인 리소스 일부 미완료.")
        if rc_tk != 0:
            print("   • tiktoken (openaipublic SSL 차단 시): 허용 PC에서 원본을 받아 수동 설치하세요.")
            print("     1) URL 확인:  python tools/launch.py tiktoken status   (각 인코딩 URL 출력)")
            print("     2) 설치   :  python tools/launch.py tiktoken install o200k_base.tiktoken cl100k_base.tiktoken")
            print("     3) 검증   :  python tools/launch.py tiktoken verify   (네트워크 차단 후 로드 입증)")
        if rc_nltk != 0:
            print(f"   • nltk : nltk.download({_NLTK_PACKAGES}, download_dir={NLTK_DATA_DIR})")
            print("           또는 .md 대신 'prepare --format txt'(기본) 사용 → NLTK 미경유")
    else:
        print("  완료: tiktoken_cache + nltk_data 고정됨 (런타임 네트워크 불요)")


def bootstrap_env() -> None:
    section("5/6 .env 부트스트랩")
    env = ROOT / ".env"
    example = ROOT / ".env.example"
    if env.exists():
        print("  .env 이미 존재 — 보존")
        return
    shutil.copyfile(example, env)
    print(f"  생성: {env}  (값을 채우세요: OPENAI_BASE_URL, 모델명 등)")


def verify() -> None:
    section("6/6 검증")
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
    provision_offline()
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
