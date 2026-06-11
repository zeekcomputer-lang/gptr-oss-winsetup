"""
bge_server — 로컬 BGE 임베딩 서버 (OpenAI 호환 /v1/embeddings)

요구사항 반영:
  - 임베딩은 별도 로컬 모델(BGE 계열)로 처리
  - 커스텀 헤더 없음 (인증/게이트웨이 헤더 미사용, localhost 전용 권장)
  - GPT-Researcher 의 EMBEDDING="openai:<model>" + EMBEDDING_BASE_URL 로 연결

엔드포인트:
  GET  /health
  GET  /v1/models
  POST /v1/embeddings   (OpenAI Embeddings API 호환)

모델:
  BGE_MODEL 환경변수로 지정. 기본 BAAI/bge-m3 (다국어, 한국어 우수).
  대안: BAAI/bge-large-en-v1.5, BAAI/bge-base-en-v1.5, BAAI/bge-small-en-v1.5

구동:
  pip install fastapi uvicorn sentence-transformers torch
  python bge_server.py            # 0.0.0.0:7997
환경변수:
  BGE_MODEL   임베딩 모델명 (기본 BAAI/bge-m3)
  BGE_HOST    바인드 호스트 (기본 127.0.0.1)
  BGE_PORT    포트 (기본 7997)
  BGE_DEVICE  cpu | cuda (기본 자동감지)
  BGE_NORMALIZE  1/0 정규화 여부 (기본 1, 코사인 유사도 권장)
"""
from __future__ import annotations

import os
import time
from typing import List, Union

from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn

BGE_MODEL = os.getenv("BGE_MODEL", "BAAI/bge-m3")
BGE_HOST = os.getenv("BGE_HOST", "127.0.0.1")
BGE_PORT = int(os.getenv("BGE_PORT", "7997"))
BGE_NORMALIZE = os.getenv("BGE_NORMALIZE", "1").strip().lower() in ("1", "true", "yes", "on")


def _resolve_device() -> str:
    dev = os.getenv("BGE_DEVICE")
    if dev:
        return dev
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


DEVICE = _resolve_device()

print(f"[bge_server] 모델 로딩: {BGE_MODEL} (device={DEVICE}, normalize={BGE_NORMALIZE}) ...")
from sentence_transformers import SentenceTransformer  # noqa: E402

_model = SentenceTransformer(BGE_MODEL, device=DEVICE)
_DIM = _model.get_sentence_embedding_dimension()
print(f"[bge_server] 로딩 완료. dim={_DIM}")

app = FastAPI(title="BGE Embedding Server (OpenAI-compatible)", version="1.0.0")


class EmbeddingRequest(BaseModel):
    input: Union[str, List[str]]
    model: str | None = None
    # OpenAI 호환 필드 (무시되지만 수용)
    encoding_format: str | None = None
    dimensions: int | None = None
    user: str | None = None


@app.get("/health")
def health():
    return {"status": "ok", "model": BGE_MODEL, "dim": _DIM, "device": DEVICE}


@app.get("/v1/models")
def list_models():
    return {
        "object": "list",
        "data": [
            {"id": BGE_MODEL, "object": "model", "owned_by": "local-bge"},
        ],
    }


@app.post("/v1/embeddings")
def create_embeddings(req: EmbeddingRequest):
    texts = [req.input] if isinstance(req.input, str) else list(req.input)
    # BGE 권장: 정규화 임베딩으로 코사인 유사도 사용
    vectors = _model.encode(
        texts,
        normalize_embeddings=BGE_NORMALIZE,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    data = [
        {
            "object": "embedding",
            "index": i,
            "embedding": vec.tolist(),
        }
        for i, vec in enumerate(vectors)
    ]
    total_tokens = sum(len(t.split()) for t in texts)  # 근사치
    return {
        "object": "list",
        "data": data,
        "model": req.model or BGE_MODEL,
        "usage": {"prompt_tokens": total_tokens, "total_tokens": total_tokens},
    }


if __name__ == "__main__":
    print(f"[bge_server] http://{BGE_HOST}:{BGE_PORT}/v1  (OpenAI 호환, 헤더 인증 없음)")
    uvicorn.run(app, host=BGE_HOST, port=BGE_PORT, log_level="info")
