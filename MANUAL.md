# MANUAL — 로컬 데이터 기반 문서 작성 실행 가이드

> GPT-Researcher × GPT-OSS 환경에서 **내 로컬 데이터(jsonl)** 를 입력으로
> 문서(보고서)를 자동 생성하는 전 과정 매뉴얼.
> GPT-OSS(생성 LLM)는 **외부 API 호출**, 임베딩은 **로컬 BGE 서버**를 사용한다.

대상 독자: 이 repo를 처음 받아 Windows(또는 WSL/Linux/macOS)에서 직접 실행하려는 운영자.

---

## 0. 한눈에 보기 — 전체 흐름

```
[내 데이터 .jsonl]                (시나리오 1)
        │  python tools/prepare_data.py
        ▼
[data/docs/*.md]  ← gpt-researcher가 읽는 로컬 문서
        │
        │   ┌─────────────────────────────────────────┐
        │   │ LLM(gpt-oss)  : 외부 OpenAI호환 API 호출  │  (시나리오 2)
        │   │ 임베딩(BGE)    : 내 로컬 서버(127.0.0.1:8999) │
        │   └─────────────────────────────────────────┘
        ▼
python tools/launch.py research "질의" --source local      (시나리오 3)
        ▼
[outputs/report-YYYYmmdd-HHMMSS.md]   ← 완성 보고서
```

핵심 사실(코드 검증 결과):

- `--source local` 은 **웹에 접속하지 않는다.** 로컬 문서를 임베딩(BGE)으로
  유사도 검색해 컨텍스트를 만들고, 그 위에서 gpt-oss가 보고서를 작성한다.
- 따라서 **local 모드 필수 구성요소 = gpt-oss(API) + BGE(로컬) + 변환된 .md** 3가지.
- 검색엔진(Tavily/DuckDuckGo) 키는 local 모드에 **불필요**.

---

## 1. 사전 점검 — 이 repo는 gpt-oss로 구동 가능한가? (검토 결론)

| 항목 | 상태 | 근거 |
|------|------|------|
| gpt-oss로 메인 파이프라인 구동 | **가능(수정 불요)** | 핵심 파이프라인이 function-calling 비의존(프롬프트+json_repair) |
| LLM 인증 헤더 주입 | **구현됨** | `patches/gptr_oss_patch.py` 가 `default_headers` 를 LLM에만 주입 |
| 임베딩 LLM과 분리 | **구현됨** | `EMBEDDING_BASE_URL` 로 BGE 서버 라우팅, 헤더 미주입 |
| tool-calling 우회 | **구현됨** | `MCP_STRATEGY=disabled` + `supports_tools()→False` |
| Windows 올인원 셋업 | **구현됨** | `windows\*.bat` + setup/launch 분리 |
| **로컬 jsonl 입력** | **본 버전에서 추가** | `tools/prepare_data.py` (jsonl→md) + `--source local` |

> 원래 repo의 `DocumentLoader`는 pdf/txt/md/docx/csv/xls(x)/html만 읽고
> **jsonl은 조용히 건너뛴다.** 그래서 jsonl→md 변환 단계를 본 매뉴얼에서 추가했다.

먼저 셋업이 끝났는지 확인:

```bat
REM Windows
windows\setup.bat
windows\doctor.bat
```
```bash
# WSL/Linux/macOS
python tools/setup.py
python tools/launch.py doctor
```

`doctor` 출력에서 `venv / vendor gptr / .env` 가 모두 OK여야 다음 단계로 진행한다.

---

## 2. 시나리오 1 — 내 로컬 데이터 준비 (→ jsonl → 변환)

### 2.1 목표 형태: JSONL

**JSONL**(JSON Lines) = 한 줄에 JSON object 1건. 문서 1건이 한 줄이다.

```jsonl
{"id": "kb-001", "title": "양자내성암호 현황", "text": "NIST는 2024년 ML-KEM ...", "category": "security"}
{"id": "kb-002", "title": "임베딩 인프라", "text": "사내 검색은 bge-m3 ...", "category": "infra"}
```

