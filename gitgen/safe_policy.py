"""safe-mode 정책: 마스킹 규칙 확장(정규식)과 전송 제한(파일/줄) 조정.

기본 정책(코드 내장)
  - 최대 10개 파일, 전체 최대 200줄, 파일당 줄 제한 없음(전체 예산을 공평 분배)
  - 내장 마스킹 규칙(safe_mode.MASK_RULES) 전부 사용, 내장 민감 파일 패턴 전부 사용

정책이 정해지는 순서
  1) 기본값 (config.py)
  2) 설정 파일  : 대상 프로젝트 루트의 .ai-gitgen.json  (또는 --safe-mode-config 로 지정)
  3) CLI 옵션   : --max-files / --max-lines / --max-lines-per-file / --mask-pattern / --exclude-file
  숫자 항목은 뒤의 값이 앞의 값을 '덮어쓰고', 목록 항목(mask_rules, exclude_files)은 뒤의 값이 '추가'된다.

설정 파일 예시 (.ai-gitgen.json)
  {
    "safe_mode": {
      "max_files": 5,
      "max_lines": 120,
      "max_lines_per_file": 40,
      "exclude_files": ["*.sql", "fixtures/customers/"],
      "disable_rules": ["KR_PHONE"],
      "mask_rules": [
        {"name": "INTERNAL_HOST", "pattern": "\\\\b[\\\\w-]+\\\\.corp\\\\.example\\\\.com\\\\b"},
        {"name": "EMPLOYEE_ID", "pattern": "\\\\bEMP-\\\\d{6}\\\\b", "label": "EMPLOYEE"},
        {"name": "DB_HOST", "pattern": "DB_HOST=(?P<value>\\\\S+)"}
      ]
    }
  }

사용자 마스킹 규칙
  - pattern 은 파이썬 정규식이다. 대소문자 무시는 (?i) 를 앞에 붙인다.
  - (?P<value>...) 이름 그룹이 있으면 그 부분만 가리고 나머지(키 이름 등)는 남긴다.
  - 가린 자리에는 [MASKED:<label>] 이 들어간다. label 을 생략하면 name 을 쓴다.
  - 규칙은 diff 본문(각 줄이 '+', '-', ' ' 로 시작하는 여러 줄 텍스트)에 적용된다.
    그래서 ^ 와 $ 는 (?m) 없이 쓰면 기대대로 동작하지 않으므로 허용하지 않는다.
  - 사용자 규칙은 내장 규칙 '다음에' 적용된다. 사용자 규칙이 먼저 일부를 가리면 내장 규칙이
    원래 잡던 비밀값(예: ACME_DB_PASSWORD=... 의 값)을 놓칠 수 있기 때문이다.
    즉 사용자 규칙은 기존 보호를 '줄이지 않고 더하기만' 한다.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from . import config
from .safe_mode import MASK_RULES, MaskRule

POLICY_FILE_NAME = ".ai-gitgen.json"
SECTION = "safe_mode"
ALLOWED_KEYS = ("max_files", "max_lines", "max_lines_per_file", "exclude_files", "disable_rules", "mask_rules")
RULE_KEYS = ("name", "pattern", "label")

# 숫자 정책의 허용 범위 (너무 큰 값은 비용 폭주, 0 이하는 의미 없음)
LIMITS = {
    "max_files": (1, 1000),
    "max_lines": (1, 20000),
    "max_lines_per_file": (0, 20000),  # 0 = 파일당 제한 없음
}
MAX_PATTERN_LENGTH = 500
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")
_INLINE_FLAGS_RE = re.compile(r"^\(\?([aiLmsux]+)\)")
# 길이 0 일치(\b, (?=..) 등)를 찾기 위한 시험 문자열 (diff 줄 모양 포함)
_ZERO_WIDTH_PROBE = "ab 12 _-x.y@z.io\n+cd EF = 'g'\n- h"

SOURCE_DEFAULT = "기본값"
SOURCE_FILE = "설정 파일"
SOURCE_CLI = "CLI 옵션"


class PolicyError(Exception):
    """정책 설정이 잘못되었을 때. 메시지에 어느 값이 왜 잘못됐는지 담는다."""


@dataclass
class CustomRule:
    name: str
    pattern: str  # 사용자가 쓴 정규식 원문
    label: str
    regex: "re.Pattern[str]"
    source: str = SOURCE_FILE

    def to_mask_rule(self) -> MaskRule:
        return MaskRule(self.name, self.regex, _custom_replacer(self.label, "value" in self.regex.groupindex))


@dataclass
class SafeModePolicy:
    max_files: int = config.SAFE_MAX_FILES
    max_lines: int = config.SAFE_MAX_LINES
    max_lines_per_file: int = config.SAFE_MAX_LINES_PER_FILE
    exclude_files: List[str] = field(default_factory=list)
    exclude_sources: List[str] = field(default_factory=list)  # exclude_files 와 같은 순서의 출처
    disabled_rules: List[str] = field(default_factory=list)
    custom_rules: List[CustomRule] = field(default_factory=list)
    config_path: Optional[str] = None  # 실제로 읽은 설정 파일
    sources: Dict[str, str] = field(default_factory=dict)  # 항목별 출처
    _mask_rules: Optional[List[MaskRule]] = field(default=None, repr=False, compare=False)

    @property
    def mask_rules(self) -> List[MaskRule]:
        """실제 적용 순서: 내장 규칙(비활성 제외) → 사용자 규칙."""
        if self._mask_rules is None:
            builtin = [r for r in MASK_RULES if r.name not in self.disabled_rules]
            custom = [r.to_mask_rule() for r in self.custom_rules]
            self._mask_rules = builtin + custom
        return self._mask_rules

    @property
    def builtin_rule_names(self) -> List[str]:
        return [r.name for r in MASK_RULES if r.name not in self.disabled_rules]

    def is_excluded(self, path: str) -> bool:
        """사용자 정책(exclude_files)에 해당하는 파일인지."""
        return any(match_exclude(path, pattern) for pattern in self.exclude_files)

    def unmatched_excludes(self, paths: Iterable[str]) -> List[str]:
        """이번 변경 파일 중 어느 것과도 일치하지 않은 제외 패턴 (오타 확인용)."""
        paths = list(paths)
        return [p for p in self.exclude_files if not any(match_exclude(path, p) for path in paths)]

    def is_policy_file(self, path: str, cwd: Optional[str] = None) -> bool:
        """정책 파일 자체인지. 정책 파일의 정규식 원문에는 가리려는 값(사내 도메인 등)이 들어 있다."""
        normalized = path.replace("\\", "/")
        if normalized.rsplit("/", 1)[-1] == POLICY_FILE_NAME:
            return True
        if self.config_path:
            full = os.path.abspath(os.path.join(cwd or os.getcwd(), path))
            return os.path.normcase(full) == os.path.normcase(self.config_path)
        return False

    def source_of(self, key: str) -> str:
        return self.sources.get(key, SOURCE_DEFAULT)

    def is_default(self) -> bool:
        return not self.sources

    def summary(self) -> str:
        """한 줄 요약: 로그와 증빙에 쓴다."""
        per_file = f"파일당 {self.max_lines_per_file}줄" if self.max_lines_per_file else "파일당 제한 없음"
        parts = [
            f"최대 {self.max_files}개 파일",
            f"전체 {self.max_lines}줄",
            per_file,
            f"사용자 규칙 {len(self.custom_rules)}개",
            f"추가 제외 {len(self.exclude_files)}개",
        ]
        if self.disabled_rules:
            parts.append(f"비활성 내장 규칙 {len(self.disabled_rules)}개")
        origin = os.path.basename(self.config_path) if self.config_path else "기본 정책"
        if SOURCE_CLI in self.sources.values():
            origin += " + CLI 옵션"
        return ", ".join(parts) + f" ({origin})"


def default_policy() -> SafeModePolicy:
    """config.py 의 기본값으로 정책을 만든다(호출할 때마다 config 값을 다시 읽는다)."""
    return SafeModePolicy(
        max_files=config.SAFE_MAX_FILES,
        max_lines=config.SAFE_MAX_LINES,
        max_lines_per_file=config.SAFE_MAX_LINES_PER_FILE,
    )


# ---------------------------------------------------------------------------
# 제외 패턴 (gitignore 에 가까운 규칙)
# ---------------------------------------------------------------------------
def match_exclude(path: str, pattern: str) -> bool:
    """exclude_files 패턴 하나가 저장소 기준 경로와 일치하는지.

    - '*.sql'            : 슬래시 없는 패턴은 파일 이름이나 경로의 어느 폴더 이름과도 비교
    - 'db/*', 'a/b.txt'  : 슬래시 있는 패턴은 저장소 루트 기준 전체 경로와 비교 (그 폴더 아래 전부 포함)
    - 'secrets/'         : 끝이 '/' 면 폴더 — 그 폴더 아래 모든 파일
    - '**/*.sql'         : 앞의 '**/' 는 '어느 폴더든(루트 포함)'
    - './db/*', '/db/*'  : 앞의 './', '/' 는 무시(루트 기준)
    대소문자는 구분하지 않는다.
    """
    p = pattern.replace("\\", "/").strip().lower()
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    dir_only = p.endswith("/")
    p = p.rstrip("/")
    if not p:
        return False
    target = path.replace("\\", "/").lower().strip("/")
    segments = target.split("/")
    dirs = segments[:-1]
    candidates = [p]
    while candidates[-1].startswith("**/"):
        candidates.append(candidates[-1][3:])

    for c in candidates:
        if "/" not in c:
            names = dirs if dir_only else segments
            if any(fnmatch.fnmatchcase(name, c) for name in names):
                return True
            continue
        # 슬래시가 있으면 루트 기준: 경로 전체 또는 상위 폴더 경로와 비교
        prefixes = ["/".join(segments[:i]) for i in range(1, len(segments) + (0 if dir_only else 1))]
        if any(fnmatch.fnmatchcase(prefix, c) for prefix in prefixes):
            return True
    return False


# ---------------------------------------------------------------------------
# 불러오기
# ---------------------------------------------------------------------------
def load_policy(
    *,
    cwd: Optional[str] = None,
    config_path: Optional[str] = None,
    max_files: Optional[int] = None,
    max_lines: Optional[int] = None,
    max_lines_per_file: Optional[int] = None,
    mask_patterns: Optional[Sequence[str]] = None,
    exclude_files: Optional[Sequence[str]] = None,
) -> SafeModePolicy:
    """기본값 ← 설정 파일 ← CLI 옵션 순서로 정책을 만든다. 잘못된 값은 PolicyError."""
    policy = default_policy()

    path = _resolve_config_path(cwd, config_path)
    if path:
        _apply_file(policy, path)

    cli_numbers = {"max_files": max_files, "max_lines": max_lines, "max_lines_per_file": max_lines_per_file}
    for key, value in cli_numbers.items():
        if value is not None:
            setattr(policy, key, _check_number(key, value, f"--{key.replace('_', '-')}"))
            policy.sources[key] = SOURCE_CLI
    for spec in mask_patterns or ():
        name, pattern = _split_pattern_option(spec)
        policy.custom_rules.append(_make_rule(name, pattern, None, f"--mask-pattern '{spec}'", SOURCE_CLI))
        policy.sources["mask_rules"] = SOURCE_CLI
    for glob in exclude_files or ():
        if not glob.strip():
            raise PolicyError("--exclude-file 값이 비어 있습니다.")
        policy.exclude_files.append(glob.strip())
        policy.exclude_sources.append(SOURCE_CLI)
        policy.sources["exclude_files"] = SOURCE_CLI

    _check_unique_names(policy.custom_rules)
    return policy


def _resolve_config_path(cwd: Optional[str], config_path: Optional[str]) -> Optional[str]:
    if config_path:
        if not os.path.isfile(config_path):
            raise PolicyError(f"safe-mode 설정 파일을 찾을 수 없습니다: {config_path}")
        return os.path.abspath(config_path)
    candidate = os.path.join(cwd or os.getcwd(), POLICY_FILE_NAME)
    return os.path.abspath(candidate) if os.path.isfile(candidate) else None


def _apply_file(policy: SafeModePolicy, path: str) -> None:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except UnicodeDecodeError:
        raise PolicyError(f"{path}: UTF-8 로 저장된 JSON 파일이어야 합니다.")
    except json.JSONDecodeError as exc:
        raise PolicyError(f"{path}: JSON 형식 오류 ({exc.lineno}행 {exc.colno}열: {exc.msg})")
    except OSError as exc:
        raise PolicyError(f"{path}: 파일을 열 수 없습니다 ({type(exc).__name__})")
    if not isinstance(data, dict):
        raise PolicyError(f"{path}: 최상위는 JSON 객체({{...}})여야 합니다.")
    policy.config_path = path
    section = data.get(SECTION)
    if section is None:
        # 다른 설정만 있는 파일(예: 보너스 2 컨벤션)은 괜찮지만, safe_mode 항목을 섹션 밖에 썼거나
        # 섹션 이름을 틀린 경우는 '조용히 무시'되면 위험하므로 오류로 알린다.
        misplaced = [k for k in data if k in ALLOWED_KEYS]
        near_miss = [k for k in data if k != SECTION and re.sub(r"[-_\s]", "", k.lower()) == "safemode"]
        if misplaced or near_miss:
            found = ", ".join(misplaced + near_miss)
            raise PolicyError(f"{path}: safe-mode 설정은 \"{SECTION}\": {{...}} 안에 적어야 합니다 (발견: {found})")
        return
    if not isinstance(section, dict):
        raise PolicyError(f"{path}: \"{SECTION}\" 은 JSON 객체여야 합니다.")

    unknown = [k for k in section if k not in ALLOWED_KEYS]
    if unknown:
        raise PolicyError(
            f"{path}: 알 수 없는 항목 {', '.join(unknown)} (사용 가능: {', '.join(ALLOWED_KEYS)})"
        )
    where = f"{os.path.basename(path)} 의 {SECTION}"

    for key in LIMITS:
        if key in section:
            setattr(policy, key, _check_number(key, section[key], f"{where}.{key}"))
            policy.sources[key] = SOURCE_FILE

    if "exclude_files" in section:
        globs = _string_list(section["exclude_files"], f"{where}.exclude_files")
        policy.exclude_files.extend(globs)
        policy.exclude_sources.extend([SOURCE_FILE] * len(globs))
        policy.sources["exclude_files"] = SOURCE_FILE

    if "disable_rules" in section:
        names = _string_list(section["disable_rules"], f"{where}.disable_rules")
        known = {r.name for r in MASK_RULES}
        bad = [n for n in names if n not in known]
        if bad:
            raise PolicyError(
                f"{where}.disable_rules: 알 수 없는 내장 규칙 {', '.join(bad)} "
                f"(`python main.py policy` 로 규칙 이름 확인)"
            )
        policy.disabled_rules = names
        policy.sources["disable_rules"] = SOURCE_FILE

    if "mask_rules" in section:
        rules = section["mask_rules"]
        if not isinstance(rules, list):
            raise PolicyError(f"{where}.mask_rules 는 배열이어야 합니다.")
        for i, item in enumerate(rules):
            label = f"{where}.mask_rules[{i}]"
            if not isinstance(item, dict):
                raise PolicyError(f"{label}: {{\"name\": ..., \"pattern\": ...}} 형태의 객체여야 합니다.")
            extra = [k for k in item if k not in RULE_KEYS]
            if extra:
                raise PolicyError(f"{label}: 알 수 없는 항목 {', '.join(extra)} (사용 가능: {', '.join(RULE_KEYS)})")
            if not isinstance(item.get("name"), str) or not isinstance(item.get("pattern"), str):
                raise PolicyError(f"{label}: name 과 pattern 은 문자열로 꼭 적어야 합니다.")
            item_label = item.get("label")
            if item_label is not None and not isinstance(item_label, str):
                raise PolicyError(f"{label}.label 은 문자열이어야 합니다.")
            policy.custom_rules.append(_make_rule(item["name"], item["pattern"], item_label, label, SOURCE_FILE))
        policy.sources["mask_rules"] = SOURCE_FILE


# ---------------------------------------------------------------------------
# 검증 도우미
# ---------------------------------------------------------------------------
def _check_number(key: str, value: Any, where: str) -> int:
    low, high = LIMITS[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyError(f"{where}: 정수여야 합니다 (현재 값: {value!r})")
    if not low <= value <= high:
        raise PolicyError(f"{where}: {low} ~ {high} 사이여야 합니다 (현재 값: {value})")
    return value


def _string_list(value: Any, where: str) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise PolicyError(f"{where}: 비어 있지 않은 문자열의 배열이어야 합니다.")
    return [v.strip() for v in value]


def _split_pattern_option(spec: str) -> "tuple[str, str]":
    """--mask-pattern 'NAME=REGEX' → (NAME, REGEX). 이름은 꼭 적어야 한다(모호한 해석 방지)."""
    head, sep, tail = spec.partition("=")
    if not sep or not _NAME_RE.match(head):
        raise PolicyError(
            f"--mask-pattern '{spec}': 'NAME=REGEX' 형식이어야 합니다. NAME 은 영문자로 시작하는 "
            "영문/숫자/밑줄 40자 이내입니다. 예) --mask-pattern \"EMPLOYEE_ID=\\bEMP-\\d{6}\\b\""
        )
    return head, tail


def _make_rule(name: str, pattern: str, label: Optional[str], where: str, source: str) -> CustomRule:
    name = name.strip()
    if not _NAME_RE.match(name):
        raise PolicyError(f"{where}: 규칙 이름은 영문자로 시작하는 영문/숫자/밑줄 40자 이내여야 합니다 (현재: {name!r})")
    name = name.upper()
    if name in {r.name for r in MASK_RULES}:
        raise PolicyError(f"{where}: '{name}' 은 내장 규칙 이름입니다. 다른 이름을 쓰세요.")
    if not pattern:
        raise PolicyError(f"{where}: pattern 이 비어 있습니다.")
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise PolicyError(f"{where}: pattern 은 {MAX_PATTERN_LENGTH}자 이하여야 합니다.")
    try:
        regex = re.compile(pattern)
    except (re.error, OverflowError, RecursionError, ValueError) as exc:
        raise PolicyError(f"{where}: 정규식 오류 - {exc}")
    if regex.search("") is not None or any(m.start() == m.end() for m in regex.finditer(_ZERO_WIDTH_PROBE)):
        raise PolicyError(
            f"{where}: 길이 0 인 위치와 일치할 수 있는 정규식입니다(예: a*, \\b, (?=x)). 반드시 1글자 이상과 일치해야 합니다."
        )
    flags = _INLINE_FLAGS_RE.match(pattern)
    body = pattern[flags.end():] if flags else pattern
    multiline = bool(flags and "m" in flags.group(1))
    if not multiline and (body.startswith("^") or (body.endswith("$") and not body.endswith("\\$"))):
        raise PolicyError(
            f"{where}: 규칙은 여러 줄짜리 diff 본문(줄마다 '+', '-', ' ' 로 시작)에 적용되므로 ^, $ 를 그대로 쓸 수 없습니다. "
            "줄 단위로 쓰려면 (?m)^[+ -]?... 처럼 (?m) 을 붙이세요."
        )
    label = (label if label is not None else name).strip()
    if not _LABEL_RE.match(label):
        raise PolicyError(f"{where}: label 은 영문/숫자/밑줄/점/하이픈 40자 이내여야 합니다 (현재: {label!r})")
    return CustomRule(name=name, pattern=pattern, label=label, regex=regex, source=source)


def _check_unique_names(rules: Iterable[CustomRule]) -> None:
    seen = set()
    for rule in rules:
        if rule.name in seen:
            raise PolicyError(f"사용자 마스킹 규칙 이름이 중복됩니다: {rule.name}")
        seen.add(rule.name)


def _custom_replacer(label: str, has_value_group: bool) -> Callable[["re.Match[str]"], str]:
    token = f"[MASKED:{label}]"

    def repl(m: "re.Match[str]") -> str:
        if m.start() == m.end():  # 안전장치: 길이 0 일치는 아무것도 바꾸지 않는다
            return m.group(0)
        if has_value_group and m.group("value"):
            start, end = m.span("value")
            if m.start() <= start and end <= m.end():  # value 가 일치 범위 안에 있을 때만 부분 치환
                whole, base = m.group(0), m.start()
                return whole[: start - base] + token + whole[end - base:]
        return token

    return repl
