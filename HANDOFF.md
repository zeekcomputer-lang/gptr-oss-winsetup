# HANDOFF — gptr-oss-winsetup (다음 AI agent용 인수인계)

> 이 파일부터 읽는다. §0 으로 30초 안에 맥락을 잡고, §6 체크리스트대로 작업한다.

## §0. 30초 요약

- **목적**: 외부 [gpt-researcher](https://github.com/assafelovic/gpt-researcher) 를 **GPT-OSS**(로컬/사내 OpenAI 호환 LLM)로 구동하는 Windows 올인원 셋업.
- **원본 무수정**: gpt-researcher 는 `vendor/` 에 clone 하고, 런타임 monkeypatch(`patches/gptr_oss_patch.py`)로만 연동.
- **현재 주력 시나리오**: 사용자의 **로컬 데이터(jsonl)** → `.txt` 변환 → `--source local` 로 **완전 오프라인** 문서 생성. (웹 리서치도 됨)
- **완전 오프라인(§8)**: 외부 리소스(tiktoken BPE·NLTK)를 setup 이 `offline/` 에 미리 받아두고, 패치가 `TIKTOKEN_CACHE_DIR`/`NLTK_DATA` 를 자동 연결 → 런타임 네트워크 0(LLM·임베딩 API 호출만).
- **역할 분리(중요)**: LLM = 외부 API(프록시·인증 헤더 가능) / 임베딩 = **사용자가 별도 운영하는 BGE 엔드포인트에 접속만**(이 repo 는 임베딩 서버를 구동하지 않음).
- **상태**: 환경 비의존 로직은 전부 실측 검증 완료. **실제 gpt-oss 엔드포인트 + BGE E2E 전체 실행은 사용자 환경에서 미수행**.
- **최신 커밋**: `8864908` (Python 3.14 numpy 셋업 픽스).

## §1. 실행 흐름 (POSIX 기준; Windows 는 windows\*.bat thin wrapper)

```
python tools/setup.py                                  # 1회: venv + vendor clone + 의존성 + .env + data/
# .env 편집: OPENAI_BASE_URL/모델, EMBEDDING_BASE_URL(내 BGE), (선택)OPENAI_EXTRA_HEADERS
python tools/launch.py check-embedding                 # 내 BGE 엔드포인트 연결/호환성 점검
python tools/launch.py prepare data/raw/x.jsonl --content-field text --clean   # jsonl→data/docs/*.md
python tools/launch.py research "질의" --source local  # 보고서 → outputs/
python tools/launch.py doctor                          # 환경 점검
```
Windows: `setup.bat / check-embedding.bat / prepare-data.bat / research-local.bat / doctor.bat`.

## §2. 파일 지도

| 경로 | 역할 |
|------|------|
| `patches/gptr_oss_patch.py` | 런타임 패치(멱등, import 시 자동 apply). 아래 4가지를 담당 |
| `tools/setup.py` | 1회성 셋업(무거움). py3.14 numpy 픽스 포함 |
| `tools/launch.py` | 반복 실행: `prepare / check-embedding / research / doctor` |
| `tools/prepare_data.py` | jsonl/csv/json → `data/docs/*.md` 변환 (gpt-researcher 가 jsonl 미지원이라 필수) |
| `tools/check_embedding.py` | 사용자 BGE 엔드포인트 연결/호환성 점검(stdlib, 프록시 미경유) |
| `tools/run_research.py` | 리서치 엔트리(`--source web|local|hybrid`, `--doc-path`) |
| `tools/_common.py` | 경로/venv/플랫폼/데이터 경로 |
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