권장 필드(없어도 자동 추정):

| 역할 | 권장 키 | 자동 추정 후보 | 비고 |
|------|---------|----------------|------|
| 식별자 | `id` | id, doc_id, uid, _id, key | 없으면 일련번호 자동 |
| 제목 | `title` | title, name, headline, subject, heading | 없으면 "문서 N" |
| **본문(필수)** | `text` | content, text, body, abstract, summary, raw_content | **이게 없으면 변환 0건** |
| 메타(선택) | 임의 | — | category, source, date 등 자유 |

> 최소 요건: **본문에 해당하는 필드 1개**만 있으면 된다. 나머지는 메타로 보존된다.

### 2.2 데이터 두는 위치(경로)

```
gptr-oss-winsetup/
├─ data/
│  ├─ raw/      ← 여기에 원본 .jsonl / .csv / .json 을 둔다 (사용자 작성)
│  └─ docs/     ← 변환 산출물 .md (자동 생성, gpt-researcher가 읽음 = DOC_PATH)
└─ examples/
   └─ sample-corpus.jsonl   ← 형식 참고용 예제(3건)
```

`data/` 는 `.gitignore` 처리되어 git에 올라가지 않는다(사내 데이터 보호).
실습은 `examples/sample-corpus.jsonl` 로 바로 해볼 수 있다.

### 2.3 다른 형태에서 jsonl 만들기 (준비 방법)

이미 jsonl이면 건너뛴다. 아니라면:

- **CSV / JSON** → 변환기가 직접 입력으로 받는다(아래 2.4에서 `.csv`/`.json` 그대로 지정).
- **여러 txt/pdf/docx** → 변환 불필요. 그 파일들을 `data/docs/` 에 그대로 넣고
  시나리오 3에서 `--source local --doc-path data/docs` 로 바로 쓰면 된다
  (gpt-researcher가 네이티브 지원하는 포맷이므로).
- **DB/스프레드시트** → 행을 한 줄 JSON으로 내보내 jsonl 작성. 예(파이썬):

  ```python
  import json
  rows = [{"id": r.id, "title": r.title, "text": r.body} for r in query_all()]
  with open("data/raw/corpus.jsonl", "w", encoding="utf-8") as f:
      for r in rows:
          f.write(json.dumps(r, ensure_ascii=False) + "\n")
  ```

### 2.4 변환 실행 (jsonl/csv/json → data/docs/*.md)

```bat
REM Windows — 예제로 먼저 검증
windows\prepare-data.bat "examples\sample-corpus.jsonl" --clean

REM 실제 데이터
windows\prepare-data.bat "data\raw\corpus.jsonl" --content-field text --clean
```
```bash
# WSL/Linux/macOS
python tools/launch.py prepare examples/sample-corpus.jsonl --clean
python tools/launch.py prepare data/raw/corpus.jsonl --content-field text --clean
```

자주 쓰는 옵션:

| 옵션 | 설명 |
|------|------|
| `--content-field text` | 본문 필드명을 명시(자동 추정이 틀릴 때) |
| `--title-field headline` | 제목 필드명을 명시 |
| `--id-field doc_id` | 식별자 필드명을 명시 |
| `--meta-field category --meta-field source` | 보존할 메타 필드만 선별(반복 지정) |
| `--out data/docs` | 출력 위치(기본 `data/docs`) |
| `--clean` | 출력 폴더의 기존 .md 를 비우고 새로 생성(재변환 시 권장) |
| `--min-chars 20` | 본문이 너무 짧은 레코드(노이즈) 제외 |

변환 결과 각 .md 형태:

```markdown
# 양자내성암호 현황

- source_id: kb-001
- category: security
- source: internal-wiki

NIST는 2024년 ML-KEM ...
```

> 출력 로그의 `생성 : N 파일` 을 확인한다. **0건이면** 본문 필드 매핑이 잘못된
> 것이므로 `--content-field` 로 실제 본문 키를 지정해 다시 실행한다.

