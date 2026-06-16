"""
glossary — 용어사전(JSON) 로더 + 프롬프트 주입 블록 렌더러

요약/보고서 문서가 전문 지식(고유명사·전문 용어 정의·약어 풀이)을 필요로 할 때,
사전에 **용어사전(JSON)** 을 주입해 LLM 이 정의를 일관되게 사용하도록 한다.
RAG 모드(보고서 프롬프트)와 chrono 모드(map-reduce 다이제스트) 양쪽에서 공용.

지원 형식(자동 판별):
  1) 평면 dict:   {"BGE": "...", "RAG": "..."}
  2) terms 배열:  {"terms": [{"term": "BGE", "definition": "...", "aliases": ["bge-m3"]}], "instruction": "..."}
  3) 항목 배열:   [{"term": "...", "definition": "...", "aliases": [...]}]
  각 항목 키 별칭 허용: term=term|word|name|key , definition=definition|def|desc|meaning|value

경로 결정(우선순위):
  1) 인자/ env GPTR_GLOSSARY (명시 경로, 우선)
  2) 기본 data/glossary.json (있으면 자동 사용)
  3) 없으면 비활성(빈 블록 반환)

env:
  GPTR_GLOSSARY        용어사전 파일 경로(.json). 미설정 시 data/glossary.json 자동탐지.
  GPTR_GLOSSARY_MAX_KB 주입 블록 최대 크기(KB, 기본 8). 초과 시 잘라내고 경고 주석을 남긴다.

stdlib only — vendor/gpt_researcher 불요.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# repo 루트(= tools/ 의 상위). 기본 용어사전 경로 계산용.
_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_PATH = _ROOT / "data" / "glossary.json"

_TERM_KEYS = ("term", "word", "name", "key")
_DEF_KEYS = ("definition", "def", "desc", "description", "meaning", "value")
_ALIAS_KEYS = ("aliases", "alias", "synonyms", "abbr", "abbreviations")


def resolve_path(explicit: str | None = None) -> Path | None:
    """용어사전 파일 경로 해석. 없으면 None."""
    cand = explicit or os.getenv("GPTR_GLOSSARY")
    if cand and cand.strip():
        p = Path(cand.strip()).expanduser()
        return p if p.exists() else None
    return _DEFAULT_PATH if _DEFAULT_PATH.exists() else None


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


def load_terms(path: Path) -> tuple[list[dict], str]:
    """파일을 읽어 (terms, instruction) 반환.
    terms = [{"term","definition","aliases":[...]}], instruction = 선택 안내문(없으면 "")."""
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    instruction = ""
    items: list = []

    if isinstance(data, dict):
        instruction = str(data.get("instruction", "") or "").strip()
        if "terms" in data and isinstance(data["terms"], list):
            items = data["terms"]
        else:
            # 평면 dict {term: definition}
            items = [{"term": k, "definition": v} for k, v in data.items()
                     if k not in ("instruction", "terms")]
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError("용어사전 JSON 은 object 또는 array 여야 합니다.")

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


_DEFAULT_INSTRUCTION = (
    "아래 용어사전의 정의를 **반드시** 준수하라. 고유명사·전문용어·약어는 이 정의에 맞춰 "
    "일관되게 사용하고, 정의와 충돌하는 추측을 하지 않는다. 용어사전에 없는 내용은 원문 근거를 따른다."
)


def render_block(terms: list[dict], instruction: str = "", max_kb: int | None = None) -> str:
    """프롬프트에 덧붙일 용어사전 지시 블록(텍스트). terms 가 비면 빈 문자열."""
    if not terms:
        return ""
    if max_kb is None:
        max_kb = int(os.getenv("GPTR_GLOSSARY_MAX_KB", "8") or 8)
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
                  f"포함됨 — GPTR_GLOSSARY_MAX_KB 상향 또는 항목 축약 권장]")
    return block


def get_block(explicit: str | None = None, max_kb: int | None = None, verbose: bool = False) -> str:
    """경로 해석 → 로드 → 블록 렌더까지 한 번에. 실패/없음이면 빈 문자열(비치명)."""
    path = resolve_path(explicit)
    if path is None:
        return ""
    try:
        terms, instruction = load_terms(path)
    except Exception as e:
        if verbose:
            print(f"[glossary][WARN] 용어사전 로드 실패({path}): {type(e).__name__}: {e}")
        return ""
    block = render_block(terms, instruction)
    if verbose and block:
        print(f"[glossary] 용어사전 적용: {path} ({len(terms)}개 용어)")
    return block


def info(explicit: str | None = None) -> dict:
    """진단용 요약 정보."""
    path = resolve_path(explicit)
    if path is None:
        return {"path": None, "count": 0, "instruction": "", "bytes": 0}
    terms, instruction = load_terms(path)
    block = render_block(terms, instruction)
    return {
        "path": str(path),
        "count": len(terms),
        "instruction": instruction or _DEFAULT_INSTRUCTION,
        "bytes": len(block.encode("utf-8")),
        "sample": [t["term"] for t in terms[:10]],
    }


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="용어사전(JSON) 점검/미리보기")
    ap.add_argument("--path", default=None, help="용어사전 경로(.json). 기본 GPTR_GLOSSARY 또는 data/glossary.json")
    ap.add_argument("--show", action="store_true", help="렌더된 주입 블록 전문 출력")
    args = ap.parse_args()

    path = resolve_path(args.path)
    if path is None:
        where = args.path or os.getenv("GPTR_GLOSSARY") or str(_DEFAULT_PATH)
        print(f"[glossary] 용어사전 없음: {where}")
        print("  → JSON 을 만들고 GPTR_GLOSSARY 로 지정하거나 data/glossary.json 에 두세요.")
        print('  형식 예: {"terms":[{"term":"BGE","definition":"...","aliases":["bge-m3"]}]}')
        return 0
    try:
        meta = info(args.path)
    except Exception as e:
        print(f"[glossary][ERROR] 로드 실패: {type(e).__name__}: {e}")
        return 1
    print(f"[glossary] 경로     : {meta['path']}")
    print(f"[glossary] 용어 수  : {meta['count']}")
    print(f"[glossary] 블록크기 : {meta['bytes']} bytes (한도 {os.getenv('GPTR_GLOSSARY_MAX_KB','8')}KB)")
    print(f"[glossary] 샘플     : {meta['sample']}")
    if args.show:
        print("\n--- 주입 블록 ---")
        print(get_block(args.path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
