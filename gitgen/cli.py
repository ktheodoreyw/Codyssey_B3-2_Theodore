"""CLI 진입점: 옵션 해석과 전체 자동화 흐름 연결.

  [1] API Key 확인 (.env 파일 → 환경변수)
  [2] Git 변경 사항 수집 (git status → git diff)
  [3] safe-mode 적용 (마스킹 / 민감 파일 제외 / 전송량 제한)
  [4] 프롬프트 구성 → Claude API 1회 호출
  [5] 결과 검증 및 후처리 → 구획을 나눠 터미널 출력

명령
  commit : 커밋 메시지
  pr     : PR 제목/본문 초안
  all    : 변경 요약 + 커밋 메시지 + PR 초안 (API 1회 호출)
"""

from __future__ import annotations

import argparse
import json
import math
import os
from typing import Dict, List, Optional, Sequence, Tuple

from . import __version__, config, console
from .ai_client import (
    AIClientError,
    AIResponse,
    api_key_problem,
    build_payload,
    call_messages,
    estimate_cost,
    get_api_key,
    supports_effort,
    supports_temperature,
)
from .env_loader import EnvLoadResult, load_env_file
from .convention import Convention, ConventionError, describe as describe_convention, load_convention
from .formatter import (
    OK,
    WARN,
    Check,
    build_commit_message,
    build_commit_message_conv,
    build_pr_draft,
    build_pr_draft_conv,
    clean_line,
)
from .git_collector import (
    DiffResult,
    GitError,
    GitStatus,
    collect_status,
    ensure_repo_root,
    select_commit_diff,
    select_pr_diff,
)
from .prompts import build_user_prompt, schema_for, system_prompt_for
from .safe_mode import MASK_RULES, PreparedFile, SafeModeReport, mask_data, mask_text, prepare_files
from .safe_policy import POLICY_FILE_NAME, PolicyError, SafeModePolicy, load_policy

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

KIND_LABEL = {"commit": "커밋 메시지", "pr": "PR 초안", "all": "커밋 메시지와 PR 초안"}
KIND_OBJECT = {"commit": "커밋 메시지를", "pr": "PR 초안을", "all": "커밋 메시지와 PR 초안을"}  # 목적격 조사 포함
MAX_FILES_SHOWN = 20
MANY_FILES_WARNING = 200  # 이보다 많으면 node_modules/venv 를 스테이징했을 가능성을 알린다


# ---------------------------------------------------------------------------
# 옵션 정의
# ---------------------------------------------------------------------------
def _temperature(value: str) -> float:
    try:
        v = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"숫자가 아닙니다: {value}")
    if not 0.0 <= v <= 1.0:
        raise argparse.ArgumentTypeError("temperature 는 0.0 ~ 1.0 사이여야 합니다.")
    return v


def _positive_int(value: str) -> int:
    try:
        v = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"정수가 아닙니다: {value}")
    if v <= 0:
        raise argparse.ArgumentTypeError("1 이상의 정수여야 합니다.")
    return v


def _non_negative_int(value: str) -> int:
    try:
        v = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"정수가 아닙니다: {value}")
    if v < 0:
        raise argparse.ArgumentTypeError("0 이상의 정수여야 합니다.")
    return v