---

## 3. 시나리오 2 — 로컬 LLM/임베딩 세팅

> GPT-OSS(생성)는 **외부 API로 호출**하므로 로컬 설치 대상이 아니다(요구사항).
> 따라서 여기서 "로컬 세팅"의 핵심은 **임베딩(BGE) 서버**다.

### 3.1 LLM(gpt-oss) — 외부 OpenAI 호환 엔드포인트 지정만

`.env` 를 열어 LLM 접속 정보를 채운다(설치 아님, 연결 설정):

```dotenv
OPENAI_BASE_URL=https://<gpt-oss-게이트웨이>/v1   # 또는 http://localhost:8000/v1
OPENAI_API_KEY=unused                            # 헤더 인증이면 더미값
FAST_LLM=openai:gpt-oss-20b
SMART_LLM=openai:gpt-oss-120b
STRATEGIC_LLM=openai:gpt-oss-120b

# 사내 게이트웨이 인증이 필요할 때만 (LLM 호출에만 주입됨):
# OPENAI_EXTRA_HEADERS={"Authorization":"Bearer xxxxx","X-Project-Id":"samsung"}
```

- `provider` 는 `openai` 로 고정(= OpenAI 호환). `model` 만 서빙 중인 gpt-oss 이름으로.
- 인증 헤더는 `OPENAI_EXTRA_HEADERS`(JSON)로. **임베딩에는 절대 적용되지 않는다.**

### 3.2 임베딩(BGE) — 별도 운영 중인 엔드포인트 연결 (local 모드 필수)

> 임베딩 서버는 **사용자가 별도 프로세스로 직접 기동**한다. 이 repo 는 서버를
> 설치하거나 구동하지 않으며, **활성화된 엔드포인트에 HTTP 로 접속만** 한다.
> (그래서 `start-bge.bat` 같은 서버 기동 단계가 없다.)

`.env` 에서 그 엔드포인트를 가리키기만 하면 된다. 기본값이 이미 사용자 서버
(`bge-m3-korean`, 포트 8999) 구조에 맞춰져 있다:

```dotenv
EMBEDDING=openai:bge-m3-korean              # provider=openai 고정, model=서버 응답 이름
EMBEDDING_BASE_URL=http://127.0.0.1:8999/v1 # 서버의 /v1 경로 (다른 PC면 127.0.0.1 -> IP)
EMBEDDING_API_KEY=***
```

> 사용자 서버 계약(제공해주신 예): `host="0.0.0.0", port=8999`, `POST /v1/embeddings`,
> `model.encode(..., normalize_embeddings=True)` -> float 리스트 반환. 이 계약은
> gpt-researcher 와 그대로 호환된다(독립 검증 완료, 부록 B 참조).

**연결 점검** (사용자가 BGE 서버를 띄운 상태에서):

```bat
windows\check-embedding.bat
```
```bash
python tools/launch.py check-embedding
```
-> `[check_embedding][OK] 정상. 벡터 N개, dim=1024` 이 나오면 연동 준비 완료.
(`doctor` 도 `BGE /v1/embeddings: OK(dim=...)` 로 같은 점검을 한다.)

> 주의(호환성 핵심, 검증됨): gpt-researcher 는 langchain 으로 임베딩을 호출한다.
> 본 repo 패치가 `check_embedding_ctx_length=False` 를 자동 주입해 서버에 **원문 텍스트**를
> 보낸다. 이 패치가 없으면 langchain 이 OpenAI 전용 tiktoken 토큰ID(정수배열)를 전송해
> BGE 임베딩이 깨진다. import 시 자동 적용되므로 추가 조치는 불필요하다.
> (langchain 이 base64 응답을 요청해도 float 리스트 응답을 정상 파싱하므로,
> 사용자 서버처럼 float 리스트를 반환해도 문제없다.)

### 3.3 tool-calling 우회 / 검색 (local 모드에선 검색 불요)

`.env` 의 아래 값은 기본 그대로 둔다:

