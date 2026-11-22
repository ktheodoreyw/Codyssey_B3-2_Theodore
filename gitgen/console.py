"""터미널 출력 도우미.

- 진행 로그([INFO]/[WARN]/[ERROR]/[DONE])는 stderr 로 출력한다.
- 생성 결과(변경 요약, 커밋 메시지, PR 초안, 검증 결과)는 stdout 으로 출력한다.

이렇게 나누면 터미널에서는 둘 다 보이지만, `--plain` 결과를 파일이나 다른 명령으로
넘길 때 로그가 섞이지 않는다.
"""

from __future__ import annotations

import os
import sys

RULE_WIDTH = 60


def setup_stdio() -> None:
    """터미널에 맞게 stdout/stderr 인코딩을 정한다.

    - 사용자가 PYTHONIOENCODING / PYTHONUTF8 을 지정했다면 그 설정을 그대로 따른다.
    - Windows PowerShell / cmd : 화면 출력은 파이썬이 항상 유니코드로 정상 표시한다.
      파이프·리다이렉트 시에는 PowerShell/cmd 가 '콘솔 출력 코드페이지'(기본 cp949, chcp 65001 이면
      UTF-8)로 읽으므로, 그 코드페이지에 맞춰 내보낸다.
    - Git Bash(mintty), macOS/Linux 터미널 : UTF-8 로 맞춘다. Git Bash 는 파이프로 UTF-8 을
      기대하는데 파이썬은 cp949 로 내보내 한글이 깨지기 때문이다.
    어느 경우든 표현할 수 없는 문자는 '?' 로 바꿔 출력 도중 죽지 않게 한다.
    """
    user_choice = os.environ.get("PYTHONIOENCODING") or os.environ.get("PYTHONUTF8") == "1"
    windows_native = sys.platform == "win32" and not (os.environ.get("MSYSTEM") or os.environ.get("TERM"))
    console_cp = _console_output_codepage() if windows_native and not user_choice else 0
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if user_choice:
                reconfigure(errors="replace")
            elif windows_native:
                is_tty = getattr(stream, "isatty", lambda: False)()
                if console_cp and not is_tty:
                    reconfigure(encoding=f"cp{console_cp}", errors="replace")
                else:
                    reconfigure(errors="replace")
            else:
                reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, LookupError):
            pass


def _console_output_codepage() -> int:
    """Windows 콘솔의 출력 코드페이지(예: 949, 65001). 콘솔이 없거나 확인할 수 없으면 0."""
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetConsoleOutputCP())  # type: ignore[attr-defined]
    except Exception:
        return 0


def _log(tag: str, message: str) -> None:
    lines = str(message).splitlines() or [""]
    prefix = f"[{tag}] "
    indent = " " * len(prefix)
    print(prefix + lines[0], file=sys.stderr)
    for line in lines[1:]:
        print(indent + line, file=sys.stderr)
    sys.stderr.flush()


def info(message: str) -> None:
    _log("INFO", message)


def warn(message: str) -> None:
    _log("WARN", message)


def error(message: str) -> None:
    _log("ERROR", message)


def done(message: str) -> None:
    _log("DONE", message)


def out(text: str = "") -> None:
    print(text, file=sys.stdout)


def flush() -> None:
    sys.stdout.flush()


def header(title: str) -> None:
    """큰 구획 머리글:  ===== 제목 ====="""
    label = f" {title} "
    side = max(3, (RULE_WIDTH - _display_width(label)) // 2)
    out("=" * side + label + "=" * side)


def block(title: str, body: str) -> None:
    """결과 블록:  --- 제목 --- / 본문 / ------"""
    out(f"--- {title} ---")
    out(body.rstrip("\n"))
    out("-" * RULE_WIDTH)


def _display_width(text: str) -> int:
    """한글 등 전각 문자는 터미널에서 2칸을 차지하므로 너비를 보정한다."""
    import unicodedata

    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width