def _positive_float(value: str) -> float:
    try:
        v = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"숫자가 아닙니다: {value}")
    if not math.isfinite(v) or v <= 0:
        raise argparse.ArgumentTypeError("0보다 큰 유한한 숫자여야 합니다.")
    return v


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="Git 변경 사항(git status / git diff)을 Claude API 로 분석해 커밋 메시지와 PR 초안을 생성합니다.",
        epilog="예) python main.py commit --temperature 0.3   |   python main.py pr --base main   |   python main.py all",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    api = common.add_argument_group("AI API 옵션")
    api.add_argument("--model", default=config.DEFAULT_MODEL, help=f"사용할 모델 ID (기본: {config.DEFAULT_MODEL})")
    api.add_argument(
        "--temperature",
        type=_temperature,
        default=config.DEFAULT_TEMPERATURE,
        help=f"0.0~1.0. 낮을수록 일관된 문구, 높을수록 다양한 표현 (기본: {config.DEFAULT_TEMPERATURE})",
    )
    api.add_argument(
        "--max-tokens",
        type=_positive_int,
        default=config.DEFAULT_MAX_TOKENS,
        help=f"응답 최대 토큰 수(출력 길이·비용 상한) (기본: {config.DEFAULT_MAX_TOKENS})",
    )
    api.add_argument(
        "--effort",
        choices=config.EFFORT_CHOICES,
        default=config.DEFAULT_EFFORT,
        help=f"모델이 들이는 노력(토큰 사용량) 수준 (기본: {config.DEFAULT_EFFORT})",
    )
    api.add_argument(
        "--timeout",
        type=_positive_float,
        default=config.DEFAULT_TIMEOUT,
        help=f"API 응답 대기 시간(초) (기본: {config.DEFAULT_TIMEOUT:.0f})",
    )

    io = common.add_argument_group("입력/출력 옵션")
    io.add_argument(
        "--safe-mode",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            f"민감정보 마스킹 + 민감 파일 제외 + 최대 {config.SAFE_MAX_FILES}개 파일/{config.SAFE_MAX_LINES}줄만 전송 "
            "(기본: 켜짐, 끄려면 --no-safe-mode)"
        ),
    )
    io.add_argument("-c", "--context", help="변경 이유/요구사항 등 AI 에게 알려 줄 추가 맥락 (예: \"로그인 실패 버그 수정\")")
    io.add_argument("--dry-run", action="store_true", help="API 를 호출하지 않고 전송될 프롬프트만 출력 (API Key 불필요)")
    io.add_argument("--plain", action="store_true", help="결과 텍스트만 출력 (파일 저장/파이프 용)")

    # safe-mode 정책 옵션: commit / pr / all / policy 공통 (기본값 ← .ai-gitgen.json ← 이 옵션)
    policy_opts = argparse.ArgumentParser(add_help=False)
    pol = policy_opts.add_argument_group("safe-mode 정책 옵션 (설정 파일 .ai-gitgen.json 보다 우선)")
    pol.add_argument(
        "--safe-mode-config",
        metavar="PATH",
        help=f"safe-mode 정책 파일 경로 (기본: 현재 폴더의 {POLICY_FILE_NAME}, 없으면 기본 정책)",
    )
    pol.add_argument("--max-files", type=_positive_int, metavar="N", help=f"전송할 최대 파일 수 (기본: {config.SAFE_MAX_FILES})")
    pol.add_argument("--max-lines", type=_positive_int, metavar="N", help=f"전송할 전체 최대 줄 수 (기본: {config.SAFE_MAX_LINES})")
    pol.add_argument(
        "--max-lines-per-file",
        type=_non_negative_int,
        metavar="N",
        help="파일 하나에서 보낼 최대 줄 수, 0 = 제한 없음 (기본: 0)",
    )
    pol.add_argument(
        "--mask-pattern",
        action="append",
        metavar="NAME=REGEX",
        help="추가 마스킹 규칙(정규식). 여러 번 지정 가능. 예) --mask-pattern \"TICKET=JIRA-\\d+\"",
    )
    pol.add_argument(
        "--exclude-file",
        action="append",
        metavar="GLOB",
        help="내용을 보내지 않을 파일 패턴. 여러 번 지정 가능. 예) --exclude-file \"*.sql\"",
    )

    # 팀 컨벤션 옵션: commit / pr / all / convention 공통 (보너스 2)
    conv_opts = argparse.ArgumentParser(add_help=False)
    conv = conv_opts.add_argument_group("팀 컨벤션 옵션 (설정 파일 .ai-gitgen.json 의 convention 보다 우선)")
    conv.add_argument(
        "--convention",
        metavar="NAME|PATH",
        help="커밋/PR 컨벤션: default(기본), 프리셋 이름(예: b2-2), 또는 JSON 파일 경로 "
        "(기본: 현재 폴더 .ai-gitgen.json 의 convention, 없으면 default)",
    )
    gen_opts = argparse.ArgumentParser(add_help=False)
    gen_opts.add_argument_group("팀 컨벤션 옵션").add_argument(
        "--issue",
        type=_positive_int,
        metavar="N",
        help="PR 본문 '관련 이슈' 에 넣을 이슈 번호 (예: --issue 26 → Closes #26)",
    )

    sub = parser.add_subparsers(dest="command", metavar="{commit,pr,all,policy,convention}")
    sub.add_parser(
        "commit",
        parents=[common, policy_opts, conv_opts],
        help="커밋 메시지 생성",
        description="스테이징된 변경(없으면 작업 트리 변경)으로 커밋 메시지를 생성합니다.",
    )
    pr = sub.add_parser(
        "pr",
        parents=[common, policy_opts, conv_opts, gen_opts],
        help="PR 제목/본문 초안 생성",
        description="기준 브랜치 대비 커밋된 변경(없으면 작업 트리 변경)으로 PR 제목/본문 초안을 생성합니다.",
    )
    pr.add_argument("--base", help="비교 기준 브랜치 (기본: main → master 순으로 자동 탐색)")
    policy_cmd = sub.add_parser(
        "policy",
        parents=[policy_opts],
        help="현재 적용되는 safe-mode 정책 보기 (API 호출 없음)",
        description="기본값·설정 파일·CLI 옵션을 합친 safe-mode 정책(전송 제한 숫자, 마스킹 규칙, 제외 파일)을 출력합니다.",
    )
    policy_cmd.add_argument(
        "--check",
        action="append",
        metavar="TEXT",
        help="이 텍스트에 정책을 적용한 결과(마스킹 전/후)를 보여 줍니다. 여러 번 지정 가능.",
    )
    sub.add_parser(
        "convention",
        parents=[conv_opts],
        help="현재 적용되는 팀 커밋/PR 컨벤션 보기 (API 호출 없음)",
        description="커밋 형식·타입·PR 템플릿(섹션 순서, 체크리스트)·브랜치 규칙 등 적용될 컨벤션을 출력합니다.",
    )
    all_ = sub.add_parser(
        "all",
        parents=[common, policy_opts, conv_opts, gen_opts],
        help="변경 요약 + 커밋 메시지 + PR 초안을 한 번에 생성 (API 1회 호출)",
        description=(
            "현재 변경(스테이징된 변경 → 작업 트리 변경 순, 둘 다 없으면 기준 브랜치 대비 커밋된 변경)으로 "
            "커밋 메시지와 PR 초안을 API 1회 호출로 함께 생성합니다."
        ),
    )
    all_.add_argument("--base", help="작업 트리 변경이 없을 때 비교할 기준 브랜치 (기본: main → master)")
    return parser


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    console.setup_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return EXIT_USAGE
    try:
        if args.command == "policy":
            return run_policy(args)
        if args.command == "convention":
            return run_convention(args)
        return run(args)
    except PolicyError as exc:
        console.error(f"safe-mode 정책 설정 오류: {exc}")
        return EXIT_USAGE
    except ConventionError as exc:
        console.error(f"팀 컨벤션 설정 오류: {exc}")
        return EXIT_USAGE
    except GitError as exc:
        console.error(str(exc))
        return EXIT_ERROR
    except AIClientError as exc:
        console.error(exc.describe())
        return EXIT_ERROR
    except KeyboardInterrupt:
        console.error("사용자가 실행을 중단했습니다.")
        return 130
    except Exception as exc:  # 마지막 안전망: 예상하지 못한 오류도 원인을 한 줄로 보여 주고 종료
        console.error(f"예상하지 못한 오류가 발생했습니다: {type(exc).__name__}: {exc}")
        return EXIT_ERROR