```dotenv
MCP_STRATEGY=disabled
GPTR_DISABLE_TOOLCALLING=1
# RETRIEVER=duckduckgo   # local 모드에선 사용 안 함(hybrid/web일 때만)
```

---

## 4. 시나리오 3 — 문서 작성 파이프라인 실행

### 4.1 사전 체크 (3종 준비 확인)

```bat
windows\doctor.bat
```
```bash
python tools/launch.py doctor
```

다음이 모두 충족되어야 한다:

- `local docs : N .md` — N ≥ 1 (시나리오 1 완료)
- BGE `/v1/embeddings : OK(dim=...)` — 임베딩 서버 응답 정상 (시나리오 2 완료).
  `python tools/launch.py check-embedding` 으로 단독 검증 가능
- `OPENAI_BASE_URL` 설정됨, LLM `/v1/models : OK` — gpt-oss 접속 가능

### 4.2 실행 (로컬 데이터 기반)

```bat
REM Windows — 전용 래퍼
windows\research-local.bat "우리 데이터에서 양자내성암호 대응 현황을 요약" --report-type research_report

REM 또는 공통 래퍼로
windows\research.bat "우리 데이터에서 ... 요약" --source local
```
```bash
# WSL/Linux/macOS
python tools/launch.py research "우리 데이터에서 양자내성암호 대응 현황을 요약" --source local
```

주요 옵션:

| 옵션 | 값 | 설명 |
|------|----|------|
| `--source` | `local` | 로컬 문서만(웹 미접속). `hybrid`=로컬+웹, `web`=웹만 |
| `--doc-path` | `data/docs` | 로컬 문서 폴더(기본값). 다른 폴더 쓰면 지정 |
| `--report-type` | `research_report` | `detailed_report`(상세/장문), `outline_report`(개요) 등 |
| `--tone` | `Objective` | 서술 톤(Objective/Analytical/Formal 등) |
| `--out` | `outputs/...md` | 출력 경로 직접 지정 |
| `--verbose` | — | 단계별 상세 로그 |

### 4.3 진행 중 일어나는 일 (파이프라인 내부)

1. **하위 질의 생성**: gpt-oss(STRATEGIC_LLM)가 질의를 여러 하위 질문으로 분해.
2. **로컬 문서 적재**: `DOC_PATH`(data/docs)의 .md 전체 로드.
3. **유사도 검색**: 각 하위 질문을 BGE 임베딩으로 문서와 매칭 → 관련 단락 추출
   (**이 단계에서 웹 스크래핑은 일어나지 않는다 — scraped_data가 이미 채워져 있음**).
4. **보고서 작성**: gpt-oss(SMART_LLM)가 추출 컨텍스트로 최종 보고서 작성.
5. **저장**: `outputs/report-YYYYmmdd-HHMMSS.md` 로 기록 + 길이 출력.

### 4.4 결과 확인

```bat
type outputs\report-*.md
```
```bash
ls -t outputs/ | head; cat "outputs/$(ls -t outputs | head -1)"
```

### 4.5 품질 조정 팁

- **분량 늘리기**: `--report-type detailed_report` + `.env` 의 `TOTAL_WORDS`(기본 1200) 상향.
- **사실성 강화**: `.env` 에 `CURATE_SOURCES=true` (관련도 낮은 문서 컷).
- **한국어 보고서**: `.env` 에 `LANGUAGE=korean`.
- **컨텍스트 부족 경고**가 뜨면: 문서 수가 적거나 임베딩 매칭 실패. 데이터 보강 또는
  `--min-chars` 를 낮춰 재변환, 혹은 `--source hybrid` 로 웹 보완.

---

## 5. 트러블슈팅

