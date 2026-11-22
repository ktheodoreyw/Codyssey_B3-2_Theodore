"""생성 결과 검증 및 다듬기(후처리).

AI 출력은 그대로 믿지 않는다. 실무 규칙(길이·템플릿·표현 제한)에 맞는지 검사하고,
어긋난 부분은 재요청 대신 '후처리'로 바로잡는다. (API 호출을 1회로 유지하기 위함)

규칙
- 커밋 제목: 50자 이내 권장, 최대 72자 (초과 시 단어 경계에서 자름)
- PR 제목  : 최대 80자
- PR 본문  : ## Why / ## What / ## How to Test 헤더 필수 + 각 섹션 최소 1개 불릿
- 표현 제한: 줄바꿈·이모지·끝 마침표 제거, 'feat: feat: ...' 같은 중복 접두사 제거

각 단계의 결과는 Check 목록으로 남겨 터미널의 '검증 결과' 구획에 보여 준다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import config
from .prompts import COMMIT_TYPES

OK, FIXED, WARN = "OK", "보정", "경고"

_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # 각종 이모지/픽토그램
    "\U00002600-\U000027BF"  # 기호, 딩뱃
    "\U0000FE0F"  # variation selector
    "\U0000200D"  # zero width joiner
    "]+"
)
# '- 항목', '* 항목', '• 항목', '1. 항목' 의 머리 기호만 제거 ('--context 옵션' 같은 내용은 보존)
_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-*•·]|\d+[.)])\s+")
_TYPE_PREFIX_RE = re.compile(r"^\s*(?:" + "|".join(COMMIT_TYPES) + r")(?:\([^)]*\))?!?\s*:\s*", re.IGNORECASE)
_TRAILING_PUNCT = ".。!！?？;；,，"

PR_SECTIONS = (("why", "Why"), ("what", "What"), ("how_to_test", "How to Test"))


@dataclass
class Check:
    level: str  # OK / 보정 / 경고
    message: str


@dataclass
class CommitMessage:
    title: str
    bullets: List[str]
    summary: str = ""

    @property
    def text(self) -> str:
        if not self.bullets:
            return self.title
        return self.title + "\n\n" + "\n".join(f"- {b}" for b in self.bullets)


@dataclass
class PRDraft:
    title: str
    sections: Dict[str, List[str]] = field(default_factory=dict)
    summary: str = ""

    @property
    def body(self) -> str:
        blocks = []
        for key, heading in PR_SECTIONS:
            items = self.sections.get(key) or []
            blocks.append(f"## {heading}\n" + "\n".join(f"- {item}" for item in items))
        return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# 문자열 정리 도우미
# ---------------------------------------------------------------------------
def clean_line(text: Any) -> str:
    """한 줄 문자열로 정리: 줄바꿈/연속 공백/이모지 제거."""
    s = str(text or "")
    s = _EMOJI_RE.sub("", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def strip_trailing_punct(text: str) -> str:
    return text.rstrip(_TRAILING_PUNCT).rstrip()


def truncate(text: str, limit: int) -> str:
    """limit 자 이내로 자른다. 가능하면 단어(공백) 경계에서 자르고 '…' 를 붙인다."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.:;-") + "…"


def clean_bullets(items: Any, limit: int) -> List[str]:
    """불릿 목록 정리: 문자열화, 앞의 '-'/'1.' 제거, 빈 항목·중복 제거, 개수 제한."""
    if isinstance(items, str):
        items = [line for line in items.splitlines()]
    result: List[str] = []
    seen = set()
    for item in items or []:
        s = clean_line(_BULLET_PREFIX_RE.sub("", str(item or "")))
        s = strip_trailing_punct(s)
        if not s or s.lower() in seen:
            continue
        seen.add(s.lower())
        result.append(s)
    return result[:limit]


def _normalize_type(value: Any) -> Optional[str]:
    t = clean_line(value).lower()
    return t if t in COMMIT_TYPES else None


def _build_title(type_: str, scope: str, subject: str) -> str:
    prefix = f"{type_}({scope})" if scope else type_
    return f"{prefix}: {subject}"


def _clean_subject(value: Any) -> str:
    subject = clean_line(value)
    # 'feat: ...', 'feat: feat: ...' 처럼 subject 안에 타입 접두사가 들어오면 모두 걷어낸다.
    while True:
        stripped = _TYPE_PREFIX_RE.sub("", subject, count=1)
        if stripped == subject:
            break
        subject = stripped
    return strip_trailing_punct(subject)


