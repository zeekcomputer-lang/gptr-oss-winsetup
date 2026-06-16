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


def _apply_offline_env() -> None:
    """오프라인 캐시 env 를 gpt_researcher import 전에 직접 설정(패치와 독립, belt-and-suspenders).
    tiktoken 은 빈 TIKTOKEN_CACHE_DIR("")를 캐싱 비활성으로 해석하므로 미설정/빈값이면 교체."""
    tk = ROOT / "offline" / "tiktoken_cache"
    nl = ROOT / "offline" / "nltk_data"
    if tk.is_dir():
        cur = os.environ.get("TIKTOKEN_CACHE_DIR")
        if not cur or not cur.strip():
            os.environ["TIKTOKEN_CACHE_DIR"] = str(tk)
    if nl.is_dir():
        prev = os.environ.get("NLTK_DATA", "")
        if str(nl) not in prev.split(os.pathsep):
            os.environ["NLTK_DATA"] = (prev + os.pathsep + str(nl)) if prev else str(nl)


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


def _scan_raw_hint() -> None:
    """data/raw 에 미변환 Office/PDF 가 있으면 변환 안내(공통 prepare 단계)."""
    raw = ROOT / "data" / "raw"
    if not raw.exists():
        return
    exts = (".docx", ".doc", ".rtf", ".pdf", ".pptx", ".ppt")
    office = [p.name for p in raw.iterdir() if p.suffix.lower() in exts]
    if office:
        head = office[:5]
        print(f"[run_research] data/raw 에 미변환 문서 {len(office)}건 발견: {head}{'...' if len(office) > 5 else ''}")
        print("  → 'python tools/launch.py prepare data/raw' 로 변환하세요(Office/PDF는 Windows+win32 COM 필요).")


