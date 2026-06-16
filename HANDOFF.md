# HANDOFF — gptr-oss-winsetup (다음 AI agent용 인수인계)

> 이 파일부터 읽는다. §0 으로 30초 안에 맥락을 잡고, §6 체크리스트대로 작업한다.

## §0. 30초 요약

- **목적**: 외부 [gpt-researcher](https://github.com/assafelovic/gpt-researcher) 를 **GPT-OSS**(로컬/사내 OpenAI 호환 LLM)로 구동하는 Windows 올인원 셋업.
- **원본 무수정**: gpt-researcher 는 `vendor/` 에 clone 하고, 런타임 monkeypatch(`patches/gptr_oss_patch.py`)로만 연동.
- **현재 주력 시나리오**: 사용자의 **로컬 데이터(jsonl)** → `.txt` 변환 → `--source local` 로 **완전 오프라인** 문서 생성. (웹 리서치도 됨)
- **완전 오프라인(§8)**: 외부 리소스(tiktoken BPE·NLTK)를 setup 이 `offline/` 에 미리 받아두고, 패치가 `TIKTOKEN_CACHE_DIR`/`NLTK_DATA` 를 자동 연결 → 런타임 네트워크 0(LLM·임베딩 API 호출만).
- **역할 분리(중요)**: LLM = 외부 API(프록시·인증 헤더 가능) / 임베딩 = **사용자가 별도 운영하는 BGE 엔드포인트에 접속만**(이 repo 는 임베딩 서버를 구동하지 않음).
- **상태**: 환경 비의존 로직은 전부 실측 검증 완료. **실제 gpt-oss 엔드포인트 + BGE E2E 전체 실행은 사용자 환경에서 미수행**.
- **실행 모드 2종(핵심)**: `--mode rag`(유사도 검색·누락 허용) / `--mode chrono`(전문서 map-reduce 요약·누락 0·시간순). → §9 참조. 출력은 기본 한글(`LANGUAGE=korean`).
- **최신 커밋**: `8864908` (Python 3.14 numpy 셋업 픽스) + 본 튜닝(모드 분기·레이트리미트·win32 변환·한글 강제).

## §1. 실행 흐름 (POSIX 기준; Windows 는 windows\*.bat thin wrapper)

```
python tools/setup.py                                  # 1회: venv + vendor clone + 의존성 + .env + data/
# .env 편집: OPENAI_BASE_URL/모델, EMBEDDING_BASE_URL(내 BGE), (선택)OPENAI_EXTRA_HEADERS
python tools/launch.py check-embedding                 # 내 BGE 엔드포인트 연결/호환성 점검
python tools/launch.py prepare data/raw --clean        # jsonl/csv/json + Office/PDF(win32) → data/docs/*.txt
python tools/launch.py research "질의" --mode rag --source local      # RAG 모드(유사도) → outputs/
python tools/launch.py research "주제" --mode chrono                   # 시간순 이벤트 모드(전문서·누락0) → outputs/
python tools/launch.py digest --query "주제"                            # (선택) 다이제스트만 생성
python tools/launch.py doctor                          # 환경 점검
```
Windows: `setup.bat / check-embedding.bat / prepare-data.bat / research-local.bat / doctor.bat`.

## §2. 파일 지도

| 경로 | 역할 |
|------|------|
| `patches/gptr_oss_patch.py` | 런타임 패치(멱등, import 시 자동 apply). 아래 4가지를 담당 |
| `tools/setup.py` | 1회성 셋업(무거움). py3.14 numpy 픽스 포함 |
| `tools/launch.py` | 반복 실행: `prepare / digest / check-embedding / research / doctor` |
| `tools/prepare_data.py` | jsonl/csv/json + **Office/PDF(win32 COM)** → `data/docs/*.txt` 변환. 날짜 best-effort, 대용량 분할 |
| `tools/win32_convert.py` | **신규** Office/PDF → 텍스트 (Word/PowerPoint COM, DRM 대응, Windows 전용). XML 직접 접근 금지 |
| `tools/build_digest.py` | **신규** chrono 모드 map-reduce 다이제스트(25KB 예산, 재귀 reduce, 커버리지 검증). stdlib only |
| `tools/rate_limit.py` | **신규** 전역 토큰버킷(4회/sec, LLM_MAX_RPS) |
| `tools/glossary.py` | **신규** 용어사전(JSON) 로더+프롬프트 블록 렌더러(stdlib). RAG/chrono 공용. `launch.py glossary` |
| `tools/md_to_docx.py` | **신규** 마크다운 보고서 → 비즈니스 DOCX(표/코드/링크/목록). deepdoc-v2 참조 개선판. python-docx 필요 |
| `examples/sample-glossary.json` | 용어사전 입력 예제(terms 배열 + instruction) |
| `tools/check_embedding.py` | 사용자 BGE 엔드포인트 연결/호환성 점검(stdlib, 프록시 미경유) |
| `tools/run_research.py` | 리서치 엔트리(`--mode rag|chrono`, `--language`, `--source`, `--doc-path`) |
| `tools/_common.py` | 경로/venv/플랫폼/데이터 경로 |
| `requirements-windows.txt` | **신규** pywin32(win32 COM 변환용, Windows 전용) |
| `MANUAL.md` | 시나리오 1·2·3 상세 + 부록 A~D |
| `examples/sample-corpus.jsonl` | 입력 형식 예제 3건 |
| `windows/*.bat` | thin wrapper (로직은 전부 tools/*.py) |

`vendor/`, `.venv/`, `.env`, `data/`, `outputs/`, `.gptr-build-requirements.txt` 는 git 제외.

## §3. 패치가 하는 일 (`gptr_oss_patch.py`)

1. **LLM default_headers 주입** — 헤더를 SDK(ChatOpenAI)의 **`default_headers`** 인자로 전달(deepdoc 방식). OpenAI 호환 provider 에만, 임베딩엔 미주입.
   - **우선순위(폴백, 병합 아님)**: 1) `.env` OPENAI_EXTRA_HEADERS  2) `_HARDCODED_LLM_HEADERS`(코드)  3) 둘 다 없으면 헤더 없이 호출. (.env 가 있으면 하드코딩 무시)
   - 값 템플릿 치환: `${uuid4}` / `${uuid4hex}` / `${epoch}` (헤더별 독립, 프로세스당 1회). 결정된 소스는 `_HEADER_SOURCE` 에 기록.
   - 하드코딩 주입점: `_HARDCODED_LLM_HEADERS` 딕셔너리(변수명까지 코드 고정, 값에 `${uuid4}` 또는 정적값).
2. **요청당 UUID 헤더** — `OPENAI_DYNAMIC_UUID_HEADER`(쉼표로 N개) 지정 시 httpx 이벤트 훅으로 매 호출 새 uuid4. sync+async 모두.
3. **임베딩 base_url 분리** — `EMBEDDING_BASE_URL` 로 BGE 라우팅, `check_embedding_ctx_length=False` 주입(원문 텍스트 전송 보장).
4. **임베딩 프록시 미경유** — `EMBEDDING_BASE_URL` host 를 `NO_PROXY` 에 자동 등록(동일 로컬 머신 직결). LLM 은 영향 없음.
5. tool-calling 차단 — `supports_tools()→False`(기본 ON).

## §4. 핵심 설계 결정 (이유)

- **임베딩 서버는 사용자 운영**: 번들 BGE 서버는 제거됨(커밋 9af1fed). repo 는 엔드포인트에 POST 만. 기본값은 `bge-m3-korean` / `127.0.0.1:8999`.
- **local 모드 = 오프라인**: gpt-researcher 가 로컬 문서를 `scraped_data` 로 채우면 웹 스크래핑 분기를 건너뜀(MANUAL 부록 A). LLM+임베딩 2자원만으로 완결.
- **임베딩 호환성 핵심**: 패치의 `check_embedding_ctx_length=False` 가 없으면 langchain 이 tiktoken 토큰ID(정수배열)를 보내 BGE 가 깨진다(MANUAL 부록 B 실측).
- **Python 3.14**: gpt-researcher 의 `numpy<2.3.0` 상한이 cp314 휠을 막아 소스빌드 유발 → setup 이 상한 완화(>=2.3.0)+`--prefer-binary`(MANUAL 부록 D, 실측 확정).

## §5. 검증 상태 (실측)

- jsonl→md 변환: 더미 코퍼스로 동작 확인(필드 자동추정/메타/깨진 줄 skip).
- 임베딩 호환성: langchain_openai 1.3.2 + mock 으로 원문 텍스트 전송/float 응답 파싱/프록시 미경유(httpx·urllib) 확인.
- UUID 헤더: 정적 2변수 독립 / 요청당 N개 갱신(async 포함) / from_provider 래핑 경로 확인.
- py3.14: 실제 3.14.3 에서 numpy `<2.3.0` 휠 부재 → `>=2.3.0` cp314 휠 설치 성공 확인.
- 전체 `py_compile` 통과. 가비지 문자 0.
- **오프라인 리소스(실측)**: setup 의 tiktoken 캐시(o200k_base/cl100k_base) + NLTK(punkt/punkt_tab 등) 다운로드 동작 확인. **불량 프록시(=네트워크 차단) 상태에서 tiktoken encode·NLTK sent_tokenize 정상** = 런타임 네트워크 불요 입증.
- prepare_data `--format txt` (기본): 더미 코퍼스 .txt 생성 확인(TextLoader 경로 → unstructured/NLTK 미경유).
- **미수행**: 실제 gpt-oss + 실제 BGE 로 research 한 건 완주(E2E). 사용자 환경에서 진행 예정.

## §6. 다음 작업 체크리스트

1. 변경 전 `git log --oneline -10` 으로 맥락 확인. 원본 vendor 는 수정 금지(monkeypatch 로만).
2. 코드 변경 시: `python -m py_compile tools/*.py patches/*.py` + 가비지 문자 스캔
   (`grep -nP '[\x{0e00}-\x{0e7f}\x{ff00}-\x{ffef}]' <파일>`).
3. 문서 변경 시: README/MANUAL/.env.example/HANDOFF 4곳 정합성 동기화. `.env.example` KEY ↔ 코드 `os.getenv` 일치 유지.
4. 검증은 가능한 한 mock 으로 실측(추측 금지). 이 프로젝트의 표준임.
5. 푸시 전 stale 스캔: `bge_server|start-bge|launch.py bge|7997` 등이 의도치 않게 남지 않았는지.

## §7. 알려진 한계 / 향후

- 사내 PyPI 미러에 cp314 휠 없는 패키지가 있으면 그 패키지만 소스빌드 필요(MANUAL 부록 D 대안).
- 임베딩 서버 동시성: 사용자 서버가 `async def`+동기 encode 면 이벤트 루프 블로킹 가능(정확성 무영향, MANUAL 부록 C.2).
- `RETRIEVER=tavily` 는 키 필요(web/hybrid 한정). local 모드는 검색 키 불요.
- 오프라인 프로비저닝은 setup(온라인) 시점에 1회 수행. 에어갭 머신이 setup 머신과 다르면 `offline/` 폴더를 함께 복사해 옮긴다.

## §8. 완전 오프라인 전략 (런타임 네트워크 0 — LLM·임베딩 API 만 예외)

증상별 원인과 해결:

| 증상 | 원인 | 해결 |
|------|------|------|
| tiktoken/nltk_data 다운로드 실패 | gpt-researcher 가 런타임에 외부 리소스 자동 다운로드 | setup `provision_offline()` 가 `offline/tiktoken_cache`·`offline/nltk_data` 에 사전 적재 → 패치가 `TIKTOKEN_CACHE_DIR`/`NLTK_DATA` 자동 연결 |
| `Resource punkt_tab not found` | `UnstructuredMarkdownLoader`(.md) 가 NLTK 문장분할 사용 | (1) `prepare --format txt`(기본)로 **TextLoader 경로** → unstructured/NLTK 미경유 (2) 그래도 .md/.docx 쓰면 NLTK 번들 사용 |
| `json_repair … 'NoneType' object is not subscriptable` | gpt-oss 가 JSON 지시 미준수 → 빈/None 응답 | 패치 `_patch_choose_agent_fallback` 가 choose_agent 를 감싸 **기본 에이전트로 폴백**(turn 미취소) |
| 문서 로드 실패/작업 취소 | 위 예외들이 async gather 에서 전파 → 취소 연쇄 | 위 3개 해소 시 사라짐. local 은 `--format txt` 가 가장 안전 |

핵심 환경변수(.env): `GPTR_OFFLINE=1`(HF/transformers 네트워크 차단), `GPTR_AGENT_JSON_FALLBACK=1`(기본 ON). tiktoken/NLTK 경로는 패치가 `offline/` 로 자동 설정(직접 지정 불요).

구현 위치: `tools/setup.py:provision_offline()`(4/6 단계) · `patches/gptr_oss_patch.py:_ensure_offline_resources()/_patch_choose_agent_fallback()` · `tools/prepare_data.py --format` · `tools/_common.py` OFFLINE 경로. `python tools/launch.py doctor` 가 `offline res` 상태(tiktoken_cache/nltk_data OK 여부) 표시.

### §8-1. tiktoken 이 SSL 로 막힌 경우 (수동 다운로드 → 직접 사용)

setup 의 자동 선다운로드도 openaipublic.blob 가 SSL 차단되면 실패한다. 이 때는 허용 PC에서 BPE 블록을 받아 직접 설치한다. tiktoken 은 `TIKTOKEN_CACHE_DIR/<sha1(URL)>` 파일이 있고 내용 sha256 이 일치하면 **절대 네트워크를 타지 않는다**(`tiktoken.load.read_file_cached` 실측 확인).

전용 투울: **`tools/tiktoken_offline.py`** (`python tools/launch.py tiktoken <status|install|verify>`).
- URL/sha1(캐시명)/expected sha256 는 모두 설치된 `tiktoken_ext.openai_public` 소스에서 **직접 추출** → tiktoken 버전이 달라져도 정확히 일치(하드코딩 의존 없음).
- `status`: 각 인코딩의 캐시 존재/무결성 + 다운로드 URL 출력.
- `install <파일...>`: 수동 원본을 올바른 해시명으로 복사(설치 전 sha256 검증; 불일치 면 거부). 인코딩 판별 우선순위=`--as` > 파일명 > 내용 sha256.
- `verify`: socket 을 강제 차단한 채 로드 → 네트워크 시도 시 즉시 예외. 성공=SSL 미접속 입증.
- 자동화: 원본을 `offline/manual/*.tiktoken` 에 두면 `setup.py provision_offline()` 가 자동 install.
- (선택) `.env`: `TIKTOKEN_CACHE_DIR`/`TIKTOKEN_ENCODINGS`/`TIKTOKEN_SHA1_<ENC>` 오버라이드.

**실측**: 수동 원본 install → status OK(sha256 일치) → verify 통과(socket 차단 상태에서 o200k/cl100k/text-embedding-3-small 로드 성공). 손상 파일 거부 + 파일명 무관 내용기반 자동판별도 확인.

**해시 파일명은 상수**: 캐시 파일명 = `sha1(고정 URL)` 이므로 변하지 않는다(o200k=`fb374d4195…`, cl100k=`9b5ad71b2c…`). tiktoken 이 URL 을 바꾸는 버전 업필만 아니면 동일.

### §8-2. “파일은 있는데 런타임에 TIKTOKEN_CACHE_DIR 가 안 잡힌다” 버그 (2026-06-16 수정)

원인: 구판은 `_ensure_offline_resources()` 가 `apply()` **내부**에서, 그것도 `import gpt_researcher`
**이후**에 호출되어 — apply 가 실패하면(import 오류/내부 API 변경 등) env 가 아예 설정되지 않아 tiktoken 이
기본 캐시(temp)를 보고 미스 → 네트워크 → SSL 실패했다.

수정(해결):
- 패치: `_ensure_offline_resources()` 를 **모듈 import 즉시**(최상위, autoapply·gpt_researcher 와 무관) 호출. 명시 apply()에서도 호출(멱등).
- 빈 값 교정: `TIKTOKEN_CACHE_DIR=""`(빈문자열)은 tiktoken 이 “캐싱 비활성=항상 네트워크”로 취급하므로, 미설정/빈값이면 오프라인 캐시로 강제 교체(유효한 사용자 경로는 존중).
- run_research: `_apply_offline_env()` 로 gpt_researcher 터치 전에 직접도 설정(패치 독립, 이중 보호).
- 진단: `python tools/launch.py doctor` 가 venv 에서 패치를 import 해 **런타임 TIKTOKEN_CACHE_DIR 실측값 + 캐시 파일 수**를 출력(“[runtime] …” 행).

## §9. 실행 모드 2종 (2026-06-16 추가 — 핵심)

요구사항: 날짜는 제목/본문/없음이 혼재 → 모드로 분기. 출력은 두 모드 모두 **한글 비즈니스 보고서**.

### 공통 Stage 0 — prepare (모드 무관)
- `python tools/launch.py prepare <파일|디렉터리>` : jsonl/csv/json + **Office/PDF** → `data/docs/*.txt`.
- **Office/PDF 변환은 `tools/win32_convert.py`(Word/PowerPoint COM)** — python-docx/pptx 같은 **XML 직접 파서 금지**(DRM 대응). Windows+Office 필수, 비-Windows 는 해당 파일만 skip(명시 메시지).
- 날짜 best-effort: `--date-field` → 파일명 `YYYY-MM-DD` → 본문 앞부분. 없으면 생략(강제 아님). `date:` 메타로 저장.
- 대용량 문서: `--max-doc-chars N` 으로 부분 분할(여러 part 파일, 동일 source_id 공유).

### Mode 1 — RAG (`--mode rag`, 기본)
- vendor 본래 동작(유사도 검색). 누락 허용. **임베딩 서버 필요**(`check-embedding`).
- 튜닝 노브(.env): `SIMILARITY_THRESHOLD`(완화), `GPTR_RAG_MAX_RESULTS`(상위N cap 상향, 패치가 vendor 하드코딩 10 대체), `GPTR_CHUNK_SIZE/OVERLAP`.
- 패치 지점: `_patch_context_retrieval`(ContextCompressor.async_get_context 래핑).

### Mode 2 — chrono (`--mode chrono`)
- **전 문서 강제 요약(누락 0)** + 시간순. 임베딩 **불요**.
- 흐름: `build_digest`(Stage1) → `data/digest/digest.md`(≤예산) → run_research 가 DOC_PATH=digest + `GPTR_LOCAL_FULL_CORPUS=1` 로 vendor 에 넘겨 한글 보고서 작성(Stage2).
- **map-reduce**: 25KB(`CHRONO_MAX_INPUT_KB`) 입력 예산으로 배치 → 배치별 map(날짜별 이벤트, 문서마다 `[[id]]` 마커) → 합본 초과 시 재귀 reduce(이벤트 보존, 산문 압축).
- **커버리지 보증(코드)**: 입력 id 집합 vs map 출력 `[[id]]` 대조 → 누락분 단건 재처리. `build_digest()` 가 `still_missing` 통계 반환.
- full-corpus 패치: `_patch_context_retrieval`(필터/cap 우회) + `_patch_full_corpus_plan`(plan_research→[], 1패스 → 중복 방지).
- **digest 단계 게이트웨이 타임아웃/용량초과 대응(자가복구)**: map 호출이 용량초과/타임아웃으로 죽으면 `build_digest.map_batch` 가 배치를 반으로→단건→문서조각으로 **적응 분할 재시도**(커버리지 유지, 분할 한계 초과분만 still_missing). 추가로 스트리밍(SSE, idle 504 회피·미지원시 비스트림 폴백), 출력 토큰 cap(`CHRONO_MAX_OUTPUT_TOKENS`=2000), 일시실패 backoff 재시도(`LLM_MAX_RETRIES`), 타임아웃 노브(`LLM_TIMEOUT`=180). 근본 완화는 `CHRONO_MAX_INPUT_KB` 하향(12~16). 504가 "총 처리시간 상한"이면 입력/출력 cap 함께 낮추고, "idle 무응답"이면 스트리밍으로 충분.

### 교차 공통
- **레이트리밋 4회/sec**: `tools/rate_limit.py` 전역 토큰버킷. 패치가 LLM httpx client 의 request 훅에 주입(`_inject_request_hooks`) + build_digest 는 직접 `get_limiter().acquire()`. 임베딩은 제외. env `LLM_MAX_RPS`.
- **한글 출력**: `LANGUAGE=korean`(공식) + `_patch_force_language`(보고서 프롬프트에 한국어 지시 덧붙임, LANGUAGE 가 korean 이면 기본 ON, `GPTR_FORCE_KOREAN`).
- **용어사전 주입(전문지식 사전 참고)**: 요약문서가 고유명·전문용어 정의를 필요로 할 때, 사전에 용어사전(JSON)을 주입해 일관 적용.
  - **파일 기반(.env 아님)**: `data/glossary.json`(단일) 또는 `data/glossary/*.json`(디렉터리 병합, 파일명 오름차순) 이 **존재할 때만** 적용, 없으면 no-op(비치명). 크기 cap 8KB 상수(`tools/glossary.py`). 시작: `cp examples/sample-glossary.json data/glossary.json`. (data/ 는 git 제외 → 사용자 데이터)
  - 형식 자동판별: 평면 dict `{term:def}` / `{"terms":[{term,definition,aliases}],"instruction":...}` / 항목 배열. 키 별칭(word/desc/synonyms 등) 허용.
  - 주입 지점: **RAG** = `_patch_inject_glossary`(PromptFamily.generate_report_* + subtopic, marker `_oss_glossary_patched`) / **chrono** = `build_digest._glossary_block()` 가 map·reduce system 프롬프트에 첨부. 점검 `python tools/launch.py glossary --show`.
  - **chrono 말미 용어집(표) 부록**(기본 ON, `GPTR_GLOSSARY_APPENDIX=0` 으로 비활성): 최종 보고서에 실제 등장한 고유명만 `## 용어집`(용어|정의|출처) 표로 정리. 출처 = `사전`(초기 용어사전) / `문서`(다이제스트에서 정의 명확한 고유명을 1회 LLM 추출—초기 용어집에 없어도 추가). 구현: run_research `_maybe_append_glossary` + `build_digest.extract_definitions`(건건한 `용어 ||| 정의` 줄형식 파싱) + `glossary.find_used_terms/render_glossary_table`. 부록은 .md·.docx(표 렌더) 모두 반영.
- **DOCX 내보내기(비즈니스 보고서·표 지원)**: 출력 마크다운(.md)을 세련된 .docx 로 변환. `tools/md_to_docx.py`(deepdoc-v2 참조 **개선판**).
  - deepdoc-v2 대비 개선: **GFM 표**(헤더 음영·테두리·교대행 음영·셀 정렬) + 순서목록 + 펜스 코드블록 + 링크(하이퍼링크) + H4 + 중첩 목록.
  - 수동: `python tools/launch.py docx outputs/report.md [-o out.docx]`(복수 파일 병합 가능). 자동: `--docx` 또는 `GPTR_EXPORT_DOCX=1` 이면 research 완료 후 같은 이름 .docx 자동 생성(비치명). python-docx 필요(setup 자동 설치).

### 검증 상태(mock 실측, 2026-06-16)
- `build_digest`: 배치 ≤예산 invariant / 커버리지 누락0(D2 누락→단건 재처리) / 시간순 정렬 / 초대형 문서 분할 / 재귀 reduce 축소 — PASS.
- `rate_limit`: 20rps 스페이싱 실측 / disabled(0) no-op — PASS.
- `prepare_data`: 날짜 정규화·추출·분할 — PASS.
- `glossary`: 평면dict/배열/키별칭 자동판별, 크기 cap truncation, 없는경로→빈블록, chrono map 주입, RAG 패치 주입·멱등성 — PASS.
- `md_to_docx`: 표(4×4 헤더+교대행 음영)·코드블록·하이퍼링크·순서/중첩목록·H1~H4 렌더링 — 실제 .docx 생성 PASS(python-docx 1.2.0).
- `py_compile` 전체 통과, 가비지 문자 0.
- **미수행**: MS Word 엔서의 시각 렌더링 육안 확인(WSL에 Word 없음 → 구조적 검증으로 대체).
- **미수행(환경 의존)**: ① 실제 gpt-oss 로 chrono/rag E2E ② win32 COM 변환 실동작(WSL 에 Office 없음 → 사용자 Windows 호스트에서 확인).