def _file_fallback_bullet(paths: Sequence[str]) -> str:
    shown = ", ".join(paths[:3])
    more = f" 외 {len(paths) - 3}개" if len(paths) > 3 else ""
    return f"변경 파일: {shown}{more}"


# ---------------------------------------------------------------------------
# 커밋 메시지
# ---------------------------------------------------------------------------
def build_commit_message(data: Dict[str, Any], changed_paths: Sequence[str]) -> "tuple[CommitMessage, List[Check]]":
    checks: List[Check] = []
    data = data if isinstance(data, dict) else {}

    type_ = _normalize_type(data.get("type"))
    if type_ is None:
        checks.append(Check(FIXED, f"알 수 없는 커밋 타입 '{data.get('type')}' → 'chore' 로 대체"))
        type_ = "chore"

    scope = re.sub(r"[^\w./-]", "", clean_line(data.get("scope")))[:20]
    subject = _clean_subject(data.get("subject"))
    if not subject:
        subject = "변경 사항 반영"
        checks.append(Check(WARN, "커밋 제목 내용이 비어 있어 기본 문구로 채움 - 직접 수정 필요"))

    title = _build_title(type_, scope, subject)
    if len(title) > config.COMMIT_TITLE_MAX and scope:
        title = _build_title(type_, "", subject)  # 먼저 scope 를 빼서 줄여 본다
        checks.append(Check(FIXED, "제목이 길어 scope 표기를 생략"))
    if len(title) > config.COMMIT_TITLE_MAX:
        original = len(title)
        title = truncate(title, config.COMMIT_TITLE_MAX)
        checks.append(Check(FIXED, f"커밋 제목 {original}자 → {len(title)}자로 자름 (최대 {config.COMMIT_TITLE_MAX}자)"))

    raw_bullets = data.get("body") or []
    bullets = clean_bullets(raw_bullets, config.COMMIT_BULLETS_MAX)
    if isinstance(raw_bullets, list) and len([b for b in raw_bullets if str(b).strip()]) > config.COMMIT_BULLETS_MAX:
        checks.append(Check(FIXED, f"본문 불릿을 {config.COMMIT_BULLETS_MAX}개로 줄임"))
    if not bullets and changed_paths:
        bullets = [_file_fallback_bullet(changed_paths)]
        checks.append(Check(FIXED, "본문 불릿이 없어 변경 파일 목록으로 채움"))
    # 요약 품질 최소 기준: 본문은 '변경 파일 1~3개 언급' 또는 '핵심 변경 1~2개 불릿' 중 하나를 만족해야 한다.
    if changed_paths and len(bullets) > 2 and not _body_mentions_files(bullets, changed_paths):
        bullets = bullets[:2]
        checks.append(Check(FIXED, "본문에 변경 파일 언급이 없어 핵심 불릿 2개로 줄임"))

    message = CommitMessage(title=title, bullets=bullets, summary=clean_line(data.get("summary")))
    checks.extend(validate_commit_text(message.text, changed_paths))
    return message, checks


def validate_commit_text(text: str, changed_paths: Sequence[str] = ()) -> List[Check]:
    """완성된 커밋 메시지 텍스트를 규칙에 비춰 검사한다(수정하지 않음)."""
    checks: List[Check] = []
    lines = text.splitlines()
    title = lines[0] if lines else ""
    n = len(title)
    if n <= config.COMMIT_TITLE_RECOMMENDED:
        checks.append(Check(OK, f"커밋 제목 {n}자 (권장 {config.COMMIT_TITLE_RECOMMENDED}자 이내)"))
    elif n <= config.COMMIT_TITLE_MAX:
        checks.append(Check(WARN, f"커밋 제목 {n}자 - 권장 {config.COMMIT_TITLE_RECOMMENDED}자 초과 (최대 {config.COMMIT_TITLE_MAX}자 이내라 허용)"))
    else:
        checks.append(Check(WARN, f"커밋 제목 {n}자 - 최대 {config.COMMIT_TITLE_MAX}자 초과"))

    if _TYPE_PREFIX_RE.match(title):
        checks.append(Check(OK, "제목 형식 'type(scope): subject' 준수"))
    else:
        checks.append(Check(WARN, "제목이 'type: subject' 형식이 아님"))

    bullets = [l for l in lines[1:] if l.startswith("- ")]
    if len(lines) > 1:
        if len(lines) < 3 or lines[1].strip():
            checks.append(Check(WARN, "제목과 본문 사이에 빈 줄이 필요함"))
        if bullets:
            checks.append(Check(OK, f"본문 핵심 변경 불릿 {len(bullets)}개"))
        body = "\n".join(lines[1:])
        mentioned = [p for p in changed_paths if _mentions_path(body, p)]
        if mentioned:
            checks.append(Check(OK, f"변경 파일 언급 {len(mentioned)}개 ({', '.join(_basename(p) for p in mentioned[:3])})"))
        if not mentioned and not (1 <= len(bullets) <= 2):
            checks.append(Check(WARN, "본문에 변경 파일 언급도, 핵심 변경 불릿 1~2개도 없음 - 직접 보완 필요"))
    return checks