def _build_chrono_digest(query: str, doc_dir: Path, max_input_kb: int) -> int:
    """chrono 모드: 전문서 map-reduce 다이제스트 생성 → DOC_PATH 를 다이제스트로 교체 + full-corpus."""
    tools_dir = ROOT / "tools"
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    try:
        import build_digest as bd  # noqa: E402
    except Exception as e:
        print(f"[run_research][ERROR] build_digest import 실패: {e}", file=sys.stderr)
        return 1
    digest_dir = ROOT / "data" / "digest"
    out = digest_dir / "digest.md"
    print(f"[run_research] chrono 모드: 전문서 다이제스트 생성 (입력한도 {max_input_kb}KB)")
    try:
        digest, stats = bd.build_digest(doc_dir, query, max_input_kb=max_input_kb)
    except Exception as e:
        print(f"[run_research][ERROR] 다이제스트 생성 실패: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    bd.write_digest(digest, out, query)
    print(f"[run_research] 다이제스트 통계: {stats}")
    if stats.get("still_missing"):
        print(f"[run_research][WARN] 다이제스트 누락 마커 {len(stats['still_missing'])}건(내용은 입력됨)")
    # Stage2: 다이제스트를 local full-corpus 로 사용(재선밄 방지)
    os.environ["DOC_PATH"] = str(digest_dir)
    os.environ["GPTR_LOCAL_FULL_CORPUS"] = "1"
    print(f"[run_research] Stage2: DOC_PATH={digest_dir} (full-corpus 전량 사용)")
    return 0


async def _run(query: str, report_type: str, tone_name: str, verbose: bool,
               report_source: str) -> str:
    # 오프라인 캐시 env 를 gpt_researcher 터치 전에 확실히 설정(패치 import 실패에도 대비)
    _apply_offline_env()
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
    ap.add_argument("--mode", default=os.getenv("GPTR_RUN_MODE", "rag"),
                    choices=["rag", "chrono"],
                    help="rag(기본, 유사도 기반·누락 허용) | chrono(시간순 이벤트·전문서 요약·누락 0)")
    ap.add_argument("--language", default=os.getenv("LANGUAGE", "korean"),
                    help="출력 언어(기본 korean). LANGUAGE env 로도 설정 가능")
    ap.add_argument("--max-input-kb", type=int, default=int(os.getenv("CHRONO_MAX_INPUT_KB", "25")),
                    help="chrono 모드 배치 입력 한도(KB, 기본 25)")
    ap.add_argument("--source", default=os.getenv("REPORT_SOURCE", "web"),
                    choices=["web", "local", "hybrid"],
                    help="web(기본)|local|hybrid. chrono 모드는 local 강제")
    ap.add_argument("--doc-path", default=None,
                    help="로컬 문서 디렉터리 (source=local/hybrid). 기본 data/docs 또는 .env DOC_PATH")
    ap.add_argument("--report-type", default="research_report")
    ap.add_argument("--tone", default="Objective")
    ap.add_argument("--out", default=None)
    ap.add_argument("--docx", action="store_true",
                    default=os.getenv("GPTR_EXPORT_DOCX", "false").lower() in ("1", "true", "yes", "on"),
                    help="보고서를 비즈니스 DOCX(.docx, 표 지원)로도 내보낸다. env GPTR_EXPORT_DOCX 로도 설정.")
    ap.add_argument("--verbose", action="store_true",
                    default=os.getenv("VERBOSE", "false").lower() in ("1", "true", "yes", "on"))
    args = ap.parse_args()

    # 필수 환경 점검
    if not os.getenv("OPENAI_BASE_URL"):
        print("[run_research][WARN] OPENAI_BASE_URL 미설정 — LLM 엔드포인트를 확인하세요.")

    # 언어 강제: vendor config 는 LANGUAGE env 를 읽어 보고서 프롬프트에 주입한다.
    os.environ["LANGUAGE"] = args.language

    # 소스/문서경로 해석 — gpt_researcher 는 REPORT_SOURCE / DOC_PATH 환경변수를 읽는다.
    report_source = "local" if args.mode == "chrono" else args.source
    os.environ["REPORT_SOURCE"] = report_source

    if report_source in ("local", "hybrid"):
        doc_path = args.doc_path or os.getenv("DOC_PATH") or str(DEFAULT_DOC_PATH)
        doc_dir = Path(doc_path)
        os.environ["DOC_PATH"] = str(doc_dir)
        doc_count = (len(list(doc_dir.glob("*.md"))) + len(list(doc_dir.glob("*.txt")))) if doc_dir.exists() else 0
        if not doc_dir.exists() or doc_count == 0:
            print(f"[run_research][WARN] DOC_PATH 에 문서가 없습니다: {doc_dir} "
                  f"— 먼저 'python tools/launch.py prepare <입력>' 로 변환하세요.")
            _scan_raw_hint()
        else:
            print(f"[run_research] source={report_source} DOC_PATH={doc_dir} (문서 {doc_count}건)")

    # chrono 모드: 전문서 map-reduce 다이제스트 선행 → 다이제스트를 full-corpus 로 사용
    if args.mode == "chrono":
        rc = _build_chrono_digest(args.query, Path(os.environ["DOC_PATH"]), args.max_input_kb)
        if rc != 0:
            return rc
    elif report_source in ("local", "hybrid"):
        # RAG 모드(local/hybrid)는 임베딩 서버 필요
        if not os.getenv("EMBEDDING_BASE_URL"):
            print("[run_research][WARN] EMBEDDING_BASE_URL 미설정 — RAG local 모드는 BGE 임베딩 서버가 필요합니다.")

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

    # 선택: 비즈니스 DOCX 내보내기(표 지원)
    if args.docx:
        rc = _export_docx(report, out_path)
        if rc != 0:
            print("[run_research][WARN] DOCX 내보내기 실패(마크다운 보고서는 정상 저장됨)")
    return 0


def _export_docx(report: str, md_path: Path) -> int:
    """마크다운 보고서를 같은 경로의 .docx 로 내보낸다(비치명: 실패해도 md 는 유지)."""
    tools_dir = ROOT / "tools"
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    try:
        import md_to_docx as m2d  # noqa: E402
    except SystemExit:
        print("[run_research][WARN] python-docx 미설치 — DOCX 건너뜀(pip install python-docx 또는 setup 재실행)")
        return 1
    except Exception as e:
        print(f"[run_research][WARN] md_to_docx 로드 실패: {e}")
        return 1
    docx_path = md_path.with_suffix(".docx")
    try:
        m2d.build_report_docx(report, docx_path)
    except Exception as e:
        print(f"[run_research][WARN] DOCX 생성 예외: {type(e).__name__}: {e}")
        return 1
    print(f"[run_research] DOCX 내보내기 완료 → {docx_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
