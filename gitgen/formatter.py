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


def clean_bullets(items: Any, limit: int, strip_period: bool = True) -> List[str]:
    """불릿 목록 정리: 문자열화, 앞의 '-'/'1.' 제거, 빈 항목·중복 제거, 개수 제한."""
    if isinstance(items, str):
        items = [line for line in items.splitlines()]
    result: List[str] = []
    seen = set()
    for item in items or []:
        s = clean_line(_BULLET_PREFIX_RE.sub("", str(item or "")))
        if strip_period:
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


# ---------------------------------------------------------------------------
# 팀 컨벤션 적용 (보너스 2) — 기본 컨벤션이 아닐 때만 사용
# ---------------------------------------------------------------------------
@dataclass
class ConvCommitMessage:
    title: str
    body: List[str]
    style: str  # bullets / prose / none
    summary: str = ""

    @property
    def text(self) -> str:
        if not self.body:
            return self.title
        lines = [f"- {b}" for b in self.body] if self.style == "bullets" else list(self.body)
        return self.title + "\n\n" + "\n".join(lines)


@dataclass
class ConvPRDraft:
    title: str
    sections: Dict[str, List[str]]
    convention: Any
    issue: Optional[int] = None
    summary: str = ""

    @property
    def body(self) -> str:
        pr = self.convention.pr
        blocks = []
        if pr.issue_heading:
            number = str(self.issue) if self.issue else ""
            blocks.append(f"## {pr.issue_heading}\n\n{pr.issue_keyword} #{number}")
        for s in pr.sections:
            items = self.sections.get(s.key) or []
            blocks.append(f"## {s.heading}\n\n" + "\n".join(f"- {item}" for item in items))
        if pr.checklist:
            blocks.append(f"## {pr.checklist_heading}\n\n" + "\n".join(f"- [ ] {item}" for item in pr.checklist))
        return "\n\n".join(blocks)


_CONVENTIONAL_PREFIX = r"(?:\([^)]*\))?!?\s*:\s*"


def _conv_prefix_res(conv: Any) -> List["re.Pattern[str]"]:
    """subject 앞에 중복으로 붙은 머리말을 떼어 낼 정규식들.

    1) 팀 형식의 머리말 (예: '[feat] ', 'feat: ')  2) 흔한 'type(scope): ' 머리말
    """
    types = "|".join(re.escape(t) for t in conv.commit.types)
    patterns = [re.compile(r"^\s*(?:" + types + r")" + _CONVENTIONAL_PREFIX, re.IGNORECASE)]
    team = conv.commit.prefix_regex()
    if team is not None:
        patterns.insert(0, team)
    return patterns


def _conv_subject(value: Any, conv: Any) -> str:
    subject = clean_line(value)
    patterns = _conv_prefix_res(conv)
    while subject:
        # 가장 길게 일치하는 머리말을 뗀다 (짧은 쪽을 먼저 떼면 'feat(auth): x' 가 '(auth): x' 로 남음)
        lengths = [m.end() for m in (p.match(subject) for p in patterns) if m and m.end() > 0]
        if not lengths:
            break
        subject = subject[max(lengths):]
        subject = re.sub(r"^[\s:!]+", "", subject)  # 머리말 뒤에 남은 ':' '!' 정리
    return strip_trailing_punct(subject)


def _conv_scope(data: Dict[str, Any], conv: Any) -> str:
    if conv.commit.scope == "none":
        return ""
    return re.sub(r"[^\w./-]", "", clean_line(data.get("scope")))[:20]


def _conv_type(data: Dict[str, Any], conv: Any, checks: List[Check], what: str) -> str:
    t = clean_line(data.get("type")).lower()
    if t in conv.commit.types:
        return t
    fallback = "chore" if "chore" in conv.commit.types else next(iter(conv.commit.types))
    checks.append(Check(FIXED, f"팀 컨벤션에 없는 {what} 타입 '{data.get('type')}' → '{fallback}' 로 대체"))
    return fallback