# ---------------------------------------------------------------------------
# PR 초안
# ---------------------------------------------------------------------------
_PR_FALLBACK = {
    "why": "(작성 필요) 변경 배경을 직접 보완하세요",
    "what": None,  # 변경 파일 목록으로 대체
    "how_to_test": "(작성 필요) 테스트 방법을 직접 보완하세요",
}


def build_pr_draft(data: Dict[str, Any], changed_paths: Sequence[str]) -> "tuple[PRDraft, List[Check]]":
    checks: List[Check] = []
    data = data if isinstance(data, dict) else {}

    type_ = _normalize_type(data.get("type"))
    if type_ is None:
        checks.append(Check(FIXED, f"알 수 없는 PR 타입 '{data.get('type')}' → 'chore' 로 대체"))
        type_ = "chore"
    subject = _clean_subject(data.get("subject"))
    if not subject:
        subject = "변경 사항 반영"
        checks.append(Check(WARN, "PR 제목 내용이 비어 있어 기본 문구로 채움 - 직접 수정 필요"))
    title = _build_title(type_, "", subject)
    if len(title) > config.PR_TITLE_MAX:
        original = len(title)
        title = truncate(title, config.PR_TITLE_MAX)
        checks.append(Check(FIXED, f"PR 제목 {original}자 → {len(title)}자로 자름 (최대 {config.PR_TITLE_MAX}자)"))

    sections: Dict[str, List[str]] = {}
    for key, heading in PR_SECTIONS:
        items = clean_bullets(data.get(key), config.PR_SECTION_BULLETS_MAX)
        if not items:
            fallback = _PR_FALLBACK[key] or _file_fallback_bullet(changed_paths or ["(알 수 없음)"])
            items = [fallback]
            checks.append(Check(FIXED, f"'{heading}' 섹션에 불릿이 없어 기본 문구를 넣음 - 직접 보완 필요"))
        sections[key] = items

    draft = PRDraft(title=title, sections=sections, summary=clean_line(data.get("summary")))
    checks.extend(validate_pr(draft.title, draft.body))
    return draft, checks


_HEADER_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")


def parse_pr_sections(body: str) -> Dict[str, List[str]]:
    """PR 본문 마크다운을 '헤더 → 불릿 목록'으로 나눈다(검증용)."""
    sections: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for line in body.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            current = m.group(1).strip().lower()
            sections.setdefault(current, [])
            continue
        if current is not None and re.match(r"^\s*[-*]\s+\S", line):
            sections[current].append(line.strip())
    return sections


def validate_pr(title: str, body: str) -> List[Check]:
    """완성된 PR 제목/본문을 규칙에 비춰 검사한다(수정하지 않음)."""
    checks: List[Check] = []
    if "\n" in title:
        checks.append(Check(WARN, "PR 제목이 한 줄이 아님"))
    n = len(title)
    level = OK if n <= config.PR_TITLE_MAX else WARN
    checks.append(Check(level, f"PR 제목 {n}자 (최대 {config.PR_TITLE_MAX}자)"))

    found = parse_pr_sections(body)
    missing = []
    counts = []
    for _, heading in PR_SECTIONS:
        bullets = found.get(heading.lower())
        if bullets is None:
            missing.append(heading)
        elif not bullets:
            missing.append(f"{heading}(불릿 없음)")
        else:
            counts.append(f"{heading} {len(bullets)}")
    if missing:
        checks.append(Check(WARN, f"PR 본문 필수 섹션 누락: {', '.join(missing)}"))
    else:
        checks.append(Check(OK, f"PR 본문 Why/What/How to Test 섹션과 불릿 확인 ({', '.join(counts)})"))
    return checks


# ---------------------------------------------------------------------------
# 기타
# ---------------------------------------------------------------------------
def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _mentions_path(text: str, path: str) -> bool:
    base = _basename(path)
    return bool(base) and (path in text or base in text)


def _body_mentions_files(bullets: Sequence[str], changed_paths: Sequence[str]) -> bool:
    text = "\n".join(bullets)
    return any(_mentions_path(text, p) for p in changed_paths)


def has_masked_token(texts: Iterable[str]) -> bool:
    return any("[MASKED" in t for t in texts)