def run(args: argparse.Namespace) -> int:
    kind: str = args.command
    label = KIND_LABEL[kind]

    # [1] API Key: .env 파일 → 환경변수 → API Key -------------------------------
    env = load_env_file()
    _log_env_file(env)
    api_key = None
    if not args.dry_run:
        api_key = get_api_key()
        names = " 또는 ".join(config.API_KEY_ENV_VARS)
        if not api_key:
            if env.error:
                where = f".env 파일을 읽지 못했습니다(위 [WARN] 참고): {env.path}"
            elif env.found:
                where = f".env 파일은 찾았지만 API Key 항목이 없습니다: {env.path}"
            else:
                where = f".env 파일이 없습니다: {env.path}"
            console.error(
                f"AI API Key 환경변수가 설정되지 않았습니다. ({names})\n"
                f"{where}\n"
                f"방법 1) .env 파일(권장): ai-gitgen 폴더({config.PROJECT_DIR})에서 cp .env.example .env 후 "
                f"{config.API_KEY_ENV_VARS[0]}=YOUR_KEY 의 YOUR_KEY 를 실제 키로 수정\n"
                f'방법 2) 환경변수 직접 설정: export {config.API_KEY_ENV_VARS[0]}="YOUR_KEY"'
            )
            return EXIT_ERROR
        problem = api_key_problem(api_key)
        if problem:
            console.error(
                f"{problem} ({names})\n"
                ".env 파일(또는 환경변수)의 키를 공백/줄바꿈/특수문자 없이 다시 복사해 설정하세요."
            )
            return EXIT_ERROR

    # safe-mode 정책: 기본값 ← .ai-gitgen.json ← CLI 옵션 (잘못된 설정은 여기서 바로 알린다)
    policy = _load_policy(args)
    if not args.safe_mode and not policy.is_default():
        console.warn("safe-mode 가 꺼져 있어 safe-mode 정책(설정 파일/정책 옵션)은 적용되지 않습니다.")
    # 팀 컨벤션: --convention ← .ai-gitgen.json 의 convention ← 기본 (보너스 2)
    convention = load_convention(args.convention)
    if not convention.is_default:
        console.info(f"팀 컨벤션: {convention.name} ({convention.source})")
    issue = getattr(args, "issue", None)
    if issue and not convention.pr.issue_heading:
        console.warn("--issue 는 '관련 이슈' 섹션이 있는 팀 컨벤션의 PR 초안에만 쓰입니다. (현재 컨벤션에는 없음)")

    # [2] Git 변경 사항 수집 ---------------------------------------------------
    ensure_repo_root()
    status = collect_status()
    console.info(f"현재 브랜치: {status.branch}")
    if status.conflicts:
        raise GitError(
            f"병합 충돌 중인 파일이 {len(status.conflicts)}개 있습니다. 충돌을 해결한 뒤 다시 실행하세요."
        )
    console.info(
        f"Git status 수집 완료: {len(status.entries)}개 파일 변경 감지 "
        f"(staged {len(status.staged)}, unstaged {len(status.unstaged)}, untracked {len(status.untracked)})"
    )

    diff = _select_diff(kind, status, getattr(args, "base", None))
    if diff.is_empty:
        if status.untracked:
            console.warn(
                f"추적되지 않은 새 파일 {len(status.untracked)}개는 git diff 에 나타나지 않습니다.\n"
                "`git add <파일>` 로 스테이징한 뒤 다시 실행하면 새 파일 내용까지 분석합니다."
            )
        console.info(f"변경 사항이 없습니다. {KIND_OBJECT[kind]} 생성하지 않고 종료합니다.")
        return EXIT_OK

    console.info(
        f"Git diff 수집 완료: {diff.total_lines}줄, {len(diff.files)}개 파일 (+{diff.additions}/-{diff.deletions}) "
        f"- {diff.label} ({diff.command})"
    )
    _explain_diff_choice(kind, status, diff)
    if len(diff.files) > MANY_FILES_WARNING:
        console.warn(
            f"변경 파일이 {len(diff.files)}개로 매우 많습니다. node_modules, venv 같은 폴더가 "
            "실수로 스테이징되지 않았는지 확인하세요. (.gitignore 권장)"
        )

    # [3] safe-mode -------------------------------------------------------------
    prepared, report = prepare_files(diff.files, args.safe_mode, policy)
    branch_for_prompt = status.branch
    context = args.context
    diff_label, diff_command = diff.label, diff.command
    if args.safe_mode:
        # diff 뿐 아니라 --context, 현재/기준 브랜치 이름도 외부로 나가므로 같은 규칙으로 가린다.
        branch_for_prompt = _mask_into(report, branch_for_prompt)
        context = _mask_into(report, context) if context else context
        diff_label = _mask_into(report, diff_label)
        diff_command = _mask_into(report, diff_command)

    # [4] 프롬프트 + API 호출 -----------------------------------------------------
    user_prompt = build_user_prompt(  # safe-mode 면 파일 경로도 여기서 가린다
        kind,
        branch_for_prompt,
        diff_label,
        diff_command,
        prepared,
        report,
        user_context=context,
    )
    _log_safe_mode(report)
    if args.safe_mode and policy.exclude_files:
        unmatched = policy.unmatched_excludes(f.path for f in diff.files)
        if unmatched:
            console.info(f"이번 변경과 일치하는 파일이 없는 제외 패턴: {', '.join(unmatched)} (오타가 아닌지 확인)")
    temperature: Optional[float] = args.temperature
    if temperature is not None and not supports_temperature(args.model):
        console.warn(f"'{args.model}' 모델은 temperature 를 지원하지 않아 요청에서 제외합니다. (품질 조절은 --effort 사용)")
        temperature = None
    effort: Optional[str] = args.effort
    if effort and not supports_effort(args.model):
        console.warn(f"'{args.model}' 모델은 effort 옵션을 지원하지 않아 요청에서 제외합니다.")
        effort = None

    payload = build_payload(
        model=args.model,
        system=system_prompt_for(kind, convention),
        user_content=user_prompt,
        max_tokens=args.max_tokens,
        temperature=temperature,
        effort=effort,
        schema=schema_for(kind, convention),
    )

    if args.dry_run:
        _print_dry_run(payload, user_prompt, convention)
        console.done("dry-run 완료: API 를 호출하지 않았습니다. (AI API 호출 횟수: 0회)")
        return EXIT_OK

    console.info(
        f"AI API 요청 중... (model={args.model}, temperature={_fmt_opt(temperature)}, "
        f"max_tokens={args.max_tokens}, effort={_fmt_opt(effort)})"
    )
    try:
        resp = call_messages(
            api_key or "",
            payload,
            timeout=args.timeout,
            on_retry=lambda n, reason: console.warn(
                f"일시적 오류로 다시 시도합니다 ({n + 1}/{config.MAX_API_ATTEMPTS}회차): {reason}"
            ),
        )
    except AIClientError as exc:
        console.info(f"AI API 호출 횟수: {exc.attempts}회")
        raise
    _log_usage(resp)

    # [5] 검증/후처리 + 출력 -------------------------------------------------------
    data = resp.data
    output_checks: List[Check] = []
    if args.safe_mode:
        # 결과에 민감정보가 섞여 나오는 경우에 대비해, 길이 검증·자르기 '전에' 필드 단위로 가린다.
        data, masked = mask_data(data, policy)
        if masked:
            output_checks.append(Check(WARN, f"결과에 민감정보 의심 패턴 {masked}건이 있어 가렸습니다 - 확인 필요"))

    summary, texts, checks = _build_outputs(kind, data, [f.path for f in diff.files], convention, issue)
    checks.extend(output_checks)
    branch_ok = convention.branch_ok(status.branch)
    if branch_ok is True:
        checks.append(Check(OK, f"브랜치 이름 '{status.branch}' 이(가) 팀 규칙과 일치"))
    elif branch_ok is False:
        checks.append(Check(WARN, f"브랜치 이름 '{status.branch}' 이(가) 팀 규칙({convention.branch_pattern})과 다름"))

    console.done(f"{label} 생성 완료")
    if args.plain and kind == "all":
        # 커밋 메시지와 PR 초안이 섞여 보이지 않도록 최소한의 머리글만 붙인다.
        console.out("\n\n".join(f"[{title}]\n{body}" for title, body in texts.items()))
    elif args.plain:
        console.out("\n\n".join(texts.values()))
    else:
        _print_result(status, diff, prepared, summary, texts, checks)
    console.flush()
    return EXIT_OK


