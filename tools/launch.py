"""
launch — 반복 실행 (가벼움)

서브커맨드:
  prepare "<입력>" [opts]      로컬 데이터(jsonl/csv/json) → data/docs(.md) 변환
  check-embedding [opts]       별도 운영 중인 BGE 임베딩 엔드포인트 호환성 점검
  research "<질의>" [opts]     리서치 실행 → outputs/ 저장
  doctor                       환경 점검 (venv/vendor/.env/엔드포인트)

  ※ 임베딩(BGE) 서버는 이 repo 가 구동하지 않는다. 사용자가 별도로 띄운
    OpenAI 호환 엔드포인트(EMBEDDING_BASE_URL)에 접속만 한다.

사용:
  python tools/launch.py prepare data/raw/corpus.jsonl --content-field text
  python tools/launch.py check-embedding
  python tools/launch.py research "우리 데이터 핵심 요약" --source local
  python tools/launch.py research "양자내성암호 2026 표준화 동향" --report-type research_report
  python tools/launch.py doctor

셋업 누락 시 rc=2 + 안내.
"""
from __future__ import annotations

import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    ROOT, VENDOR_DIR, OUTPUTS_DIR, DOCS_DIR, venv_python, venv_exists, vendor_exists, run,
)

RUN_RESEARCH = ROOT / "tools" / "run_research.py"
PREPARE_DATA = ROOT / "tools" / "prepare_data.py"
CHECK_EMBEDDING = ROOT / "tools" / "check_embedding.py"


def _ensure_setup() -> None:
    missing = []
    if not venv_exists():
        missing.append(".venv (python tools/setup.py)")
    if not vendor_exists():
        missing.append("vendor/gpt-researcher (python tools/setup.py)")
    if not (ROOT / ".env").exists():
        missing.append(".env (cp .env.example .env)")
    if missing:
        print("[launch] 셋업이 필요합니다:")
        for m in missing:
            print(f"  - {m}")
        raise SystemExit(2)


def _load_env_into_os() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        os.environ.setdefault(k, v)


def cmd_prepare(argv: list[str]) -> int:
    # prepare 는 vendor/.env 불요(순수 변환). venv 만 있으면 venv python, 없으면 현재 python.
    if not argv:
        print('[launch] 사용법: prepare "<입력 .jsonl/.json/.csv 또는 디렉터리>" [--content-field ...]')
        return 2
    py = str(venv_python()) if venv_exists() else sys.executable
    return run([py, str(PREPARE_DATA), *argv], check=False)


def cmd_research(argv: list[str]) -> int:
    _ensure_setup()
    if not argv:
        print('[launch] 사용법: research "<질의>" [--report-type ...] [--tone ...]')
        return 2
    return run([str(venv_python()), str(RUN_RESEARCH), *argv], check=False)


def _probe(url: str) -> str:
    """GET 프로브. 4xx/5xx 응답도 서버가 살아있다는 뜻이므로 REACHABLE 로 간주."""
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return f"OK({r.status})"
    except urllib.error.HTTPError as e:
        return f"REACHABLE(HTTP {e.code})"
    except Exception as e:
        return f"FAIL({type(e).__name__})"


def _probe_embeddings(base: str) -> str:
    """임베딩 서버는 /health 가 없을 수 있으므로 /v1/embeddings 에 실제 POST 로 확인."""
    import json as _json
    url = base.rstrip("/") + "/embeddings"
    body = _json.dumps({"input": "ping", "model": os.getenv("EMBEDDING", "x").split(":", 1)[-1]}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                headers={"Content-Type": "application/json"})
    # 임베딩은 로컬 직결 — 프록시 미경유
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=8) as r:
            data = _json.loads(r.read().decode("utf-8"))
        emb = data.get("data", [{}])[0].get("embedding")
        if isinstance(emb, list):
            return f"OK(dim={len(emb)})"
        if isinstance(emb, str):
            return "OK(base64)"
        return "WARN(응답형식 확인필요)"
    except Exception as e:
        return f"FAIL({type(e).__name__})"


def cmd_embed_check(argv: list[str]) -> int:
    # 임베딩 서버 호환성 점검 (vendor 불요, stdlib only)
    py = str(venv_python()) if venv_exists() else sys.executable
    return run([py, str(CHECK_EMBEDDING), *argv], check=False)


def cmd_doctor(argv: list[str]) -> int:
    _load_env_into_os()
    print("[doctor] 환경 점검")
    print(f"  venv         : {'OK' if venv_exists() else 'MISSING'}")
    print(f"  vendor gptr  : {'OK' if vendor_exists() else 'MISSING'}")
    print(f"  .env         : {'OK' if (ROOT / '.env').exists() else 'MISSING'}")
    md_count = len(list(DOCS_DIR.glob("*.md"))) if DOCS_DIR.exists() else 0
    print(f"  local docs   : {md_count} .md @ {DOCS_DIR}")
    print(f"  REPORT_SOURCE      = {os.getenv('REPORT_SOURCE', '(unset/web)')}")
    print(f"  DOC_PATH           = {os.getenv('DOC_PATH', '(unset)')}")
    base = os.getenv("OPENAI_BASE_URL", "")
    emb = os.getenv("EMBEDDING_BASE_URL", "")
    print(f"  OPENAI_BASE_URL    = {base or '(unset)'}")
    print(f"  EMBEDDING_BASE_URL = {emb or '(unset)'}")
    print(f"  MCP_STRATEGY       = {os.getenv('MCP_STRATEGY', '(unset)')}")
    hdr = os.getenv("OPENAI_EXTRA_HEADERS")
    print(f"  OPENAI_EXTRA_HEADERS = {'set' if hdr else '(none)'}")
    if base:
        print(f"  LLM   /v1/models   : {_probe(base.rstrip('/') + '/models')}")
    if emb:
        # /health 가 없는 서버(사용자 BGE 등)도 있으므로 실제 임베딩 POST 로 점검
        print(f"  BGE   /v1/embeddings: {_probe_embeddings(emb)}")
    return 0


_CMDS = {"prepare": cmd_prepare, "research": cmd_research,
         "check-embedding": cmd_embed_check, "doctor": cmd_doctor}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in _CMDS:
        print("사용법: python tools/launch.py [prepare|check-embedding|research|doctor] ...")
        return 2
    return _CMDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
