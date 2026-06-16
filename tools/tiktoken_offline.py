"""
tiktoken_offline — SSL 차단 환경에서 수동 다운로드한 tiktoken BPE 블록을 직접 사용하게 세팅/점검.

배경:
  tiktoken 은 인코딩(o200k_base/cl100k_base 등)을 처음 쓸 때 BPE 블록을
  openaipublic.blob.core.windows.net 에서 받는다. 사내망에서 이 도메인이 SSL 로 막히면
  매 LLM 호출의 비용산정(utils/costs.py)에서 실패한다.

  tiktoken 의 캐시 규칙(tiktoken.load.read_file_cached, 실측 확인):
    cache_dir   = os.environ["TIKTOKEN_CACHE_DIR"]            # 1순위
    cache_name  = sha1(<blob_url>).hexdigest()               # 파일명 = URL 의 sha1
    if exists(cache_dir/cache_name) and sha256(내용)==expected_hash:
        return 내용                                          # ← 네트워크 미접속(SSL 시도 없음)
  즉 "cache_dir/<sha1(URL)>" 에 올바른 파일만 있으면 tiktoken 은 절대 네트워크를 타지 않는다.

  ※ URL/sha1/expected_hash 는 모두 설치된 tiktoken 의 tiktoken_ext.openai_public 소스에서
    직접 추출한다 → tiktoken 버전이 달라져도 정확히 일치(하드코딩 의존 없음).

서브커맨드:
  status                 캐시 디렉터리의 각 인코딩 파일 존재/무결성 상태
  install <파일...>       수동 다운로드한 원본(.tiktoken)을 올바른 해시명으로 캐시에 설치
                         --as <enc> 로 인코딩 명시 가능(파일명/내용으로 자동판별도 시도)
  verify                 네트워크를 강제 차단한 채 인코딩 로드 → SSL/네트워크 미접속 입증

환경변수:
  TIKTOKEN_CACHE_DIR     캐시 디렉터리(미지정 시 <repo>/offline/tiktoken_cache).
  TIKTOKEN_ENCODINGS     대상 인코딩 CSV(기본 "o200k_base,cl100k_base").
  TIKTOKEN_SHA1_<ENC>    (선택) 해당 인코딩의 캐시 파일명(sha1)을 직접 지정해 덮어씀.
                         예: TIKTOKEN_SHA1_O200K_BASE=fb374d4195...  (보통 불필요)
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import TIKTOKEN_CACHE_DIR, section  # noqa: E402

# 파이프라인이 실제로 쓰는 인코딩: o200k_base(costs.get_encoding) + cl100k_base(text-embedding-3-small)
_DEFAULT_ENCODINGS = ["o200k_base", "cl100k_base"]


def _cache_dir() -> Path:
    d = os.getenv("TIKTOKEN_CACHE_DIR")
    return Path(d) if d else TIKTOKEN_CACHE_DIR


def _target_encodings() -> list[str]:
    raw = os.getenv("TIKTOKEN_ENCODINGS")
    if raw and raw.strip():
        return [e.strip() for e in raw.split(",") if e.strip()]
    return list(_DEFAULT_ENCODINGS)


def _encoding_meta() -> dict[str, dict]:
    """설치된 tiktoken 의 openai_public 소스에서 {enc: {url, sha1, expected}} 를 추출.

    sha1     = 캐시 파일명 = sha1(URL)
    expected = 내용 sha256(무결성 검증값). 소스에 없으면 None.
    """
    try:
        import inspect
        import tiktoken_ext.openai_public as op  # type: ignore
    except Exception as e:
        raise SystemExit(f"[tiktoken_offline] tiktoken 미설치/로드 실패: {e}\n"
                         f"  → 먼저 'python tools/setup.py' 로 venv 에 의존성을 설치하세요.")
    src = inspect.getsource(op)
    meta: dict[str, dict] = {}
    for enc in _target_encodings():
        # "https://....<enc>.tiktoken"  (선택) , expected_hash="<sha256>"
        m = re.search(
            r'"(https://[^"]*' + re.escape(enc) + r'\.tiktoken)"'
            r'(?:\s*,\s*expected_hash\s*=\s*"([0-9a-f]{64})")?',
            src,
        )
        if not m:
            print(f"[tiktoken_offline][WARN] openai_public 소스에서 '{enc}' URL 을 찾지 못함 — 건너뜀")
            continue
        url = m.group(1)
        expected = m.group(2)
        # .env 로 해시명(sha1) 직접 지정 시 우선
        override = os.getenv("TIKTOKEN_SHA1_" + enc.upper())
        sha1 = override.strip() if override and override.strip() else hashlib.sha1(url.encode()).hexdigest()
        meta[enc] = {"url": url, "sha1": sha1, "expected": expected}
    if not meta:
        raise SystemExit("[tiktoken_offline] 대상 인코딩 메타를 하나도 추출하지 못했습니다.")
    return meta


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cmd_status(args) -> int:
    section("tiktoken 오프라인 캐시 상태")
    cache = _cache_dir()
    meta = _encoding_meta()
    print(f"  TIKTOKEN_CACHE_DIR = {cache}")
    all_ok = True
    for enc, m in meta.items():
        path = cache / m["sha1"]
        if not path.exists():
            print(f"  [{enc}] MISSING   파일명={m['sha1']}  (install 필요)")
            all_ok = False
            continue
        if m["expected"]:
            actual = _sha256_file(path)
            ok = (actual == m["expected"])
            print(f"  [{enc}] {'OK' if ok else 'HASH-MISMATCH'}  파일명={m['sha1']}  "
                  f"sha256={'일치' if ok else actual[:16]+'…≠expected'}")
            all_ok = all_ok and ok
        else:
            print(f"  [{enc}] PRESENT(무결성 미검증)  파일명={m['sha1']}")
    print(f"  → {'모두 OK (오프라인 로드 가능)' if all_ok else '미비 항목 있음 — install 또는 파일 재다운로드 필요'}")
    print(f"\n  참고 — 각 인코딩 원본을 받을 URL(사내망 허용 PC/우회로 받으면 됨):")
    for enc, m in meta.items():
        print(f"    {enc}: {m['url']}")
    return 0 if all_ok else 1


def _identify(file: Path, meta: dict[str, dict], as_enc: str | None) -> str | None:
    """파일이 어느 인코딩인지 판별: --as > 파일명 매칭 > 내용 sha256 매칭."""
    if as_enc:
        return as_enc if as_enc in meta else None
    name = file.name.lower()
    for enc in meta:
        if enc in name:
            return enc
    # 내용 sha256 으로 역추적(expected 가 있는 경우)
    digest = _sha256_file(file)
    for enc, m in meta.items():
        if m["expected"] and m["expected"] == digest:
            return enc
    return None


def cmd_install(args) -> int:
    section("tiktoken 오프라인 캐시 설치 (수동 다운로드 원본 → 해시명)")
    cache = _cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    meta = _encoding_meta()

    files = [Path(f) for f in args.files]
    if not files:
        print("[tiktoken_offline] 설치할 파일을 지정하세요. 예: install o200k_base.tiktoken cl100k_base.tiktoken")
        return 2

    rc = 0
    for f in files:
        if not f.exists():
            print(f"  [ERR] 파일 없음: {f}")
            rc = 1
            continue
        enc = _identify(f, meta, args.as_enc)
        if not enc:
            print(f"  [ERR] '{f.name}' 의 인코딩을 판별 못함 — --as o200k_base|cl100k_base 로 지정하세요")
            rc = 1
            continue
        m = meta[enc]
        # 무결성 사전 검증(틀린 파일을 캐시에 넣으면 런타임에 tiktoken 이 지우고 네트워크를 탐 → SSL fail)
        if m["expected"]:
            actual = _sha256_file(f)
            if actual != m["expected"]:
                msg = (f"  [{'WARN' if args.force else 'ERR'}] {f.name} sha256 불일치 "
                       f"(expected {m['expected'][:16]}…, got {actual[:16]}…)")
                print(msg)
                if not args.force:
                    print("        → 올바른 파일을 다시 받으세요. 강제 설치는 --force (권장 안 함).")
                    rc = 1
                    continue
        dst = cache / m["sha1"]
        shutil.copyfile(f, dst)
        print(f"  [OK] {enc}: {f.name} → {dst.name}  ({'무결성 검증됨' if m['expected'] else '검증값 없음'})")
    if rc == 0:
        print("\n  설치 완료. 'python tools/launch.py tiktoken verify' 로 SSL 미접속을 점검하세요.")
    return rc


def cmd_verify(args) -> int:
    """네트워크를 강제 차단한 채 인코딩을 로드해 'SSL/네트워크 미접속'을 입증한다.

    socket 을 막아 어떤 전송 라이브러리든 네트워크 시도 시 즉시 실패하게 만든 뒤,
    tiktoken 으로 대상 인코딩을 로드한다. 성공 = 캐시만으로 동작(네트워크 불요).
    """
    section("tiktoken 오프라인 검증 (네트워크 강제 차단 후 로드)")
    cache = _cache_dir()
    os.environ["TIKTOKEN_CACHE_DIR"] = str(cache)
    # 프록시를 블랙홀로(혹시 socket 우회 라이브러리 대비) + HF/transformers 오프라인
    os.environ["HTTPS_PROXY"] = os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
    os.environ.setdefault("NO_PROXY", "")

    import socket

    class _Blocked(Exception):
        pass

    def _blocked(*a, **k):
        raise _Blocked("network disabled for offline verification")

    # 모든 신규 소켓/주소조회 차단 → 네트워크 시도 시 즉시 예외
    socket.socket = _blocked          # type: ignore
    socket.create_connection = _blocked  # type: ignore
    socket.getaddrinfo = _blocked     # type: ignore

    try:
        import tiktoken
    except Exception as e:
        print(f"  [ERR] tiktoken import 실패: {e}")
        return 1

    encs = _target_encodings()
    ok = True
    for enc in encs:
        try:
            e = tiktoken.get_encoding(enc)
            toks = e.encode("오프라인 토큰화 점검 offline check")
            print(f"  [OK] get_encoding('{enc}') — 네트워크 미접속, 토큰 {len(toks)}개")
        except _Blocked:
            print(f"  [FAIL] '{enc}' 로드 중 네트워크 시도 발생 → 캐시 누락/해시 불일치. "
                  f"tiktoken status 로 점검 후 재설치하세요.")
            ok = False
        except Exception as e:
            print(f"  [FAIL] '{enc}' 로드 오류: {type(e).__name__}: {e}")
            ok = False
    # 파이프라인 비용산정이 쓰는 모델 경로도 점검(text-embedding-3-small → cl100k_base)
    try:
        tiktoken.encoding_for_model("text-embedding-3-small").encode("x")
        print("  [OK] encoding_for_model('text-embedding-3-small') — 네트워크 미접속")
    except _Blocked:
        print("  [FAIL] encoding_for_model 경로에서 네트워크 시도 발생 (cl100k_base 캐시 확인)")
        ok = False
    except Exception as e:
        print(f"  [WARN] encoding_for_model 점검 예외: {type(e).__name__}: {e}")

    print(f"\n  → {'검증 통과: SSL 연결 없이 동작 보장' if ok else '검증 실패: 위 항목을 install 하세요'}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="tiktoken 오프라인 캐시 설치/검증 (SSL 차단 환경)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="캐시 상태/무결성 점검")

    p_ins = sub.add_parser("install", help="수동 다운로드 원본을 해시명으로 캐시에 설치")
    p_ins.add_argument("files", nargs="*", help="원본 .tiktoken 파일들")
    p_ins.add_argument("--as", dest="as_enc", default=None,
                       choices=["o200k_base", "cl100k_base", "p50k_base", "r50k_base"],
                       help="인코딩 명시(자동판별 실패 시)")
    p_ins.add_argument("--force", action="store_true", help="무결성 불일치여도 강제 설치(권장 안 함)")

    sub.add_parser("verify", help="네트워크 차단 후 로드 → SSL 미접속 입증")

    args = ap.parse_args()
    return {"status": cmd_status, "install": cmd_install, "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
