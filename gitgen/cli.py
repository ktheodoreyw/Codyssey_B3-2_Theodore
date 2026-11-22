"""CLI 진입점: 옵션 해석과 전체 자동화 흐름 연결.

  [1] API Key 확인 (환경변수)
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
from .formatter import WARN, Check, build_commit_message, build_pr_draft, clean_line
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
from .safe_mode import PreparedFile, SafeModeReport, mask_data, mask_text, prepare_files

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

    sub = parser.add_subparsers(dest="command", metavar="{commit,pr,all}")
    sub.add_parser(
        "commit",
        parents=[common],
        help="커밋 메시지 생성",
        description="스테이징된 변경(없으면 작업 트리 변경)으로 커밋 메시지를 생성합니다.",
    )
    pr = sub.add_parser(
        "pr",
        parents=[common],
        help="PR 제목/본문 초안 생성",
        description="기준 브랜치 대비 커밋된 변경(없으면 작업 트리 변경)으로 PR 제목/본문 초안을 생성합니다.",
    )
    pr.add_argument("--base", help="비교 기준 브랜치 (기본: main → master 순으로 자동 탐색)")
    all_ = sub.add_parser(
        "all",
        parents=[common],
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
        return run(args)
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

    # [1] API Key ------------------------------------------------------------
    api_key = None
    if not args.dry_run:
        api_key = get_api_key()
        names = " 또는 ".join(config.API_KEY_ENV_VARS)
        if not api_key:
            console.error(
                f"AI API Key 환경변수가 설정되지 않았습니다. ({names})\n"
                f'예) export {config.API_KEY_ENV_VARS[0]}="YOUR_KEY"'
            )
            return EXIT_ERROR
        problem = api_key_problem(api_key)
        if problem:
            console.error(f"{problem} ({names})\n키를 공백/줄바꿈/특수문자 없이 다시 복사해 설정하세요.")
            return EXIT_ERROR

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
    prepared, report = prepare_files(diff.files, args.safe_mode)
    branch_for_prompt = status.branch
    context = args.context
    if args.safe_mode:
        # diff 뿐 아니라 사용자가 입력한 --context 와 브랜치 이름도 외부로 나가므로 함께 가린다.
        branch_for_prompt = _mask_into(report, branch_for_prompt)
        context = _mask_into(report, context) if context else context

    # [4] 프롬프트 + API 호출 -----------------------------------------------------
    user_prompt = build_user_prompt(  # safe-mode 면 파일 경로도 여기서 가린다
        kind,
        branch_for_prompt,
        diff.label,
        diff.command,
        prepared,
        report,
        user_context=context,
    )
    _log_safe_mode(report)
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
        system=system_prompt_for(kind),
        user_content=user_prompt,
        max_tokens=args.max_tokens,
        temperature=temperature,
        effort=effort,
        schema=schema_for(kind),
    )

    if args.dry_run:
        _print_dry_run(payload, user_prompt)
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
        data, masked = mask_data(data)
        if masked:
            output_checks.append(Check(WARN, f"결과에 민감정보 의심 패턴 {masked}건이 있어 가렸습니다 - 확인 필요"))

    summary, texts, checks = _build_outputs(kind, data, [f.path for f in diff.files])
    checks.extend(output_checks)

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
    kind: str, data: dict, changed_paths: List[str]
) -> Tuple[str, Dict[str, str], List[Check]]:
    """AI 응답(JSON)을 검증·후처리해 (요약, {구획 제목: 텍스트}, 검증 결과)로 만든다."""
    texts: Dict[str, str] = {}
    checks: List[Check] = []
    summary = str(data.get("summary") or "")
    if kind in ("commit", "all"):
        commit_data = data if kind == "commit" else _sub(data, "commit")
        message, commit_checks = build_commit_message(commit_data, changed_paths)
        texts["Commit Message"] = message.text
        checks.extend(commit_checks)
        summary = summary or message.summary
    if kind in ("pr", "all"):
        pr_data = data if kind == "pr" else _sub(data, "pr")
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
# 로그 도우미
# ---------------------------------------------------------------------------
def _mask_into(report: SafeModeReport, text: str) -> str:
    masked, counts = mask_text(text)
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
    parts = [f"마스킹 {report.masked_total}건"]
    if report.excluded_sensitive:
        parts.append(f"민감 파일 제외 {len(report.excluded_sensitive)}개")
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


def _print_dry_run(payload: dict, user_prompt: str) -> None:
    params = {k: v for k, v in payload.items() if k not in ("system", "messages", "output_config")}
    output_config = dict(payload.get("output_config") or {})
    if "format" in output_config:
        output_config["format"] = "json_schema (prompts.py 의 COMMIT_SCHEMA / PR_SCHEMA / ALL_SCHEMA)"
    params["output_config"] = output_config
    console.out()
    console.header("요청 미리보기 (dry-run)")
    console.block("Request Parameters", json.dumps(params, ensure_ascii=False, indent=2))
    console.out()
    console.block("System Prompt", payload["system"])
    console.out()
    console.block("User Prompt", user_prompt)
