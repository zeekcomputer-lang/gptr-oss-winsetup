"""
prepare_data — 로컬 데이터(jsonl/csv/json) → gpt-researcher 로컬 문서(.md) 변환기

배경:
  gpt-researcher 의 DocumentLoader 는 pdf/txt/md/docx/csv/xls(x)/html 만 적재한다.
  **jsonl 은 기본 미지원**(loader 에 매핑 없음 → 조용히 건너뜀).
  따라서 사용자의 jsonl(또는 csv/json) 을 레코드별 .md 파일로 펼쳐
  DOC_PATH(기본 data/docs) 에 저장한다. 이후 REPORT_SOURCE=local 로 리서치한다.

입력(자동 감지, 확장자/내용 기반):
  - .jsonl / .ndjson  : 한 줄당 JSON object 1건
  - .json             : 최상위가 배열이면 각 원소가 1건, object 면 1건
  - .csv              : 헤더 행 기준, 각 데이터 행이 1건

각 레코드 → 1개 .md:
  ┌──────────────────────────────
  │ # <title>
  │
  │ - source_id: <id>
  │ - <meta-key>: <meta-val>   (선택)
  │ ...
  │
  │ <content 본문>
  └──────────────────────────────

필드 매핑:
  --title-field    제목 필드명 (기본: 자동 추정 title|name|headline|subject)
  --content-field  본문 필드명 (기본: 자동 추정 content|text|body|abstract|summary)
  --id-field       식별자 필드명 (기본: 자동 추정 id|doc_id|uid; 없으면 일련번호)
  --meta-field     메타로 보존할 필드 (반복 지정 가능). 미지정 시 나머지 전체 보존
  지정한 필드가 레코드에 없으면 다음 후보로 폴백, 그래도 없으면 빈값 처리.

사용:
  python tools/prepare_data.py data/raw/my.jsonl
  python tools/prepare_data.py data/raw/ --content-field text --title-field headline
  python tools/prepare_data.py corpus.jsonl --out data/docs --clean

옵션:
  path             입력 파일 또는 디렉터리(여러 파일 일괄)
  --out            출력 디렉터리 (기본: data/docs = DOC_PATH)
  --clean          출력 디렉터리를 비우고 새로 생성
  --max-records    상한 (디버그용)
  --min-chars      본문 최소 길이(미만은 skip, 기본 1)
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import DATA_RAW_DIR, DOCS_DIR, section  # noqa: E402

_TITLE_CANDIDATES = ["title", "name", "headline", "subject", "heading"]
_CONTENT_CANDIDATES = ["content", "text", "body", "abstract", "summary", "raw_content", "page_content"]
_ID_CANDIDATES = ["id", "doc_id", "uid", "_id", "key"]

_SAFE = re.compile(r"[^0-9A-Za-z가-힣._-]+")


def _pick(rec: dict, preferred: str | None, candidates: list[str]) -> tuple[str, object]:
    """preferred 우선, 없으면 candidates 순으로 첫 존재 필드 반환. (key, value)"""
    if preferred and preferred in rec and rec[preferred] not in (None, ""):
        return preferred, rec[preferred]
    for c in candidates:
        if c in rec and rec[c] not in (None, ""):
            return c, rec[c]
    return "", ""


def _sanitize(s: str, fallback: str) -> str:
    s = _SAFE.sub("_", str(s)).strip("_")
    return (s or fallback)[:60]


def _iter_records(path: Path):
    """입력 파일에서 dict 레코드를 순차 yield."""
    suffix = path.suffix.lower()
    if suffix in (".jsonl", ".ndjson"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"  [skip] {path.name}:{i} JSON 파싱 실패: {e}")
                continue
            if isinstance(obj, dict):
                yield obj
            else:
                yield {"content": str(obj)}
    elif suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            for obj in data:
                yield obj if isinstance(obj, dict) else {"content": str(obj)}
        elif isinstance(data, dict):
            # {"records":[...]} 같은 래핑도 흔함 → 배열 값 자동 탐색
            for v in data.values():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    for obj in v:
                        yield obj
                    return
            yield data
    elif suffix == ".csv":
        with path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                yield dict(row)
    else:
        print(f"  [skip] 미지원 확장자: {path.name} (.jsonl/.json/.csv 만)")


def _collect_inputs(path: Path) -> list[Path]:
    if path.is_dir():
        out = []
        for ext in ("*.jsonl", "*.ndjson", "*.json", "*.csv"):
            out += sorted(path.glob(ext))
        return out
    return [path]


def convert(args) -> int:
    in_path = Path(args.path)
    if not in_path.is_absolute():
        # 상대경로는 data/raw 기준으로도 시도
        cand = DATA_RAW_DIR / args.path
        in_path = in_path if in_path.exists() else (cand if cand.exists() else in_path)
    if not in_path.exists():
        print(f"[prepare_data][ERROR] 입력 없음: {in_path}")
        return 1

    out_dir = Path(args.out) if args.out else DOCS_DIR
    ext = ".txt" if args.format == "txt" else ".md"
    if args.clean and out_dir.exists():
        for pat in ("*.md", "*.txt"):
            for p in out_dir.glob(pat):
                p.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)

    inputs = _collect_inputs(in_path)
    if not inputs:
        print(f"[prepare_data][ERROR] 변환할 파일 없음: {in_path}")
        return 1

    section(f"변환 시작 — 입력 {len(inputs)}개 → {out_dir}")
    written = 0
    skipped = 0
    seq = 0
    for src in inputs:
        print(f"[file] {src}")
        for rec in _iter_records(src):
            if args.max_records and written >= args.max_records:
                break
            seq += 1
            content_key, content = _pick(rec, args.content_field, _CONTENT_CANDIDATES)
            content = "" if content is None else str(content).strip()
            if len(content) < args.min_chars:
                skipped += 1
                continue
            title_key, title = _pick(rec, args.title_field, _TITLE_CANDIDATES)
            title = str(title).strip() if title else f"문서 {seq}"
            id_key, rid = _pick(rec, args.id_field, _ID_CANDIDATES)
            rid = str(rid).strip() if rid else f"{seq:06d}"

            # 메타: 지정 필드만 또는 (미지정 시) 실제 사용한 본문/제목/ID 키 제외 나머지
            meta_lines = []
            if args.meta_field:
                for mf in args.meta_field:
                    if mf in rec and rec[mf] not in (None, ""):
                        meta_lines.append(f"- {mf}: {rec[mf]}")
            else:
                used = {content_key, title_key, id_key}
                used.discard("")
                for k, v in rec.items():
                    if k in used or v in (None, ""):
                        continue
                    sval = str(v)
                    if len(sval) > 200:
                        sval = sval[:200] + "…"
                    meta_lines.append(f"- {k}: {sval}")

            fname = f"{seq:06d}_{_sanitize(rid if id_key else title, str(seq))}{ext}"
            # txt: 제목/메타를 평문 텍스트로 (TextLoader — unstructured/NLTK 미경유, 오프라인 견고)
            # md : Markdown 헤더 (UnstructuredMarkdownLoader — NLTK punkt_tab 필요)
            if ext == ".txt":
                body = [title, "", f"source_id: {rid}"]
                body += [ln.lstrip("- ") for ln in meta_lines]
            else:
                body = [f"# {title}", "", f"- source_id: {rid}"]
                body += meta_lines
            body += ["", content, ""]
            (out_dir / fname).write_text("\n".join(body), encoding="utf-8")
            written += 1
        if args.max_records and written >= args.max_records:
            break

    section("변환 완료")
    print(f"  생성  : {written} 파일({ext}) → {out_dir}")
    print(f"  건너뜀: {skipped} (본문 < {args.min_chars}자)")
    print(f"  DOC_PATH 로 사용할 경로: {out_dir}")
    if written == 0:
        print("  [WARN] 0건 생성 — 필드 매핑(--content-field 등)을 확인하세요.")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="로컬 데이터(jsonl/csv/json) → gpt-researcher 문서(.md) 변환")
    ap.add_argument("path", help="입력 파일/디렉터리 (.jsonl/.json/.csv)")
    ap.add_argument("--out", default=None, help="출력 디렉터리 (기본 data/docs)")
    ap.add_argument("--title-field", default=None)
    ap.add_argument("--content-field", default=None)
    ap.add_argument("--id-field", default=None)
    ap.add_argument("--meta-field", action="append", default=None,
                    help="메타로 보존할 필드(반복 지정). 미지정 시 나머지 전체 보존")
    ap.add_argument("--format", choices=["txt", "md"], default="txt",
                    help="출력 포맷. txt(기본, TextLoader—오프라인 권장) | md(UnstructuredMarkdownLoader—NLTK 필요)")
    ap.add_argument("--clean", action="store_true", help="출력 디렉터리의 .md/.txt 를 먼저 비움")
    ap.add_argument("--max-records", type=int, default=0)
    ap.add_argument("--min-chars", type=int, default=1)
    return convert(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
