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
  OPENAI_EXTRA_HEADERS     LLM 전용 추가 헤더 (JSON 문자열). 예:
                           {"Authorization":"Bearer xxx","X-Project-Id":"abc"}
                           값에 플레이스홀더 사용 가능(패치가 치환, 프로세스당 1회):
                             ${uuid4}    → 36자 UUID4 (예: 3f9c...-...)
                             ${uuid4hex} → 32자 hex UUID
                             ${epoch}    → 유닉스 초
                           예: {"X-Request-Id":"${uuid4}","Authorization":"Bearer xxx"}
  OPENAI_DYNAMIC_UUID_HEADER  설정하면 해당 이름의 헤더를 **매 요청마다 새 UUID4** 로
                           붙인다(httpx 이벤트 훅). 예: X-Request-Id
                           - 비워두면 비활성. LLM 호출에만 적용, 임베딩은 무관.
  EMBEDDING_BASE_URL       임베딩(BGE) 전용 엔드포인트 (예: http://127.0.0.1:8999/v1)
                           - LLM 과 분리. 헤더 주입 없음(요구사항).
  EMBEDDING_API_KEY        임베딩 SDK 필수값 충족용 (기본 'unused')
  GPTR_DISABLE_TOOLCALLING 1/true 면 supports_tools() → False 강제 (기본 1)

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


def _expand_templates(value: str) -> str:
    """헤더 값의 플레이스홀더를 치환한다(프로세스당 1회, apply 시점).
    ${uuid4} / ${uuid4hex} / ${epoch} 지원.
    """
    if "${uuid4}" in value:
        value = value.replace("${uuid4}", str(uuid.uuid4()))
    if "${uuid4hex}" in value:
        value = value.replace("${uuid4hex}", uuid.uuid4().hex)
    if "${epoch}" in value:
        value = value.replace("${epoch}", str(int(time.time())))
    return value


def _parse_extra_headers() -> dict:
    raw = os.getenv("OPENAI_EXTRA_HEADERS")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {str(k): _expand_templates(str(v)) for k, v in data.items()}
        print(f"[gptr_oss_patch][WARN] OPENAI_EXTRA_HEADERS 는 JSON object 여야 함: {raw!r}")
    except json.JSONDecodeError as e:
        print(f"[gptr_oss_patch][WARN] OPENAI_EXTRA_HEADERS JSON 파싱 실패: {e}")
    return {}


def _truthy(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _inject_uuid_request_hook(kwargs: dict, header_name: str) -> None:
    """ChatOpenAI 에 전달할 http_client / http_async_client 를 생성해
    매 요청마다 header_name 에 새 uuid4 를 붙이는 이벤트 훅을 달아준다.

    - default_headers(정적)와 달리 요청당 값이 갱신된다(request-id 용도).
    - 사용자가 이미 http_client 를 넘겼으면 손대지 않는다.
    - httpx 미설치 등 실패 시 조용히 건너뛴다(정적 헤더는 영향 없음).
    """
    try:
        import httpx
    except Exception as e:  # pragma: no cover
        print(f"[gptr_oss_patch][WARN] httpx 없음 — 요청당 UUID 헤더 건너뜀: {e}")
        return

    def _sync_hook(request):
        request.headers[header_name] = str(uuid.uuid4())

    async def _async_hook(request):
        request.headers[header_name] = str(uuid.uuid4())

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
    uuid_header = (os.getenv("OPENAI_DYNAMIC_UUID_HEADER") or "").strip()
    # default_headers 를 받는 OpenAI 호환 provider 화이트리스트
    _OPENAI_COMPATIBLE = {
        "openai", "azure_openai", "dashscope", "deepseek", "openrouter",
        "vllm_openai", "aimlapi", "forge", "avian", "minimax", "together",
    }

    _orig = _base.GenericLLMProvider.from_provider.__func__  # classmethod underlying fn

    def _wrapped(cls, provider: str, chat_log=None, verbose: bool = True, **kwargs):
        if provider in _OPENAI_COMPATIBLE:
            # langchain_openai.ChatOpenAI 는 default_headers 를 지원.
            # 이미 사용자가 넣었으면 병합(사용자 우선).
            if extra_headers:
                merged = dict(extra_headers)
                if "default_headers" in kwargs and isinstance(kwargs["default_headers"], dict):
                    merged.update(kwargs["default_headers"])
                kwargs["default_headers"] = merged
            # 요청당 새 UUID 헤더 — httpx 이벤트 훅으로 매 호출마다 갱신
            if uuid_header:
                _inject_uuid_request_hook(kwargs, uuid_header)
        return _orig(cls, provider, chat_log=chat_log, verbose=verbose, **kwargs)

    _wrapped._oss_patched = True
    _base.GenericLLMProvider.from_provider = classmethod(_wrapped)
    if extra_headers:
        keys = ", ".join(sorted(extra_headers.keys()))
        print(f"[gptr_oss_patch] LLM default_headers 주입 활성화: [{keys}]")
    else:
        print("[gptr_oss_patch] OPENAI_EXTRA_HEADERS 미설정 — LLM 정적 헤더 주입 없음")
    if uuid_header:
        print(f"[gptr_oss_patch] LLM 요청당 UUID 헤더 활성화: {uuid_header} (매 호출 새 uuid4)")


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

    _ensure_no_proxy_for_embedding()
    _patch_llm_default_headers()
    _patch_embedding_base_url()
    _patch_disable_toolcalling()
    _APPLIED = True
    print("[gptr_oss_patch] 적용 완료.")


# import 시 자동 적용(편의). 비활성화하려면 GPTR_OSS_PATCH_AUTOAPPLY=0
if _truthy(os.getenv("GPTR_OSS_PATCH_AUTOAPPLY"), default=True):
    try:
        apply()
    except Exception:
        # import-time 실패는 조용히 넘기고, 명시 apply() 시 재시도하게 둔다.
        _APPLIED = False
