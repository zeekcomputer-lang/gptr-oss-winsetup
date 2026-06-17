"""
sanitize_report — 최종 보고서 본문에서 인라인 출처(링크/URL/문서ID)를 제거하는 결정적 후처리.

배경(왜 필요한가):
  - vendor(gpt-researcher) 보고서 프롬프트가 "모든 URL을 [text](url) 하이퍼링크로, APA 인용" 을
    강제 → local 모드에서 모델이 임의 링크를 지어내고 거기에 문서 ID를 붙인다.
  - chrono 모드는 digest 의 `[[id]]` 마커(커버리지 검증용)가 최종 본문으로 새어 나온다.
  - 프롬프트 하드닝(_patch_suppress_inline_citations)만으론 약체 모델이 안 따르므로,
    이 모듈이 **코드와 무관하게** 본문을 결정적으로 정리한다.

정책(GPTR_SOURCE_MODE):
  - none (기본): 인라인 전부 제거 + 말미 출처 섹션도 제거 → 출처 일절 없음.
  - end        : 인라인 전부 제거 + 본문에 등장한 [[id]] 를 title 로 해석해 **맨 끝 '## 출처'** 한 곳에만 목록화.
  인라인 억제는 두 모드 모두 항상 수행한다.

보호 영역: 펜스 코드블록(```)·인라인 코드(`...`) 내부는 절대 손대지 않는다(링크가 있어도 보존).
멱등: 두 번 실행해도 결과가 동일하다(end 모드는 이전 출처 라벨을 재사용).

이 모듈은 stdlib 만 사용하며 vendor 구조와 무관하게 동작한다(mock 테스트 가능).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# 출처/참고문헌 섹션으로 간주할 헤딩(헤딩 텍스트 전체가 키워드일 때만 — 오탐 방지)
_SOURCE_HEAD_RE = re.compile(
    r"^\s{0,3}#{1,6}\s*"
    r"(참고\s*문헌|참고\s*자료|참고\s*목록|참고|출처|참조|인용|"
    r"references?|sources?|bibliography|citations?|works\s+cited)"
    r"\s*[:：]?\s*$",
    re.IGNORECASE,
)

_FENCED_PH = "\x00F{}\x00"
_INLINE_PH = "\x00I{}\x00"

_FENCED_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")

# 인라인 제거 규칙(보호되지 않은 본문 세그먼트에만 적용)
_MARKER_RE = re.compile(r"\[\[[^\[\]]*\]\]")            # [[id]] 마커
_IMG_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")         # ![alt](url) → alt
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")         # [text](url) → text
_REFLINK_RE = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")     # [text][ref] → text
_AUTOLINK_RE = re.compile(r"<https?://[^>\s]+>")        # <http...>
_RAWURL_RE = re.compile(r"https?://[^\s)\]>,]+")        # raw URL
_EMPTY_PAREN_RE = re.compile(r"\(\s*\)")
_EMPTY_BRACK_RE = re.compile(r"\[\s*\]")


def extract_ref_ids(text: str) -> list[str]:
    """본문의 [[id]] 마커에서 문서 id 를 등장 순서대로(중복 제거) 추출."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _MARKER_RE.finditer(text):
        raw = m.group(0)[2:-2].strip()
        for piece in re.split(r"[,\s]+", raw):
            p = piece.strip()
            # "id part 1/3" 같은 꼬리 제거 → 첫 토큰만 id 로
            p = p.split()[0] if p else p
            if p and p not in seen:
                seen.add(p)
                out.append(p)
    return out


def _protect(text: str, pattern: re.Pattern, ph: str, store: list[str]) -> str:
    def _sub(m: re.Match) -> str:
        store.append(m.group(0))
        return ph.format(len(store) - 1)
    return pattern.sub(_sub, text)


def _restore(text: str, ph: str, store: list[str]) -> str:
    for i, val in enumerate(store):
        text = text.replace(ph.format(i), val)
    return text