def _select_diff(kind: str, status: GitStatus, base: Optional[str]) -> DiffResult:
    if kind == "commit":
        return select_commit_diff(status)
    if kind == "pr":
        return select_pr_diff(status, base)
    # all: 지금 작업 중인 변경을 우선하고, 없으면 브랜치에 커밋된 변경으로 PR 초안까지 만든다.
    diff = select_commit_diff(status)
    return diff if not diff.is_empty else select_pr_diff(status, base)


def _build_outputs(
    kind: str,
    data: dict,
    changed_paths: List[str],
    convention: Optional[Convention] = None,
    issue: Optional[int] = None,
) -> Tuple[str, Dict[str, str], List[Check]]:
    """AI 응답(JSON)을 검증·후처리해 (요약, {구획 제목: 텍스트}, 검증 결과)로 만든다.

    팀 컨벤션이 있으면 그 컨벤션의 형식·템플릿으로 조립하고 검증한다(보너스 2).
    """
    texts: Dict[str, str] = {}
    checks: List[Check] = []
    team = convention is not None and not convention.is_default
    summary = str(data.get("summary") or "")
    if kind in ("commit", "all"):
        commit_data = data if kind == "commit" else _sub(data, "commit")
        if team:
            message, commit_checks = build_commit_message_conv(commit_data, changed_paths, convention)
        else:
            message, commit_checks = build_commit_message(commit_data, changed_paths)
        texts["Commit Message"] = message.text
        checks.extend(commit_checks)
        summary = summary or message.summary
    if kind in ("pr", "all"):
        pr_data = data if kind == "pr" else _sub(data, "pr")
        if team:
            draft, pr_checks = build_pr_draft_conv(pr_data, changed_paths, convention, issue)
        else:
            draft, pr_checks = build_pr_draft(pr_data, changed_paths)
        texts["PR Title"] = draft.title
        texts["PR Body"] = draft.body
        checks.extend(pr_checks)
        summary = summary or draft.summary
    return clean_line(summary), texts, checks


