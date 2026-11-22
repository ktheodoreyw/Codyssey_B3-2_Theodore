"""Git 변경 사항 수집.

과제 제약에 따라 변경 사항 수집은 `git status` 와 `git diff` 두 명령만 사용한다.

- git status --porcelain=v1 -z -b --untracked-files=all
    → 현재 브랜치와 변경 파일 목록(staged / unstaged / untracked)
- git diff --cached            → 스테이징된 변경 (커밋될 내용)
- git diff                     → 스테이징되지 않은 작업 트리 변경
- git diff <base>...HEAD       → PR 용: 기준 브랜치에서 갈라진 이후 커밋된 변경

출력 파싱 함수(parse_status, parse_diff)는 순수 함수로 분리해 테스트하기 쉽게 했다.
"""

from __future__ import annotations

import locale
import os
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

# 내용을 보낼 가치가 낮은(대부분 자동 생성되는) 파일. diff 본문은 생략하고 통계만 남긴다.
LOCKFILE_NAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "composer.lock",
    "Gemfile.lock",
    "go.sum",
}


class GitError(Exception):
    """git 실행 실패 또는 사용할 수 없는 저장소 상태."""


# ---------------------------------------------------------------------------
# 데이터 구조
# ---------------------------------------------------------------------------
@dataclass
class StatusEntry:
    """git status 한 줄. code 는 XY 두 글자(X=스테이징 영역, Y=작업 트리)."""

    code: str
    path: str
    orig_path: Optional[str] = None

    @property
    def is_untracked(self) -> bool:
        return self.code == "??"

    @property
    def is_ignored(self) -> bool:
        return self.code == "!!"

    @property
    def is_staged(self) -> bool:
        return not self.is_untracked and not self.is_ignored and self.code[0] not in (" ", "?")

    @property
    def is_unstaged(self) -> bool:
        return not self.is_untracked and not self.is_ignored and self.code[1] not in (" ", "?")

    @property
    def is_conflict(self) -> bool:
        return "U" in self.code or self.code in ("AA", "DD")


@dataclass
class GitStatus:
    branch: str
    entries: List[StatusEntry] = field(default_factory=list)

    @property
    def staged(self) -> List[StatusEntry]:
        return [e for e in self.entries if e.is_staged]

    @property
    def unstaged(self) -> List[StatusEntry]:
        return [e for e in self.entries if e.is_unstaged]

    @property
    def untracked(self) -> List[StatusEntry]:
        return [e for e in self.entries if e.is_untracked]

    @property
    def conflicts(self) -> List[StatusEntry]:
        return [e for e in self.entries if e.is_conflict]

    @property
    def is_clean(self) -> bool:
        return not self.entries


@dataclass
class FileDiff:
    """한 파일의 diff 조각."""

    path: str
    old_path: Optional[str] = None
    change: str = "M"  # A(추가) / M(수정) / D(삭제) / R(이름 변경)
    additions: int = 0
    deletions: int = 0
    is_binary: bool = False
    lines: List[str] = field(default_factory=list)  # diff --git 으로 시작하는 원문 줄들

    @property
    def is_lockfile(self) -> bool:
        return os.path.basename(self.path) in LOCKFILE_NAMES

    @property
    def display_path(self) -> str:
        if self.change == "R" and self.old_path and self.old_path != self.path:
            return f"{self.old_path} -> {self.path}"
        return self.path


@dataclass
class DiffResult:
    """선택된 diff 와 그 출처 설명."""

    label: str  # 사람이 읽는 설명 (예: "스테이징된 변경")
    command: str  # 실행한 명령 (예: "git diff --cached")
    files: List[FileDiff] = field(default_factory=list)

    @property
    def total_lines(self) -> int:
        return sum(len(f.lines) for f in self.files)

    @property
    def additions(self) -> int:
        return sum(f.additions for f in self.files)

    @property
    def deletions(self) -> int:
        return sum(f.deletions for f in self.files)

    @property
    def is_empty(self) -> bool:
        return not self.files


# ---------------------------------------------------------------------------
# git 실행
# ---------------------------------------------------------------------------
def run_git(args: Sequence[str], cwd: Optional[str] = None) -> str:
    """git 명령을 실행하고 stdout 을 문자열로 돌려준다.

    - core.quotepath=false : 한글 파일명이 \\354\\225... 처럼 이스케이프되지 않게 한다.
    - 출력은 바이트로 받아 직접 디코딩한다(decode_git_output). Windows 기본 인코딩(cp949)과
      무관하게 UTF-8 로 읽되, cp949 로 저장된 소스 파일의 줄은 cp949 로 다시 읽는다.
    """
    cmd = ["git", "-c", "core.quotepath=false", *args]
    try:
        proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise GitError("git 명령을 찾을 수 없습니다. Git 이 설치되어 있고 PATH 에 등록되어 있는지 확인하세요.") from exc
    if proc.returncode != 0:
        detail = decode_git_output(proc.stderr or proc.stdout).strip()
        raise GitError(f"`git {' '.join(args)}` 실행 실패: {detail}")
    return decode_git_output(proc.stdout)