def strip_trailing_sources(text: str) -> tuple[str, list[str]]:
    """말미의 vendor 출처/참고문헌 섹션을 제거하고, 그 안의 불릿 라인을 함께 반환.

    가장 마지막에 등장하는 출처 헤딩부터 문서 끝까지 잘라낸다(섹션은 통상 맨 끝).
    반환된 불릿 라인은 end 모드 멱등성(이전 출처 라벨 재사용)에 쓰인다.
    """
    lines = text.splitlines()
    cut = None
    for i, ln in enumerate(lines):
        if _SOURCE_HEAD_RE.match(ln):
            cut = i  # 마지막 매치를 채택
    if cut is None:
        return text, []
    captured = lines[cut + 1:]
    kept = "\n".join(lines[:cut]).rstrip()
    return kept, captured


def strip_inline(text: str) -> str:
    """보호되지 않은 본문에서 링크/URL/마커를 제거(텍스트는 보존)."""
    text = _MARKER_RE.sub("", text)
    text = _IMG_RE.sub(r"\1", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _REFLINK_RE.sub(r"\1", text)
    text = _AUTOLINK_RE.sub("", text)
    text = _RAWURL_RE.sub("", text)
    text = _EMPTY_PAREN_RE.sub("", text)
    text = _EMPTY_BRACK_RE.sub("", text)
    # 가벼운 정리: 줄 시작 들여쓰기는 보존하고, 단어 사이 다중 공백/구두점 앞 공백만 정리
    text = re.sub(r"(?<=\S) {2,}", " ", text)
    text = re.sub(r" +([,.;:、，。])", r"\1", text)
    return text


def build_sources_section(entries: list[str], heading: str = "출처") -> str:
    out: list[str] = []
    seen: set[str] = set()
    for e in entries:
        label = (e or "").strip()
        if not label or label in seen:
            continue
        seen.add(label)
        out.append(f"- {label}")
    if not out:
        return ""
    return f"\n\n## {heading}\n\n" + "\n".join(out) + "\n"


def load_idmap(path: str | Path | None) -> dict:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def sanitize_report(report: str, source_mode: str = "none",
                    idmap: dict | None = None) -> str:
    """보고서 본문의 인라인 출처를 제거. source_mode 에 따라 말미 출처를 처리.

    none: 인라인 제거 + 출처 섹션 제거(출처 없음)
    end : 인라인 제거 + 맨 끝 '## 출처'(title 목록) 한 곳만
    """
    mode = (source_mode or "none").strip().lower()
    if mode not in ("none", "end"):
        mode = "none"
    idmap = idmap or {}

    fenced: list[str] = []
    inline: list[str] = []
    text = _protect(report, _FENCED_RE, _FENCED_PH, fenced)
    text = _protect(text, _INLINE_CODE_RE, _INLINE_PH, inline)

    # 보호된 상태에서 마커 수집 + vendor 출처 섹션 제거
    ref_ids = extract_ref_ids(text)
    text, captured = strip_trailing_sources(text)
    text = strip_inline(text)

    # 보호 영역 복원
    text = _restore(text, _INLINE_PH, inline)
    text = _restore(text, _FENCED_PH, fenced)

    if mode == "end":
        entries = [idmap.get(i, i) for i in ref_ids]
        if not entries:
            # 멱등 경로: 이전에 우리가 만든 '- label'(링크 없음) 라인 재사용
            entries = [ln.strip()[2:].strip() for ln in captured
                       if ln.strip().startswith("- ") and "](" not in ln]
        text = text.rstrip() + build_sources_section(entries)

    return text.rstrip() + "\n"


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="보고서 인라인 출처 제거(본문 정리)")
    ap.add_argument("path", help="입력 .md")
    ap.add_argument("--mode", default="none", choices=["none", "end"])
    ap.add_argument("--idmap", default=None, help="id→title JSON 경로(end 모드)")
    ap.add_argument("-o", "--out", default=None, help="출력(미지정 시 덮어쓰기)")
    args = ap.parse_args()
    p = Path(args.path)
    text = p.read_text(encoding="utf-8")
    cleaned = sanitize_report(text, args.mode, load_idmap(args.idmap))
    out = Path(args.out) if args.out else p
    out.write_text(cleaned, encoding="utf-8")
    print(f"[sanitize_report] 완료 → {out} ({len(cleaned)} chars, mode={args.mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
