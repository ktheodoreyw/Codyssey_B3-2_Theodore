"""gitgen/cli.py end-to-end 테스트.

실제 임시 Git 저장소를 만들고(cwd 이동), gitgen.cli.call_messages 를 가짜(Mock)로
바꿔치기한 상태에서 cli.main() 을 실행한다. 실제 Anthropic API 는 절대 호출하지 않는다.

- stdout/stderr 는 io.StringIO 로 캡처한다. (console.setup_stdio 는 reconfigure 가 없는
  스트림을 건너뛰므로 StringIO 와 함께 써도 안전하다.)
- 사용자의 전역/시스템 git 설정이 결과에 영향을 주지 않도록 GIT_CONFIG_GLOBAL 을 빈 파일로,
  GIT_CONFIG_NOSYSTEM=1 로 고정한다.
- API Key 환경변수는 테스트용 가짜 값으로 덮어쓰거나 지운다(patch.dict 가 원복).

실행: python -m unittest tests.test_cli -v
"""

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from gitgen import cli
from gitgen.ai_client import AIClientError, AIResponse

DUMMY_KEY = "test-dummy-key-not-real"
# 마스킹 검증용 가짜 Anthropic 키 형태 문자열 (실제 키 아님): 접두사 + 40자
FAKE_ANTHROPIC_KEY = "sk-ant-api03-" + ("abcDEF0123" * 4)

COMMIT_DATA = {
    "summary": "인사말 함수를 추가했다.",
    "type": "feat",
    "scope": "app",
    "subject": "인사말 함수 추가",
    "body": ["app.py: greet 함수 추가", "README.md: 사용법 갱신"],
}
COMMIT_TEXT = "feat(app): 인사말 함수 추가\n\n- app.py: greet 함수 추가\n- README.md: 사용법 갱신"

PR_DATA = {
    "summary": "인사말 기능을 추가했다.",
    "type": "feat",
    "subject": "인사말 기능 추가",
    "why": ["사용자에게 인사 메시지를 보여 주기 위함"],
    "what": ["app.py: greet 함수 추가", "app.py: 실행 진입점 추가"],
    "how_to_test": ["`python app.py` 실행 후 인사말 출력 확인"],
}


def make_response(data, model="claude-opus-4-6", attempts=1):
    """구조화 출력 JSON 이 파싱된 것처럼 AIResponse 를 직접 만든다."""
    return AIResponse(
        data=data,
        raw_text=json.dumps(data, ensure_ascii=False),
        model=model,
        stop_reason="end_turn",
        input_tokens=1234,
        output_tokens=56,
        request_id="req_test_0001",
        attempts=attempts,
        elapsed=0.42,
    )