def _sub(data: dict, key: str) -> dict:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# safe-mode 정책
# ---------------------------------------------------------------------------
def _load_policy(args: argparse.Namespace) -> SafeModePolicy:
    return load_policy(
        config_path=args.safe_mode_config,
        max_files=args.max_files,
        max_lines=args.max_lines,
        max_lines_per_file=args.max_lines_per_file,
        mask_patterns=args.mask_pattern,
        exclude_files=args.exclude_file,
    )


def run_convention(args: argparse.Namespace) -> int:
    """`python main.py convention`: 적용될 팀 컨벤션을 문서처럼 보여 준다. Git/API 와 무관하다."""
    conv = load_convention(args.convention)
    console.out()
    console.header("커밋/PR 컨벤션")
    for line in describe_convention(conv):
        console.out(line)
    console.out()
    console.flush()
    return EXIT_OK


def run_policy(args: argparse.Namespace) -> int:
    """`python main.py policy`: 적용될 safe-mode 정책을 보여 준다. Git/API 와 무관하다."""
    policy = _load_policy(args)
    _print_policy(policy)
    for text in args.check or []:
        _print_policy_check(policy, text)
    console.flush()
    return EXIT_OK


def _print_policy(policy: SafeModePolicy) -> None:
    from .safe_mode import SENSITIVE_FILE_PATTERNS, SENSITIVE_PATH_PATTERNS

    def origin(key: str, default_text: str) -> str:
        src = policy.source_of(key)
        return "기본값" if src == "기본값" else f"{src}, 기본 {default_text}"

    per_file = f"{policy.max_lines_per_file}줄" if policy.max_lines_per_file else "제한 없음"
    console.out()
    console.header("safe-mode 정책")
    if policy.config_path:
        console.out(f"설정 파일 : {_display_path(policy.config_path)}")
    else:
        console.out(f"설정 파일 : 없음 (현재 폴더에 {POLICY_FILE_NAME} 이 없어 기본 정책 사용)")
    console.out()
    console.out("[전송 제한]  safe-mode ON 일 때 외부 API 로 보내는 diff 의 양")
    console.out(f"  최대 파일 수      : {policy.max_files}개  ({origin('max_files', f'{config.SAFE_MAX_FILES}개')})")
    console.out(f"  전체 최대 줄 수   : {policy.max_lines}줄  ({origin('max_lines', f'{config.SAFE_MAX_LINES}줄')})")
    console.out(f"  파일당 최대 줄 수 : {per_file}  ({origin('max_lines_per_file', '제한 없음')})")
    console.out(f"  전송 하드 상한    : {config.HARD_MAX_CHARS:,}자  (고정, safe-mode OFF 에도 적용)")
    console.out()
    console.out("[민감 파일 제외]  내용은 보내지 않고 파일명과 +/- 통계만 전송")
    # '.env' 로 시작하는 이름은 모두 제외하므로(.env, .env.*, .envrc 포함) '.env*' 한 줄로 보여 준다.
    builtin = [".env*"] + [p for p in SENSITIVE_FILE_PATTERNS if not p.startswith(".env")] + list(SENSITIVE_PATH_PATTERNS)
    console.out(f"  내장 패턴 {len(builtin)}개 : {', '.join(builtin)}")
    console.out("    (이름이 .example / .sample / .template / .dist 로 끝나는 예시 파일은 제외하지 않고 마스킹 후 전송)")
    if policy.exclude_files:
        console.out(f"  사용자 패턴 {len(policy.exclude_files)}개 (목록 항목은 설정 파일 + CLI 옵션이 합쳐짐)")
        for glob, src in zip(policy.exclude_files, policy.exclude_sources):
            console.out(f"    - {glob}  ({src})")
    else:
        console.out("  사용자 패턴 : 없음")
    console.out(f"  정책 파일({POLICY_FILE_NAME}) 자체도 내용을 보내지 않음 (가리려는 값이 정규식 원문에 들어 있으므로)")
    console.out()
    console.out("[마스킹 규칙]  위에서부터 순서대로 적용, 가린 자리는 [MASKED:라벨]")
    active = policy.builtin_rule_names
    disabled = f", 비활성: {', '.join(policy.disabled_rules)}" if policy.disabled_rules else ""
    console.out(f"  1) 내장 규칙 {len(active)}개 (전체 {len(MASK_RULES)}개{disabled})")
    for start in range(0, len(active), 6):
        console.out("     " + ", ".join(active[start:start + 6]))
    if policy.custom_rules:
        console.out(f"  2) 사용자 규칙 {len(policy.custom_rules)}개 (내장 규칙 다음에 적용 → 기존 보호를 줄이지 않고 추가만 함)")
        for i, rule in enumerate(policy.custom_rules, start=1):
            how = "value 그룹만" if "value" in rule.regex.groupindex else "일치 부분 전체"
            console.out(f"     {i}. {rule.name}  /{rule.pattern}/  → {how} [MASKED:{rule.label}]  ({rule.source})")
    else:
        console.out("  2) 사용자 규칙 : 없음")
    console.out()