def decode_git_output(raw: bytes) -> str:
    """git 출력 바이트를 문자열로 바꾼다.

    대부분 UTF-8 이지만, 한국어 Windows 에서 cp949(EUC-KR)로 저장된 파일의 diff 는
    UTF-8 로 읽으면 한글이 깨진다(�). 그런 줄만 골라 시스템 인코딩(cp949)으로 다시 읽는다.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    fallback = locale.getpreferredencoding(False) or "cp949"
    if fallback.lower().replace("-", "") in ("utf8", "ascii"):
        fallback = "cp949"
    lines = []
    for line in raw.split(b"\n"):
        try:
            lines.append(line.decode("utf-8"))
        except UnicodeDecodeError:
            lines.append(line.decode(fallback, errors="replace"))
    return "\n".join(lines)


def ensure_repo_root(cwd: Optional[str] = None) -> None:
    """현재 디렉토리가 Git 프로젝트 루트(.git 이 있는 곳)인지 확인한다."""
    root = cwd or os.getcwd()
    if os.path.exists(os.path.join(root, ".git")):
        return
    # .git 이 없으면, 하위 폴더에서 실행했는지 / 아예 저장소가 아닌지 구분해 안내한다.
    try:
        run_git(["status", "--porcelain"], cwd=root)
    except GitError:
        raise GitError(
            "Git 저장소가 아닙니다. Git 이 초기화된 프로젝트 루트 디렉토리에서 실행하세요.\n"
            "예) git init"
        )
    raise GitError(
        "프로젝트 루트 디렉토리가 아닙니다. .git 폴더가 있는 최상위 디렉토리로 이동한 뒤 실행하세요."
    )


# ---------------------------------------------------------------------------
# git status
# ---------------------------------------------------------------------------
def collect_status(cwd: Optional[str] = None) -> GitStatus:
    raw = run_git(["status", "--porcelain=v1", "-z", "-b", "--untracked-files=all"], cwd=cwd)
    return parse_status(raw)


def parse_status(raw: str) -> GitStatus:
    """`git status --porcelain=v1 -z -b` 출력을 해석한다.

    -z 형식에서는 항목이 NUL(\\0)로 구분되고 경로에 따옴표/이스케이프가 붙지 않는다.
    이름 변경(R)/복사(C) 항목은 '새 경로' 다음 항목으로 '원래 경로'가 따라온다.
    """
    tokens = raw.split("\0")
    branch = "(알 수 없음)"
    entries: List[StatusEntry] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if not token:
            continue
        if token.startswith("## "):
            branch = _parse_branch_header(token[3:])
            continue
        if len(token) < 4:
            continue
        code, path = token[:2], token[3:]
        orig = None
        if code[0] in ("R", "C") or code[1] in ("R", "C"):
            if i < len(tokens):
                orig = tokens[i]
                i += 1
        if code == "!!":
            continue
        entries.append(StatusEntry(code=code, path=path, orig_path=orig))
    return GitStatus(branch=branch, entries=entries)


def _parse_branch_header(header: str) -> str:
    """'## ' 뒤의 브랜치 정보를 브랜치 이름으로 바꾼다.

    예) 'main...origin/main [ahead 1]' -> 'main'
        'No commits yet on main'       -> 'main'
        'HEAD (no branch)'             -> '(detached HEAD)'
    """
    for prefix in ("No commits yet on ", "Initial commit on "):
        if header.startswith(prefix):
            # 빈 원격 저장소를 clone 한 경우: 'No commits yet on main...origin/main [gone]'
            header = header[len(prefix):]
            break
    if header.startswith("HEAD (no branch)"):
        return "(detached HEAD)"
    name = header.split("...", 1)[0]
    name = name.split(" [", 1)[0]
    return name.strip() or "(알 수 없음)"


# ---------------------------------------------------------------------------
# git diff
# ---------------------------------------------------------------------------
# 사용자의 git 설정(diff.noprefix, diff.mnemonicPrefix, diff.submodule, color.ui 등)과
# 무관하게 항상 같은 형식의 diff 가 나오도록 옵션을 고정한다.
DIFF_BASE_ARGS = [
    "diff",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--src-prefix=a/",
    "--dst-prefix=b/",
    "--submodule=short",
    "-M",
]


def collect_diff(extra_args: Sequence[str], label: str, cwd: Optional[str] = None) -> DiffResult:
    args = [*DIFF_BASE_ARGS, *extra_args]
    raw = run_git(args, cwd=cwd)
    shown = " ".join(["git", "diff", *extra_args])
    return DiffResult(label=label, command=shown, files=parse_diff(raw))


def parse_diff(raw: str) -> List[FileDiff]:
    """unified diff 텍스트를 파일 단위로 나누고 추가/삭제 줄 수를 센다."""
    files: List[FileDiff] = []
    current: Optional[FileDiff] = None
    in_hunk = False

    # splitlines() 는 U+2028, \x0c 같은 문자에서도 줄을 나누므로, git 의 실제 줄 구분자인 '\n' 으로만 나눈다.
    raw_lines = raw.split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()
    for line in raw_lines:
        line = line[:-1] if line.endswith("\r") else line  # CRLF 파일의 줄 끝 \r 제거
        if line.startswith("diff --git "):
            current = FileDiff(path=_path_from_diff_header(line))
            files.append(current)
            current.lines.append(line)
            in_hunk = False
            continue
        if current is None:
            continue
        current.lines.append(line)

        if line.startswith("@@"):
            in_hunk = True
            continue
        if in_hunk:
            if line.startswith("+"):
                current.additions += 1
            elif line.startswith("-"):
                current.deletions += 1
            continue

        # 아래는 파일 헤더 영역(첫 @@ 이전)
        if line.startswith("new file mode"):
            current.change = "A"
        elif line.startswith("deleted file mode"):
            current.change = "D"
        elif line.startswith("rename from "):
            current.change = "R"
            current.old_path = line[len("rename from "):]
        elif line.startswith("rename to "):
            current.change = "R"
            current.path = line[len("rename to "):]
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            current.is_binary = True
        elif line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
            current.path = _strip_prefix(line[4:], "b/")
        elif line.startswith("--- ") and not line.startswith("--- /dev/null"):
            if current.change != "R":
                old = _strip_prefix(line[4:], "a/")
                if current.change == "D":
                    current.path = old
    return files


def _strip_prefix(path: str, prefix: str) -> str:
    path = path.rstrip("\t")
    return path[len(prefix):] if path.startswith(prefix) else path


def _path_from_diff_header(line: str) -> str:
    """'diff --git a/x b/x' 에서 경로를 꺼낸다.

    경로에 공백이 있어도 a/ 와 b/ 경로가 같으면 정확히 절반으로 나눌 수 있다.
    이름이 바뀐 경우는 이후 'rename to' 줄에서 다시 덮어쓴다.
    """
    rest = line[len("diff --git "):]
    if rest.startswith("a/"):
        n = len(rest)
        if (n - 5) % 2 == 0:
            half = (n - 5) // 2
            a_path, b_part = rest[2:2 + half], rest[2 + half:]
            if b_part == f" b/{a_path}":
                return a_path
        idx = rest.find(" b/")
        if idx != -1:
            return rest[idx + 3:]
    return rest


# ---------------------------------------------------------------------------
# 명령별 diff 선택
# ---------------------------------------------------------------------------
def select_commit_diff(status: GitStatus, cwd: Optional[str] = None) -> DiffResult:
    """커밋 메시지용 diff 를 고른다.

    1) 스테이징된 변경이 있으면 그것이 '이번에 커밋될 내용'이므로 git diff --cached 를 쓴다.
    2) 없으면 작업 트리의 변경(git diff)으로 초안을 만든다.
    """
    staged = collect_diff(["--cached"], "스테이징된 변경", cwd=cwd)
    if not staged.is_empty:
        return staged
    return collect_diff([], "스테이징되지 않은 작업 트리 변경", cwd=cwd)


def select_pr_diff(
    status: GitStatus, base: Optional[str] = None, cwd: Optional[str] = None
) -> DiffResult:
    """PR 초안용 diff 를 고른다.

    - 기준 브랜치(--base, 기본: main → master 순으로 시도)에서 갈라진 이후의
      커밋된 변경(git diff <base>...HEAD)을 사용한다.
    - 기준 브랜치가 없거나 커밋된 변경이 없으면 작업 트리 변경으로 대체한다.
    """
    candidates = [base] if base else ["main", "master"]
    for candidate in candidates:
        if candidate == status.branch and not base:
            # main 브랜치 위에서 실행했다면 main 이 기준 브랜치이므로 master 로 넘어가지 않는다.
            break
        try:
            result = collect_diff(
                [f"{candidate}...HEAD"], f"'{candidate}' 브랜치 대비 커밋된 변경", cwd=cwd
            )
        except GitError:
            if base:
                raise GitError(
                    f"기준 브랜치 '{base}' 와 비교할 수 없습니다. 브랜치 이름과 커밋 존재 여부를 확인하세요."
                )
            continue  # 이 이름의 브랜치가 없으면 다음 후보(master)를 시도
        if not result.is_empty:
            return result
        # 기준 브랜치는 있지만 커밋된 변경이 없다 → 다른 후보로 넘어가지 않고 작업 트리 변경을 쓴다.
        break
    return select_commit_diff(status, cwd=cwd)
