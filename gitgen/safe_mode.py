"""safe-mode: 외부 AI API 로 보내기 전에 diff 를 안전하게 다듬는다.

과제 요구(보안/민감정보)의 두 방식을 모두 적용한다.

(A) 마스킹  : API Key, 토큰, 비밀번호, 이메일, 전화번호 등 민감정보 패턴을 [MASKED:종류] 로 치환
(B) 전송 제한: 최대 10개 파일, 최대 200줄까지만 전송 (config.SAFE_MAX_FILES / SAFE_MAX_LINES)

추가로 .env, *.pem 같은 '민감 파일'은 내용 자체를 보내지 않고 파일명과 통계만 남긴다.

safe-mode 를 끄면(--no-safe-mode) 마스킹/제외/파일·줄 제한을 하지 않는다.
단, 비용 폭주를 막기 위한 문자 수 하드 상한(config.HARD_MAX_CHARS)은 항상 적용한다.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Tuple

from . import config
from .git_collector import FileDiff

# ---------------------------------------------------------------------------
# (A) 마스킹 규칙
# ---------------------------------------------------------------------------
# 순서가 중요하다. 형태가 분명한 토큰(특정 서비스 키)을 먼저 치환하고,
# 'password = "..."' 같은 일반 대입 패턴은 마지막에 처리한다.

_PLACEHOLDER_PREFIXES = ("[MASKED", "$", "<", "{", "%", "your", "YOUR", "xxx", "XXX", "***")
_LITERAL_WORDS = {"none", "null", "nil", "true", "false", "undefined", "empty"}


def _keep_if_placeholder(value: str, label: str) -> str:
    """'YOUR_KEY', '${TOKEN}', '<api-key>', 숫자, None/true 같은 비밀이 아닌 값은 그대로 둔다."""
    if value.startswith(_PLACEHOLDER_PREFIXES) or value.isdigit() or value.lower() in _LITERAL_WORDS:
        return value
    return f"[MASKED:{label}]"


@dataclass(frozen=True)
class MaskRule:
    name: str
    pattern: "re.Pattern[str]"
    replace: Callable[["re.Match[str]"], str]


def _simple(label: str) -> Callable[["re.Match[str]"], str]:
    return lambda m: f"[MASKED:{label}]"


def _assignment(label: str) -> Callable[["re.Match[str]"], str]:
    # group(1)=키 이름과 대입 기호, group(2)=따옴표(없을 수 있음), group(3)=값
    def repl(m: "re.Match[str]") -> str:
        quote = m.group(2) or ""
        return f"{m.group(1)}{quote}{_keep_if_placeholder(m.group(3), label)}{quote}"

    return repl


_CONSTANT_REF_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")  # DEFAULT_MAX_TOKENS 같은 상수 이름
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][\w.]*$")  # api_key, self.api_key, config.TOKEN
_CODE_CHARS = set("(){}[],;")
# 'password: string', 'token: bytes' 같은 타입 선언의 타입 이름
_TYPE_NAMES = {
    "str", "string", "bytes", "int", "integer", "number", "bool", "boolean", "any", "object",
    "unknown", "secretstr", "secretbytes", "optional", "none", "char", "text", "varchar",
}


def _looks_like_code_reference(name: str, value: str) -> bool:
    """'self.api_key = api_key', 'max_tokens = DEFAULT_MAX_TOKENS' 처럼 값이 변수/상수 참조인지."""
    if _CONSTANT_REF_RE.match(value):
        return True
    if not _IDENTIFIER_RE.match(value):
        return False
    lhs = name.rsplit(".", 1)[-1].lower()
    rhs = value.rsplit(".", 1)[-1].lower()
    if not lhs or not rhs:
        return False
    # 이름의 경계(_ 또는 .)에서만 비교: max_tokens ↔ DEFAULT_MAX_TOKENS 는 참조, password ↔ mypassword 는 아님
    return rhs == lhs or rhs.endswith(("_" + lhs, "." + lhs)) or lhs.endswith("_" + rhs)


def _unquoted_assignment(label: str) -> Callable[["re.Match[str]"], str]:
    """따옴표 없는 설정값용.

    - .env / 셸 / .properties 스타일(`KEY=value`, '=' 앞뒤 공백 없음): 값 전체를 비밀로 보고 가린다.
      (Xk9;mP2!vQ, Sup3r#S3cret 처럼 특수문자가 섞인 비밀번호도 통째로 가린다)
    - 그 밖(`name = value`, YAML/타입 선언 `name: value`): 코드일 가능성이 있으므로
      함수 호출·괄호·타입 이름·URL·한글 문장·변수 참조로 보이면 그대로 둔다.
    """

    def repl(m: "re.Match[str]") -> str:
        head, value = m.group(1), m.group(3)
        sep = re.search(r"([\w.\-]+)([ \t]*)([:=])([ \t]*)$", head)
        if sep is None:
            return m.group(0)
        name, before, symbol, after = sep.groups()
        env_style = symbol == "=" and not before and not after
        if not env_style:
            stripped = value.rstrip("!?")
            if (
                not value.isascii()  # 한글 문장 (AI 결과의 'token.py: 리팩터링' 등)
                or "://" in value  # URL
                or (symbol == ":" and stripped.lower() in _TYPE_NAMES)  # 타입 선언
                or (symbol == ":" and (value.endswith((",", ";")) or "(" in value))  # JS/TS 객체·인터페이스
                or (symbol == "=" and _CODE_CHARS & set(value))  # 함수 호출, 인덱싱 등 코드 표현식
                or _looks_like_code_reference(name, value)
            ):
                return m.group(0)
        return f"{head}{_keep_if_placeholder(value, label)}"

    return repl


def _basic_auth(m: "re.Match[str]") -> str:
    """'Basic dXNlcjpwYXNz' 는 가리고, 'Basic Authentication' 같은 평범한 영어 단어는 둔다."""
    token = m.group(1)
    if token.isalpha() and (token.islower() or token.istitle() or token.isupper()):
        return m.group(0)
    return "[MASKED:CREDENTIAL]"


def _query_token(m: "re.Match[str]") -> str:
    return m.group(1) + _keep_if_placeholder(m.group(2), "TOKEN")


# 비밀 값을 담는 변수/키 이름에 흔히 들어가는 단어. (DB_PASS, smtp_pass 의 'pass' 포함, 'bypass' 는 제외)
_SECRET_WORDS = (
    r"(?:api[_-]?key|secret|token(?!iz)|passw(?:or)?d|(?<![a-z])pass(?![a-z])|pwd"
    r"|access[_-]?key|private[_-]?key|credential)"
)
# 반복 횟수에 상한({0,64})을 두어, 아주 긴 한 줄에서 정규식이 느려지지(백트래킹 폭증) 않게 한다.
_NAME_AROUND = r"[\w.\-]{0,64}"

MASK_RULES: List[MaskRule] = [
    MaskRule(
        "PRIVATE_KEY",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?"
            r"(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
            re.DOTALL,
        ),
        _simple("PRIVATE_KEY"),
    ),
    MaskRule("ANTHROPIC_KEY", re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"), _simple("API_KEY")),
    MaskRule("OPENAI_KEY", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"), _simple("API_KEY")),
    MaskRule("STRIPE_KEY", re.compile(r"\b[rs]k_(?:live|test)_[0-9A-Za-z]{16,}"), _simple("API_KEY")),
    MaskRule("AWS_ACCESS_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), _simple("AWS_KEY")),
    MaskRule("GITHUB_TOKEN", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})"), _simple("TOKEN")),
    MaskRule("GITLAB_TOKEN", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"), _simple("TOKEN")),
    MaskRule("HUGGINGFACE_TOKEN", re.compile(r"\bhf_[A-Za-z0-9]{30,}"), _simple("TOKEN")),
    MaskRule("GOOGLE_API_KEY", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"), _simple("API_KEY")),
    MaskRule("SLACK_TOKEN", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), _simple("TOKEN")),
    MaskRule("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), _simple("JWT")),
    MaskRule("BEARER", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]{16,}=*"), _simple("TOKEN")),
    MaskRule("BASIC_AUTH", re.compile(r"\b[Bb]asic[ \t]+([A-Za-z0-9+/]{12,}={0,2})"), _basic_auth),
    MaskRule(
        "URL_CREDENTIAL",
        re.compile(r"(?i)\b([a-z][a-z0-9+.\-]{0,30}://)([^\s:/@]{1,256}):([^\s@/]{1,256})@"),
        lambda m: f"{m.group(1)}[MASKED:CREDENTIAL]@",
    ),
    MaskRule(
        "QUERY_TOKEN",
        # ?access_token= · &client_secret= · &refresh_token= · ?apiKey= 등 (이름 앞뒤에 다른 글자가 붙어도 매치)
        re.compile(
            r"(?i)([?&][\w\-]{0,32}(?:token|secret|passw(?:or)?d|api[_-]?key|apikey)[\w\-]{0,32}=)"
            r"([^&\s\"'#`<>(){}\[\]]{6,})"
        ),
        _query_token,
    ),
    MaskRule(
        "EMAIL",
        re.compile(r"\b[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,253}\.[A-Za-z]{2,24}\b"),
        _simple("EMAIL"),
    ),
    MaskRule("KR_RRN", re.compile(r"\b\d{6}-[1-4]\d{6}\b"), _simple("RRN")),
    MaskRule("KR_PHONE", re.compile(r"\b01[016789][-. ]?\d{3,4}[-. ]?\d{4}\b"), _simple("PHONE")),
    MaskRule("CARD_NUMBER", re.compile(r"\b(?:\d{4}[- ]){3}\d{4}\b"), _simple("CARD")),
    # password = "abc123" / "apiKey": "..." 처럼 따옴표로 감싼 값
    MaskRule(
        "SECRET_ASSIGNMENT",
        re.compile(
            r"(?i)(\b" + _NAME_AROUND + _SECRET_WORDS + _NAME_AROUND
            + r"[\"']?\s*(?:=|:|=>)\s*)([\"'])([^\"'\s]{4,})\2"
        ),
        _assignment("SECRET"),
    ),
    # 따옴표 없는 설정값: .env / 셸 / YAML / .properties / INI / docker-compose
    #   DB_PASSWORD=abc123 · export API_KEY=abc · password: hunter2 · - POSTGRES_PASSWORD=x
    # 줄이 '이름 [:=] 값' 으로 끝나는 경우만 대상이다. ('#' 은 앞에 공백이 있을 때만 주석으로 본다)
    # 코드인지 설정인지는 _unquoted_assignment 가 판단한다.
    MaskRule(
        "ENV_SECRET",
        re.compile(
            r"(?im)^([+\- ]?[ \t]*(?:-[ \t]*)?(?:export[ \t]+)?" + _NAME_AROUND + _SECRET_WORDS + _NAME_AROUND
            + r"[ \t]*[:=][ \t]*)()([^\s\"']\S{3,})(?=(?:[ \t]+#.*)?[ \t]*$)"
        ),
        _unquoted_assignment("SECRET"),
    ),
]


def mask_text(text: str) -> Tuple[str, Dict[str, int]]:
    """민감정보 패턴을 치환하고 (치환된 텍스트, 규칙별 치환 건수)를 돌려준다."""
    counts: Dict[str, int] = {}
    for rule in MASK_RULES:
        hits = 0

        def _repl(m: "re.Match[str]", _rule: MaskRule = rule) -> str:
            nonlocal hits
            new = _rule.replace(m)
            if new != m.group(0):
                hits += 1
            return new

        text = rule.pattern.sub(_repl, text)
        if hits:
            counts[rule.name] = counts.get(rule.name, 0) + hits
    return text, counts


# ---------------------------------------------------------------------------
# 민감 파일 (내용 제외)
# ---------------------------------------------------------------------------
# 파일 이름(basename) 기준
SENSITIVE_FILE_PATTERNS = (
    ".env",
    ".env.*",
    "*.env",
    ".envrc",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.ppk",
    "*.jks",
    "*.keystore",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "*.kdbx",
    "credentials",
    "credentials*.json",
    "service-account*.json",
    "*secret*",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".pgpass",
    ".git-credentials",
    "kubeconfig",
    "*.tfstate",
    "*.tfstate.*",
    "*.tfvars",
)
# 전체 경로 기준 (홈 디렉토리 설정 폴더 등)
SENSITIVE_PATH_PATTERNS = (".aws/*", ".ssh/*", ".kube/config", ".docker/config.json")
# 예시/템플릿 파일은 실제 값이 없으므로 제외하지 않는다. (.env.example, app.env.sample 등)
SENSITIVE_ALLOWED_SUFFIXES = (".example", ".sample", ".template", ".dist")


def is_sensitive_file(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    name = normalized.rsplit("/", 1)[-1]
    if name.endswith(SENSITIVE_ALLOWED_SUFFIXES):
        return False
    if any(fnmatch.fnmatch(name, pattern) for pattern in SENSITIVE_FILE_PATTERNS):
        return True
    return any(
        fnmatch.fnmatch(normalized, p) or fnmatch.fnmatch(normalized, "*/" + p) for p in SENSITIVE_PATH_PATTERNS
    )


# ---------------------------------------------------------------------------
# 결과 구조
# ---------------------------------------------------------------------------
@dataclass
class PreparedFile:
    """프롬프트에 넣을 파일 정보."""

    source: FileDiff
    text: str = ""  # 전송할 diff 본문 (생략된 경우 빈 문자열)
    note: str = ""  # 생략/축소 사유
    sent_lines: int = 0

    @property
    def omitted(self) -> bool:
        return not self.text


@dataclass
class SafeModeReport:
    enabled: bool
    masked: Dict[str, int] = field(default_factory=dict)
    excluded_sensitive: List[str] = field(default_factory=list)
    skipped_over_file_limit: List[str] = field(default_factory=list)
    truncated: List[str] = field(default_factory=list)
    original_lines: int = 0
    sent_lines: int = 0
    hard_limited: bool = False

    @property
    def masked_total(self) -> int:
        return sum(self.masked.values())


# ---------------------------------------------------------------------------
# 전체 처리
# ---------------------------------------------------------------------------
def prepare_files(files: List[FileDiff], safe_mode: bool) -> Tuple[List[PreparedFile], SafeModeReport]:
    """FileDiff 목록을 전송용 PreparedFile 목록으로 바꾼다."""
    report = SafeModeReport(enabled=safe_mode)
    report.original_lines = sum(len(f.lines) for f in files)

    prepared: List[PreparedFile] = []
    candidates: List[Tuple[PreparedFile, List[str]]] = []  # 본문을 보낼 후보

    for f in files:
        pf = PreparedFile(source=f)
        prepared.append(pf)
        if f.is_binary:
            pf.note = "바이너리 파일: 내용 생략"
            continue
        if f.is_lockfile:
            pf.note = "의존성 lock 파일: 내용 생략"
            continue
        # 이름이 바뀐 경우(.env → env.backup)에도 걸러지도록 원래 경로까지 확인한다.
        if safe_mode and (is_sensitive_file(f.path) or (f.old_path and is_sensitive_file(f.old_path))):
            pf.note = "민감 파일로 판단되어 내용 제외(safe-mode)"
            report.excluded_sensitive.append(f.path)
            continue

        body = "\n".join(f.lines)
        if safe_mode:
            body, counts = mask_text(body)
            for name, n in counts.items():
                report.masked[name] = report.masked.get(name, 0) + n
        candidates.append((pf, body.split("\n")))

    if safe_mode:
        # (B-1) 파일 수 제한
        over = candidates[config.SAFE_MAX_FILES:]
        candidates = candidates[: config.SAFE_MAX_FILES]
        for pf, _ in over:
            pf.note = f"파일 수 제한({config.SAFE_MAX_FILES}개) 초과: 내용 생략(safe-mode)"
            report.skipped_over_file_limit.append(pf.source.path)
        # (B-2) 줄 수 제한: 파일마다 공평하게 줄 예산을 나눈다.
        budgets = allocate_line_budget([len(lines) for _, lines in candidates], config.SAFE_MAX_LINES)
    else:
        budgets = [len(lines) for _, lines in candidates]

    for (pf, lines), budget in zip(candidates, budgets):
        _fill(pf, lines, budget, report)

    # 하드 상한(문자 수): safe-mode 여부와 무관하게 비용 보호용으로 적용
    _apply_hard_char_limit(prepared, report)

    report.sent_lines = sum(pf.sent_lines for pf in prepared)
    return prepared, report


def allocate_line_budget(sizes: List[int], total: int) -> List[int]:
    """줄 예산을 파일별로 '공평하게' 나눈다 (water-filling).

    작은 파일은 전부 보내고, 남은 예산을 큰 파일들이 똑같이 나눠 갖는다.
    예) sizes=[10, 500, 500], total=200 → [10, 95, 95]
    """
    budgets = [0] * len(sizes)
    remaining = total
    pending = sorted(range(len(sizes)), key=lambda i: sizes[i])
    while remaining > 0:
        # 이미 다 채운(또는 크기가 0인) 파일은 빼고 계산한다.
        pending = [i for i in pending if sizes[i] > budgets[i]]
        if not pending:
            break
        share = remaining // len(pending)
        if share == 0:
            # 남은 예산이 파일 수보다 적으면 앞(작은 파일)부터 1줄씩
            for i in pending[:remaining]:
                budgets[i] += 1
            break
        idx = pending[0]
        need = sizes[idx] - budgets[idx]
        if need <= share:
            budgets[idx] += need
            remaining -= need
        else:
            for i in pending:
                budgets[i] += share
            remaining -= share * len(pending)
    return budgets


def _fill(pf: PreparedFile, lines: List[str], budget: int, report: SafeModeReport) -> None:
    if budget <= 0:
        pf.note = pf.note or f"줄 수 제한({config.SAFE_MAX_LINES}줄)으로 내용 생략(safe-mode)"
        report.truncated.append(pf.source.path)
        return
    if budget >= len(lines):
        pf.text = "\n".join(lines)
        pf.sent_lines = len(lines)
        return
    kept = lines[:budget]
    omitted = len(lines) - budget
    kept.append(f"... (이하 {omitted}줄 생략)")
    pf.text = "\n".join(kept)
    pf.sent_lines = budget
    pf.note = f"diff {len(lines)}줄 중 {budget}줄만 전송"
    report.truncated.append(pf.source.path)


def _apply_hard_char_limit(prepared: List[PreparedFile], report: SafeModeReport) -> None:
    used = 0
    limit = config.HARD_MAX_CHARS
    for pf in prepared:
        if not pf.text:
            continue
        if used + len(pf.text) <= limit:
            used += len(pf.text)
            continue
        room = max(0, limit - used)
        report.hard_limited = True
        if room < 200:
            pf.text = ""
            pf.sent_lines = 0
            pf.note = "전송 하드 상한 초과로 내용 생략"
        else:
            cut = pf.text[:room]
            cut = cut[: cut.rfind("\n")] if "\n" in cut else cut
            pf.sent_lines = cut.count("\n") + 1
            pf.text = cut + "\n... (전송 하드 상한으로 이하 생략)"
            pf.note = "전송 하드 상한으로 일부만 전송"
        used = limit


def mask_output(text: str) -> Tuple[str, int]:
    """AI 가 생성한 결과에 혹시라도 민감정보가 섞여 나오면 한 번 더 가린다."""
    masked, counts = mask_text(text)
    return masked, sum(counts.values())


def mask_data(data: Any) -> Tuple[Any, int]:
    """AI 응답 JSON 의 문자열 값(제목, 불릿 하나하나)을 각각 마스킹한다.

    완성된 텍스트가 아니라 '필드 단위'로 가리기 때문에, 이후의 길이 검증·자르기가
    마스킹된 최종 문자열을 기준으로 동작하고, 한 불릿의 치환이 다른 섹션을 망가뜨리지 않는다.
    """
    if isinstance(data, str):
        return mask_output(data)
    if isinstance(data, list):
        total = 0
        items = []
        for item in data:
            masked, n = mask_data(item)
            items.append(masked)
            total += n
        return items, total
    if isinstance(data, dict):
        total = 0
        result = {}
        for key, value in data.items():
            result[key], n = mask_data(value)
            total += n
        return result, total
    return data, 0