def _display_path(path: str) -> str:
    """현재 폴더 아래 파일이면 상대 경로로 보여 준다(화면·증빙에 개인 PC 경로를 남기지 않기 위함)."""
    try:
        rel = os.path.relpath(path)
    except ValueError:  # 다른 드라이브
        return path
    return path if rel.startswith("..") else rel.replace("\\", "/")


def _print_policy_check(policy: SafeModePolicy, text: str) -> None:
    """--check: 입력을 실제 diff 의 추가 줄(+)로 넣어 마스킹한다(실제 전송과 같은 조건)."""
    body = f"@@ -0,0 +1 @@\n+{text}"
    masked_body, counts = mask_text(body, policy)
    masked = masked_body.split("\n", 1)[1]
    masked = masked[1:] if masked.startswith("+") else masked
    console.header("정책 적용 확인 (--check)")
    console.out(f"입력 : {text}")
    console.out(f"결과 : {masked}")
    applied = ", ".join(f"{name} {n}" for name, n in counts.items()) if counts else "가려진 값 없음"
    console.out(f"적용 : {applied}  (diff 의 추가 줄 '+...' 로 넣어 실제 전송과 같은 조건에서 확인)")
    console.out()


# ---------------------------------------------------------------------------
# 로그 도우미
# ---------------------------------------------------------------------------
def _log_env_file(env: EnvLoadResult) -> None:
    """.env 로드 결과를 알린다. 변수 '이름'만 출력하고 값(키)은 절대 출력하지 않는다."""
    if env.error:
        console.warn(env.error)
    if env.loaded:
        console.info(f".env 파일에서 환경변수 {len(env.loaded)}개를 불러왔습니다 ({', '.join(env.loaded)})")
    if env.skipped:
        console.info(f"이미 설정된 환경변수({', '.join(env.skipped)})는 .env 값으로 덮어쓰지 않습니다.")


