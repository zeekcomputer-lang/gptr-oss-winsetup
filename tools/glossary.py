"""
glossary — 용어사전(파일) 로더 + 프롬프트 주입 블록 렌더러

요약/보고서 문서가 전문 지식(고유명사·전문 용어 정의·약어 풀이)을 필요로 할 때,
사전에 **용어사전 파일**을 주입해 LLM 이 정의를 일관되게 사용하도록 한다.
RAG 모드(보고서 프롬프트)와 chrono 모드(map-reduce 다이제스트) 양쪽에서 공용.

★ 설정 방식: **.env 가 아니라 data 하위 "파일"** 로 존재한다(존재할 때만 활용).
   - `data/glossary.json`            ← 단일 파일(권장)
   - `data/glossary/*.json`          ← 디렉터리(여러 파일을 병합; 파일명 오름차순)
   둘 다 없으면 비활성(no-op) — 기존 동작 그대로. 환경변수로 경로/크기를 지정하지 않는다.
   (시작 템플릿: `cp examples/sample-glossary.json data/glossary.json`)

지원 형식(자동 판별):
  1) 평면 dict:   {"BGE": "...", "RAG": "..."}
  2) terms 배열:  {"terms": [{"term": "BGE", "definition": "...", "aliases": ["bge-m3"]}], "instruction": "..."}
  3) 항목 배열:   [{"term": "...", "definition": "...", "aliases": [...]}]
  각 항목 키 별칭 허용: term=term|word|name|key , definition=definition|def|desc|meaning|value

stdlib only — vendor/gpt_researcher 불요.
"""
from __future__ import annotations

import json
from pathlib import Path

# repo 루트(= tools/ 의 상위). data 하위 용어사전 경로 계산용.
_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _ROOT / "data"
_GLOSSARY_FILE = _DATA_DIR / "glossary.json"      # 단일 파일
_GLOSSARY_DIR = _DATA_DIR / "glossary"            # 디렉터리(여러 파일 병합)

# 주입 블록 최대 크기(KB). 파일 기반 운영이므로 상수로 고정(.env 미사용).
_DEFAULT_MAX_KB = 8

_TERM_KEYS = ("term", "word", "name", "key")
_DEF_KEYS = ("definition", "def", "desc", "description", "meaning", "value")
_ALIAS_KEYS = ("aliases", "alias", "synonyms", "abbr", "abbreviations")


def resolve_sources(explicit: str | None = None) -> list[Path]:
    """활용할 용어사전 파일 목록을 반환(존재하는 것만).

    우선순위:
      1) explicit (CLI --path 로 명시한 단일 파일/디렉터리) — 미리보기/임시용
      2) data/glossary.json (단일 파일)
      3) data/glossary/*.json (디렉터리 병합, 파일명 오름차순)
      없으면 빈 리스트 → 비활성(no-op).
    """
    if explicit and explicit.strip():
        p = Path(explicit.strip()).expanduser()
        if p.is_dir():
            return sorted(p.glob("*.json"))
        return [p] if p.exists() else []
    if _GLOSSARY_FILE.exists():
        return [_GLOSSARY_FILE]
    if _GLOSSARY_DIR.is_dir():
        return sorted(_GLOSSARY_DIR.glob("*.json"))
    return []


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v)]


def _pick(d: dict, keys) -> str:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return str(d[k]).strip()
    return ""


