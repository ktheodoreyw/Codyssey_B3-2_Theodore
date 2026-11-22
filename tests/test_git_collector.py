"""gitgen/git_collector.py 테스트.

- parse_status / parse_diff 는 순수 함수이므로 문자열 입력만으로 검증한다.
- select_commit_diff / select_pr_diff / ensure_repo_root / collect_status 는
  임시 디렉토리에 실제 Git 저장소를 만들어(git init -b main) 통합 테스트한다.

통합 테스트 설계
- 사용자의 전역/시스템 Git 설정에 영향을 받지 않도록 모듈 실행 동안
  GIT_CONFIG_GLOBAL(빈 파일), GIT_CONFIG_NOSYSTEM=1, GIT_CEILING_DIRECTORIES 를 지정한다.
  (run_git 은 os.environ 을 그대로 물려받으므로 테스트 대상 코드에도 똑같이 적용된다.)
- git 프로세스 하나를 띄우는 비용이 Windows 에서 큰 편이라, 클래스마다 '템플릿 저장소'를
  한 번만 만들고 각 테스트는 그 폴더를 복사(shutil.copytree)해서 독립된 저장소로 쓴다.

실행: python -m unittest tests.test_git_collector -v
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from typing import Dict, List, Optional
from unittest import mock

from gitgen.git_collector import (
    DiffResult,
    FileDiff,
    GitError,
    GitStatus,
    StatusEntry,
    collect_status,
    ensure_repo_root,
    parse_diff,
    parse_status,
    run_git,
    select_commit_diff,
    select_pr_diff,
)


# ---------------------------------------------------------------------------
# 공용 도우미
# ---------------------------------------------------------------------------
def _chmod_and_retry(func, path, exc_info):
    """Windows 에서 .git/objects 의 읽기 전용 파일 삭제 실패 시 쓰기 권한을 주고 다시 시도한다."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)
    except OSError:
        pass


def _rmtree(path: Optional[str]) -> None:
    if path and os.path.isdir(path):
        shutil.rmtree(path, onerror=_chmod_and_retry)


def _paths(result: DiffResult) -> List[str]:
    return [f.path for f in result.files]


def _git(cwd: str, *args: str) -> str:
    """테스트 준비용 git 실행 (테스트 대상인 run_git 과 분리)."""
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}: {proc.stderr.strip()}")
    return proc.stdout


