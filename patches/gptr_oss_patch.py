"""
gptr_oss_patch — GPT-Researcher × GPT-OSS 런타임 패치 (monkeypatch, repo 무수정)

목적:
  1) LLM(챗) 호출에만 커스텀 default_headers 주입 (deep-doc-pipeline 패턴 차용)
       - OpenAI 호환 엔드포인트 + 게이트웨이/프록시 인증 헤더 지원
       - 임베딩(BGE)에는 절대 주입하지 않음 (요구사항: 임베딩은 헤더 없음)
  2) tool-calling 경로 차단 보조: supports_tools() 를 False 로 강제(옵션)
  3) GPT-OSS는 strict function-calling/JSON mode 미지원 가정 → 메인 파이프라인만 사용

사용:
  import gptr_oss_patch          # import 시점에 자동 적용
  gptr_oss_patch.apply()         # 또는 명시 호출(멱등)

환경변수:
  OPENAI_BASE_URL          LLM 엔드포인트 (예: http://localhost:11434/v1)
  OPENAI_API_KEY           SDK 필수값 충족용 (헤더 인증이면 'unused' 등 임의값)
  OPENAI_EXTRA_HEADERS     LLM 전용 헤더 (JSON). SDK(ChatOpenAI)의 **default_headers**
                           인자로 전달된다(deepdoc 방식). 예:
                           {"x-ticket":"key_123","user_Id":"abcde"}
                           값에 플레이스홀더 사용 가능(패치가 치환, 프로세스당 1회):
                             ${uuid4}    → 36자 UUID4 (예: 3f9c...-...)
                             ${uuid4hex} → 32자 hex UUID
                             ${epoch}    → 유닉스 초
                           예: {"X-Request-Id":"${uuid4}","Authorization":"Bearer xxx"}
  OPENAI_DYNAMIC_UUID_HEADER  설정하면 해당 이름의 헤더를 **매 요청마다 새 UUID4** 로
                           붙인다(httpx 이벤트 훅). 쉼표로 여러 개 지정 가능(헤더별 독립 uuid):
                             예: X-Trace-Id,X-Session-Id
                           - 비워두면 비활성. LLM 호출에만 적용, 임베딩은 무관.

  헤더 우선순위: 1) OPENAI_EXTRA_HEADERS(.env)  2) _HARDCODED_LLM_HEADERS(코드)  3) 없으면 헤더 없이 호출.
                           (.env 가 있으면 그것만 사용, 없으면 하드코딩 폴백 — 병합 아님)
  EMBEDDING_BASE_URL       임베딩(BGE) 전용 엔드포인트 (예: http://127.0.0.1:8999/v1)
                           - LLM 과 분리. 헤더 주입 없음(요구사항).
  EMBEDDING_API_KEY        임베딩 SDK 필수값 충족용 (기본 'unused')
  GPTR_DISABLE_TOOLCALLING 1/true 면 supports_tools() → False 강제 (기본 1)
  (용어사전) 설정은 .env 가 아니라 **파일**로 한다 — `data/glossary.json` 또는 `data/glossary/*.json`
                           이 있을 때만 보고서 프롬프트에 전문용어·고유명 정의를 주입한다(없으면 no-op).
                           자세한 건 tools/glossary.py 참고.

주의:
  - 이 모듈은 gpt_researcher 를 import 하기 전이나 후 아무 때나 apply() 가능.
  - 멱등: 여러 번 호출해도 1회만 적용.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid

_APPLIED = False

# repo 루트/offline 번들 경로 (tools/_common 없이도 동작하도록 자체 계산)
_PATCH_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OFFLINE_DIR = os.path.join(_PATCH_ROOT, "offline")
_TIKTOKEN_CACHE = os.path.join(_OFFLINE_DIR, "tiktoken_cache")
_NLTK_DATA = os.path.join(_OFFLINE_DIR, "nltk_data")

# ===========================================================
#  하드코딩 주입점 (2순위) — gpt-oss 서비스가 "고정 변수명"으로 헤더를 요구할 때.
#  .env(OPENAI_EXTRA_HEADERS)가 없을 때만 이 딕셔너리가 사용된다(폴백).
#  - 값은 정적 문자열 또는 ${uuid4}/${uuid4hex}/${epoch} 플레이스홀더(각각 독립 생성).
#  - str(uuid.uuid4()) 처럼 파이썬 값을 직접 써도 된다(import 시 1회 평가 = 프로세스 고정).
#  - 가장 원시적인 형태(서비스가 요구하는 변수명을 그대로):
#      _HARDCODED_LLM_HEADERS = {
#          "x-ticket":     "key_123",
#          "user_Id":      "abcde",
#          "extra_keys12": "${uuid4}",     # 또는 str(uuid.uuid4())
#      }
# ===========================================================
_HARDCODED_LLM_HEADERS: dict = {
    # "x-ticket":     "key_123",
    # "user_Id":      "abcde",
    # "extra_keys12": "${uuid4}",
}

# 마지막으로 결정된 헤더 소스(로그/디버그용): ".env" | "하드코딩" | "없음"
_HEADER_SOURCE = "없음"


def _expand_templates(value: str) -> str:
    """헤더 값의 플레이스홀더를 치환한다(프로세스당 1회, apply 시점).
    ${uuid4} / ${uuid4hex} / ${epoch} 지원. 일반 문자열은 그대로 통과.
    """
    if "${uuid4}" in value:
        value = value.replace("${uuid4}", str(uuid.uuid4()))
    if "${uuid4hex}" in value:
        value = value.replace("${uuid4hex}", uuid.uuid4().hex)
    if "${epoch}" in value:
        value = value.replace("${epoch}", str(int(time.time())))
    return value


def _parse_extra_headers() -> dict:
    """LLM 기본 헤더(SDK 의 default_headers)를 구성한다.

    우선순위(폴백 방식, 병합 아님):
      1순위) .env 의 OPENAI_EXTRA_HEADERS (JSON) — 유효하면 이것만 사용.
      2순위) 하드코딩 _HARDCODED_LLM_HEADERS — .env 가 없거나 비었거나 파싱 실패 시.
      3) 둘 다 없으면 빈 dict → 헤더 없이 호출.
    모든 값에 ${uuid4}/${uuid4hex}/${epoch} 플레이스홀더를 치환(헤더별 독립).
    """
    global _HEADER_SOURCE

    # 1순위: .env
    raw = os.getenv("OPENAI_EXTRA_HEADERS")
    if raw and raw.strip():
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data:
                _HEADER_SOURCE = ".env"
                return {str(k): _expand_templates(str(v)) for k, v in data.items()}
            if not isinstance(data, dict):
                print(f"[gptr_oss_patch][WARN] OPENAI_EXTRA_HEADERS 는 JSON object 여야 함: {raw!r} → 하드코딩 폴백")
            # 빈 객체({})면 설정 없음으로 보고 폴백
        except json.JSONDecodeError as e:
            print(f"[gptr_oss_patch][WARN] OPENAI_EXTRA_HEADERS JSON 파싱 실패: {e} → 하드코딩 폴백")

    # 2순위: 하드코딩
    if _HARDCODED_LLM_HEADERS:
        _HEADER_SOURCE = "하드코딩"
        return {str(k): _expand_templates(str(v)) for k, v in _HARDCODED_LLM_HEADERS.items()}

    # 3) 없음
    _HEADER_SOURCE = "없음"
    return {}


def _truthy(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _ensure_offline_resources() -> None:
    """완전 오프라인(LLM/임베딩 API 호출 제외) 구동을 위한 로컬 리소스 연결.

    gpt-researcher 내부는 다음 외부 리소스를 런타임에 자동 다운로드하려 한다:
      - tiktoken BPE 블롭(o200k_base/cl100k_base) ← openaipublic.blob (비용산정 토크나이저)
      - NLTK punkt/punkt_tab 등 ← nltk.org (unstructured 계열 로더의 문장분할)
    에어갰9/폐쇄망에서는 이 다운로드가 실패한다. setup 이 미리 채워둔 offline/ 번들로
    환경변수를 연결해 네트워크 없이 로컬 파일만 읽게 한다.

    - TIKTOKEN_CACHE_DIR : 해당 디렉터리에 블롭이 있으면 tiktoken 은 네트워크를 타지 않는다.
    - NLTK_DATA      : nltk 검색 경로. 사전 다운로드된 패키지를 여기서 찾는다.
    - GPTR_OFFLINE=1 : HF/transformers 등 라이브러리 네트워크까지 차단(안전망).
    값은 이미 환경에 있으면 존중(setdefault).
    """
    if os.path.isdir(_TIKTOKEN_CACHE):
        cur = os.environ.get("TIKTOKEN_CACHE_DIR")
        # tiktoken 은 TIKTOKEN_CACHE_DIR=""(빈문자열)을 "캐싱 비활성=항상 네트워크"로 해석한다.
        # 미설정/빈값이면 우리 오프라인 캐시로 강제. (유효한 사용자 지정 경로는 존중)
        if not cur or not cur.strip():
            os.environ["TIKTOKEN_CACHE_DIR"] = _TIKTOKEN_CACHE
    if os.path.isdir(_NLTK_DATA):
        prev = os.environ.get("NLTK_DATA", "")
        if _NLTK_DATA not in prev.split(os.pathsep):
            os.environ["NLTK_DATA"] = (prev + os.pathsep + _NLTK_DATA) if prev else _NLTK_DATA
        try:
            import nltk  # type: ignore
            if _NLTK_DATA not in nltk.data.path:
                nltk.data.path.insert(0, _NLTK_DATA)
        except Exception:
            pass  # nltk 미설치면 조용히 패스(해당 로더 미사용 시 문제없음)

    if _truthy(os.getenv("GPTR_OFFLINE"), default=False):
        # transformers/HF 라이브러리의 네트워크 접근 차단 — 추가 안전망
        for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
            os.environ.setdefault(k, "1")
        # 명시적 캠시 경로가 아예 없으면 tiktoken 이 임시 다운로드를 시도하므로 경고
        if not os.getenv("TIKTOKEN_CACHE_DIR"):
            print("[gptr_oss_patch][WARN] GPTR_OFFLINE=1 이지만 tiktoken 캠시 없음 — "
                  "setup 의 오프라인 프로비저닝을 먼저 실행하세요(python tools/setup.py).")
    print(f"[gptr_oss_patch] 오프라인 리소스: "
          f"tiktoken_cache={'ON' if os.getenv('TIKTOKEN_CACHE_DIR') else 'off'}, "
          f"nltk_data={'ON' if os.path.isdir(_NLTK_DATA) else 'off'}, "
          f"GPTR_OFFLINE={'1' if _truthy(os.getenv('GPTR_OFFLINE'), False) else '0'}")


def _patch_choose_agent_fallback() -> None:
    """choose_agent 를 감싸 LLM 응답이 비었거나 JSON 파싱 실패해도 파이프라인이
    죽지 않도록 기본 에이전트로 폴백한다.

    증상: "Failed to parse agent JSON with json_repair: TypeError: 'NoneType'
    object is not subscriptable" — gpt-oss 가 JSON 지시를 따르지 않고 빈/None
    응답을 낼 때 json_repair.loads(None) 이 내부 subscript 에서 터지며, 일부
    vendor 버전은 이를 제대로 폴백하지 못해 전체 turn 이 취소된다.
    이 래퍼는 vendor 버전과 무관하게 항상 (server, role_prompt) 튜플을 보장한다.
    """
    if not _truthy(os.getenv("GPTR_AGENT_JSON_FALLBACK"), default=True):
        return
    try:
        from gpt_researcher.actions import agent_creator as _ac
    except Exception as e:  # pragma: no cover
        print(f"[gptr_oss_patch][WARN] agent_creator 패치 건너뜀: {e}")
        return
    if getattr(_ac.choose_agent, "_oss_patched", False):
        return

    _orig_choose = _ac.choose_agent
    _DEFAULT = (
        "Default Agent",
        "You are an AI critical thinker research assistant. Your sole purpose is to write "
        "well written, critically acclaimed, objective and structured reports on given text.",
    )

    async def _safe_choose_agent(*args, **kwargs):
        try:
            result = await _orig_choose(*args, **kwargs)
        except Exception as e:
            print(f"[gptr_oss_patch][WARN] choose_agent 예외 → 기본 에이전트로 폴백: "
                  f"{type(e).__name__}: {e}")
            return _DEFAULT
        # None/빈값/비튜플 방어
        if not result or not isinstance(result, (tuple, list)) or len(result) < 2 \
                or not result[0] or not result[1]:
            print("[gptr_oss_patch][WARN] choose_agent 빈 결과 → 기본 에이전트로 폴백")
            return _DEFAULT
        return result[0], result[1]

    _safe_choose_agent._oss_patched = True
    _ac.choose_agent = _safe_choose_agent
    # 이미 import 된 참조도 교체(researcher/agent 가 from-import 했을 수 있음)
    for modname in ("gpt_researcher.skills.researcher", "gpt_researcher.agent"):
        mod = sys.modules.get(modname)
        if mod is not None and getattr(mod, "choose_agent", None) is _orig_choose:
            mod.choose_agent = _safe_choose_agent
    print("[gptr_oss_patch] choose_agent 기본에이전트 폴백 활성화(JSON 파싱 실패 대비)")


def _get_rate_limiter():
    """tools/rate_limit 의 전역 리미터 반환. 비활성/실패 시 None."""
    try:
        tools_dir = os.path.join(_PATCH_ROOT, "tools")
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        from rate_limit import get_limiter  # type: ignore
        lim = get_limiter()
        return lim if getattr(lim, "enabled", False) else None
    except Exception as e:  # pragma: no cover
        print(f"[gptr_oss_patch][WARN] 레이트리미터 로딩 실패(무제한 진행): {e}")
        return None


def _inject_request_hooks(kwargs: dict, header_names) -> None:
    """ChatOpenAI 에 전달할 http_client / http_async_client 를 생성해
    매 요청마다 header_names 각각에 새 uuid4 를 붙이는 이벤트 훅을 달아준다.

    - header_names 는 문자열(1개) 또는 리스트(N개). 각 헤더는 서로 다른 uuid4 를 받는다.
    - default_headers(정적)와 달리 요청당 값이 갱신된다(request-id 용도).
    - 사용자가 이미 http_client 를 넘겼으면 손대지 않는다.
    - httpx 미설치 등 실패 시 조용히 건너뛴다(정적 헤더는 영향 없음).
    """
    if isinstance(header_names, str):
        header_names = [header_names]
    header_names = [h for h in header_names if h]
    limiter = _get_rate_limiter()
    if not header_names and limiter is None:
        return
    try:
        import httpx
    except Exception as e:  # pragma: no cover
        print(f"[gptr_oss_patch][WARN] httpx 없음 — 요청당 UUID 헤더 건너뜀: {e}")
        return

    def _sync_hook(request):
        if limiter is not None:
            limiter.acquire()
        for name in header_names:
            request.headers[name] = str(uuid.uuid4())

    async def _async_hook(request):
        if limiter is not None:
            await limiter.acquire_async()
        for name in header_names:
            request.headers[name] = str(uuid.uuid4())

    if "http_client" not in kwargs:
        kwargs["http_client"] = httpx.Client(event_hooks={"request": [_sync_hook]})
    if "http_async_client" not in kwargs:
        kwargs["http_async_client"] = httpx.AsyncClient(event_hooks={"request": [_async_hook]})


def _ensure_no_proxy_for_embedding() -> None:
    """임베딩 엔드포인트는 동일 로컬 머신 직접 호출이므로 프록시를 타지 않게 한다.

    EMBEDDING_BASE_URL 의 host 를 NO_PROXY / no_proxy 에 병합한다(기존 값 보존).
    - LLM(게이트웨이)은 다른 host 이므로 영향 없음 — LLM은 계속 프록시/헤더 사용 가능.
    - HTTP_PROXY/HTTPS_PROXY 가 걸려 있어도 127.0.0.1 호출이 프록시로 새지 않도록 보장.
    """
    from urllib.parse import urlparse

    base = os.getenv("EMBEDDING_BASE_URL")
    if not base:
        return
    host = urlparse(base).hostname
    if not host:
        return
    additions = {host}
    if host in ("127.0.0.1", "::1"):
        additions.add("localhost")
    elif host == "localhost":
        additions.update({"127.0.0.1", "::1"})

    changed = set()
    for var in ("NO_PROXY", "no_proxy"):
        items = [x.strip() for x in os.getenv(var, "").split(",") if x.strip()]
        for a in additions:
            if a not in items:
                items.append(a)
                changed.add(a)
        os.environ[var] = ",".join(items)
    print(f"[gptr_oss_patch] 임베딩 프록시 우회(직결): NO_PROXY ⊇ {sorted(additions)} (host={host})")


def _patch_llm_default_headers() -> None:
    """GenericLLMProvider.from_provider 를 래핑하여
    OpenAI 호환 LLM provider 에 default_headers 를 주입한다.
    (provider 가 ChatOpenAI 계열일 때만; 임베딩은 별도 경로라 영향 없음)
    """
    from gpt_researcher.llm_provider.generic import base as _base

    if getattr(_base.GenericLLMProvider.from_provider, "_oss_patched", False):
        return

    extra_headers = _parse_extra_headers()
    # 요청당 UUID 헤더 이름 — 쉼표로 구분해 여러 개 지정 가능(예: "X-Trace-Id,X-Session-Id")
    uuid_headers = [h.strip() for h in (os.getenv("OPENAI_DYNAMIC_UUID_HEADER") or "").split(",") if h.strip()]
    # default_headers 를 받는 OpenAI 호환 provider 화이트리스트
    _OPENAI_COMPATIBLE = {
        "openai", "azure_openai", "dashscope", "deepseek", "openrouter",
        "vllm_openai", "aimlapi", "forge", "avian", "minimax", "together",
    }

    _orig = _base.GenericLLMProvider.from_provider.__func__  # classmethod underlying fn

    def _wrapped(cls, provider: str, chat_log=None, verbose: bool = True, **kwargs):
        if provider in _OPENAI_COMPATIBLE:
            # SDK 필수 인자 충족: langchain_openai.ChatOpenAI 는 헤더를
            # **default_headers** 라는 이름으로 받는다(deepdoc 방식).
            # 호출자가 이미 default_headers 를 넣었으면 그 값이 우선.
            if extra_headers:
                merged = dict(extra_headers)
                if "default_headers" in kwargs and isinstance(kwargs["default_headers"], dict):
                    merged.update(kwargs["default_headers"])
                kwargs["default_headers"] = merged
            # 훅 주입: 레이트리미트(항상) + 요청당 UUID 헤더(설정 시). httpx 이벤트 훅.
            _inject_request_hooks(kwargs, uuid_headers)
        return _orig(cls, provider, chat_log=chat_log, verbose=verbose, **kwargs)

    _wrapped._oss_patched = True
    _base.GenericLLMProvider.from_provider = classmethod(_wrapped)
    if extra_headers:
        keys = ", ".join(sorted(extra_headers.keys()))
        print(f"[gptr_oss_patch] LLM default_headers 주입({_HEADER_SOURCE}): [{keys}]")
    else:
        print("[gptr_oss_patch] LLM 정적 헤더 없음(.env·하드코딩 모두 미설정) — 헤더 없이 호출")
    if uuid_headers:
        print(f"[gptr_oss_patch] LLM 요청당 UUID 헤더 활성화: {uuid_headers} (매 호출 헤더별 새 uuid4)")
    _lim = _get_rate_limiter()
    if _lim is not None:
        print(f"[gptr_oss_patch] LLM 레이트리미트 활성화: {_lim.rps}회/sec (LLM_MAX_RPS)")
    else:
        print("[gptr_oss_patch] LLM 레이트리미트 비활성(LLM_MAX_RPS<=0 또는 로딩 실패)")


def _patch_embedding_base_url() -> None:
    """임베딩(BGE)을 LLM 과 분리된 base_url 로 강제한다.

    gpt_researcher.memory.Memory 의 openai/custom 분기는 OPENAI_BASE_URL 을
    공유하므로, EMBEDDING_BASE_URL 이 있으면 그 값을 embedding_kwargs 에
    openai_api_base 로 주입하여 LLM 엔드포인트와 충돌하지 않게 한다.
    헤더는 절대 주입하지 않는다(요구사항: 임베딩은 커스텀 헤더 없음).
    """
    emb_base = os.getenv("EMBEDDING_BASE_URL")
    if not emb_base:
        print("[gptr_oss_patch] EMBEDDING_BASE_URL 미설정 — 임베딩 base_url 분리 없음")
        return

    from gpt_researcher.memory import embeddings as _emb

    if getattr(_emb.Memory.__init__, "_oss_patched", False):
        return

    emb_key = os.getenv("EMBEDDING_API_KEY", "unused")
    _orig_init = _emb.Memory.__init__

    def _wrapped_init(self, embedding_provider, model, **embedding_kwargs):
        if embedding_provider in ("openai", "custom"):
            embedding_kwargs.setdefault("openai_api_base", emb_base)
            embedding_kwargs.setdefault("openai_api_key", emb_key)
            # BGE 는 컨텍스트 길이 체크 불필요 + 로컬
            embedding_kwargs.setdefault("check_embedding_ctx_length", False)
        return _orig_init(self, embedding_provider, model, **embedding_kwargs)

    _wrapped_init._oss_patched = True
    _emb.Memory.__init__ = _wrapped_init
    print(f"[gptr_oss_patch] 임베딩 base_url 분리: {emb_base} (헤더 없음)")


def _patch_disable_toolcalling() -> None:
    """supports_tools() 를 False 로 강제하여 챗 경로의 bind_tools 시도를 차단.
    (메인 리서치 파이프라인은 애초에 tool-calling 미사용)
    """
    if not _truthy(os.getenv("GPTR_DISABLE_TOOLCALLING"), default=True):
        return
    try:
        from gpt_researcher.utils import tools as _tools
    except Exception as e:  # pragma: no cover
        print(f"[gptr_oss_patch][WARN] tools 모듈 패치 건너뜀: {e}")
        return

    if getattr(_tools.supports_tools, "_oss_patched", False):
        return

    def _no_tools(provider: str) -> bool:
        return False

    _no_tools._oss_patched = True
    _tools.supports_tools = _no_tools

    def _empty_providers():
        return []

    _empty_providers._oss_patched = True
    _tools.get_available_providers_with_tools = _empty_providers
    print("[gptr_oss_patch] tool-calling 비활성화 (supports_tools→False)")


def _patch_context_retrieval() -> None:
    """ContextCompressor.async_get_context 래핑 — 로컬 문서 검색 동작 제어.

    두 가지 모드:
      (1) GPTR_LOCAL_FULL_CORPUS truthy → 임베딩 유사도 필터/상위N cap 을 **우회**하고
          전체 문서를 촉크 분할해 전량 컨텍스트에 포함(Stage2: 다이제스트 재선밄 방지용).
          GPTR_LOCAL_MAX_CHUNKS>0 이면 그 수만큼만 cap(안전망).
      (2) 그 외 → GPTR_RAG_MAX_RESULTS>0 이면 RAG 상위N cap(기본 하드코딩 10)을 상향.
    SIMILARITY_THRESHOLD 는 vendor 가 이미 env 로 읽으므로 여기서 건드리지 않는다.
    """
    try:
        from gpt_researcher.context.compression import ContextCompressor
    except Exception as e:  # pragma: no cover
        print(f"[gptr_oss_patch][WARN] ContextCompressor 패치 건너뛴: {e}")
        return
    if getattr(ContextCompressor.async_get_context, "_oss_patched", False):
        return

    _orig = ContextCompressor.async_get_context

    async def _wrapped(self, query, max_results: int = 5, cost_callback=None):
        if _truthy(os.getenv("GPTR_LOCAL_FULL_CORPUS"), default=False):
            try:
                from langchain_text_splitters import RecursiveCharacterTextSplitter
                from langchain_core.documents import Document
                cs = int(os.getenv("GPTR_CHUNK_SIZE", "1000"))
                co = int(os.getenv("GPTR_CHUNK_OVERLAP", "100"))
                splitter = RecursiveCharacterTextSplitter(chunk_size=cs, chunk_overlap=co)
                docs = [Document(page_content=str(d.get("raw_content", "") or ""), metadata=d)
                        for d in self.documents]
                chunks = splitter.split_documents(docs)
                cap = int(os.getenv("GPTR_LOCAL_MAX_CHUNKS", "0") or 0)
                if cap > 0:
                    chunks = chunks[:cap]
                print(f"[gptr_oss_patch] full-corpus: 전체 {len(chunks)} 촉크 컨텍스트 포함(필터/cap 우회)")
                return self.prompt_family.pretty_print_docs(chunks, None)
            except Exception as e:
                print(f"[gptr_oss_patch][WARN] full-corpus 처리 실패 → 기본 경로: {e}")
        rr = int(os.getenv("GPTR_RAG_MAX_RESULTS", "0") or 0)
        if rr > 0:
            max_results = rr
        return await _orig(self, query, max_results=max_results, cost_callback=cost_callback)

    _wrapped._oss_patched = True
    ContextCompressor.async_get_context = _wrapped
    print("[gptr_oss_patch] 컨텍스트 검색 패치(full-corpus/RAG max_results)")


def _patch_full_corpus_plan() -> None:
    """GPTR_LOCAL_FULL_CORPUS 일 때 plan_research → [] (하위질의 fan-out 제거, 1패스).
    → 전체 문서를 정확히 1회만 컨텍스트에 싣어 중복 방지(다이제스트가 이미 전문서 대표)."""
    try:
        from gpt_researcher.skills.researcher import ResearchConductor
    except Exception as e:  # pragma: no cover
        print(f"[gptr_oss_patch][WARN] ResearchConductor 패치 건너뛴: {e}")
        return
    if getattr(ResearchConductor.plan_research, "_oss_patched", False):
        return
    _orig = ResearchConductor.plan_research

    async def _wrapped(self, query, query_domains=None):
        if _truthy(os.getenv("GPTR_LOCAL_FULL_CORPUS"), default=False):
            print("[gptr_oss_patch] full-corpus: 하위질의 생성 생략(1패스)")
            return []
        return await _orig(self, query, query_domains)

    _wrapped._oss_patched = True
    ResearchConductor.plan_research = _wrapped
    print("[gptr_oss_patch] full-corpus plan_research 패치(1패스)")


_KOREAN_DIRECTIVE = (
    "\n\n[언어 강제] 최종 산출물의 모든 문장(제목·서론·본론·결론 포함)은 반드시 한국어로 작성한다. "
    "영어 문장을 섮지 않는다. 고유명·약어는 원문 병기 가능."
)


def _get_glossary_block() -> str:
    """tools/glossary 로 용어사전 주입 블록을 구성. 없으면 빈 문자열(비치명)."""
    try:
        tools_dir = os.path.join(_PATCH_ROOT, "tools")
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        import glossary as _g  # type: ignore
        return _g.get_block(verbose=True)
    except Exception as e:  # pragma: no cover
        print(f"[gptr_oss_patch][WARN] 용어사전 로딩 실패(무시하고 진행): {e}")
        return ""


def _patch_inject_glossary() -> None:
    """보고서 생성 프롬프트에 용어사전(전문용어·고유명 정의) 블록을 덧붙인다(RAG 모드).

    요약/보고서가 전문지식을 요구할 때, 사전에 주입한 정의를 LLM 이 일관되게 쓰도록 한다.
    data/glossary.json (또는 data/glossary/*.json) 파일이 있을 때만 동작(없으면 no-op). .env 미사용.
    PromptFamily 의 generate_report_* 는 staticmethod → 래핑 후 staticmethod 로 재할당.
    주입 순서: 기본프롬프트 + [용어사전] (+ 이후 _patch_force_language 가 한국어지시 추가).
    """
    block = _get_glossary_block()
    if not block:
        return
    try:
        from gpt_researcher.prompts import PromptFamily
    except Exception as e:  # pragma: no cover
        print(f"[gptr_oss_patch][WARN] PromptFamily(용어사전) 패치 건너뜀: {e}")
        return
    targets = ["generate_report_prompt", "generate_report_introduction",
               "generate_report_conclusion", "generate_subtopic_report_prompt"]
    patched = []
    for name in targets:
        fn = getattr(PromptFamily, name, None)
        if fn is None or getattr(fn, "_oss_glossary_patched", False):
            continue

        def _make(orig, blk):
            def _w(*args, **kwargs):
                return f"{orig(*args, **kwargs)}{blk}"
            _w._oss_glossary_patched = True
            return _w

        try:
            setattr(PromptFamily, name, staticmethod(_make(fn, block)))
            patched.append(name)
        except Exception as e:  # pragma: no cover
            print(f"[gptr_oss_patch][WARN] {name} 용어사전 주입 실패: {e}")
    if patched:
        print(f"[gptr_oss_patch] 용어사전 주입 적용: {patched}")


def _patch_force_language() -> None:
    """보고서 생성 프롬프트에 강한 한국어 지시문을 덧붙여 gpt-oss 언어 이탈 방지(하드닝).

    LANGUAGE=korean 이 1차(공식) 경로. 이 패치는 약체 모델 대비 2차 보강.
    GPTR_FORCE_KOREAN 명시 설정 우선, 미설정 시 LANGUAGE 가 한국어면 기본 ON.
    PromptFamily 의 generate_report_* 는 staticmethod → 래핑 후 staticmethod 로 재할당.
    """
    lang = os.getenv("LANGUAGE", "").strip().lower()
    want = _truthy(os.getenv("GPTR_FORCE_KOREAN"),
                   default=lang.startswith("korea") or lang in ("ko", "kr", "한국어"))
    if not want:
        return
    try:
        from gpt_researcher.prompts import PromptFamily
    except Exception as e:  # pragma: no cover
        print(f"[gptr_oss_patch][WARN] PromptFamily 패치 건너뛴: {e}")
        return
    targets = ["generate_report_prompt", "generate_report_introduction", "generate_report_conclusion"]
    patched = []
    for name in targets:
        fn = getattr(PromptFamily, name, None)
        if fn is None or getattr(fn, "_oss_patched", False):
            continue

        def _make(orig):
            def _w(*args, **kwargs):
                return f"{orig(*args, **kwargs)}{_KOREAN_DIRECTIVE}"
            _w._oss_patched = True
            return _w

        try:
            setattr(PromptFamily, name, staticmethod(_make(fn)))
            patched.append(name)
        except Exception as e:  # pragma: no cover
            print(f"[gptr_oss_patch][WARN] {name} 한국어 하드닝 실패: {e}")
    if patched:
        print(f"[gptr_oss_patch] 한국어 출력 하드닝 적용: {patched}")


def apply() -> None:
    global _APPLIED
    if _APPLIED:
        return
    # gpt_researcher 가 아직 import 안 됐다면 import 시도
    try:
        import gpt_researcher  # noqa: F401
    except Exception as e:
        print(f"[gptr_oss_patch][ERROR] gpt_researcher import 실패: {e}", file=sys.stderr)
        raise

    _ensure_offline_resources()
    _ensure_no_proxy_for_embedding()
    _patch_llm_default_headers()
    _patch_embedding_base_url()
    _patch_disable_toolcalling()
    _patch_choose_agent_fallback()
    for _fn in (_patch_context_retrieval, _patch_full_corpus_plan,
                _patch_inject_glossary, _patch_force_language):
        try:
            _fn()
        except Exception as _e:  # pragma: no cover
            print(f"[gptr_oss_patch][WARN] {_fn.__name__} 실패(핵심 계속): {_e}")
    _APPLIED = True
    print("[gptr_oss_patch] 적용 완료.")


# ★ 오프라인 리소스 env(TIKTOKEN_CACHE_DIR/NLTK_DATA)는 gpt_researcher import 성공 여부와
#   무관하게 "import 즉시" 적용한다. (apply() 가 gpt_researcher 미설치/내부변경 등으로
#   실패해도 tiktoken 이 먼저 네트워크를 타며 SSL 실패하는 것을 막는다.)
try:
    _ensure_offline_resources()
except Exception as e:  # pragma: no cover
    print(f"[gptr_oss_patch][WARN] 오프라인 리소스 env 설정 실패: {e}")

# import 시 자동 적용(편의). 비활성화하려면 GPTR_OSS_PATCH_AUTOAPPLY=0
if _truthy(os.getenv("GPTR_OSS_PATCH_AUTOAPPLY"), default=True):
    try:
        apply()
    except Exception:
        # import-time 실패는 조용히 넘기고, 명시 apply() 시 재시도하게 둔다.
        # (단, 위에서 오프라인 env 는 이미 적용됨)
        _APPLIED = False
