"""
run_research — GPT-Researcher × GPT-OSS 실행 엔트리포인트

흐름:
  1) .env 로드
  2) gptr_oss_patch 적용 (LLM default_headers 주입 / 임베딩 base_url 분리 / tool-calling 차단)
  3) GPTResearcher 로 리서치 수행 → 보고서 파일 저장

사용:
  python tools/run_research.py "퀀텀 컴퓨팅 2026 동향" --report-type research_report
  python tools/run_research.py "..." --tone Objective --out report.md

옵션:
  query                  (위치) 리서치 질의
  --report-type          research_report(기본) | detailed_report | outline_report ...
  --tone                 Objective(기본) 등 Tone enum 값
  --out                  출력 파일 경로 (기본 outputs/report-<timestamp>.md)
  --verbose              상세 로그
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

# ── 경로 설정: 이 repo 의 patches/ 와 vendored gpt-researcher 를 path 에 추가 ──
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "patches"))

# vendor/gpt-researcher 가 있으면 우선 사용, 없으면 설치된 패키지 사용
_VENDOR = ROOT / "vendor" / "gpt-researcher"
if _VENDOR.exists():
    sys.path.insert(0, str(_VENDOR))

DEFAULT_DOC_PATH = ROOT / "data" / "docs"


def _load_dotenv() -> None:
    """간단 .env 로더 (python-dotenv 없이도 동작). 기존 환경변수 우선."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        # 따옴표 제거
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        os.environ.setdefault(key, val)


async def _run(query: str, report_type: str, tone_name: str, verbose: bool,
               report_source: str) -> str:
    # 패치는 gpt_researcher import 전에 import 되어도 되고 후에도 됨(멱등)
    import gptr_oss_patch  # noqa: F401  (import 시 자동 apply)

    from gpt_researcher import GPTResearcher
    from gpt_researcher.utils.enum import Tone

    tone = getattr(Tone, tone_name, Tone.Objective)

    researcher = GPTResearcher(
        query=query,
        report_type=report_type,
        report_source=report_source,
        tone=tone,
        verbose=verbose,
    )
    await researcher.conduct_research()
    report = await researcher.write_report()
    return report


def main() -> int:
    _load_dotenv()

    ap = argparse.ArgumentParser(description="GPT-Researcher × GPT-OSS runner")
    ap.add_argument("query", help="리서치 질의")
    ap.add_argument("--source", default=os.getenv("REPORT_SOURCE", "web"),
                    choices=["web", "local", "hybrid"],
                    help="web(기본)|local|hybrid")
    ap.add_argument("--doc-path", default=None,
                    help="로컬 문서 디렉터리 (source=local/hybrid). 기본 data/docs 또는 .env DOC_PATH")
    ap.add_argument("--report-type", default="research_report")
    ap.add_argument("--tone", default="Objective")
    ap.add_argument("--out", default=None)
    ap.add_argument("--verbose", action="store_true",
                    default=os.getenv("VERBOSE", "false").lower() in ("1", "true", "yes", "on"))
    args = ap.parse_args()

    # 필수 환경 점검
    if not os.getenv("OPENAI_BASE_URL"):
        print("[run_research][WARN] OPENAI_BASE_URL 미설정 — LLM 엔드포인트를 확인하세요.")

    # 소스/문서경로 해석 — gpt_researcher 는 REPORT_SOURCE / DOC_PATH 환경변수를 읽는다.
    report_source = args.source
    os.environ["REPORT_SOURCE"] = report_source
    if report_source in ("local", "hybrid"):
        doc_path = args.doc_path or os.getenv("DOC_PATH") or str(DEFAULT_DOC_PATH)
        doc_dir = Path(doc_path)
        os.environ["DOC_PATH"] = str(doc_dir)
        doc_count = (len(list(doc_dir.glob("*.md"))) + len(list(doc_dir.glob("*.txt")))) if doc_dir.exists() else 0
        if not doc_dir.exists() or doc_count == 0:
            print(f"[run_research][WARN] DOC_PATH 에 문서가 없습니다: {doc_dir} "
                  f"— 먼저 'python tools/prepare_data.py <jsonl>' 로 변환하세요.")
        else:
            print(f"[run_research] source={report_source} DOC_PATH={doc_dir} (문서 {doc_count}건)")
        if not os.getenv("EMBEDDING_BASE_URL"):
            print("[run_research][WARN] EMBEDDING_BASE_URL 미설정 — local 모드는 BGE 임베딩 서버가 필요합니다.")

    out_path = Path(args.out) if args.out else (
        ROOT / "outputs" / f"report-{time.strftime('%Y%m%d-%H%M%S')}.md"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[run_research] query={args.query!r} type={args.report_type} "
          f"tone={args.tone} source={report_source}")
    try:
        report = asyncio.run(_run(args.query, args.report_type, args.tone,
                                  args.verbose, report_source))
    except Exception as e:
        print(f"[run_research][ERROR] 리서치 실패: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    out_path.write_text(report, encoding="utf-8")
    print(f"[run_research] 완료 → {out_path}")
    print(f"[run_research] 보고서 길이: {len(report)} chars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