def _mask_into(report: SafeModeReport, text: str) -> str:
    masked, counts = mask_text(text, report.policy)
    for name, n in counts.items():
        report.masked[name] = report.masked.get(name, 0) + n
    return masked


def _explain_diff_choice(kind: str, status: GitStatus, diff: DiffResult) -> None:
    diffed = {f.path for f in diff.files}
    if diff.command == "git diff --cached":
        rest = [e for e in status.unstaged if e.path not in diffed] + status.untracked
        if rest:
            console.info(f"스테이징되지 않은 변경/새 파일 {len(rest)}개는 이번 {KIND_LABEL[kind]}에서 제외합니다.")
    elif diff.command == "git diff":
        console.info("스테이징된 변경이 없어 작업 트리 변경으로 초안을 만듭니다. (실제 커밋 전 git add 필요)")
    elif not status.is_clean:
        console.warn(f"커밋되지 않은 변경 {len(status.entries)}개 파일은 PR 초안에 포함되지 않습니다.")


def _log_safe_mode(report: SafeModeReport) -> None:
    if not report.enabled:
        console.warn("safe-mode 가 꺼져 있습니다. diff 가 마스킹 없이 외부 AI API 로 전송됩니다.")
        if report.hard_limited:
            console.warn(f"전송 하드 상한({config.HARD_MAX_CHARS:,}자)을 넘어 일부 diff 를 잘랐습니다.")
        console.info(f"전송 diff: {report.sent_lines}줄 / 원본 {report.original_lines}줄")
        return
    policy = report.policy
    if policy is not None and not policy.is_default():
        console.info(f"safe-mode 정책: {policy.summary()}")
    parts = [f"마스킹 {report.masked_total}건"]
    if report.excluded_sensitive:
        parts.append(f"민감 파일 제외 {len(report.excluded_sensitive)}개")
    if report.excluded_by_policy:
        parts.append(f"정책 제외 {len(report.excluded_by_policy)}개")
    if report.skipped_over_file_limit:
        parts.append(f"파일 수 제한으로 생략 {len(report.skipped_over_file_limit)}개")
    parts.append(f"전송 diff {report.sent_lines}줄 / 원본 {report.original_lines}줄")
    console.info("safe-mode ON: " + ", ".join(parts))
    if report.masked:
        detail = ", ".join(f"{k} {v}" for k, v in sorted(report.masked.items()))
        console.info(f"마스킹 상세: {detail}")