| 증상 | 원인 | 조치 |
|------|------|------|
| 변환 `생성: 0 파일` | 본문 필드 자동추정 실패 | `--content-field <실제키>` 지정 |
| `DOC_PATH 에 문서가 없습니다` | 변환 미실행 / 경로 불일치 | 시나리오 1 재실행, `--doc-path` 확인 |
| `EMBEDDING_BASE_URL 미설정` 경고 | .env 임베딩 구간 누락 | `.env` 임베딩 값 채우고 BGE 기동 |
| BGE `/v1/embeddings : FAIL` | 서버 미기동/포트/URL 불일치 | 내 BGE 서버 기동 확인, `EMBEDDING_BASE_URL` 점검, `check-embedding` 실행 |
| LLM `/v1/models : FAIL` | gpt-oss 엔드포인트/헤더 오류 | `OPENAI_BASE_URL`, `OPENAI_EXTRA_HEADERS` 점검 |
| 보고서가 비거나 짧음 | 매칭 컨텍스트 부족 | 데이터 보강, `CURATE_SOURCES=false`, `hybrid` 시도 |
| jsonl 파싱 skip 로그 | 깨진 줄(중간 줄바꿈 등) | 해당 줄 수정. 변환기는 깨진 줄만 건너뛰고 계속 진행 |

---

## 6. 빠른 명령 요약 (Cheat Sheet)

```bash
# 0) 셋업(최초 1회)
python tools/setup.py

# 1) 데이터 준비: jsonl -> data/docs/*.md
python tools/launch.py prepare data/raw/corpus.jsonl --content-field text --clean

# 2) 임베딩: 사용자가 BGE 서버를 별도로 띄운다(포트 8999) → .env 의 EMBEDDING_BASE_URL 지정
python tools/launch.py check-embedding      # 엔드포인트 연결/호환성 검증

# 2') .env 에 OPENAI_BASE_URL / 모델 / (선택)헤더 입력

# 3) 로컬 데이터 기반 보고서 생성
python tools/launch.py research "질의문" --source local --report-type detailed_report

# 점검
python tools/launch.py doctor
```

Windows는 위 `python tools/launch.py X` 를 각각
`windows\setup.bat` / `prepare-data.bat` / `check-embedding.bat` / `research-local.bat` / `doctor.bat`
로 대체하면 된다. (임베딩 서버 기동은 이 repo 밖, 사용자가 별도 수행)

---

## 부록 A. 동작 원리 — 왜 local 모드가 오프라인인가

`gpt_researcher/skills/researcher.py` 의 `_process_sub_query`:

```python
# Get web search context using non-MCP retrievers (if no scraped data provided)
if not scraped_data:
    scraped_data = await self._scrape_data_by_urls(sub_query, query_domains)
# Get similar content based on scraped data
if scraped_data:
    web_context = await self.researcher.context_manager.get_similar_content_by_query(sub_query, scraped_data)
```

`report_source=local` 이면 로컬 문서가 `scraped_data` 로 먼저 채워진다.
`if not scraped_data` 가 False가 되어 **웹 스크래핑 분기를 건너뛴다.**
이후 `get_similar_content_by_query` 가 BGE 임베딩으로 유사 단락만 뽑는다.
→ **LLM(gpt-oss, API) + 임베딩(BGE, 로컬) 두 자원만으로 완결.**

## 부록 B. 검증 상태

- jsonl→md 변환기: 더미 코퍼스로 동작 확인(필드 자동추정/메타 보존/깨진 줄 skip).
- 전체 파이썬 모듈 `py_compile` 통과.
- `run_research.py --source/--doc-path` 인자 파싱 확인.
- **임베딩 호환성 독립 검증 완료** (langchain_openai 1.3.2 실제 설치 + 사용자 서버
  계약 모사 mock 대상):
  - 패치 적용 시 langchain 이 서버에 **원문 텍스트**(`"첫 번째 문서..."`) 전송 → 정상.
  - 패치 미적용 시 tiktoken **토큰ID 정수배열**(`[36155,104,...]`) 전송 → BGE 에서 깨짐 확인.
  - 서버가 float 리스트로 응답해도(base64 아니어도) langchain 이 정상 파싱 → dim=1024 수신.
  - `tools/check_embedding.py` 가 이 경로를 그대로 재현해 사용자 서버로 자가점검 가능.
