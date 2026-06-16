# gptr-oss-winsetup

GPT-Researcher를 **GPT-OSS(로컬/사내 OpenAI 호환 LLM)** 로 구동하기 위한 **Windows 올인원 셋업** repo.
원본 [gpt-researcher](https://github.com/assafelovic/gpt-researcher)는 **수정하지 않고** vendoring + 런타임 monkeypatch로 연동한다.

> 📄 **내 로컬 데이터(jsonl)로 문서 작성**하려면 → [`MANUAL.md`](MANUAL.md) (시나리오 1·2·3 상세 가이드)
> 🧩 **유지보수·다음 작업자(AI 포함)** → [`HANDOFF.md`](HANDOFF.md) (구조·설계결정·검증상태·체크리스트)

## 핵심 설계

| 항목 | 방식 |
|------|------|
| **LLM 호출** | OpenAI 호환 `base_url` + `default_headers`(deep-doc-pipeline 패턴). 게이트웨이/프록시 인증 헤더를 **LLM 호출에만** 주입 |
| **임베딩** | 사용자가 **별도 운영**하는 BGE 엔드포인트(OpenAI 호환 `/v1/embeddings`)에 **접속만** 함. 이 repo 는 임베딩 서버를 구동하지 않음. `EMBEDDING_BASE_URL`로 LLM과 별도 분리, **헤더 미주입 + 동일 로컬 머신 직결(프록시 미경유)** |
| **tool-calling 우회** | `MCP_STRATEGY=disabled` + `supports_tools()→False` 강제. 메인 파이프라인은 애초에 function-calling 미사용 |
| **원본 무수정** | `patches/gptr_oss_patch.py` 런타임 패치(멱등). repo는 `vendor/`에 clone |
| **Windows** | setup(무거움)/launch(가벼움) 분리, `.bat`은 thin wrapper만 |

## 디렉터리

```
gptr-oss-winsetup/
├─ patches/gptr_oss_patch.py   # 런타임 패치: LLM 헤더 주입 / 임베딩 base_url 분리 / tool-calling 차단
├─ tools/
│  ├─ _common.py               # 공유 유틸 (경로/venv/플랫폼/데이터 경로)
│  ├─ setup.py                 # 1회성 셋업 (venv + vendoring + 의존성 + .env + data/)
│  ├─ launch.py                # 반복 실행 (prepare / check-embedding / tiktoken / research / doctor)
│  ├─ prepare_data.py          # jsonl/csv/json → data/docs/*.txt(기본)·.md 변환기 (로컬 데이터)
│  ├─ check_embedding.py       # 별도 운영 BGE 엔드포인트 연결/호환성 점검 (stdlib)
│  ├─ tiktoken_offline.py      # SSL 차단 환경용 tiktoken 캐시 설치/검증 (status/install/verify)
│  └─ run_research.py          # 리서치 엔트리포인트 (--source web|local|hybrid)
├─ examples/sample-corpus.jsonl # 로컬 데이터 형식 예제(3건)
├─ data/                       # raw/(원본) + docs/(변환본=DOC_PATH). git 제외
├─ windows/                    # .bat thin wrapper (setup/prepare-data/check-embedding/research[-local]/doctor)
├─ MANUAL.md                   # 로컬 데이터 문서작성 실행 가이드(시나리오 1·2·3 + 부록 A~D)
├─ HANDOFF.md                  # 다음 작업자 인수인계(구조·결정·검증·체크리스트)
├─ .env.example                # 환경설정 템플릿
└─ vendor/gpt-researcher/      # (셋업 시 clone) 원본 repo
```

## 빠른 시작 (Windows)

```bat
REM 1) 셋업 (venv 생성 + gpt-researcher clone + 의존성 + .env 생성)
windows\setup.bat

REM 2) .env 편집: OPENAI_BASE_URL / 모델명 / EMBEDDING_BASE_URL / (선택) OPENAI_EXTRA_HEADERS

REM 3) 임베딩 서버는 별도로 운영 — 이 repo 는 접속만 한다. 연결 점검:
windows\check-embedding.bat

REM 4) 리서치 실행
windows\research.bat "양자내성암호 2026 표준화 동향" --report-type research_report

REM 환경 점검
windows\doctor.bat
```

POSIX(WSL/Linux/macOS)에서는 동일하게 `python tools/setup.py`, `python tools/launch.py check-embedding`, `python tools/launch.py research "..."`.

> 🔒 **tiktoken 이 SSL로 막힌 사내망**: BPE 블록을 수동으로 받아 직접 설치·검증 —
> `tiktoken status`(다운로드 URL 확인) → `tiktoken install <파일>` → `tiktoken verify`(SSL 미접속 입증). 상세는 MANUAL 부록 E.

> ⚠ 임베딩(BGE) 서버는 이 repo 가 구동하지 않는다. 사용자가 OpenAI 호환 `/v1/embeddings`
> 엔드포인트를 별도로 띄우고, `.env` 의 `EMBEDDING_BASE_URL` 로 그 주소를 가리킨다.

## 로컬 데이터(jsonl) 기반 문서 작성

웹 대신 **내 로컬 데이터**로 보고서를 만들려면 (웹 미접속, BGE 임베딩 유사도만 사용):

```bat
REM 1) jsonl/csv/json → data\docs\*.txt 변환 (원본 gpt-researcher는 jsonl 미지원이라 변환 필요; txt=오프라인 권장)
windows\prepare-data.bat "data\raw\corpus.jsonl" --content-field text --clean

REM 2) 임베딩: 사용자가 BGE 서버(:8999)를 별도 기동 → .env 의 EMBEDDING_BASE_URL 지정
REM    연결/호환성 검증:  windows\check-embedding.bat

REM 3) 로컬 데이터 기반 보고서 생성
windows\research-local.bat "우리 데이터 핵심 요약" --report-type detailed_report
```
```bash
# POSIX
python tools/launch.py prepare data/raw/corpus.jsonl --content-field text --clean
python tools/launch.py check-embedding   # 사용자 BGE 엔드포인트 연결/호환성 검증
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

# 임베딩 (BGE) — 사용자가 별도 운영하는 엔드포인트에 접속만 (LLM과 별도, 헤더 없음)
EMBEDDING=openai:bge-m3-korean
EMBEDDING_BASE_URL=http://127.0.0.1:8999/v1
EMBEDDING_API_KEY=unused
# 설정 후 연결 검증:  python tools/launch.py check-embedding

# tool-calling 우회
MCP_STRATEGY=disabled
GPTR_DISABLE_TOOLCALLING=1

# 검색 (무료 기본)
RETRIEVER=duckduckgo
```

## default_header 주입 원리

`gptr_oss_patch.py`가 `GenericLLMProvider.from_provider`를 래핑하여,
OpenAI 호환 provider 생성 시 `OPENAI_EXTRA_HEADERS`(JSON)를 `ChatOpenAI(default_headers=...)`로 전달한다.
**임베딩 경로(`Memory.__init__`)에는 헤더를 주입하지 않으며**, `EMBEDDING_BASE_URL`로 사용자 BGE 엔드포인트로만 라우팅한다.
→ "LLM은 인증 헤더, 임베딩은 헤더 없는 별도 모델" 요구사항을 코드 분리로 보장.

**UUID 헤더**: gpt-oss 호출 헤더에 UUID 를 넣는 두 방식 — (A) `OPENAI_EXTRA_HEADERS` 값에
`${uuid4}` 플레이스홀더(프로세스당 1회 고정), (B) `OPENAI_DYNAMIC_UUID_HEADER=X-Request-Id`
(매 요청 새 uuid4, httpx 훅). 둘 다 LLM 전용·임베딩 무관. 상세는 MANUAL §3.1.1.

## gpt-oss 서빙 예시

- **Ollama**: `ollama run gpt-oss:20b` → `OPENAI_BASE_URL=http://localhost:11434/v1`
- **vLLM**: `vllm serve openai/gpt-oss-20b --port 8000` → `OPENAI_BASE_URL=http://localhost:8000/v1`
- **사내 게이트웨이**: `OPENAI_BASE_URL=https://gw.internal/v1` + `OPENAI_EXTRA_HEADERS`로 인증

## 주의

- **임베딩 서버는 이 repo 가 구동하지 않는다.** 사용자가 별도로 띄운 OpenAI 호환
  `/v1/embeddings` 엔드포인트에 접속만 한다. 연결 점검은 `tools/launch.py check-embedding`.
- **임베딩 호출은 동일 로컬 머신 직결(프록시 미경유).** 패치가 `EMBEDDING_BASE_URL`
  host 를 `NO_PROXY` 에 자동 등록해, `HTTP(S)_PROXY` 가 걸려 있어도 127.0.0.1
  호출이 프록시로 새지 않는다. (LLM 게이트웨이는 다른 host 라 프록시/헤더 그대로)
- BGE 모델은 임베딩 전용. gpt-oss(생성)와 역할이 다르므로 반드시 분리 운용.
- `--source local` 은 웹에 접속하지 않는다(임베딩 유사도만). `web`/`hybrid` 만
  `RETRIEVER` 사용 — `tavily`는 `TAVILY_API_KEY` 필요, 키 없으면 `duckduckgo`(무키).
- **Python 3.14 셋업 시 numpy 컴파일러/`vswhere.exe` 오류**: gpt-researcher 가 `numpy<2.3.0`
  을 묶는데 cp314 휠은 2.3.x부터라 소스빌드를 시도해 발생. setup 이 자동으로 numpy
  상한을 완화하고 휠 우선 설치하므로 **컴파일러 설치 불필**. 상세는 MANUAL 부록 D.
