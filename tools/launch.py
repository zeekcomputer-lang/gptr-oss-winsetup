"""
launch — 반복 실행 (가벼움)

서브커맨드:
  bge                          BGE 임베딩 서버 기동 (포그라운드)
  research "<질의>" [opts]     리서치 실행 → outputs/ 저장
  doctor                       환경 점검 (venv/vendor/.env/엔드포인트)

사용:
  python tools/launch.py bge
  python tools/launch.py research "양자내성암호 2026 표준화 동향" --report-type research_report
  python tools/launch.py doctor

셋업 누락 시 rc=2 + 안내.
"""
from __future__ import annotations

import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    ROOT, VENDOR_DIR, OUTPUTS_DIR, venv_python, venv_exists, vendor_exists, run,
)

BGE_SERVER = ROOT / "bge_server" / "bge_server.py"
RUN_RESEARCH = ROOT / "tools" / "run_research.py"


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


def cmd_bge(argv: list[str]) -> int:
    _ensure_setup()
    _load_env_into_os()
    print(f"[launch] BGE 서버 기동: {BGE_SERVER}")
    return run([str(venv_python()), str(BGE_SERVER)], check=False)


def cmd_research(argv: list[str]) -> int:
    _ensure_setup()
    if not argv:
        print('[launch] 사용법: research "<질의>" [--report-type ...] [--tone ...]')
        return 2
    return run([str(venv_python()), str(RUN_RESEARCH), *argv], check=False)


def _probe(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return f"OK({r.status})"
    except Exception as e:
        return f"FAIL({type(e).__name__})"


def cmd_doctor(argv: list[str]) -> int:
    _load_env_into_os()
    print("[doctor] 환경 점검")
    print(f"  venv         : {'OK' if venv_exists() else 'MISSING'}")
    print(f"  vendor gptr  : {'OK' if vendor_exists() else 'MISSING'}")
    print(f"  .env         : {'OK' if (ROOT / '.env').exists() else 'MISSING'}")
    base = os.getenv("OPENAI_BASE_URL", "")
    emb = os.getenv("EMBEDDING_BASE_URL", "")
    print(f"  OPENAI_BASE_URL    = {base or '(unset)'}")
    print(f"  EMBEDDING_BASE_URL = {emb or '(unset)'}")
    print(f"  MCP_STRATEGY       = {os.getenv('MCP_STRATEGY', '(unset)')}")
    hdr = os.getenv("OPENAI_EXTRA_HEADERS")
    print(f"  OPENAI_EXTRA_HEADERS = {'set' if hdr else '(none)'}")
    if base:
        root = base.rstrip("/").rsplit("/v1", 1)[0]
        print(f"  LLM   /v1/models   : {_probe(base.rstrip('/') + '/models')}")
    if emb:
        print(f"  BGE   /health      : {_probe(emb.rstrip('/').rsplit('/v1',1)[0] + '/health')}")
    return 0


_CMDS = {"bge": cmd_bge, "research": cmd_research, "doctor": cmd_doctor}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in _CMDS:
        print("사용법: python tools/launch.py [bge|research|doctor] ...")
        return 2
    return _CMDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