- **실제 gpt-oss 엔드포인트 + BGE E2E 전체 실행은 사용자 환경에서 수행 예정.**

## 부록 C. 임베딩 서버 수정 필요 여부 (검색 측면 포함)

**결론: 파이프라인 정확성·검색 품질 관점에서 서버 수정은 불필요하다.** 제공된
`bge-m3-korean` 서버는 그대로 연동된다. 근거와 점검 항목은 아래와 같다.

### C.1 수정 불필요 (검증 완료)

| 항목 | 사용자 서버 | 판정 |
|------|-------------|------|
| OpenAI 호환 `POST /v1/embeddings` | 구현됨 | ✅ langchain 이 호출하는 유일 엔드포인트 |
| 추가 필드(`encoding_format`,`dimensions`,`user`) 수용 | Pydantic v2 기본 `extra=ignore` | ✅ 422 안 남(실측 확인) |
| 응답 형식 | float 리스트 | ✅ langchain 이 base64 요청해도 float 리스트 정상 파싱 |
| **정규화** | `normalize_embeddings=True` | ✅ **코사인 유사도 검색의 핵심** — 이미 충족 |
| 쿼리/문서 인코딩 | 동일(대칭) | ✅ **bge-m3 는 쿼리 instruction prefix 불필요** → 대칭이 정답 |

> "검색"에 대한 핵심: `--source local` 에서는 웹 retriever(Tavily/DuckDuckGo)를 **쓰지
> 않는다.** 로컬 문서 "검색" = 임베딩 코사인 유사도이고, 유사도 계산은
> gpt-researcher(langchain) 쪽에서 수행한다. **서버는 정규화된 벡터만 반환하면 되며,
> 서버에 별도 검색/랭킹 기능을 넣을 필요가 없다.** normalize=True 가 이미 있으므로
> 검색 품질 측면의 서버 수정도 불필요하다.
>
> (참고) 만약 모델이 `bge-large-en-v1.5` 였다면 쿼리에 instruction prefix 를 붙이는 게
> 권장되지만, **bge-m3 계열은 prefix 가 불필요**하므로 현재 대칭 인코딩이 올바르다.

### C.2 선택적 개선 (필수 아님 — 운영 견고성)

단일 사용자/소규모면 무시해도 된다. 다중 동시 요청·대용량에서만 의미.

1. **이벤트 루프 블로킹(동시성)** — `async def create_embeddings` 안에서 동기
   `model.encode(...)` 를 호출하면 그 사이 이벤트 루프가 막힌다. gpt-researcher 가
   하위질의를 병렬로 임베딩 요청하면 직렬화·지연이 생길 수 있다(정확성에는 무영향).
   개선: 핸들러를 `def`(동기)로 바꿔 FastAPI 스레드풀에 맡기거나 `run_in_executor` 사용.

   ```python
   # 동시성 개선 예: async def → def 로 변경 (FastAPI 가 자동으로 스레드풀에서 실행)
   @app.post("/v1/embeddings")
   def create_embeddings(request: EmbeddingRequest):
       ...
   ```

2. **대용량 배치 타임아웃** — langchain 은 최대 `chunk_size`(기본 1000)건을 1회 POST 로
   보낸다. sentence-transformers 가 내부적으로 batch 처리하므로 OOM 위험은 낮으나,
   1000건 인코딩이 길어지면 클라이언트 타임아웃이 날 수 있다. 필요 시 `model.encode(...,
   batch_size=32)` 명시 또는 서버 타임아웃 여유 확보.

3. **(선택) `/health`, `/v1/models` 추가** — 파이프라인엔 불필요(본 repo `doctor` 는
   `/v1/embeddings` POST 로 점검). 모니터링/로드밸런서가 필요하면 추가하면 편하다.

### C.3 권장 점검 순서

```bash
# 서버를 띄운 뒤
python tools/launch.py check-embedding     # OK(dim=...) 확인이면 연동 끝
python tools/launch.py doctor              # BGE /v1/embeddings: OK 재확인
```
