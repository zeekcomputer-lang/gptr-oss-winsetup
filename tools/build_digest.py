"""
build_digest — 시간순 이벤트 정리 모드(Mode 2)용 map-reduce 다이제스트 엔진

목표(요구사항):
  - 유사도와 무관하게 **모든 문서를 반드시 읽고 요약**(누락 0). 시간 정보가 있으면 참고로 활용(강제 아님).
  - 컨텍스트 윈도우 초과 사전 회피: **입력 1회당 25KB(기본) 초과 금지**. 초과가 예상되면
    문서를 배치로 나눠 핵심을 압축/요약(map)하고, 합본이 또 크면 재귀 재요약(reduce).
  - 최종 산출물(다이제스트)은 예산 내 단일 문서 → Stage 2(gpt-researcher)가 한글 보고서로 작성.

누락 방지(코드 강제):
  - 각 문서는 고유 id 를 가지며, map 프롬프트는 문서마다 `[[<id>]]` 마커를 1개 이상 남기도록 지시.
  - map 결과에서 등장한 id 집합을 입력 id 집합과 대조 → 누락분은 **단건 배치로 자동 재처리**.
  - 이 검증은 LLM 신뢰가 아니라 마커 대조(프로그램)로 보장한다.

레이트리밋/내성(게이트웨이 타임아웃 대응):
  - 모든 LLM 호출은 rate_limit.get_limiter() (기본 4회/sec)를 통과한다.
  - 스트리밍(SSE) 기본 ON → idle 타임아웃 회피(미지원 서버는 비스트림 자동 폴백).
  - map 호출 용량초과/타임아웃 시 **적응형 분할 재시도**(배치→반으로→단건→문서조각).
  - 일시 실패는 LLM_MAX_RETRIES 회 backoff 재시도. 출력 토큰은 CHRONO_MAX_OUTPUT_TOKENS(기본 2000)로 cap.

의존성: stdlib only (urllib). vendor/gpt_researcher 불요 → prepare 직후 단독 실행 가능.

CLI:
  python tools/build_digest.py --doc-path data/docs --query "<주제>" --out data/digest/digest.md
env:
  OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_EXTRA_HEADERS, SMART_LLM,
  TEMPERATURE, LLM_MAX_RPS, CHRONO_MAX_INPUT_KB(기본 25), LANGUAGE(기본 korean),
  CHRONO_MAX_OUTPUT_TOKENS(기본 2000), LLM_TIMEOUT(기본 180), LLM_MAX_RETRIES(기본 2), GPTR_LLM_STREAM(기본 1),
  (용어사전은 .env 가 아니라 data/glossary.json 또는 data/glossary/*.json 파일로 주입 — map/reduce system 프롬프트에 덧붙)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rate_limit import get_limiter  # noqa: E402

# 프롬프트/마진 오버헤드(바이트). 배치 payload 예산 = 입력한도 - 이 값.
_PROMPT_OVERHEAD_BYTES = 3000


# ─────────────────────────────────────────────────────────────
#  LLM 호출 (stdlib urllib). 테스트는 LLM_CALL 을 교체해 mock 한다.
# ─────────────────────────────────────────────────────────────
def _expand(value: str) -> str:
    if "${uuid4}" in value:
        value = value.replace("${uuid4}", str(uuid.uuid4()))
    if "${uuid4hex}" in value:
        value = value.replace("${uuid4hex}", uuid.uuid4().hex)
    if "${epoch}" in value:
        value = value.replace("${epoch}", str(int(time.time())))
    return value


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    key = os.getenv("OPENAI_API_KEY")
    if key:
        h["Authorization"] = f"Bearer {key}"
    raw = os.getenv("OPENAI_EXTRA_HEADERS")
    if raw and raw.strip():
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                for k, v in data.items():
                    h[str(k)] = _expand(str(v))
        except json.JSONDecodeError:
            pass
    return h


def _model() -> str:
    m = os.getenv("SMART_LLM", "openai:gpt-oss-120b")
    return m.split(":", 1)[-1] if ":" in m else m


def _truthy(v, default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _out_tokens() -> int:
    """digest(map/reduce/추출) 전용 출력 토큰 cap. 기본 2000(생성시간·용량 완화).
    SMART_TOKEN_LIMIT(최종 보고서용, 8000)과 분리 — 요약은 긴 출력이 불필요."""
    return int(os.getenv("CHRONO_MAX_OUTPUT_TOKENS", "2000"))


def _timeout() -> float:
    return float(os.getenv("LLM_TIMEOUT", "180"))


def _default_llm_call(system: str, user: str) -> str:
    """OpenAI 호환 /chat/completions 동기 호출(레이트리밋 적용)."""
    import urllib.request

    base = os.getenv("OPENAI_BASE_URL", "").rstrip("/")
    if not base:
        raise RuntimeError("OPENAI_BASE_URL 미설정 — LLM 엔드포인트가 필요합니다.")
    url = base + "/chat/completions"
    stream = _truthy(os.getenv("GPTR_LLM_STREAM"), default=True)
    payload = {
        "model": _model(),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": float(os.getenv("TEMPERATURE", "0.4")),
        "max_tokens": _out_tokens(),
        "stream": stream,
    }
    get_limiter().acquire()  # 4회/sec 스페이싱
    if not stream:
        return _post_once(url, payload)
    # 스트리밍: 토큰을 흡려 게이트웨이 idle 타임아웃 회피. 미지원/빈응답이면 비스트림 폴백.
    try:
        content = _post_stream(url, payload)
    except Exception as e:
        print(f"[digest][WARN] 스트리밍 실패({type(e).__name__}) → 비스트림 재시도")
        payload["stream"] = False
        return _post_once(url, payload)
    if not content.strip():
        payload["stream"] = False
        return _post_once(url, payload)
    return content


def _post_once(url: str, payload: dict) -> str:
    """비스트림 POST 1회."""
    import urllib.request
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers=_headers())
    with urllib.request.urlopen(req, timeout=_timeout()) as r:
        resp = json.loads(r.read().decode("utf-8"))
    return resp["choices"][0]["message"]["content"] or ""


def _post_stream(url: str, payload: dict) -> str:
    """SSE 스트리밍 POST — delta.content 를 이어붙인다(연결 유지로 idle 504 회피)."""
    import urllib.request
    data = json.dumps(payload).encode("utf-8")
    headers = dict(_headers())
    headers["Accept"] = "text/event-stream"
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    parts: list[str] = []
    with urllib.request.urlopen(req, timeout=_timeout()) as r:
        while True:
            raw = r.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "ignore").strip()
            if not line:
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                break
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            ch = (obj.get("choices") or [{}])[0]
            piece = (ch.get("delta") or {}).get("content")
            if piece is None:  # 일부 서버는 message.content 로 전체를 한 번에
                piece = (ch.get("message") or {}).get("content")
            if piece:
                parts.append(piece)
    return "".join(parts)


# 테스트에서 교체 가능한 호출 훅
LLM_CALL = _default_llm_call


def _llm_call_resilient(system: str, user: str) -> str:
    """일시적 실패(타임아웃/5xx/네트워크)에 backoff 재시도. LLM_MAX_RETRIES(기본 2)회.
    capacity 류 영구 실패는 호출측(map 적응형 분할)에서 배치를 줄여 처리한다."""
    attempts = max(1, int(os.getenv("LLM_MAX_RETRIES", "2")) + 1)
    last: Exception | None = None
    for i in range(attempts):
        try:
            return LLM_CALL(system, user)
        except Exception as e:  # noqa: BLE001
            last = e
            if i < attempts - 1:
                back = min(2 ** i, 8)
                print(f"[digest][WARN] LLM 호출 실패({type(e).__name__}: {e}) → "
                      f"{back}s 후 재시도 {i + 1}/{attempts - 1}")
                time.sleep(back)
    assert last is not None
    raise last


# ─────────────────────────────────────────────────────────────
#  문서 로딩 / 정렬 / 배치
# ─────────────────────────────────────────────────────────────
_ID_RE = re.compile(r"^\s*-?\s*source_id\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_DATE_RE = re.compile(r"^\s*-?\s*date\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def _nbytes(s: str) -> int:
    return len(s.encode("utf-8"))


def load_docs(doc_dir: Path) -> list[dict]:
    """data/docs 의 .txt/.md 를 읽어 {id,date,title,content,path} 리스트로."""
    docs: list[dict] = []
    files = sorted(list(doc_dir.glob("*.txt")) + list(doc_dir.glob("*.md")))
    for i, p in enumerate(files, 1):
        raw = p.read_text(encoding="utf-8", errors="replace")
        mid = _ID_RE.search(raw)
        mdate = _DATE_RE.search(raw)
        doc_id = (mid.group(1).strip() if mid else "") or f"{p.stem}"
        first = raw.splitlines()[0] if raw.splitlines() else p.stem
        title = first.lstrip("# ").strip() or p.stem
        docs.append({
            "id": doc_id,
            "date": (mdate.group(1).strip() if mdate else ""),
            "title": title,
            "content": raw,
            "path": str(p),
            "seq": i,
        })
    return docs


def order_docs(docs: list[dict]) -> list[dict]:
    """시간순(날짜 있는 것 우선, 오름차순), 날짜 동률/없음은 seq(입력순) tie-break."""
    def key(d):
        has = 0 if d["date"] else 1  # 날짜 있는 문서 먼저
        return (has, d["date"], d["seq"])
    return sorted(docs, key=key)


def batch_docs(docs: list[dict], payload_budget: int) -> list[list[dict]]:
    """문서를 payload_budget(바이트) 이하 배치로 그룹핑.
    단건이 예산을 넘으면 content 를 잘라 여러 sub-doc(같은 id)으로 쪼갠다(누락 방지)."""
    batches: list[list[dict]] = []
    cur: list[dict] = []
    cur_bytes = 0
    for d in docs:
        units = _split_oversized(d, payload_budget)
        for u in units:
            ub = _nbytes(u["content"])
            if cur and cur_bytes + ub > payload_budget:
                batches.append(cur)
                cur, cur_bytes = [], 0
            cur.append(u)
            cur_bytes += ub
    if cur:
        batches.append(cur)
    return batches


def _split_oversized(doc: dict, budget: int) -> list[dict]:
    content = doc["content"]
    if _nbytes(content) <= budget:
        return [doc]
    # 바이트 예산에 맞춰 문자 단위로 안전 분할(멀티바이트 깨짐 방지 위해 인코딩 길이로 추정)
    parts: list[str] = []
    buf = ""
    for line in content.splitlines(keepends=True):
        if _nbytes(buf) + _nbytes(line) > budget and buf:
            parts.append(buf)
            buf = ""
        # 단일 라인이 예산 초과면 강제 절단
        while _nbytes(line) > budget:
            cut = line[: max(1, budget // 4)]
            parts.append(cut)
            line = line[len(cut):]
        buf += line
    if buf:
        parts.append(buf)
    return [
        {**doc, "content": f"[{doc['id']} part {i}/{len(parts)}]\n{seg}"}
        for i, seg in enumerate(parts, 1)
    ]


# ─────────────────────────────────────────────────────────────
#  Map / Coverage / Reduce
# ─────────────────────────────────────────────────────────────
def _lang() -> str:
    return os.getenv("LANGUAGE", "korean")


_GLOSSARY_CACHE: str | None = None


def _glossary_block() -> str:
    """용어사전(JSON) 주입 블록을 1회 로드해 캐시. 없으면 빈 문자열.
    chrono map/reduce 의 system 프롬프트에 덧붙여 전문용어 정의를 일관 적용."""
    global _GLOSSARY_CACHE
    if _GLOSSARY_CACHE is None:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import glossary as _g  # noqa: E402
            _GLOSSARY_CACHE = _g.get_block(verbose=True)
        except Exception as e:
            print(f"[digest][WARN] 용어사전 로드 실패(무시): {e}")
            _GLOSSARY_CACHE = ""
    return _GLOSSARY_CACHE


_MAP_SYSTEM = (
    "너는 다수의 원본 문서에서 핵심 이벤트를 누락 없이 추출하는 분석가다. "
    "반드시 {lang} 로 작성한다. 추측/창작 금지, 원문 근거만 사용한다."
)
_MAP_USER_TMPL = (
    "아래 문서들에서 핵심 이벤트/사실을 추출해 시간순(날짜 정보가 있으면 활용, 없으면 원문 순서) "
    "불릿으로 정리하라.\n"
    "규칙:\n"
    "1) 각 문서마다 최소 1개 이상의 항목을 남긴다(어떤 문서도 누락 금지).\n"
    "2) 각 항목 끝에 해당 문서 식별자를 `[[<id>]]` 형식으로 정확히 표기한다.\n"
    "3) 날짜가 있으면 항목 앞에 (YYYY-MM-DD) 로 표기한다. 없으면 생략한다.\n"
    "4) 간결하되 중요한 수치·고유명사·결정사항은 보존한다.\n"
    "5) 출력은 {lang} 불릿 목록만. 머리말/맺음말 금지.\n\n"
    "주제(참고): {query}\n\n"
    "=== 문서 목록 시작 ===\n{docs}\n=== 문서 목록 끝 ==="
)


def _render_batch(batch: list[dict]) -> str:
    blocks = []
    for d in batch:
        head = f"[id: {d['id']}]"
        if d["date"]:
            head += f" [date: {d['date']}]"
        blocks.append(f"{head}\n{d['content']}")
    return "\n\n----\n\n".join(blocks)


def _map_once(batch: list[dict], query: str) -> str:
    sys_p = _MAP_SYSTEM.format(lang=_lang()) + _glossary_block()
    usr_p = _MAP_USER_TMPL.format(lang=_lang(), query=query or "(미지정)", docs=_render_batch(batch))
    return _llm_call_resilient(sys_p, usr_p).strip()


def map_batch(batch: list[dict], query: str, payload_budget: int | None = None,
              verbose: bool = True) -> str:
    """배치 map — **적응형 분할 재시도**. 호출이 용량초과/타임아웃으로 실패하면
    배치를 반으로 쪼개서 재시도, 단건이면 문서를 더 잘게 분할해 게이트웨이 504 구조적 회피.
    분할 한계까지 실패한 문서는 건너뛴(커버리지 검증이 still_missing 으로 표시)."""
    if payload_budget is None:
        payload_budget = max(2000, int(os.getenv("CHRONO_MAX_INPUT_KB", "25")) * 1024
                             - _PROMPT_OVERHEAD_BYTES - _nbytes(_glossary_block()))
    try:
        return _map_once(batch, query)
    except Exception as e:  # noqa: BLE001
        if len(batch) > 1:
            mid = len(batch) // 2
            if verbose:
                print(f"[digest][WARN] 배치 map 실패 → 분할 재시도 "
                      f"({len(batch)}건 → {mid}+{len(batch) - mid}): {type(e).__name__}")
            left = map_batch(batch[:mid], query, payload_budget, verbose)
            right = map_batch(batch[mid:], query, payload_budget, verbose)
            return (left + "\n" + right).strip()
        # 단건: 문서를 더 잘게 쪼개 재시도
        doc = batch[0]
        smaller = max(2000, payload_budget // 2)
        parts = _split_oversized(doc, smaller)
        if len(parts) > 1:
            if verbose:
                print(f"[digest][WARN] 단건 map 실패 → 문서 분할({len(parts)}조각, "
                      f"예산 {smaller}B) 재시도: id={doc['id']}")
            return "\n".join(map_batch([p], query, smaller, verbose) for p in parts).strip()
        print(f"[digest][ERROR] map 실패(분할 한계 도달) → 건너뜀: id={doc['id']} ({type(e).__name__}: {e})")
        return ""


def found_ids(text: str) -> set[str]:
    """map 출력에서 [[id]] 마커로 등장한 문서 id 집합."""
    return set(m.strip() for m in re.findall(r"\[\[(.+?)\]\]", text or ""))


_REDUCE_SYSTEM = (
    "너는 시간순 이벤트 요약본을 더 짧게 통합하는 편집자다. 반드시 {lang} 로 작성한다."
)
_REDUCE_USER_TMPL = (
    "아래는 여러 배치에서 추출한 시간순 이벤트 요약 조각들이다. 이를 하나의 일관된 시간순 "
    "요약으로 통합·압축하라.\n"
    "규칙:\n"
    "1) 날짜·핵심 이벤트는 절대 삭제하지 않는다(중복만 병합, 산문만 압축).\n"
    "2) 시간 정보가 있으면 (YYYY-MM-DD) 로 시간순 정렬한다.\n"
    "3) 출력은 {lang} 불릿 목록만.\n\n"
    "주제(참고): {query}\n\n"
    "=== 요약 조각 시작 ===\n{chunks}\n=== 요약 조각 끝 ==="
)


def _reduce_once(pc: str, query: str) -> str:
    sys_p = _REDUCE_SYSTEM.format(lang=_lang()) + _glossary_block()
    usr_p = _REDUCE_USER_TMPL.format(lang=_lang(), query=query or "(미지정)", chunks=pc)
    return _llm_call_resilient(sys_p, usr_p).strip()


def _reduce_piece_adaptive(pc: str, query: str, min_bytes: int = 2000, verbose: bool = True) -> str:
    """reduce 호출이 용량초과/타임아웃으로 죽으면 조각을 반으로 쪼개서 재시도.
    더 이상 못 쪼개면 원문 조각을 그대로 보존(이벤트 누락 방지)."""
    try:
        return _reduce_once(pc, query)
    except Exception as e:  # noqa: BLE001
        if _nbytes(pc) > min_bytes:
            halves = _split_by_budget(pc, max(min_bytes, _nbytes(pc) // 2 + 1))
            if len(halves) > 1:
                if verbose:
                    print(f"[digest][WARN] reduce 조각 실패 → 분할 재시도"
                          f"({_nbytes(pc)}B → {len(halves)}조각): {type(e).__name__}")
                return "\n".join(_reduce_piece_adaptive(h, query, min_bytes, verbose)
                                 for h in halves).strip()
        print(f"[digest][ERROR] reduce 실패(분할 한계) → 조각 원문 보존: {type(e).__name__}: {e}")
        return pc


def reduce_text(text: str, payload_budget: int, query: str, max_levels: int = 5) -> str:
    """합본이 예산 초과면 조각을 묶어 재귀 재요약(이벤트 보존, 산문 압축).
    payload_budget 은 이미 (용어사전 포함) 시스템 프롬프트 오버헤드를 제외한 **유저 컨텐츠** 예산이다.
    각 조각은 payload_budget 이하로 나눔 → system(REDUCE+용어사전)+user 총합이 한도 내."""
    level = 0
    while _nbytes(text) > payload_budget and level < max_levels:
        level += 1
        pieces = _split_by_budget(text, payload_budget)
        if len(pieces) <= 1:
            break
        outs = [_reduce_piece_adaptive(pc, query) for pc in pieces]
        new_text = "\n".join(outs)
        if _nbytes(new_text) >= _nbytes(text):
            # 더 이상 줄지 않으면(조각 보존 등) 무한루프 방지
            break
        text = new_text
    return text


# ──────────────────────────────────────────────────
#  고유명·전문용어 정의 추출 (문서 유래 — 초기 용어집에 없더라도 본문에 정의가 명확한 것)
# ──────────────────────────────────────────────────
_EXTRACT_SYSTEM = (
    "너는 문서에서 고유명·전문용어와 그 정의를 추출하는 분석가다. "
    "본문에 정의가 명확히 제시된 항목만 추출한다. 추측·일반상식 보완 금지. 반드시 {lang} 로 작성."
)
_EXTRACT_USER_TMPL = (
    "아래 문서에서 고유명/전문용어 중 '정의가 본문에 명확히 드러난' 것만 추출하라.\n"
    "출력 형식(엄격): 한 줄에 하나씩 `용어 ||| 한 줄 정의`.\n"
    "규칙:\n"
    "1) 정의가 불확실하거나 본문에 없으면 제외(추측 금지).\n"
    "2) 일반 명사·흔한 단어 제외. 고유명·약어·전문용어 위주.\n"
    "3) 이미 알려진 용어는 제외: {known}\n"
    "4) 머리말·맺음말·번호·불릿 없이 `용어 ||| 정의` 줄만 출력. 없으면 빈 출력.\n\n"
    "=== 문서 시작 ===\n{doc}\n=== 문서 끝 ==="
)


def extract_definitions(text: str, known_terms: list[str] | None = None,
                        max_input_kb: int | None = None) -> list[dict]:
    """문서(다이제스트)에서 정의가 명확한 고유명/전문용어를 추출.
    건전한 줄 형식(`용어 ||| 정의`)으로 받아 JSON 미준수 모델에도 강건. 실패해도 빈 리스트."""
    if not text or not text.strip():
        return []
    if max_input_kb is None:
        max_input_kb = int(os.getenv("CHRONO_MAX_INPUT_KB", "25"))
    budget = max(2000, max_input_kb * 1024 - _PROMPT_OVERHEAD_BYTES)
    if _nbytes(text) > budget:
        text = text.encode("utf-8")[:budget].decode("utf-8", "ignore")
    known = ", ".join(known_terms or []) or "(없음)"
    sys_p = _EXTRACT_SYSTEM.format(lang=_lang())
    usr_p = _EXTRACT_USER_TMPL.format(known=known, doc=text)
    try:
        out = _llm_call_resilient(sys_p, usr_p).strip()
    except Exception as e:
        print(f"[digest][WARN] 용어 정의 추출 실패(건너뜀): {type(e).__name__}: {e}")
        return []
    terms: list[dict] = []
    seen: set[str] = set()
    for line in out.splitlines():
        line = re.sub(r'^\s*([-*•]|\d+[.)])\s+', '', line.strip()).strip()
        if "|||" not in line:
            continue
        term, _, defi = line.partition("|||")
        term, defi = term.strip().strip('`*').strip(), defi.strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            terms.append({"term": term, "definition": defi, "aliases": []})
    return terms


def _split_by_budget(text: str, budget: int) -> list[str]:
    lines = text.splitlines(keepends=True)
    out, buf = [], ""
    for ln in lines:
        if _nbytes(buf) + _nbytes(ln) > budget and buf:
            out.append(buf)
            buf = ""
        buf += ln
    if buf:
        out.append(buf)
    return out


# ─────────────────────────────────────────────────────────────
#  파이프라인
# ─────────────────────────────────────────────────────────────
def build_digest(doc_dir: Path, query: str, max_input_kb: int = 25,
                 verbose: bool = True) -> tuple[str, dict]:
    """전 문서를 map-reduce 로 압축한 시간순 다이제스트(text)와 통계(dict) 반환."""
    docs = order_docs(load_docs(doc_dir))
    if not docs:
        raise RuntimeError(f"문서 없음: {doc_dir} — 먼저 prepare 로 변환하세요.")
    all_ids = {d["id"] for d in docs}
    # 용어사전 블록(최대 8KB)은 map/reduce system 프롬프트에 매번 들어간다.
    # 그 크기를 입력 예산에서 빼지 않으면 system+user 총합이 한도를 넘어 504 발생(용어사전 추가 후 회귀).
    gloss_bytes = _nbytes(_glossary_block())
    overhead = _PROMPT_OVERHEAD_BYTES + gloss_bytes
    payload_budget = max(2000, max_input_kb * 1024 - overhead)

    batches = batch_docs(docs, payload_budget)
    if verbose:
        extra = f", 용어사전 {gloss_bytes}B 반영" if gloss_bytes else ""
        print(f"[digest] 문서 {len(docs)}건 / 고유 id {len(all_ids)}개 / "
              f"배치 {len(batches)}개 (입력예산 {payload_budget}B{extra})")

    map_outputs: list[str] = []
    for bi, batch in enumerate(batches, 1):
        if verbose:
            print(f"[digest] map {bi}/{len(batches)} (문서 {len(batch)}건, "
                  f"{_nbytes(_render_batch(batch))}B)")
        map_outputs.append(map_batch(batch, query, payload_budget))

    combined = "\n".join(map_outputs)

    # 커버리지 검증 + 누락분 단건 재처리
    seen = found_ids(combined)
    missing = sorted(all_ids - seen)
    reprocessed = 0
    if missing and verbose:
        print(f"[digest][WARN] 마커 누락 {len(missing)}건 → 단건 재처리: {missing[:10]}{'...' if len(missing) > 10 else ''}")
    for mid in missing:
        doc = next((d for d in docs if d["id"] == mid), None)
        if doc is None:
            continue
        for sub in _split_oversized(doc, payload_budget):
            combined += "\n" + map_batch([sub], query, payload_budget)
        reprocessed += 1

    # 재검증(최종 누락 여부 기록)
    final_seen = found_ids(combined)
    still_missing = sorted(all_ids - final_seen)

    # 예산 초과 시 재귀 reduce
    digest = reduce_text(combined, payload_budget, query)

    stats = {
        "docs": len(docs),
        "unique_ids": len(all_ids),
        "batches": len(batches),
        "reprocessed": reprocessed,
        "still_missing": still_missing,
        "digest_bytes": _nbytes(digest),
        "payload_budget": payload_budget,
    }
    return digest, stats


def write_digest(digest: str, out_path: Path, query: str) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# 시간순 이벤트 다이제스트\n\n"
        f"- 주제: {query or '(미지정)'}\n"
        f"- 생성: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"---\n\n"
    )
    out_path.write_text(header + digest + "\n", encoding="utf-8")


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _common import DOCS_DIR, DATA_DIR  # noqa: E402

    ap = argparse.ArgumentParser(description="시간순 이벤트 map-reduce 다이제스트 생성")
    ap.add_argument("--doc-path", default=str(DOCS_DIR))
    ap.add_argument("--query", default="")
    ap.add_argument("--out", default=str(DATA_DIR / "digest" / "digest.md"))
    ap.add_argument("--max-input-kb", type=int,
                    default=int(os.getenv("CHRONO_MAX_INPUT_KB", "25")))
    args = ap.parse_args()

    doc_dir = Path(args.doc_path)
    try:
        digest, stats = build_digest(doc_dir, args.query, args.max_input_kb)
    except RuntimeError as e:
        print(f"[digest][ERROR] {e}", file=sys.stderr)
        return 1
    write_digest(digest, Path(args.out), args.query)
    print(f"[digest] 완료 → {args.out}")
    print(f"[digest] 통계: {stats}")
    if stats["still_missing"]:
        print(f"[digest][WARN] 최종 누락 id {len(stats['still_missing'])}건: "
              f"{stats['still_missing'][:10]} — 모델이 마커를 안 남겼을 수 있음(내용은 입력됨).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