def _on_rm_error(func, path, exc_info):
    """Windows 에서 .git/objects 의 읽기 전용 파일 삭제 실패 시 권한을 풀고 재시도."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def _remove_tree(path):
    if path and os.path.isdir(path):
        shutil.rmtree(path, onerror=_on_rm_error)


def _write_file(path, content):
    """줄바꿈 변환 없이(LF 그대로) UTF-8 로 쓴다. (Windows 의 CRLF 변환 방지)"""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


def _run_git(cwd, args, env=None):
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout


# ---------------------------------------------------------------------------
# 모듈 공통 준비: 격리된 git 설정 파일 + '초기 커밋이 끝난' 템플릿 저장소
# (Windows 에서 git 프로세스 1회 실행이 수백 ms 걸리므로, 템플릿을 한 번만 만들고
#  테스트마다 복사해서 쓴다.)
# ---------------------------------------------------------------------------
_MODULE_TMP = None
_GITCONFIG = None
_TEMPLATE_REPO = None

_GITCONFIG_TEXT = (
    "[user]\n"
    "\tname = Test User\n"
    "\temail = test@example.invalid\n"
    "[commit]\n"
    "\tgpgsign = false\n"
    "[core]\n"
    "\tautocrlf = false\n"
    "[init]\n"
    "\tdefaultBranch = main\n"
)

_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")


def setUpModule():
    global _MODULE_TMP, _GITCONFIG, _TEMPLATE_REPO
    _MODULE_TMP = os.path.realpath(tempfile.mkdtemp(prefix="gitgen_cli_tpl_"))
    _GITCONFIG = os.path.join(_MODULE_TMP, "gitconfig")
    _write_file(_GITCONFIG, _GITCONFIG_TEXT)

    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    env.update({"GIT_CONFIG_GLOBAL": _GITCONFIG, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CEILING_DIRECTORIES": _MODULE_TMP})

    _TEMPLATE_REPO = os.path.join(_MODULE_TMP, "template")
    os.mkdir(_TEMPLATE_REPO)
    _run_git(_TEMPLATE_REPO, ["init", "-q", "-b", "main"], env=env)
    _write_file(os.path.join(_TEMPLATE_REPO, "README.md"), "# demo\n")
    _write_file(os.path.join(_TEMPLATE_REPO, "app.py"), "print('hello')\n")
    _run_git(_TEMPLATE_REPO, ["add", "-A"], env=env)
    _run_git(_TEMPLATE_REPO, ["commit", "-q", "-m", "init"], env=env)


def tearDownModule():
    _remove_tree(_MODULE_TMP)


class CliTestBase(unittest.TestCase):
    """임시 저장소 + 환경변수 격리 + call_messages 패치를 공통으로 준비한다.

    self.repo 는 main 브랜치에 초기 커밋(README.md, app.py)이 있는 깨끗한 저장소다.
    """

    def setUp(self):
        self._orig_cwd = os.getcwd()
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="gitgen_cli_test_"))
        self.addCleanup(self._remove_tmp)

        self.repo = os.path.join(self.tmp, "repo")
        shutil.copytree(_TEMPLATE_REPO, self.repo)

        env_patch = mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_GLOBAL": _GITCONFIG,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CEILING_DIRECTORIES": self.tmp,
                "ANTHROPIC_API_KEY": DUMMY_KEY,
            },
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for name in ("AI_API_KEY",) + _GIT_ENV_DROP:
            os.environ.pop(name, None)

        call_patch = mock.patch("gitgen.cli.call_messages")
        self.call_mock = call_patch.start()
        self.addCleanup(call_patch.stop)
        self.call_mock.return_value = make_response(COMMIT_DATA)

    # -- helpers -------------------------------------------------------------
    def _remove_tmp(self):
        os.chdir(self._orig_cwd)
        _remove_tree(self.tmp)

    def git(self, *args):
        return _run_git(self.repo, args)

    def write(self, rel, content):
        _write_file(os.path.join(self.repo, rel), content)

    def stage_change(self):
        """app.py 를 수정해 스테이징한다."""
        self.write("app.py", "def greet(name):\n    return f'hi {name}'\n\nprint(greet('x'))\n")
        self.git("add", "app.py")

    def run_cli(self, argv, cwd=None):
        """cwd 로 이동해 cli.main(argv) 를 실행하고 (exit code, stdout, stderr) 를 돌려준다."""
        out, err = io.StringIO(), io.StringIO()
        os.chdir(cwd or self.repo)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(argv)
        finally:
            os.chdir(self._orig_cwd)
        return code, out.getvalue(), err.getvalue()

    def sent_payload(self):
        self.assertEqual(self.call_mock.call_count, 1, "API 는 정확히 1회 호출되어야 한다")
        args, kwargs = self.call_mock.call_args
        return args[1]

    def sent_user_prompt(self):
        return self.sent_payload()["messages"][0]["content"]


# ---------------------------------------------------------------------------
# [1] API Key / dry-run
# ---------------------------------------------------------------------------
class ApiKeyAndDryRunTests(CliTestBase):
    def test_missing_api_key_exits_1_without_calling_api(self):
        """API Key 환경변수가 둘 다 없으면 exit 1, ANTHROPIC_API_KEY 안내, API 미호출."""
        self.stage_change()
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("AI_API_KEY", None)

        code, out, err = self.run_cli(["commit"])

        self.assertEqual(code, 1)
        self.assertIn("[ERROR]", err)
        self.assertIn("ANTHROPIC_API_KEY", err)
        self.assertIn("AI_API_KEY", err)
        self.assertEqual(out, "")
        self.call_mock.assert_not_called()

    def test_blank_api_key_is_treated_as_missing(self):
        """공백뿐인 API Key 는 없는 것으로 취급되어 exit 1."""
        self.stage_change()
        os.environ["ANTHROPIC_API_KEY"] = "   "

        code, _, err = self.run_cli(["commit"])

        self.assertEqual(code, 1)
        self.assertIn("ANTHROPIC_API_KEY", err)
        self.call_mock.assert_not_called()

    def test_ai_api_key_fallback_is_passed_to_client(self):
        """ANTHROPIC_API_KEY 가 없으면 AI_API_KEY 값이 call_messages 의 api_key 로 전달된다."""
        self.stage_change()
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ["AI_API_KEY"] = "fallback-dummy-key"

        code, _, _ = self.run_cli(["commit"])

        self.assertEqual(code, 0)
        self.assertEqual(self.call_mock.call_args[0][0], "fallback-dummy-key")

    def test_dry_run_works_without_api_key(self):
        """--dry-run 은 API Key 없이 동작하고, 프롬프트만 출력하며 API 를 호출하지 않는다."""
        self.stage_change()
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("AI_API_KEY", None)

        code, out, err = self.run_cli(["commit", "--dry-run"])

        self.assertEqual(code, 0)
        self.assertIn("--- Request Parameters ---", out)
        self.assertIn("--- System Prompt ---", out)
        self.assertIn("--- User Prompt ---", out)
        self.assertIn("diff --git a/app.py b/app.py", out)
        self.assertIn("dry-run", err)
        self.assertIn("[DONE]", err)
        self.assertNotIn("[ERROR]", err)
        self.call_mock.assert_not_called()

    def test_dry_run_request_parameters_reflect_options(self):
        """--dry-run 의 Request Parameters 블록에 CLI 옵션 값이 반영된다(스키마 본문은 생략)."""
        self.stage_change()

        code, out, _ = self.run_cli(
            ["commit", "--dry-run", "--temperature", "0.7", "--max-tokens", "512", "--effort", "high"]
        )

        self.assertEqual(code, 0)
        start = out.index("--- Request Parameters ---") + len("--- Request Parameters ---")
        end = out.index("-" * 60, start)
        params = json.loads(out[start:end])
        self.assertEqual(params["model"], "claude-opus-4-6")
        self.assertEqual(params["temperature"], 0.7)
        self.assertEqual(params["max_tokens"], 512)
        self.assertEqual(params["output_config"]["effort"], "high")
        self.assertIsInstance(params["output_config"]["format"], str)
        self.call_mock.assert_not_called()


# ---------------------------------------------------------------------------
# [2] Git 상태별 분기
# ---------------------------------------------------------------------------
class GitStateTests(CliTestBase):
    def test_clean_repo_exits_0_with_no_changes_message(self):
        """변경이 없는 저장소: exit 0, '변경 사항이 없습니다' 안내, API 미호출."""
        code, out, err = self.run_cli(["commit"])

        self.assertEqual(code, 0)
        self.assertIn("변경 사항이 없습니다", err)
        self.assertEqual(out, "")
        self.call_mock.assert_not_called()

    def test_untracked_only_repo_guides_git_add(self):
        """추적되지 않은 새 파일만 있으면 exit 0 + git add 안내 + '변경 사항이 없습니다'."""
        self.write("new_module.py", "x = 1\n")

        code, _, err = self.run_cli(["commit"])

        self.assertEqual(code, 0)
        self.assertIn("[WARN]", err)
        self.assertIn("git add", err)
        self.assertIn("1개", err)
        self.assertIn("변경 사항이 없습니다", err)
        self.call_mock.assert_not_called()

    def test_not_a_git_repo_exits_1(self):
        """.git 이 없는(상위에도 없는) 디렉토리에서 실행하면 GitError → exit 1."""
        plain = os.path.join(self.tmp, "plain_dir")
        os.mkdir(plain)

        code, _, err = self.run_cli(["commit"], cwd=plain)

        self.assertEqual(code, 1)
        self.assertIn("[ERROR]", err)
        self.assertIn("Git 저장소가 아닙니다", err)
        self.call_mock.assert_not_called()

    def test_subdirectory_of_repo_exits_1(self):
        """저장소 하위 폴더에서 실행하면 '프로젝트 루트 디렉토리가 아닙니다' → exit 1."""
        sub = os.path.join(self.repo, "sub")
        os.mkdir(sub)

        code, _, err = self.run_cli(["commit"], cwd=sub)

        self.assertEqual(code, 1)
        self.assertIn("프로젝트 루트 디렉토리가 아닙니다", err)
        self.call_mock.assert_not_called()

    def test_unstaged_changes_are_used_when_nothing_staged(self):
        """스테이징된 변경이 없으면 작업 트리 변경(git diff)으로 초안을 만든다."""
        self.write("app.py", "print('changed')\n")

        code, _, err = self.run_cli(["commit"])

        self.assertEqual(code, 0)
        prompt = self.sent_user_prompt()
        self.assertIn("(git diff)", prompt)
        self.assertIn("+print('changed')", prompt)
        self.assertIn("스테이징된 변경이 없어", err)

    def test_staged_changes_take_priority_over_unstaged(self):
        """staged 와 unstaged 가 섞여 있으면 git diff --cached 만 전송한다."""
        self.stage_change()
        self.write("README.md", "# demo\n\nunstaged-only-line\n")

        code, _, err = self.run_cli(["commit"])

        self.assertEqual(code, 0)
        prompt = self.sent_user_prompt()
        self.assertIn("(git diff --cached)", prompt)
        self.assertIn("def greet(name):", prompt)
        self.assertNotIn("unstaged-only-line", prompt)
        self.assertIn("1개는 이번 커밋 메시지에서 제외", err)


# ---------------------------------------------------------------------------
# [3] commit 명령
# ---------------------------------------------------------------------------
class CommitCommandTests(CliTestBase):
    def test_commit_happy_path_prints_sections_and_passes_options(self):
        """commit 성공: 변경 요약/커밋 메시지/검증 결과 구획 출력 + 옵션이 payload 에 반영."""
        self.stage_change()
        self.call_mock.return_value = make_response(COMMIT_DATA, model="claude-sonnet-4-6")

        code, out, err = self.run_cli(
            [
                "commit",
                "--model", "claude-sonnet-4-6",
                "--temperature", "0.5",
                "--max-tokens", "1000",
                "--effort", "low",
                "--timeout", "30",
            ]
        )

        self.assertEqual(code, 0, err)
        # 출력 구획
        self.assertIn("변경 요약", out)
        self.assertIn("--- Commit Message ---", out)
        self.assertIn("검증 결과", out)
        self.assertIn(COMMIT_TEXT, out)
        self.assertIn("AI 요약   : 인사말 함수를 추가했다.", out)
        self.assertIn("app.py", out)
        self.assertIn("(git diff --cached)", out)
        self.assertLess(out.index("변경 요약"), out.index("--- Commit Message ---"))
        self.assertLess(out.index("--- Commit Message ---"), out.index("검증 결과"))
        self.assertIn("[DONE]", err)
        self.assertNotIn("[ERROR]", err)

        # payload 에 CLI 옵션 반영
        payload = self.sent_payload()
        self.assertEqual(payload["model"], "claude-sonnet-4-6")
        self.assertEqual(payload["temperature"], 0.5)
        self.assertEqual(payload["max_tokens"], 1000)
        self.assertEqual(payload["output_config"]["effort"], "low")
        self.assertEqual(payload["output_config"]["format"]["type"], "json_schema")
        self.assertIn("subject", payload["output_config"]["format"]["schema"]["properties"])
        self.assertEqual(payload["messages"][0]["role"], "user")
        self.assertIn("커밋 메시지", payload["system"])

        args, kwargs = self.call_mock.call_args
        self.assertEqual(args[0], DUMMY_KEY)
        self.assertEqual(kwargs["timeout"], 30.0)

    def test_commit_defaults_are_used_without_options(self):
        """옵션을 주지 않으면 config 기본값(opus-4-6, 0.2, 2048, medium)이 전송된다."""
        self.stage_change()

        code, _, _ = self.run_cli(["commit"])

        self.assertEqual(code, 0)
        payload = self.sent_payload()
        self.assertEqual(payload["model"], "claude-opus-4-6")
        self.assertEqual(payload["temperature"], 0.2)
        self.assertEqual(payload["max_tokens"], 2048)
        self.assertEqual(payload["output_config"]["effort"], "medium")

    def test_temperature_unsupported_model_drops_temperature_with_warning(self):
        """--model claude-opus-5-5 이면 payload 에서 temperature 를 빼고 WARN 을 남긴다."""
        self.stage_change()
        self.call_mock.return_value = make_response(COMMIT_DATA, model="claude-opus-5-5")

        code, _, err = self.run_cli(["commit", "--model", "claude-opus-5-5", "--temperature", "0.5"])

        self.assertEqual(code, 0)
        payload = self.sent_payload()
        self.assertEqual(payload["model"], "claude-opus-5-5")
        self.assertNotIn("temperature", payload)
        warn_lines = [l for l in err.splitlines() if l.startswith("[WARN]")]
        self.assertTrue(
            any("claude-opus-5-5" in l and "temperature" in l for l in warn_lines),
            f"temperature 제외 경고가 없음: {warn_lines}",
        )
        self.assertIn("temperature=미전송", err)

    def test_plain_prints_only_message_text(self):
        """--plain 이면 stdout 에 커밋 메시지 텍스트만 출력된다(구획 머리글 없음)."""
        self.stage_change()

        code, out, err = self.run_cli(["commit", "--plain"])

        self.assertEqual(code, 0)
        self.assertEqual(out, COMMIT_TEXT + "\n")
        for marker in ("변경 요약", "---", "검증 결과", "===", "[INFO]", "[DONE]"):
            self.assertNotIn(marker, out)
        self.assertIn("[DONE]", err)  # 로그는 stderr 로만 간다

    def test_context_option_is_included_in_user_prompt(self):
        """-c/--context 값이 user 프롬프트의 <user_context> 에 들어간다."""
        self.stage_change()

        code, _, _ = self.run_cli(["commit", "-c", "로그인 실패 버그 수정"])

        self.assertEqual(code, 0)
        prompt = self.sent_user_prompt()
        self.assertIn("<user_context>\n로그인 실패 버그 수정\n</user_context>", prompt)


# ---------------------------------------------------------------------------
# [4] pr 명령
# ---------------------------------------------------------------------------
class PrCommandTests(CliTestBase):
    def _feature_branch_with_commit(self):
        self.git("checkout", "-q", "-b", "feature/greet")
        self.write("app.py", "def greet():\n    return 'hi'\n\nprint(greet())\n")
        self.git("commit", "-q", "-am", "add greet")

    def test_pr_on_feature_branch_uses_three_dot_diff_against_main(self):
        """기능 브랜치에서 pr: git diff main...HEAD 사용 + PR 제목/본문(Why/What/How to Test) 출력."""
        self._feature_branch_with_commit()
        self.call_mock.return_value = make_response(PR_DATA)

        code, out, err = self.run_cli(["pr"])

        self.assertEqual(code, 0, err)
        self.assertIn("git diff main...HEAD", err)
        prompt = self.sent_user_prompt()
        self.assertIn("(git diff main...HEAD)", prompt)
        self.assertIn("현재 브랜치: feature/greet", prompt)
        self.assertIn("def greet():", prompt)
        payload = self.sent_payload()
        self.assertIn("how_to_test", payload["output_config"]["format"]["schema"]["properties"])

        self.assertIn("--- PR Title ---\nfeat: 인사말 기능 추가\n", out)
        self.assertIn("--- PR Body ---", out)
        for heading in ("## Why", "## What", "## How to Test"):
            self.assertIn(heading, out)
        self.assertIn("- app.py: greet 함수 추가", out)
        self.assertIn("- `python app.py` 실행 후 인사말 출력 확인", out)
        self.assertLess(out.index("## Why"), out.index("## What"))
        self.assertLess(out.index("## What"), out.index("## How to Test"))
        self.assertIn("검증 결과", out)

    def test_pr_explicit_base(self):
        """--base main 을 명시해도 같은 3-dot diff 를 사용한다."""
        self._feature_branch_with_commit()
        self.call_mock.return_value = make_response(PR_DATA)

        code, _, _ = self.run_cli(["pr", "--base", "main"])

        self.assertEqual(code, 0)
        self.assertIn("(git diff main...HEAD)", self.sent_user_prompt())

    def test_pr_unknown_base_exits_1(self):
        """존재하지 않는 --base 브랜치는 GitError → exit 1, API 미호출."""
        self._feature_branch_with_commit()

        code, _, err = self.run_cli(["pr", "--base", "no-such-branch"])

        self.assertEqual(code, 1)
        self.assertIn("[ERROR]", err)
        self.assertIn("no-such-branch", err)
        self.call_mock.assert_not_called()

    def test_pr_plain_prints_title_and_body_only(self):
        """pr --plain: 제목과 본문만 빈 줄로 이어 출력한다."""
        self._feature_branch_with_commit()
        self.call_mock.return_value = make_response(PR_DATA)

        code, out, _ = self.run_cli(["pr", "--plain"])

        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("feat: 인사말 기능 추가\n\n## Why\n"), out)
        self.assertNotIn("--- PR Title ---", out)
        self.assertNotIn("검증 결과", out)


# ---------------------------------------------------------------------------
# [5] safe-mode
# ---------------------------------------------------------------------------
class SafeModeTests(CliTestBase):
    def _stage_fake_key(self):
        self.write("settings.py", f'CLIENT_FALLBACK = "{FAKE_ANTHROPIC_KEY}"\nTIMEOUT = 30\n')
        self.git("add", "settings.py")

    def test_safe_mode_masks_fake_key_in_user_prompt(self):
        """safe-mode(기본 ON)는 diff 속 sk-ant- 형태 키를 [MASKED:API_KEY] 로 가려서 전송한다."""
        self._stage_fake_key()

        code, _, err = self.run_cli(["commit"])

        self.assertEqual(code, 0, err)
        prompt = self.sent_user_prompt()
        self.assertNotIn(FAKE_ANTHROPIC_KEY, prompt)
        self.assertNotIn("sk-ant-", prompt)
        self.assertIn("[MASKED:API_KEY]", prompt)
        self.assertIn("<notes>", prompt)
        self.assertIn("safe-mode ON", err)
        self.assertIn("ANTHROPIC_KEY 1", err)
        self.assertNotIn(FAKE_ANTHROPIC_KEY, err)

    def test_no_safe_mode_warns_and_sends_unmasked(self):
        """--no-safe-mode: safe-mode 꺼짐 경고를 남기고 diff 를 마스킹하지 않는다."""
        self._stage_fake_key()

        code, _, err = self.run_cli(["commit", "--no-safe-mode"])

        self.assertEqual(code, 0, err)
        self.assertIn("[WARN] safe-mode 가 꺼져 있습니다", err)
        self.assertIn(FAKE_ANTHROPIC_KEY, self.sent_user_prompt())

    def test_safe_mode_excludes_sensitive_file_content(self):
        """.env 같은 민감 파일은 파일명만 남기고 내용은 전송하지 않는다."""
        self.write(".env", "PLAIN_VALUE=visible-only-if-leaked\n")
        self.git("add", ".env")

        code, _, err = self.run_cli(["commit"])

        self.assertEqual(code, 0, err)
        prompt = self.sent_user_prompt()
        self.assertIn(".env", prompt)
        self.assertNotIn("visible-only-if-leaked", prompt)
        self.assertIn("민감 파일 제외 1개", err)

    def test_secret_in_ai_output_is_masked_with_warning(self):
        """AI 결과에 키 형태 문자열이 섞여 와도 safe-mode 가 출력에서 가리고 경고 체크를 남긴다."""
        self.stage_change()
        data = dict(COMMIT_DATA, body=[f"settings.py: 키 {FAKE_ANTHROPIC_KEY} 사용"])
        self.call_mock.return_value = make_response(data)

        code, out, _ = self.run_cli(["commit"])

        self.assertEqual(code, 0)
        self.assertNotIn(FAKE_ANTHROPIC_KEY, out)
        self.assertIn("[MASKED:API_KEY]", out)
        self.assertIn("[경고] 결과에 민감정보 의심 패턴 1건", out)


# ---------------------------------------------------------------------------
# [6] API 오류
# ---------------------------------------------------------------------------
class ApiErrorTests(CliTestBase):
    def test_ai_client_error_exits_1_with_error_log(self):
        """call_messages 가 AIClientError 를 던지면 exit 1, [ERROR] + 상태/안내 출력, 결과 미출력."""
        self.stage_change()
        self.call_mock.side_effect = AIClientError(
            "Overloaded",
            kind="http",
            status=529,
            error_type="overloaded_error",
            request_id="req_err_01",
            hint="잠시 후 다시 실행하세요.",
            attempts=2,
        )

        code, out, err = self.run_cli(["commit"])

        self.assertEqual(code, 1)
        self.assertIn("[ERROR]", err)
        self.assertIn("HTTP 529 overloaded_error", err)
        self.assertIn("잠시 후 다시 실행하세요.", err)
        self.assertIn("req_err_01", err)
        self.assertIn("AI API 호출 횟수: 2회", err)
        self.assertNotIn("--- Commit Message ---", out)
        self.call_mock.assert_called_once()

    def test_retry_callback_logs_warning(self):
        """cli 가 넘긴 on_retry 콜백을 호출하면 재시도 WARN 로그가 찍힌다."""
        self.stage_change()

        def fake_call(api_key, payload, **kwargs):
            kwargs["on_retry"](1, "HTTP 529")
            return make_response(COMMIT_DATA, attempts=2)

        self.call_mock.side_effect = fake_call

        code, _, err = self.run_cli(["commit"])

        self.assertEqual(code, 0)
        self.assertIn("[WARN] 일시적 오류로 다시 시도합니다 (2/2회차): HTTP 529", err)
        self.assertIn("AI API 호출 횟수: 2회", err)


# ---------------------------------------------------------------------------
# [7] argparse
# ---------------------------------------------------------------------------
class ArgparseTests(unittest.TestCase):
    def _main_expect_exit(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("gitgen.cli.call_messages") as call_mock, mock.patch("gitgen.cli.run") as run_mock:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(argv)
        call_mock.assert_not_called()
        run_mock.assert_not_called()
        return ctx.exception.code, err.getvalue()

    def test_temperature_out_of_range_is_usage_error(self):
        """--temperature 1.5 는 argparse 가 거부해 SystemExit(2)."""
        code, err = self._main_expect_exit(["commit", "--temperature", "1.5"])
        self.assertEqual(code, 2)
        self.assertIn("temperature", err)

    def test_other_invalid_option_values_are_usage_errors(self):
        """범위/형식이 틀린 옵션 값은 모두 SystemExit(2)."""
        cases = [
            ["commit", "--temperature", "-0.1"],
            ["commit", "--temperature", "abc"],
            ["commit", "--max-tokens", "0"],
            ["commit", "--max-tokens", "1.5"],
            ["commit", "--effort", "extreme"],
            ["pr", "--timeout", "0"],
            ["deploy"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                code, _ = self._main_expect_exit(argv)
                self.assertEqual(code, 2)

    def test_boundary_temperatures_are_accepted(self):
        """temperature 0.0 과 1.0 은 경계값으로 허용된다."""
        parser = cli.build_parser()
        self.assertEqual(parser.parse_args(["commit", "--temperature", "0"]).temperature, 0.0)
        self.assertEqual(parser.parse_args(["commit", "--temperature", "1"]).temperature, 1.0)

    def test_safe_mode_flag_defaults_and_toggle(self):
        """--safe-mode 기본값은 True, --no-safe-mode 로 끌 수 있다. pr 은 --base 를 받는다."""
        parser = cli.build_parser()
        self.assertTrue(parser.parse_args(["commit"]).safe_mode)
        self.assertFalse(parser.parse_args(["commit", "--no-safe-mode"]).safe_mode)
        self.assertEqual(parser.parse_args(["pr", "--base", "develop"]).base, "develop")

    def test_no_subcommand_returns_usage_code(self):
        """main([]) 은 도움말을 출력하고 2 를 돌려준다(예외 없이)."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("gitgen.cli.call_messages") as call_mock:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main([])
        self.assertEqual(code, 2)
        self.assertIn("usage:", out.getvalue())
        call_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