def _log_usage(resp: AIResponse) -> None:
    cost = estimate_cost(resp.model, resp.input_tokens, resp.output_tokens)
    cost_text = f" | 예상 비용 ${cost:.4f}" if cost is not None else ""
    console.info(
        f"AI API 호출 횟수: {resp.attempts}회 | 토큰 입력 {resp.input_tokens:,} / 출력 {resp.output_tokens:,} "
        f"| {resp.elapsed:.1f}초{cost_text}"
    )


def _fmt_opt(value: object) -> str:
    return "미전송" if value is None else str(value)


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------
def _print_summary(status: GitStatus, diff: DiffResult, prepared: List[PreparedFile], summary: str) -> None:
    console.header("변경 요약")
    console.out(f"브랜치    : {status.branch}")
    console.out(f"비교 대상 : {diff.label} ({diff.command})")
    console.out(f"변경 파일 : {len(diff.files)}개 (+{diff.additions} / -{diff.deletions})")
    for pf in prepared[:MAX_FILES_SHOWN]:
        f = pf.source
        note = f"  ※ {pf.note}" if pf.note else ""
        console.out(f"  {f.change}  {f.display_path}  (+{f.additions}/-{f.deletions}){note}")
    if len(prepared) > MAX_FILES_SHOWN:
        console.out(f"  ... 외 {len(prepared) - MAX_FILES_SHOWN}개 파일")
    if summary:
        console.out(f"AI 요약   : {summary}")
    console.out()


def _print_result(
    status: GitStatus,
    diff: DiffResult,
    prepared: List[PreparedFile],
    summary: str,
    texts: Dict[str, str],
    checks: List[Check],
) -> None:
    console.out()
    _print_summary(status, diff, prepared, summary)
    for title, body in texts.items():
        console.block(title, body)
        console.out()
    console.header("검증 결과")
    for check in checks:
        console.out(f"[{check.level}] {check.message}")
    console.out("※ 생성된 문구는 초안입니다. 내용을 검토한 뒤 적용하세요.")


def _print_dry_run(payload: dict, user_prompt: str, convention: Optional[Convention] = None) -> None:
    params = {k: v for k, v in payload.items() if k not in ("system", "messages", "output_config")}
    output_config = dict(payload.get("output_config") or {})
    team_schema = None
    if "format" in output_config:
        if convention is not None and not convention.is_default:
            # 팀 컨벤션은 스키마도 바뀌므로(type enum, scope, PR 섹션 키) 실제로 보낼 스키마를 그대로 보여 준다
            team_schema = (output_config["format"] or {}).get("schema")
            output_config["format"] = "json_schema (팀 컨벤션 스키마: 아래 Response Schema)"
        else:
            output_config["format"] = "json_schema (prompts.py 의 COMMIT_SCHEMA / PR_SCHEMA / ALL_SCHEMA)"
    params["output_config"] = output_config
    console.out()
    console.header("요청 미리보기 (dry-run)")
    console.block("Request Parameters", json.dumps(params, ensure_ascii=False, indent=2))
    console.out()
    console.block("System Prompt", payload["system"])
    console.out()
    console.block("User Prompt", user_prompt)
    if team_schema is not None:
        console.out()
        console.block("Response Schema", json.dumps(team_schema, ensure_ascii=False, indent=2))