def _load_one(path: Path) -> tuple[list[dict], str]:
    """파일 1개 → (terms, instruction)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    instruction = ""
    items: list = []
    if isinstance(data, dict):
        instruction = str(data.get("instruction", "") or "").strip()
        if "terms" in data and isinstance(data["terms"], list):
            items = data["terms"]
        else:
            items = [{"term": k, "definition": v} for k, v in data.items()
                     if k not in ("instruction", "terms")]
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError(f"{path.name}: 용어사전 JSON 은 object 또는 array 여야 합니다.")

    terms: list[dict] = []
    for it in items:
        if isinstance(it, dict):
            term = _pick(it, _TERM_KEYS)
            definition = _pick(it, _DEF_KEYS)
            aliases = []
            for ak in _ALIAS_KEYS:
                if ak in it:
                    aliases = _as_list(it[ak])
                    break
        elif isinstance(it, (list, tuple)) and len(it) >= 2:
            term, definition, aliases = str(it[0]).strip(), str(it[1]).strip(), []
        else:
            continue
        if term:
            terms.append({"term": term, "definition": definition, "aliases": aliases})
    return terms, instruction


def load_terms(sources: list[Path]) -> tuple[list[dict], str]:
    """여러 파일을 병합해 (terms, instruction) 반환.
    - instruction: 첫 번째로 발견된 비어있지 않은 값 사용.
    - terms: 파일 순서대로 누적, 동일 term(대소문자 무시)은 먼저 등장한 정의 유지."""
    merged: list[dict] = []
    seen: set[str] = set()
    instruction = ""
    for p in sources:
        terms, inst = _load_one(p)
        if not instruction and inst:
            instruction = inst
        for t in terms:
            key = t["term"].lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(t)
    return merged, instruction


_DEFAULT_INSTRUCTION = (
    "아래 용어사전의 정의를 **반드시** 준수하라. 고유명사·전문용어·약어는 이 정의에 맞춰 "
    "일관되게 사용하고, 정의와 충돌하는 추측을 하지 않는다. 용어사전에 없는 내용은 원문 근거를 따른다."
)


def render_block(terms: list[dict], instruction: str = "", max_kb: int = _DEFAULT_MAX_KB) -> str:
    """프롬프트에 덧붙일 용어사전 지시 블록(텍스트). terms 가 비면 빈 문자열."""
    if not terms:
        return ""
    budget = max(1024, max_kb * 1024)
    head = "\n\n[용어사전] " + (instruction.strip() or _DEFAULT_INSTRUCTION) + "\n"
    lines: list[str] = []
    truncated = False
    cur = len(head.encode("utf-8")) + len("[용어사전 끝]".encode("utf-8")) + 8
    for t in terms:
        alias = f" (별칭: {', '.join(t['aliases'])})" if t.get("aliases") else ""
        defi = t.get("definition") or "(정의 미기재)"
        line = f"- {t['term']}{alias}: {defi}"
        b = len((line + "\n").encode("utf-8"))
        if cur + b > budget:
            truncated = True
            break
        lines.append(line)
        cur += b
    block = head + "\n".join(lines) + "\n[용어사전 끝]"
    if truncated:
        block += (f"\n[주: 용어사전이 {max_kb}KB 한도를 초과해 {len(lines)}/{len(terms)}개만 "
                  f"포함됨 — 항목 축약 또는 우선순위 정리 권장]")
    return block


def get_block(explicit: str | None = None, verbose: bool = False) -> str:
    """경로 해석 → 로드 → 블록 렌더까지 한 번에. 파일 없음/실패면 빈 문자열(비치명)."""
    sources = resolve_sources(explicit)
    if not sources:
        return ""
    try:
        terms, instruction = load_terms(sources)
    except Exception as e:
        if verbose:
            print(f"[glossary][WARN] 용어사전 로드 실패({[p.name for p in sources]}): "
                  f"{type(e).__name__}: {e}")
        return ""
    block = render_block(terms, instruction)
    if verbose and block:
        where = sources[0].name if len(sources) == 1 else f"{len(sources)}개 파일 병합"
        print(f"[glossary] 용어사전 적용: {where} ({len(terms)}개 용어)")
    return block


def info(explicit: str | None = None) -> dict:
    """진단용 요약 정보."""
    sources = resolve_sources(explicit)
    if not sources:
        return {"sources": [], "count": 0, "instruction": "", "bytes": 0, "sample": []}
    terms, instruction = load_terms(sources)
    block = render_block(terms, instruction)
    return {
        "sources": [str(p) for p in sources],
        "count": len(terms),
        "instruction": instruction or _DEFAULT_INSTRUCTION,
        "bytes": len(block.encode("utf-8")),
        "sample": [t["term"] for t in terms[:10]],
    }


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="용어사전(파일) 점검/미리보기")
    ap.add_argument("--path", default=None,
                    help="임시 점검용 경로(.json 파일 또는 디렉터리). 기본: data/glossary.json 또는 data/glossary/")
    ap.add_argument("--show", action="store_true", help="렌더된 주입 블록 전문 출력")
    args = ap.parse_args()

    sources = resolve_sources(args.path)
    if not sources:
        print("[glossary] 용어사전 파일 없음.")
        print(f"  탐색 경로: {_GLOSSARY_FILE}  또는  {_GLOSSARY_DIR}/*.json")
        print("  → 시작 템플릿: cp examples/sample-glossary.json data/glossary.json")
        print('  형식 예: {"terms":[{"term":"BGE","definition":"...","aliases":["bge-m3"]}]}')
        return 0
    try:
        meta = info(args.path)
    except Exception as e:
        print(f"[glossary][ERROR] 로드 실패: {type(e).__name__}: {e}")
        return 1
    print(f"[glossary] 파일     : {meta['sources']}")
    print(f"[glossary] 용어 수  : {meta['count']}")
    print(f"[glossary] 블록크기 : {meta['bytes']} bytes (한도 {_DEFAULT_MAX_KB}KB)")
    print(f"[glossary] 샘플     : {meta['sample']}")
    if args.show:
        print("\n--- 주입 블록 ---")
        print(get_block(args.path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
