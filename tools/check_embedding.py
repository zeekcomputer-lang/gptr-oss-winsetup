"""
check_embedding — 로컬 BGE 임베딩 서버 호환성 점검 (stdlib only)

gpt-researcher 가 임베딩을 호출하는 방식(OpenAI 호환 POST /v1/embeddings, 원문 텍스트
전송)을 그대로 모사해 사용자의 BGE 서버가 정상 응답하는지 확인한다.

검증 항목:
  1) EMBEDDING_BASE_URL/embeddings 로 POST 가 200 응답하는가
  2) data[i].embedding 이 float 배열인가 (base64 문자열이어도 길이 보고)
  3) 임베딩 차원이 일관적인가

사용:
  python tools/check_embedding.py
  python tools/check_embedding.py --base-url http://127.0.0.1:8999/v1 --model bge-m3-korean

.env 의 EMBEDDING_BASE_URL / EMBEDDING(provider:model) 를 자동으로 읽는다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
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


def _model_from_embedding_env() -> str:
    raw = os.getenv("EMBEDDING", "openai:bge-m3-korean")
    # 형식 "<provider>:<model>" → model 부분
    return raw.split(":", 1)[1] if ":" in raw else raw


def main() -> int:
    _load_env()
    ap = argparse.ArgumentParser(description="로컬 BGE 임베딩 서버 호환성 점검")
    ap.add_argument("--base-url", default=os.getenv("EMBEDDING_BASE_URL", "http://127.0.0.1:8999/v1"))
    ap.add_argument("--model", default=_model_from_embedding_env())
    ap.add_argument("--api-key", default=os.getenv("EMBEDDING_API_KEY", "unused"))
    args = ap.parse_args()

    url = args.base_url.rstrip("/") + "/embeddings"
    payload = {"input": ["첫 번째 한국어 문서입니다.", "두 번째 테스트 문장."], "model": args.model}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {args.api_key}"},
    )
    print(f"[check_embedding] POST {url}")
    print(f"[check_embedding] model={args.model!r} inputs=2 (원문 텍스트 전송 = gpt-researcher 패치 경로)")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[check_embedding][FAIL] 요청 실패: {type(e).__name__}: {e}")
        print("  - 서버 기동/포트/EMBEDDING_BASE_URL 을 확인하세요.")
        return 1

    items = data.get("data")
    if not isinstance(items, list) or not items:
        print(f"[check_embedding][FAIL] 응답에 data 배열 없음: {str(data)[:200]}")
        return 1

    dims = []
    for it in items:
        emb = it.get("embedding")
        if isinstance(emb, list):
            kind = f"float[{len(emb)}]"
            dims.append(len(emb))
        elif isinstance(emb, str):
            kind = f"base64(len={len(emb)})"
        else:
            print(f"[check_embedding][FAIL] embedding 타입 비정상: {type(emb).__name__}")
            return 1
        print(f"  - index={it.get('index')} embedding={kind}")

    if dims and len(set(dims)) == 1:
        print(f"[check_embedding][OK] 정상. 벡터 {len(items)}개, dim={dims[0]} (일관)")
    elif dims:
        print(f"[check_embedding][WARN] 차원 불일치: {dims}")
        return 1
    else:
        print("[check_embedding][OK] 응답 수신(base64). 파이프라인에서 langchain 이 디코드함.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