def _conv_title(data: Dict[str, Any], conv: Any, checks: List[Check], what: str, limit: int) -> str:
    """type / scope / subject 를 팀 형식으로 조립한다 (커밋·PR 공통)."""
    c = conv.commit
    type_ = _conv_type(data, conv, checks, what)
    scope = _conv_scope(data, conv)
    if c.scope == "required" and not scope:
        checks.append(Check(WARN, f"팀 컨벤션은 scope 가 필수인데 {what} 제목의 scope 가 비어 있음 - 직접 보완 필요"))
    subject = _conv_subject(data.get("subject"), conv)
    if not subject:
        subject = "변경 사항 반영"
        checks.append(Check(WARN, f"{what} 제목 내용이 비어 있어 기본 문구로 채움 - 직접 수정 필요"))
    title = c.render_title(type_, scope, subject)
    if len(title) > limit:
        # 제목 전체를 끝에서 자르면 subject 뒤의 type/scope 가 잘려 팀 형식이 깨질 수 있으므로 subject 만 줄인다
        original = len(title)
        if scope and len(c.render_title(type_, scope, "")) > limit - 10:
            scope = ""
        subject = truncate(subject, max(10, limit - len(c.render_title(type_, scope, ""))))
        title = c.render_title(type_, scope, subject)
        if len(title) > limit:  # 형식 자체가 너무 긴 경우
            title = truncate(title, limit)
        checks.append(Check(FIXED, f"{what} 제목 {original}자 → {len(title)}자로 자름 (최대 {limit}자)"))
    return title


def _title_subject(title: str, conv: Any) -> str:
    """완성된 제목에서 subject 부분만 꺼낸다 (형식과 다르면 머리말을 떼어 낸 나머지)."""
    m = conv.commit.title_regex().match(title)
    return m.group("subject") if m else _conv_subject(title, conv)


def _forbidden_hit(subject: str, conv: Any) -> Optional[str]:
    lowered = strip_trailing_punct(subject.strip()).lower()
    for word in conv.commit.forbidden:
        if lowered == word.lower():
            return word
    return None


def _limited(items: Any, limit: int, strip_period: bool) -> "tuple[List[str], int]":
    """clean_bullets 와 같지만, 잘라 내기 전 개수도 돌려준다 (잘랐으면 [보정] 으로 알리기 위해)."""
    full = clean_bullets(items, 10 ** 6, strip_period=strip_period)
    return full[:limit], len(full)


def build_commit_message_conv(
    data: Dict[str, Any], changed_paths: Sequence[str], conv: Any
) -> "tuple[ConvCommitMessage, List[Check]]":
    checks: List[Check] = []
    data = data if isinstance(data, dict) else {}
    c = conv.commit
    title = _conv_title(data, conv, checks, "커밋", config.COMMIT_TITLE_MAX)

    body: List[str] = []
    if c.body != "none":
        body, total = _limited(data.get("body") or [], c.body_max, strip_period=(c.body == "bullets"))
        if total > c.body_max:
            kind = "문장" if c.body == "prose" else "불릿"
            checks.append(Check(FIXED, f"본문 {kind} {total}개 → 팀 기준 {c.body_max}개로 줄임"))
    message = ConvCommitMessage(title=title, body=body, style=c.body, summary=clean_line(data.get("summary")))
    checks.extend(validate_commit_conv(message.text, changed_paths, conv))
    return message, checks


def validate_commit_conv(text: str, changed_paths: Sequence[str], conv: Any) -> List[Check]:
    c = conv.commit
    checks: List[Check] = []
    lines = text.splitlines()
    title = lines[0] if lines else ""
    if c.title_regex().match(title):
        checks.append(Check(OK, f"팀 커밋 형식 '{c.format}' 준수"))
    else:
        checks.append(Check(WARN, f"팀 커밋 형식 '{c.format}' 과 다름"))
    n = len(title)
    if n <= c.subject_max:
        checks.append(Check(OK, f"커밋 제목 {n}자 (팀 기준 {c.subject_max}자 이내)"))
    else:
        checks.append(Check(WARN, f"커밋 제목 {n}자 - 팀 기준 {c.subject_max}자 초과"))
    hit = _forbidden_hit(_title_subject(title, conv), conv)
    if hit:
        checks.append(Check(WARN, f"팀 금지 제목('{hit}') - 무엇을 바꿨는지 드러나게 수정 필요"))
    body = [line for line in lines[2:] if line.strip()]
    if body:
        mentioned = [p for p in changed_paths if _mentions_path("\n".join(body), p)]
        kind = "문장" if c.body == "prose" else "불릿"
        checks.append(Check(OK, f"본문 {kind} {len(body)}개"))
        if mentioned:
            checks.append(Check(OK, f"변경 파일 언급 {len(mentioned)}개 ({', '.join(_basename(p) for p in mentioned[:3])})"))
        elif len(body) > 2:
            checks.append(Check(WARN, "본문에 변경 파일 언급이 없음 - 직접 보완 필요"))
    return checks


