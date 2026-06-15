# gptr-oss-winsetup

GPT-Researcher를 **GPT-OSS(로컬/사내 OpenAI 호환 LLM)** 로 구동하기 위한 **Windows 올인원 셋업** repo.
원본 [gpt-researcher](https://github.com/assafelovic/gpt-researcher)는 **수정하지 않고** vendoring + 런타임 monkeypatch로 연동한다.

> 📄 **내 로컬 데이터(jsonl)로 문서 작성**하려면 → [`MANUAL.md`](MANUAL.md) (시나리오 1·2·3 상세 가이드)

## 핵심 설계

| 항목 | 방식 |
|------|------|
| **LLM 호출** | OpenAI 호환 `base_url` + `default_headers`(deep-doc-pipeline 패턴). 게이트웨이/프록시 인증 헤더를 **LLM 호출에만** 주입 |
| **임베딩** | 로컬 **BGE 서버**(내 서버 연결 권장; 번들 `bge_server/` 대안 제공). OpenAI 호환 `/v1/embeddings`, **커스텀 헤더 없음**, LLM과 **별도 base_url**로 완전 분리 |
| **tool-calling 우회** | `MCP_STRATEGY=disabled` + `supports_tools()→False` 강제. 메인 파이프라인은 애초에 function-calling 미사용 |
| **원본 무수정** | `patches/gptr_oss_patch.py` 런타임 패치(멱등). repo는 `vendor/`에 clone |
| **Windows** | setup(무거움)/launch(가벼움) 분리, `.bat`은 thin wrapper만 |

## 디렉터리

```
gptr-oss-winsetup/
├─ patches/gptr_oss_patch.py   # 런타임 패치: LLM 헤더 주입 / 임베딩 base_url 분리 / tool-calling 차단
├─ bge_server/bge_server.py    # 로컬 BGE 임베딩 서버 (OpenAI 호환, 헤더 없음)
├─ tools/
│  ├─ _common.py               # 공유 유틸 (경로/venv/플랫폼/데이터 경로)
│  ├─ setup.py                 # 1회성 셋업 (venv + vendoring + 의존성 + .env + data/)
│  ├─ launch.py                # 반복 실행 (prepare / bge / research / doctor)
│  ├─ prepare_data.py          # jsonl/csv/json → data/docs/*.md 변환기 (로컬 데이터)
│  ├─ check_embedding.py       # 내 BGE 임베딩 서버 호환성 점검 (stdlib)
│  └─ run_research.py          # 리서치 엔트리포인트 (--source web|local|hybrid)
├─ examples/sample-corpus.jsonl # 로컬 데이터 형식 예제(3건)
├─ data/                       # raw/(원본) + docs/(변환본=DOC_PATH). git 제외
├─ windows/                    # .bat thin wrapper (setup/prepare-data/start-bge/research[-local]/doctor)
├─ MANUAL.md                   # 로컬 데이터 문서작성 실행 가이드(시나리오 1·2·3)
├─ .env.example                # 환경설정 템플릿
└─ vendor/gpt-researcher/      # (셋업 시 clone) 원본 repo
```

## 빠른 시작 (Windows)

```bat
REM 1) 셋업 (venv 생성 + gpt-researcher clone + 의존성 + .env 생성)
windows\setup.bat

REM 2) .env 편집: OPENAI_BASE_URL / 모델명 / (선택) OPENAI_EXTRA_HEADERS

REM 3) BGE 임베딩 서버 기동 (별도 창에서 계속 실행)
windows\start-bge.bat

REM 4) 리서치 실행
windows\research.bat "양자내성암호 2026 표준화 동향" --report-type research_report

REM 환경 점검
windows\doctor.bat
```

POSIX(WSL/Linux/macOS)에서는 동일하게 `python tools/setup.py`, `python tools/launch.py bge`, `python tools/launch.py research "..."`.

## 로컬 데이터(jsonl) 기반 문서 작성

웹 대신 **내 로컬 데이터**로 보고서를 만들려면 (웹 미접속, BGE 임베딩 유사도만 사용):

```bat
REM 1) jsonl/csv/json → data\docs\*.md 변환 (원본 gpt-researcher는 jsonl 미지원이라 변환 필요)
windows\prepare-data.bat "data\raw\corpus.jsonl" --content-field text --clean

REM 2) 임베딩 서버: "내 BGE 서버"(:8999) 기동 후 .env 의 EMBEDDING_BASE_URL 지정
REM    → 호환성 검증:  windows\check-embedding.bat   (대안: 번들 windows\start-bge.bat)

REM 3) 로컬 데이터 기반 보고서 생성
windows\research-local.bat "우리 데이터 핵심 요약" --report-type detailed_report
```
```bash
# POSIX
python tools/launch.py prepare data/raw/corpus.jsonl --content-field text --clean
python tools/launch.py check-embedding   # 내 BGE 서버 호환성 검증 (대안: launch.py bge)
python tools/launch.py research "우리 데이터 핵심 요약" --source local
```

예제 데이터: `examples/sample-corpus.jsonl`. 전체 절차는 **[`MANUAL.md`](MANUAL.md)** 참조.

## 환경설정 (.env)

핵심 항목 (`.env.example` 참조):

```dotenv
# LLM (gpt-oss) — OpenAI 호환
OPENAI_BASE_URL=http://localhost:11434/v1
OPENAI_API_KEY=unused
FAST_LLM=openai:gpt-oss-20b
SMART_LLM=openai:gpt-oss-120b
STRATEGIC_LLM=openai:gpt-oss-120b
# 게이트웨이 인증이 필요할 때만 (LLM 전용):
# OPENAI_EXTRA_HEADERS={"Authorization":"Bearer xxx","X-Project-Id":"abc"}

# 임베딩 (로컬 BGE) — LLM과 별도, 헤더 없음
# 기본은 "내 BGE 서버"(bge-m3-korean, :8999). 번들 서버 쓸 때는 BAAI/bge-m3 + :7997.
EMBEDDING=openai:bge-m3-korean
EMBEDDING_BASE_URL=http://127.0.0.1:8999/v1
EMBEDDING_API_KEY=unused
# 설정 후 검증:  python tools/launch.py check-embedding

# tool-calling 우회
MCP_STRATEGY=disabled
GPTR_DISABLE_TOOLCALLING=1

# 검색 (무료 기본)
RETRIEVER=duckduckgo
```

## default_header 주입 원리

`gptr_oss_patch.py`가 `GenericLLMProvider.from_provider`를 래핑하여,
OpenAI 호환 provider 생성 시 `OPENAI_EXTRA_HEADERS`(JSON)를 `ChatOpenAI(default_headers=...)`로 전달한다.
**임베딩 경로(`Memory.__init__`)에는 헤더를 주입하지 않으며**, `EMBEDDING_BASE_URL`로 BGE 서버로만 라우팅한다.
→ "LLM은 인증 헤더, 임베딩은 헤더 없는 로컬 모델" 요구사항을 코드 분리로 보장.

## gpt-oss 서빙 예시

- **Ollama**: `ollama run gpt-oss:20b` → `OPENAI_BASE_URL=http://localhost:11434/v1`
- **vLLM**: `vllm serve openai/gpt-oss-20b --port 8000` → `OPENAI_BASE_URL=http://localhost:8000/v1`
- **사내 게이트웨이**: `OPENAI_BASE_URL=https://gw.internal/v1` + `OPENAI_EXTRA_HEADERS`로 인증

## 주의

- BGE 모델은 임베딩 전용. gpt-oss(생성)와 역할이 다르므로 반드시 분리 운용.
- 첫 BGE 기동 시 모델 다운로드(수백 MB~) 발생. GPU 사용 시 `.env`에 `BGE_DEVICE=cuda`.
- `RETRIEVER=tavily`는 품질이 높지만 `TAVILY_API_KEY` 필요. 키 없으면 `duckduckgo` 사용.