def _write(root: str, rel_path: str, content: str) -> None:
    full = os.path.join(root, *rel_path.split("/"))
    parent = os.path.dirname(full)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(full, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


def _commit_all(root: str, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--no-verify", "-m", message)


# 로컬(.git/config) 설정: `git config user.name ...` 등을 네 번 실행하는 것과 같은 내용을
# 파일에 직접 덧붙여 git 프로세스 실행 횟수를 줄인다.
_LOCAL_CONFIG = (
    "[user]\n"
    "\tname = Gitgen Test\n"
    "\temail = gitgen-test@example.invalid\n"
    "[core]\n"
    "\tautocrlf = false\n"
    "[commit]\n"
    "\tgpgsign = false\n"
)

_ROOT: Optional[str] = None
_EMPTY_TEMPLATE: Optional[str] = None
_ENV_PATCH = None


def setUpModule() -> None:
    global _ROOT, _EMPTY_TEMPLATE, _ENV_PATCH
    _ROOT = os.path.realpath(tempfile.mkdtemp(prefix="gitgen-test-"))
    empty_global = os.path.join(_ROOT, "empty-gitconfig")
    with open(empty_global, "w", encoding="utf-8"):
        pass
    _ENV_PATCH = mock.patch.dict(
        os.environ,
        {
            # _ROOT 위로는 저장소를 찾아 올라가지 않는다 → '저장소 아님' 테스트가 환경과 무관해진다.
            "GIT_CEILING_DIRECTORIES": _ROOT,
            "GIT_CONFIG_GLOBAL": empty_global,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    _ENV_PATCH.start()
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
        os.environ.pop(var, None)
    _TEMPLATE_CACHE.clear()
    _EMPTY_TEMPLATE = _make_template("empty-main", branch="main")


def tearDownModule() -> None:
    if _ENV_PATCH is not None:
        _ENV_PATCH.stop()
    _rmtree(_ROOT)


def _make_template(name: str, branch: str = "main", base: Optional[str] = None) -> str:
    """<_ROOT>/templates/<name> 에 저장소를 만든다. base 가 있으면 그 템플릿을 복사해 시작한다."""
    assert _ROOT is not None, "setUpModule 이 먼저 실행되어야 합니다"
    path = os.path.join(_ROOT, "templates", name)
    if base:
        shutil.copytree(base, path)
        return path
    os.makedirs(path)
    _git(path, "init", "-q", "-b", branch)
    with open(os.path.join(path, ".git", "config"), "a", encoding="utf-8", newline="\n") as fh:
        fh.write(_LOCAL_CONFIG)
    return path


class TempGitRepoTestCase(unittest.TestCase):
    """템플릿 저장소를 복사해 테스트마다 독립된 실제 git 저장소를 제공한다.

    구조:  <_ROOT>/<tmp>/repo   ← 이 테스트의 저장소
           <_ROOT>/<tmp>/plain  ← 저장소가 아닌 폴더 (필요 시 생성)
    """

    template: Optional[str] = None  # None 이면 커밋 없는 빈 main 저장소

    def setUp(self) -> None:
        self.parent = tempfile.mkdtemp(dir=_ROOT)
        self.addCleanup(_rmtree, self.parent)
        self.repo = os.path.join(self.parent, "repo")
        shutil.copytree(self.template or _EMPTY_TEMPLATE, self.repo)

    def git(self, *args: str) -> str:
        return _git(self.repo, *args)

    def write(self, rel_path: str, content: str) -> None:
        _write(self.repo, rel_path, content)

    def status(self) -> GitStatus:
        return collect_status(cwd=self.repo)


# ===========================================================================
# parse_status (순수 함수)
# ===========================================================================
class ParseStatusBranchTest(unittest.TestCase):
    def test_branch_header_variants(self) -> None:
        """'## ...' 헤더의 여러 형태에서 브랜치 이름을 정확히 뽑는다."""
        cases = {
            "## main": "main",
            "## main...origin/main": "main",
            "## main...origin/main [ahead 1]": "main",
            "## main...origin/main [ahead 1, behind 2]": "main",
            "## feature/login...origin/feature/login [behind 3]": "feature/login",
            "## No commits yet on main": "main",
            "## Initial commit on master": "master",
            "## HEAD (no branch)": "(detached HEAD)",
        }
        for header, expected in cases.items():
            with self.subTest(header=header):
                self.assertEqual(parse_status(header + "\0").branch, expected)

    def test_empty_output_is_clean_with_unknown_branch(self) -> None:
        """출력이 비어 있으면 변경 없음 + 브랜치 '(알 수 없음)'."""
        status = parse_status("")
        self.assertTrue(status.is_clean)
        self.assertEqual(status.branch, "(알 수 없음)")

    def test_header_only_is_clean(self) -> None:
        """브랜치 헤더만 있으면 is_clean 이 True."""
        status = parse_status("## main...origin/main\0")
        self.assertTrue(status.is_clean)
        self.assertEqual(status.entries, [])


class ParseStatusEntriesTest(unittest.TestCase):
    RAW = "\0".join(
        [
            "## main...origin/main [ahead 1]",
            "M  staged.py",
            " M unstaged.py",
            "MM both.py",
            "A  added.py",
            " D deleted_in_tree.py",
            "D  deleted_staged.py",
            "?? new.txt",
            "?? 문서/새 파일.md",
            "!! ignored.log",
        ]
    ) + "\0"

    def setUp(self) -> None:
        self.status = parse_status(self.RAW)

    def test_staged_classification(self) -> None:
        """X(인덱스) 칸이 공백/? 가 아니면 staged 로 분류한다."""
        self.assertEqual(
            [e.path for e in self.status.staged],
            ["staged.py", "both.py", "added.py", "deleted_staged.py"],
        )

    def test_unstaged_classification(self) -> None:
        """Y(작업 트리) 칸이 공백/? 가 아니면 unstaged 로 분류한다 (MM 은 양쪽 모두)."""
        self.assertEqual(
            [e.path for e in self.status.unstaged],
            ["unstaged.py", "both.py", "deleted_in_tree.py"],
        )

    def test_untracked_classification_with_korean_and_space(self) -> None:
        """'??' 항목은 untracked 이며 staged/unstaged 어느 쪽에도 들어가지 않는다."""
        self.assertEqual([e.path for e in self.status.untracked], ["new.txt", "문서/새 파일.md"])
        for entry in self.status.untracked:
            self.assertFalse(entry.is_staged)
            self.assertFalse(entry.is_unstaged)

    def test_ignored_entries_are_dropped(self) -> None:
        """'!!'(무시된 파일) 항목은 결과에 포함하지 않는다."""
        self.assertNotIn("ignored.log", [e.path for e in self.status.entries])
        self.assertEqual(len(self.status.entries), 8)

    def test_branch_parsed_together_with_entries(self) -> None:
        """헤더와 항목이 함께 있어도 브랜치 이름을 잃지 않는다."""
        self.assertEqual(self.status.branch, "main")
        self.assertFalse(self.status.is_clean)


class ParseStatusRenameTest(unittest.TestCase):
    def test_rename_consumes_orig_path_token(self) -> None:
        """-z 형식의 R 항목: '새 경로' 다음 NUL 토큰이 '원래 경로'이며, 별도 항목으로 오인하지 않는다."""
        raw = "## main\0R  new name.txt\0old name.txt\0 M other.py\0"
        status = parse_status(raw)
        self.assertEqual(len(status.entries), 2)
        renamed, other = status.entries
        self.assertEqual(renamed.code, "R ")
        self.assertEqual(renamed.path, "new name.txt")
        self.assertEqual(renamed.orig_path, "old name.txt")
        self.assertTrue(renamed.is_staged)
        self.assertFalse(renamed.is_unstaged)
        self.assertEqual(other.path, "other.py")
        self.assertIsNone(other.orig_path)

    def test_rename_with_worktree_modification(self) -> None:
        """'RM' 은 staged 이면서 unstaged 이고, orig_path 가 채워진다."""
        status = parse_status("## main\0RM 새이름.py\0옛이름.py\0")
        (entry,) = status.entries
        self.assertEqual((entry.path, entry.orig_path), ("새이름.py", "옛이름.py"))
        self.assertTrue(entry.is_staged)
        self.assertTrue(entry.is_unstaged)

    def test_copy_entry_also_has_orig_path(self) -> None:
        """C(복사) 항목도 R 과 같은 방식으로 원래 경로 토큰을 소비한다."""
        status = parse_status("## main\0C  copy.py\0src.py\0?? x.txt\0")
        self.assertEqual([(e.code, e.path, e.orig_path) for e in status.entries],
                         [("C ", "copy.py", "src.py"), ("??", "x.txt", None)])


class ParseStatusConflictTest(unittest.TestCase):
    def test_conflict_codes_detected(self) -> None:
        """병합 충돌 코드(UU/AA/DD/AU/UA/DU/UD)를 모두 conflict 로 판단한다."""
        conflict_codes = ["UU", "AA", "DD", "AU", "UA", "DU", "UD"]
        raw = "## main\0" + "".join(f"{c} c{i}.txt\0" for i, c in enumerate(conflict_codes))
        status = parse_status(raw)
        self.assertEqual([e.code for e in status.conflicts], conflict_codes)

    def test_normal_codes_are_not_conflicts(self) -> None:
        """일반 변경 코드(M/A/D/R/??)는 conflict 가 아니다."""
        for code in ["M ", " M", "MM", "A ", "AM", "D ", " D", "R ", "??"]:
            with self.subTest(code=code):
                self.assertFalse(StatusEntry(code=code, path="f").is_conflict)

    def test_status_without_conflicts(self) -> None:
        status = parse_status("## main\0M  a.py\0?? b.py\0")
        self.assertEqual(status.conflicts, [])


# ===========================================================================
# parse_diff (순수 함수)
# ===========================================================================
MODIFIED_DIFF = """\
diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,4 +1,5 @@
 import os
-print("old")
+print("new")
+print("extra")
 x = 1
@@ -10,2 +11,2 @@ def f():
-    return 1
+    return 2
"""

ADDED_DIFF = """\
diff --git a/docs/new.md b/docs/new.md
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/docs/new.md
@@ -0,0 +1,3 @@
+# Title
+
+body
"""

DELETED_DIFF = """\
diff --git a/old/gone.txt b/old/gone.txt
deleted file mode 100644
index 4444444..0000000
--- a/old/gone.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-line one
-line two
"""

RENAMED_WITH_EDIT_DIFF = """\
diff --git a/pkg/old_name.py b/pkg/new_name.py
similarity index 88%
rename from pkg/old_name.py
rename to pkg/new_name.py
index 5555555..6666666 100644
--- a/pkg/old_name.py
+++ b/pkg/new_name.py
@@ -1,3 +1,3 @@
 a = 1
-b = 2
+b = 3
 c = 4
"""

PURE_RENAME_DIFF = """\
diff --git a/a.txt b/b.txt
similarity index 100%
rename from a.txt
rename to b.txt
"""

BINARY_ADDED_DIFF = """\
diff --git a/assets/logo.png b/assets/logo.png
new file mode 100644
index 0000000..7777777
Binary files /dev/null and b/assets/logo.png differ
"""

BINARY_MODIFIED_DIFF = """\
diff --git a/img/photo.jpg b/img/photo.jpg
index 8888888..9999999 100644
Binary files a/img/photo.jpg and b/img/photo.jpg differ
"""


class ParseDiffChangeTypeTest(unittest.TestCase):
    def test_modified_file_counts_across_hunks(self) -> None:
        """수정 파일: 여러 hunk 의 +/- 줄을 모두 센다. 헤더(---/+++)는 세지 않는다."""
        (f,) = parse_diff(MODIFIED_DIFF)
        self.assertEqual(f.path, "src/app.py")
        self.assertEqual(f.change, "M")
        self.assertEqual((f.additions, f.deletions), (3, 2))
        self.assertFalse(f.is_binary)
        self.assertEqual(f.lines[0], "diff --git a/src/app.py b/src/app.py")
        self.assertEqual(len(f.lines), len(MODIFIED_DIFF.splitlines()))

    def test_added_file(self) -> None:
        """'new file mode' → change A, 경로는 b/ 쪽."""
        (f,) = parse_diff(ADDED_DIFF)
        self.assertEqual((f.path, f.change, f.additions, f.deletions), ("docs/new.md", "A", 3, 0))

    def test_deleted_file(self) -> None:
        """'deleted file mode' → change D, '+++ /dev/null' 이어도 경로는 a/ 쪽을 쓴다."""
        (f,) = parse_diff(DELETED_DIFF)
        self.assertEqual((f.path, f.change, f.additions, f.deletions), ("old/gone.txt", "D", 0, 2))

    def test_renamed_file_with_edit(self) -> None:
        """이름 변경 + 수정: change R, old_path/path, display_path 가 'old -> new'."""
        (f,) = parse_diff(RENAMED_WITH_EDIT_DIFF)
        self.assertEqual(f.change, "R")
        self.assertEqual(f.old_path, "pkg/old_name.py")
        self.assertEqual(f.path, "pkg/new_name.py")
        self.assertEqual(f.display_path, "pkg/old_name.py -> pkg/new_name.py")
        self.assertEqual((f.additions, f.deletions), (1, 1))

    def test_pure_rename_without_hunks(self) -> None:
        """내용 변경 없는 이름 변경(similarity 100%)도 R 로 인식한다."""
        (f,) = parse_diff(PURE_RENAME_DIFF)
        self.assertEqual((f.change, f.old_path, f.path), ("R", "a.txt", "b.txt"))
        self.assertEqual((f.additions, f.deletions), (0, 0))

    def test_binary_files(self) -> None:
        """'Binary files ... differ' 는 is_binary 로 표시하고 줄 수는 0."""
        added, modified = parse_diff(BINARY_ADDED_DIFF + BINARY_MODIFIED_DIFF)
        self.assertTrue(added.is_binary)
        self.assertEqual((added.path, added.change), ("assets/logo.png", "A"))
        self.assertTrue(modified.is_binary)
        self.assertEqual((modified.path, modified.change), ("img/photo.jpg", "M"))
        self.assertEqual((modified.additions, modified.deletions), (0, 0))

    def test_git_binary_patch_marker(self) -> None:
        """--binary 형식의 'GIT binary patch' 도 바이너리로 인식한다."""
        raw = (
            "diff --git a/a.bin b/a.bin\n"
            "index 1..2 100644\n"
            "GIT binary patch\n"
            "literal 4\n"
            "LcmZQzWMT#Y01f~L\n"
        )
        (f,) = parse_diff(raw)
        self.assertTrue(f.is_binary)

    def test_multiple_files_in_order_and_totals(self) -> None:
        """여러 파일 diff 를 순서대로 나누고 DiffResult 합계가 맞다."""
        raw = MODIFIED_DIFF + ADDED_DIFF + DELETED_DIFF + RENAMED_WITH_EDIT_DIFF + BINARY_ADDED_DIFF
        files = parse_diff(raw)
        self.assertEqual(
            [(f.path, f.change) for f in files],
            [
                ("src/app.py", "M"),
                ("docs/new.md", "A"),
                ("old/gone.txt", "D"),
                ("pkg/new_name.py", "R"),
                ("assets/logo.png", "A"),
            ],
        )
        result = DiffResult(label="t", command="git diff", files=files)
        self.assertEqual(result.additions, 3 + 3 + 0 + 1 + 0)
        self.assertEqual(result.deletions, 2 + 0 + 2 + 1 + 0)
        self.assertEqual(result.total_lines, len(raw.splitlines()))
        self.assertFalse(result.is_empty)

    def test_empty_and_preamble(self) -> None:
        """빈 입력은 빈 목록, 첫 'diff --git' 이전의 잡음 줄은 무시한다."""
        self.assertEqual(parse_diff(""), [])
        files = parse_diff("warning: something\n+not a diff line\n" + ADDED_DIFF)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].additions, 3)
        self.assertTrue(files[0].lines[0].startswith("diff --git "))

    def test_lockfile_property(self) -> None:
        """하위 폴더에 있어도 파일 이름으로 lock 파일을 판단한다."""
        self.assertTrue(FileDiff(path="web/package-lock.json").is_lockfile)
        self.assertTrue(FileDiff(path="poetry.lock").is_lockfile)
        self.assertFalse(FileDiff(path="src/lock.py").is_lockfile)


class ParseDiffPathTest(unittest.TestCase):
    def test_path_with_spaces_and_trailing_tab(self) -> None:
        """공백 경로: git 이 ---/+++ 줄 끝에 붙이는 탭을 제거하고 경로를 그대로 유지한다."""
        raw = (
            "diff --git a/my docs/read me.txt b/my docs/read me.txt\n"
            "index 1..2 100644\n"
            "--- a/my docs/read me.txt\t\n"
            "+++ b/my docs/read me.txt\t\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
        )
        (f,) = parse_diff(raw)
        self.assertEqual(f.path, "my docs/read me.txt")
        self.assertEqual((f.additions, f.deletions), (1, 1))

    def test_path_containing_b_slash_sequence(self) -> None:
        """경로 안에 ' b/' 가 들어 있어도 a/·b/ 경로가 같으면 정확히 절반으로 나눈다."""
        raw = "diff --git a/x b/y.txt b/x b/y.txt\nold mode 100644\nnew mode 100755\n"
        (f,) = parse_diff(raw)
        self.assertEqual(f.path, "x b/y.txt")

    def test_korean_filename(self) -> None:
        """core.quotepath=false 상태의 한글 파일명을 그대로 경로로 쓴다."""
        raw = (
            "diff --git a/문서/한글 파일.md b/문서/한글 파일.md\n"
            "new file mode 100644\n"
            "index 0000000..1234567\n"
            "--- /dev/null\n"
            "+++ b/문서/한글 파일.md\t\n"
            "@@ -0,0 +1,2 @@\n"
            "+안녕하세요\n"
            "+반갑습니다\n"
        )
        (f,) = parse_diff(raw)
        self.assertEqual(f.path, "문서/한글 파일.md")
        self.assertEqual((f.change, f.additions), ("A", 2))


class ParseDiffHunkCountingTest(unittest.TestCase):
    def test_removed_line_starting_with_dashes_counts_as_deletion(self) -> None:
        """hunk 안의 '--- ...'(내용이 '--' 로 시작하는 삭제 줄)은 헤더가 아니라 삭제로 센다."""
        raw = (
            "diff --git a/schema.sql b/schema.sql\n"
            "index 1..2 100644\n"
            "--- a/schema.sql\n"
            "+++ b/schema.sql\n"
            "@@ -1,4 +1,3 @@\n"
            "--- a/evil header lookalike\n"
            "--- SQL comment removed\n"
            "+++ b/also not a header\n"
            "+++counter;\n"
            " SELECT 1;\n"
        )
        (f,) = parse_diff(raw)
        self.assertEqual(f.deletions, 2)
        self.assertEqual(f.additions, 2)
        # hunk 안의 '---'/'+++' 줄 때문에 경로가 바뀌면 안 된다.
        self.assertEqual(f.path, "schema.sql")
        self.assertEqual(f.change, "M")

    def test_no_newline_marker_is_not_counted(self) -> None:
        """'\\ No newline at end of file' 줄은 추가/삭제로 세지 않지만 원문 줄에는 남는다."""
        raw = (
            "diff --git a/a.txt b/a.txt\n"
            "index 1..2 100644\n"
            "--- a/a.txt\n"
            "+++ b/a.txt\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "\\ No newline at end of file\n"
            "+new\n"
            "\\ No newline at end of file\n"
        )
        (f,) = parse_diff(raw)
        self.assertEqual((f.additions, f.deletions), (1, 1))
        self.assertIn("\\ No newline at end of file", f.lines)
        self.assertEqual(len(f.lines), 9)

    def test_hunk_state_resets_for_next_file(self) -> None:
        """다음 파일의 '---'/'+++' 헤더는 이전 파일의 hunk 줄로 세지 않는다."""
        files = parse_diff(MODIFIED_DIFF + DELETED_DIFF)
        self.assertEqual((files[0].additions, files[0].deletions), (3, 2))
        self.assertEqual((files[1].additions, files[1].deletions), (0, 2))
        self.assertEqual(files[1].path, "old/gone.txt")

    # 회귀 테스트: parse_diff 는 git 의 줄 구분자인 LF 로만 줄을 나눈다(U+2028 등은 내용의 일부).
    def test_unicode_line_separator_inside_content_does_not_split_line(self) -> None:
        """내용 안의 U+2028(LINE SEPARATOR)은 diff 줄 구분자가 아니다: '+x<U+2028>-y' 는 추가 1줄."""
        raw = (
            "diff --git a/a.js b/a.js\n"
            "index 1..2 100644\n"
            "--- a/a.js\n"
            "+++ b/a.js\n"
            "@@ -0,0 +1 @@\n"
            "+const s = 'x -y';\n"
        )
        (f,) = parse_diff(raw)
        self.assertEqual((f.additions, f.deletions), (1, 0))
        self.assertEqual(len(f.lines), 6)


# ===========================================================================
# run_git
# ===========================================================================
class RunGitUnitTest(unittest.TestCase):
    def test_missing_git_executable_raises_git_error(self) -> None:
        """git 실행 파일이 없으면(FileNotFoundError) 안내 메시지가 담긴 GitError 로 바꾼다."""
        with mock.patch("gitgen.git_collector.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaises(GitError) as cm:
                run_git(["status"])
        self.assertIn("git 명령을 찾을 수 없습니다", str(cm.exception))

    def test_command_passes_quotepath_false(self) -> None:
        """한글 경로가 이스케이프되지 않도록 항상 '-c core.quotepath=false' 를 붙인다."""
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok 한글".encode("utf-8"), stderr=b"")
        with mock.patch("gitgen.git_collector.subprocess.run", return_value=fake) as run:
            self.assertEqual(run_git(["diff", "--cached"], cwd="X"), "ok 한글")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:3], ["git", "-c", "core.quotepath=false"])
        self.assertEqual(cmd[3:], ["diff", "--cached"])
        # 출력은 바이트로 받아 decode_git_output 으로 직접 디코딩한다(텍스트 모드 미사용).
        self.assertNotIn("encoding", run.call_args[1])


# ===========================================================================
# 통합 테스트: 실제 임시 git 저장소
# ===========================================================================
_TEMPLATE_CACHE: Dict[str, str] = {}


def _committed_template() -> str:
    """main 에 a.txt / b.txt / old.txt / .gitignore 를 커밋한 템플릿."""
    if "committed" not in _TEMPLATE_CACHE or not os.path.isdir(_TEMPLATE_CACHE["committed"]):
        path = _make_template("committed", base=_EMPTY_TEMPLATE)
        _write(path, "a.txt", "alpha\n")
        _write(path, "b.txt", "beta\n")
        _write(path, "old.txt", "same content for rename detection\n" * 5)
        _write(path, ".gitignore", "*.log\n")
        _commit_all(path, "init")
        _TEMPLATE_CACHE["committed"] = path
    return _TEMPLATE_CACHE["committed"]


def _pr_template() -> str:
    """PR 시나리오 템플릿 (체크아웃: main).

        base ──┬── main_only (main, no-new-commits)
               ├── feature.txt (feature)
               └── (develop: base 커밋에 머무름)
    """
    if "pr" not in _TEMPLATE_CACHE or not os.path.isdir(_TEMPLATE_CACHE["pr"]):
        path = _make_template("pr", base=_EMPTY_TEMPLATE)
        _write(path, "base.txt", "base\n")
        _commit_all(path, "base")
        _git(path, "branch", "develop")
        _git(path, "checkout", "-q", "-b", "feature")
        _write(path, "feature.txt", "feature work\n")
        _commit_all(path, "feature work")
        _git(path, "checkout", "-q", "main")
        _write(path, "main_only.txt", "main\n")
        _commit_all(path, "main moves on")
        _git(path, "branch", "no-new-commits")
        _TEMPLATE_CACHE["pr"] = path
    return _TEMPLATE_CACHE["pr"]


class RunGitIntegrationTest(TempGitRepoTestCase):
    def test_nonzero_exit_raises_git_error_with_detail(self) -> None:
        """git 이 0 이 아닌 코드로 끝나면 명령과 stderr 를 담은 GitError."""
        with self.assertRaises(GitError) as cm:
            run_git(["rev-parse", "--verify", "no-such-ref"], cwd=self.repo)
        self.assertIn("rev-parse --verify no-such-ref", str(cm.exception))
        self.assertIn("실행 실패", str(cm.exception))


class EnsureRepoRootTest(TempGitRepoTestCase):
    def test_repo_root_passes(self) -> None:
        """.git 이 있는 루트에서는 예외 없이 통과한다."""
        ensure_repo_root(cwd=self.repo)

    def test_non_repo_directory_raises(self) -> None:
        """Git 저장소가 아닌 폴더 → '저장소가 아닙니다' GitError."""
        plain = os.path.join(self.parent, "plain")
        os.mkdir(plain)
        with self.assertRaises(GitError) as cm:
            ensure_repo_root(cwd=plain)
        self.assertIn("Git 저장소가 아닙니다", str(cm.exception))

    def test_subdirectory_raises_not_root(self) -> None:
        """저장소의 하위 폴더 → '프로젝트 루트 디렉토리가 아닙니다' GitError (비저장소와 구분)."""
        sub = os.path.join(self.repo, "src", "pkg")
        os.makedirs(sub)
        with self.assertRaises(GitError) as cm:
            ensure_repo_root(cwd=sub)
        self.assertIn("프로젝트 루트 디렉토리가 아닙니다", str(cm.exception))
        self.assertNotIn("Git 저장소가 아닙니다", str(cm.exception))


class CollectStatusUnbornTest(TempGitRepoTestCase):
    def test_unborn_branch_header(self) -> None:
        """커밋이 없는 새 저장소: 'No commits yet on main' 에서 main 을 얻는다."""
        self.write("a.txt", "a\n")
        status = self.status()
        self.assertEqual(status.branch, "main")
        self.assertEqual([e.path for e in status.untracked], ["a.txt"])


class CollectStatusIntegrationTest(TempGitRepoTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = _committed_template()

    def test_real_porcelain_output_classification(self) -> None:
        """실제 git status -z 출력: staged/unstaged/untracked/rename 분류가 맞고 무시 파일은 빠진다."""
        self.write("a.txt", "a changed\n")
        self.git("add", "a.txt")
        self.write("b.txt", "b changed\n")
        self.git("mv", "old.txt", "new name.txt")
        self.write("새 파일.txt", "hello\n")
        self.write("debug.log", "ignored\n")

        status = self.status()
        self.assertEqual(status.branch, "main")
        self.assertEqual(sorted(e.path for e in status.staged), ["a.txt", "new name.txt"])
        self.assertEqual([e.path for e in status.unstaged], ["b.txt"])
        self.assertEqual([e.path for e in status.untracked], ["새 파일.txt"])
        self.assertEqual(status.conflicts, [])
        renamed = [e for e in status.entries if e.code.startswith("R")]
        self.assertEqual(len(renamed), 1)
        self.assertEqual(renamed[0].orig_path, "old.txt")
        self.assertNotIn("debug.log", [e.path for e in status.entries])
        self.assertNotIn("old.txt", [e.path for e in status.entries])

    def test_detached_head(self) -> None:
        """detached HEAD 상태는 '(detached HEAD)' 로 표시한다."""
        self.git("checkout", "-q", "--detach")
        self.assertEqual(self.status().branch, "(detached HEAD)")


class SelectCommitDiffTest(TempGitRepoTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = _committed_template()

    MAIN = GitStatus(branch="main")  # select_commit_diff 는 status 내용과 무관하게 동작한다

    def test_prefers_staged_changes(self) -> None:
        """staged 변경이 있으면 unstaged 변경이 있어도 git diff --cached 만 사용한다."""
        self.write("a.txt", "alpha\nalpha2\n")
        self.git("add", "a.txt")
        self.write("b.txt", "beta changed\n")
        result = select_commit_diff(self.MAIN, cwd=self.repo)
        self.assertEqual(result.command, "git diff --cached")
        self.assertEqual(result.label, "스테이징된 변경")
        self.assertEqual(_paths(result), ["a.txt"])
        self.assertEqual((result.additions, result.deletions), (1, 0))

    def test_falls_back_to_unstaged_changes(self) -> None:
        """staged 변경이 없으면 작업 트리 변경(git diff)을 사용한다."""
        self.write("b.txt", "beta changed\n")
        result = select_commit_diff(self.MAIN, cwd=self.repo)
        self.assertEqual(result.command, "git diff")
        self.assertEqual(result.label, "스테이징되지 않은 작업 트리 변경")
        self.assertEqual(_paths(result), ["b.txt"])
        self.assertEqual((result.additions, result.deletions), (1, 1))

    def test_untracked_only_gives_empty_diff(self) -> None:
        """untracked 파일만 있으면 git diff 에 나오지 않으므로 빈 결과다 (status 에는 보인다)."""
        self.write("c.txt", "new\n")
        status = self.status()
        result = select_commit_diff(status, cwd=self.repo)
        self.assertTrue(result.is_empty)
        self.assertEqual(result.command, "git diff")
        self.assertEqual([e.path for e in status.untracked], ["c.txt"])

    def test_korean_filename_with_space_staged(self) -> None:
        """실제 git 출력에서도 한글+공백 파일명이 이스케이프 없이 그대로 나온다."""
        self.write("문서/한글 파일.md", "첫 줄\n둘째 줄\n")
        self.git("add", "-A")
        result = select_commit_diff(self.MAIN, cwd=self.repo)
        self.assertEqual(_paths(result), ["문서/한글 파일.md"])
        (f,) = result.files
        self.assertEqual((f.change, f.additions), ("A", 2))
        self.assertTrue(any("첫 줄" in line for line in f.lines))

    def test_staged_rename_and_delete(self) -> None:
        """git mv 는 R(이름 변경), git rm 은 D(삭제)로 파싱된다."""
        self.git("mv", "a.txt", "renamed.txt")
        self.git("rm", "-q", "b.txt")
        result = select_commit_diff(self.MAIN, cwd=self.repo)
        by_change = {f.change: f for f in result.files}
        self.assertEqual(set(by_change), {"R", "D"})
        self.assertEqual(by_change["R"].display_path, "a.txt -> renamed.txt")
        self.assertEqual(by_change["D"].path, "b.txt")
        self.assertEqual(by_change["D"].deletions, 1)


class SelectCommitDiffUnbornTest(TempGitRepoTestCase):
    def test_first_commit_staged_files(self) -> None:
        """커밋이 하나도 없는 저장소에서도 staged 새 파일을 git diff --cached 로 읽는다."""
        self.write("main.py", "print('hi')\n")
        self.git("add", "main.py")
        result = select_commit_diff(GitStatus(branch="main"), cwd=self.repo)
        self.assertEqual(result.command, "git diff --cached")
        self.assertEqual([(f.path, f.change) for f in result.files], [("main.py", "A")])


class SelectPrDiffTest(TempGitRepoTestCase):
    """select_pr_diff 는 status 중 branch 만 사용하므로, 대부분 GitStatus(branch=...) 를 직접 넘긴다."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.template = _pr_template()

    def test_feature_branch_uses_main_three_dot(self) -> None:
        """feature 브랜치: main...HEAD 의 '커밋된' 변경만 사용 (main 의 이후 커밋·작업 트리 변경 제외)."""
        self.git("checkout", "-q", "feature")
        self.write("base.txt", "base\nuncommitted\n")
        status = self.status()  # 실제 collect_status 로 브랜치 이름까지 확인 (end-to-end)
        self.assertEqual(status.branch, "feature")
        result = select_pr_diff(status, cwd=self.repo)
        self.assertEqual(result.command, "git diff main...HEAD")
        self.assertIn("'main'", result.label)
        self.assertEqual(_paths(result), ["feature.txt"])
        self.assertEqual((result.additions, result.deletions), (1, 0))

    def test_auto_detects_master_when_main_missing(self) -> None:
        """main 이 없고 master 가 있으면 master...HEAD 를 사용한다."""
        self.git("branch", "-m", "main", "master")
        self.git("checkout", "-q", "feature")
        result = select_pr_diff(GitStatus(branch="feature"), cwd=self.repo)
        self.assertEqual(result.command, "git diff master...HEAD")
        self.assertEqual(_paths(result), ["feature.txt"])

    def test_on_main_falls_back_to_working_tree(self) -> None:
        """현재 브랜치가 main 이면 비교 대상이 없으므로 작업 트리 변경으로 대체한다."""
        self.write("base.txt", "base\nmore\n")
        result = select_pr_diff(GitStatus(branch="main"), cwd=self.repo)
        self.assertEqual(result.command, "git diff")
        self.assertEqual(_paths(result), ["base.txt"])

    def test_on_main_falls_back_to_staged_first(self) -> None:
        """대체 시에도 select_commit_diff 규칙(staged 우선)을 따른다."""
        self.write("staged.txt", "s\n")
        self.git("add", "staged.txt")
        self.write("base.txt", "base\nmore\n")
        result = select_pr_diff(GitStatus(branch="main"), cwd=self.repo)
        self.assertEqual(result.command, "git diff --cached")
        self.assertEqual(_paths(result), ["staged.txt"])

    def test_missing_default_base_falls_back(self) -> None:
        """main/master 둘 다 없으면(예: trunk) 오류 없이 작업 트리 변경으로 대체한다."""
        self.git("branch", "-m", "main", "trunk")
        self.git("checkout", "-q", "feature")
        self.write("feature.txt", "feature work\nwip\n")
        result = select_pr_diff(GitStatus(branch="feature"), cwd=self.repo)
        self.assertEqual(result.command, "git diff")
        self.assertEqual(_paths(result), ["feature.txt"])

    def test_feature_without_new_commits_falls_back(self) -> None:
        """main...HEAD 가 비어 있으면(갈라진 뒤 커밋 없음) 작업 트리 변경으로 대체한다."""
        self.git("checkout", "-q", "no-new-commits")
        self.write("base.txt", "base\nedited\n")
        result = select_pr_diff(GitStatus(branch="no-new-commits"), cwd=self.repo)
        self.assertEqual(result.command, "git diff")
        self.assertEqual(_paths(result), ["base.txt"])

    def test_explicit_base_is_used(self) -> None:
        """--base 로 지정한 브랜치를 기준으로 비교한다."""
        self.git("checkout", "-q", "feature")
        result = select_pr_diff(GitStatus(branch="feature"), base="develop", cwd=self.repo)
        self.assertEqual(result.command, "git diff develop...HEAD")
        self.assertIn("'develop'", result.label)
        self.assertEqual(_paths(result), ["feature.txt"])

    def test_explicit_missing_base_raises(self) -> None:
        """존재하지 않는 --base 는 조용히 대체하지 않고 GitError 를 낸다."""
        self.git("checkout", "-q", "feature")
        self.write("feature.txt", "feature work\nwip\n")  # 대체할 변경이 있어도 오류여야 한다
        with self.assertRaises(GitError) as cm:
            select_pr_diff(GitStatus(branch="feature"), base="no-such-branch", cwd=self.repo)
        self.assertIn("no-such-branch", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