def build_pr_draft_conv(
    data: Dict[str, Any], changed_paths: Sequence[str], conv: Any, issue: Optional[int] = None
) -> "tuple[ConvPRDraft, List[Check]]":
    checks: List[Check] = []
    data = data if isinstance(data, dict) else {}
    p = conv.pr
    title = _conv_title(data, conv, checks, "PR", config.PR_TITLE_MAX)

    sections: Dict[str, List[str]] = {}
    for s in p.sections:
        items, total = _limited(data.get(s.key), s.max, strip_period=False)
        if total > s.max:
            checks.append(Check(FIXED, f"'{s.heading}' 섹션 불릿 {total}개 → 팀 기준 {s.max}개로 줄임"))
        if len(items) < s.min:
            if s.key == "what" and changed_paths:
                items.append(_file_fallback_bullet(changed_paths))
            else:
                items.append(f"(작성 필요) {s.heading} 내용을 직접 보완하세요")
            checks.append(Check(FIXED, f"'{s.heading}' 섹션 불릿이 {s.min}개보다 적어 기본 문구를 넣음 - 직접 보완 필요"))
        sections[s.key] = items

    draft = ConvPRDraft(title=title, sections=sections, convention=conv, issue=issue,
                        summary=clean_line(data.get("summary")))
    checks.extend(validate_pr_conv(draft.title, draft.body, conv))
    return draft, checks


def validate_pr_conv(title: str, body: str, conv: Any) -> List[Check]:
    p = conv.pr
    checks: List[Check] = []
    if conv.commit.title_regex().match(title):
        checks.append(Check(OK, f"PR 제목이 팀 형식 '{conv.commit.format}' 준수"))
    else:
        checks.append(Check(WARN, f"PR 제목이 팀 형식 '{conv.commit.format}' 과 다름"))
    n = len(title)
    level = OK if n <= p.title_max else WARN
    checks.append(Check(level, f"PR 제목 {n}자 (팀 기준 {p.title_max}자 이내)"))
    hit = _forbidden_hit(_title_subject(title, conv), conv)
    if hit:
        checks.append(Check(WARN, f"팀 금지 제목('{hit}') - PR 제목에 무엇을 바꿨는지 드러나게 수정 필요"))

    headings = [m.group(1).strip() for m in re.finditer(r"^#{1,6}\s+(.+?)\s*$", body, re.M)]
    expected = ([p.issue_heading] if p.issue_heading else []) + [s.heading for s in p.sections]
    if p.checklist:
        expected.append(p.checklist_heading)
    if headings == expected:
        checks.append(Check(OK, f"PR 템플릿 섹션 {len(expected)}개가 팀 순서대로 있음"))
    else:
        missing = [h for h in expected if h not in headings]
        checks.append(Check(WARN, f"PR 템플릿 섹션 순서/누락 문제: {', '.join(missing) or '순서 다름'}"))
    found = parse_pr_sections(body)
    short = [s.heading for s in p.sections if len(found.get(s.heading.lower(), [])) < s.min]
    if short:
        checks.append(Check(WARN, f"불릿이 모자란 섹션: {', '.join(short)}"))
    else:
        checks.append(Check(OK, "섹션마다 팀 기준 이상의 불릿"))
    if p.issue_heading:
        if re.search(re.escape(p.issue_keyword) + r" #\d+", body):
            checks.append(Check(OK, f"관련 이슈 연결({p.issue_keyword} #번호)"))
        else:
            checks.append(Check(WARN, f"관련 이슈 번호가 비어 있음 - --issue N 으로 지정하거나 '{p.issue_keyword} #' 뒤에 직접 채우세요"))
    if p.checklist:
        # 다른 섹션(How 등)에 쓴 '[ ]' 항목은 세지 않고 체크리스트 섹션만 센다
        items = found.get(p.checklist_heading.lower(), [])
        boxes = sum(1 for item in items if re.match(r"^- \[[ xX]\] ", item))
        level = OK if boxes == len(p.checklist) else WARN
        checks.append(Check(level, f"체크리스트 {boxes}/{len(p.checklist)}개 (제출 전 직접 체크)"))
    return checks
