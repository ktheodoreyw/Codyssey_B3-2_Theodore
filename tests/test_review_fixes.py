"""코드 리뷰에서 확인된 문제들의 회귀 테스트.

각 테스트는 '리뷰에서 재현된 실패 시나리오'를 그대로 입력으로 쓰고,
수정 후 기대 동작을 확인한다. 실제 Anthropic API 는 호출하지 않는다.

실행: python -m unittest discover -s tests -v
"""

import contextlib
import io
import os
import time
import unittest
from unittest import mock

import requests

from gitgen import ai_client, cli, config, formatter, prompts
from gitgen.ai_client import AIClientError, api_key_problem, call_messages
from gitgen.git_collector import FileDiff, decode_git_output, parse_diff, parse_status
from gitgen.safe_mode import (
    PreparedFile,
    SafeModeReport,
    is_sensitive_file,
    mask_data,
    mask_text,
    prepare_files,
)

try:  # discover -s tests 로 실행하면 최상위 모듈, -m unittest tests.xxx 로 실행하면 패키지 모듈
    from tests import test_cli as base
except ImportError:  # pragma: no cover
    import test_cli as base


def setUpModule():
    base.setUpModule()


def tearDownModule():
    base.tearDownModule()


ALL_DATA = {
    "summary": "인사말 기능을 추가했다.",
    "commit": {"type": "feat", "scope": "", "subject": "인사말 함수 추가", "body": ["app.py: greet 함수 추가"]},
    "pr": {
        "type": "feat",
        "subject": "인사말 기능 추가",
        "why": ["사용자 인사 메시지 필요"],
        "what": ["app.py: greet 함수 추가"],
        "how_to_test": ["`python app.py` 실행"],
    },
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
class AllCommandTests(base.CliTestBase):
    def test_all_prints_summary_commit_and_pr_with_one_call(self):
        """all: API 1회 호출로 변경 요약 + 커밋 메시지 + PR 제목/본문을 모두 출력한다."""
        self.stage_change()
        self.call_mock.return_value = base.make_response(ALL_DATA)
        code, out, err = self.run_cli(["all"])
        self.assertEqual(code, 0, err)
        payload = self.sent_payload()  # 정확히 1회 호출 확인 포함
        schema = payload["output_config"]["format"]["schema"]
        self.assertEqual(set(schema["properties"]), {"summary", "commit", "pr"})
        for marker in ("변경 요약", "--- Commit Message ---", "--- PR Title ---", "--- PR Body ---", "검증 결과"):
            self.assertIn(marker, out)
        self.assertIn("feat: 인사말 함수 추가\n\n- app.py: greet 함수 추가", out)
        for heading in ("## Why", "## What", "## How to Test"):
            self.assertIn(heading, out)
        self.assertIn("AI 요약   : 인사말 기능을 추가했다.", out)

    def test_all_uses_branch_diff_when_working_tree_clean(self):
        """all: 작업 트리가 깨끗하면 기준 브랜치 대비 커밋된 변경으로 만든다."""
        self.git("checkout", "-q", "-b", "feature/x")
        self.write("app.py", "print('changed')\n")
        self.git("commit", "-q", "-am", "change")
        self.call_mock.return_value = base.make_response(ALL_DATA)
        code, _, err = self.run_cli(["all"])
        self.assertEqual(code, 0, err)
        self.assertIn("(git diff main...HEAD)", self.sent_user_prompt())


class ApiKeyValidationTests(base.CliTestBase):
    def test_key_with_invisible_character_is_rejected_without_echo(self):
        """보이지 않는 문자(U+200B)가 섞인 키: 트레이스백 없이 안내 후 종료, 키 값은 출력하지 않는다."""
        bad = "sk-ant-api03-abc​def"
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": bad}):
            self.stage_change()
            code, out, err = self.run_cli(["commit"])
        self.assertEqual(code, 1)
        self.assertIn("[ERROR]", err)
        self.assertNotIn("sk-ant-api03", err + out)
        self.call_mock.assert_not_called()

    def test_key_with_embedded_newline_is_rejected(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-abc\ndef"}):
            self.stage_change()
            code, _, err = self.run_cli(["commit"])
        self.assertEqual(code, 1)
        self.assertIn("줄바꿈", err)
        self.call_mock.assert_not_called()


class SafeModeCliTests(base.CliTestBase):
    def test_context_text_is_masked_in_safe_mode(self):
        """--context 로 넣은 토큰/이메일도 safe-mode 에서 가려진다."""
        token = "ghp_" + "A1b2C3d4E5" * 4
        self.stage_change()
        # dry-run 은 call_messages 를 부르지 않으므로, 출력된 User Prompt 에서 확인한다.
        code, out, err = self.run_cli(["commit", "--dry-run", "-c", f"kim@corp.com 신고, 토큰 {token} 로 재현"])
        self.assertEqual(code, 0, err)
        self.assertNotIn(token, out)
        self.assertNotIn("kim@corp.com", out)
        self.assertIn("[MASKED:TOKEN]", out)

    def test_output_is_masked_before_length_validation(self):
        """결과 마스킹은 검증 전에 필드 단위로 적용되어, 제목이 72자를 넘지 않는다."""
        self.stage_change()
        subject = "관리자 알림 수신 주소를 a@b.io 로 변경하고 알림 발송 로직 정리 및 설정값 주석 보강"
        data = dict(base.COMMIT_DATA, scope="", subject=subject)
        self.call_mock.return_value = base.make_response(data)
        code, out, err = self.run_cli(["commit", "--plain"])
        self.assertEqual(code, 0, err)
        title = out.splitlines()[0]
        self.assertIn("[MASKED:EMAIL]", title)
        self.assertLessEqual(len(title), config.COMMIT_TITLE_MAX)

    def test_masked_private_key_in_one_bullet_keeps_other_pr_sections(self):
        """한 불릿의 PRIVATE KEY 마스킹이 나머지 PR 섹션을 지우지 않는다."""
        self.stage_change()
        data = dict(base.PR_DATA, why=["-----BEGIN RSA PRIVATE KEY----- 헤더 파싱 실패 수정"])
        self.call_mock.return_value = base.make_response(data)
        code, out, err = self.run_cli(["pr"])
        self.assertEqual(code, 0, err)
        for heading in ("## Why", "## What", "## How to Test"):
            self.assertIn(heading, out)
        self.assertIn("- app.py: greet 함수 추가", out)


class ModelOptionTests(base.CliTestBase):
    def test_effort_dropped_for_model_without_effort_support(self):
        self.stage_change()
        code, _, err = self.run_cli(["commit", "--model", "claude-haiku-4-5"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("effort", self.sent_payload()["output_config"])
        self.assertIn("effort=미전송", err)

    def test_timeout_nan_and_inf_rejected(self):
        for value in ("nan", "inf"):
            with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
                cli.build_parser().parse_args(["commit", "--timeout", value])
            self.assertEqual(ctx.exception.code, 2)

    def test_unexpected_exception_is_reported_not_traceback(self):
        self.stage_change()
        self.call_mock.side_effect = RuntimeError("boom")
        code, _, err = self.run_cli(["commit"])
        self.assertEqual(code, 1)
        self.assertIn("예상하지 못한 오류", err)
        self.assertIn("RuntimeError", err)


# ---------------------------------------------------------------------------
# ai_client
# ---------------------------------------------------------------------------
class FakeResp:
    def __init__(self, status, body=None, text=None, headers=None, reason="Error"):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else ("" if body is None else str(body))
        self.headers = headers or {}
        self.reason = reason

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _post_returning(*responses):
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs)
        item = responses[min(len(calls), len(responses)) - 1]
        if isinstance(item, BaseException):
            raise item
        return item

    post.calls = calls
    return post


def _call(post):
    return call_messages("k", {"model": "m"}, post=post, sleep=lambda s: None)


class ApiClientRobustnessTests(unittest.TestCase):
    def test_api_key_problem(self):
        self.assertIsNone(api_key_problem("sk-ant-api03-ABCdef_123-xyz"))
        self.assertIsNotNone(api_key_problem("sk-ant abc"))
        self.assertIsNotNone(api_key_problem("sk-ant‑abc"))  # 전각/특수 하이픈

    def test_error_body_with_string_error(self):
        post = _post_returning(FakeResp(403, {"error": "Forbidden by proxy"}))
        with self.assertRaises(AIClientError) as ctx:
            _call(post)
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn("Forbidden by proxy", ctx.exception.message)

    def test_success_body_that_is_a_list(self):
        with self.assertRaises(AIClientError):
            _call(_post_returning(FakeResp(200, [])))

    def test_text_block_with_null_text_is_skipped(self):
        body = {
            "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": None},
                        {"type": "text", "text": '{"a": 1}'}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        self.assertEqual(_call(_post_returning(FakeResp(200, body))).data, {"a": 1})

    def test_invalid_header_is_not_retried(self):
        post = _post_returning(requests.exceptions.InvalidHeader("bad header"))
        with self.assertRaises(AIClientError) as ctx:
            _call(post)
        self.assertEqual(ctx.exception.kind, "request")
        self.assertEqual(len(post.calls), 1)

    def test_ssl_error_is_not_retried(self):
        post = _post_returning(requests.exceptions.SSLError("cert"))
        with self.assertRaises(AIClientError):
            _call(post)
        self.assertEqual(len(post.calls), 1)

    def test_connect_timeout_reports_connection(self):
        post = _post_returning(requests.exceptions.ConnectTimeout("x"), requests.exceptions.ConnectTimeout("x"))
        with self.assertRaises(AIClientError) as ctx:
            _call(post)
        self.assertIn("연결", ctx.exception.message)
        self.assertEqual(ctx.exception.attempts, 2)

    def test_unicode_error_from_http_layer_becomes_client_error(self):
        post = _post_returning(UnicodeEncodeError("latin-1", "‑", 0, 1, "ordinal not in range"))
        with self.assertRaises(AIClientError) as ctx:
            _call(post)
        self.assertEqual(ctx.exception.kind, "request")

    def test_supports_effort(self):
        self.assertTrue(ai_client.supports_effort("claude-opus-4-6"))
        self.assertFalse(ai_client.supports_effort("claude-haiku-4-5"))
        self.assertFalse(ai_client.supports_effort("claude-sonnet-4-5"))


# ---------------------------------------------------------------------------
# git_collector
# ---------------------------------------------------------------------------
class GitCollectorFixTests(unittest.TestCase):
    def test_unborn_branch_with_upstream(self):
        status = parse_status("## No commits yet on main...origin/main [gone]\0A  a.txt\0")
        self.assertEqual(status.branch, "main")

    def test_cp949_line_is_decoded(self):
        raw = "+// 정상 UTF-8\n".encode("utf-8") + "+// 로그인 재시도\n".encode("cp949")
        text = decode_git_output(raw)
        self.assertIn("정상 UTF-8", text)
        self.assertIn("로그인 재시도", text)

    def test_crlf_content_is_normalized(self):
        raw = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-x\r\n+y\r\n"
        f = parse_diff(raw)[0]
        self.assertEqual((f.additions, f.deletions), (1, 1))
        self.assertFalse(any(line.endswith("\r") for line in f.lines))


class PrBaseSelectionTests(base.CliTestBase):
    def test_does_not_fall_through_to_master_when_main_exists(self):
        """main 이 있지만 커밋된 변경이 없으면, 오래된 master 와 비교하지 않고 작업 트리 변경을 쓴다."""
        self.git("branch", "master")  # 오래된 master
        self.write("extra.txt", "x\n")
        self.git("add", "extra.txt")
        self.git("commit", "-q", "-m", "main only work")
        self.git("checkout", "-q", "-b", "feature/y")
        self.write("app.py", "print('wip')\n")
        self.call_mock.return_value = base.make_response(base.PR_DATA)
        code, _, err = self.run_cli(["pr"])
        self.assertEqual(code, 0, err)
        prompt = self.sent_user_prompt()
        self.assertNotIn("master...HEAD", prompt)
        self.assertIn("(git diff)", prompt)

    def test_diff_parsing_ignores_user_noprefix_config(self):
        """사용자가 diff.noprefix=true 를 설정해도 경로가 올바르게 파싱된다."""
        self.git("config", "diff.noprefix", "true")
        self.stage_change()
        code, out, err = self.run_cli(["commit", "--dry-run"])
        self.assertEqual(code, 0, err)
        self.assertIn("[수정] app.py (+", out)


# ---------------------------------------------------------------------------
# safe_mode
# ---------------------------------------------------------------------------
class SafeModeFixTests(unittest.TestCase):
    def test_renamed_env_file_is_excluded(self):
        f = FileDiff(path="env.backup", old_path=".env", change="R", lines=["+DB_PASS=hunter2"])
        prepared, report = prepare_files([f], safe_mode=True)
        self.assertEqual(prepared[0].text, "")
        self.assertEqual(report.excluded_sensitive, ["env.backup"])

    def test_more_sensitive_file_names(self):
        for path in ("prod.env", "config/app.env", ".envrc", ".git-credentials", ".pgpass",
                     "infra/terraform.tfstate", "home/.aws/credentials", ".docker/config.json"):
            self.assertTrue(is_sensitive_file(path), path)
        for path in (".env.example", "app.env.sample", "src/main.py"):
            self.assertFalse(is_sensitive_file(path), path)

    def test_code_references_are_not_masked(self):
        for line in ("+    self.api_key = api_key", "+    max_tokens = DEFAULT_MAX_TOKENS",
                     "+    token = get_token()", "+    api_key: Optional[str] = None", "+  password: None"):
            self.assertEqual(mask_text(line)[1], {}, line)

    def test_long_line_masking_is_fast(self):
        line = "+" + "a0." * 15000  # 약 45KB 한 줄
        started = time.monotonic()
        mask_text(line)
        self.assertLess(time.monotonic() - started, 5.0)

    def test_mask_data_masks_each_field(self):
        data = {"subject": "a@b.io 변경", "body": ["ok", "token ghp_" + "x" * 36], "n": 1}
        masked, count = mask_data(data)
        self.assertEqual(count, 2)
        self.assertEqual(masked["n"], 1)
        self.assertIn("[MASKED:EMAIL]", masked["subject"])
        self.assertEqual(masked["body"][0], "ok")


class FinalRoundMaskingTests(unittest.TestCase):
    """최종 검증에서 확인된 오탐/누락 사례."""

    SHOULD_MASK = [
        "+MYSQL_ROOT_PASSWORD=Xk9;mP2!vQ",  # 특수문자가 섞인 .env 값
        "+  - POSTGRES_PASSWORD=pg(2024)secure",
        "+API_TOKEN=abc,def,ghi123",
        "+DB_PASSWORD=Sup3r#S3cret!",  # 공백 없는 '#' 은 값의 일부
        "+secret: my-secret",
        "+jwt.secret=supersecret",
        "+ADMIN_PASSWORD=ADMIN_2024",
        '+AUTH = "https://a.example.com/oauth/token?client_id=web&client_secret=9f8e7d6c5b4a3928"',
        '+    r = s.post(url + "?grant_type=refresh_token&refresh_token=1a2b3c4d5e6f7a8b9c0d")',
        "+curl -H 'Authorization: Basic dXNlcjpwYXNzd29yZA=='",
    ]
    SHOULD_KEEP = [
        "+    # Use basic authentication for the admin API",
        "+## Basic Configuration",
        "+  password: string",
        "+  password: String!",
        "+    token: bytes",
        "+    password: SecretStr",
        "+tokenizer: bert-base-uncased",
        "+token_url: https://oauth2.googleapis.com/token",
        '+SECRET_KEY = os.environ["SECRET_KEY"]',
        "+    password: hashPassword(pw),",
        "+  fetch(`/api?apiKey=${apiKey}`)",
    ]

    def test_should_mask(self):
        for line in self.SHOULD_MASK:
            self.assertTrue(mask_text(line)[1], line)

    def test_should_keep(self):
        for line in self.SHOULD_KEEP:
            self.assertEqual(mask_text(line)[1], {}, line)

    def test_comment_after_value_is_kept(self):
        self.assertEqual(mask_text("+DB_PASSWORD=abc123  # 운영 비밀번호")[0], "+DB_PASSWORD=[MASKED:SECRET]  # 운영 비밀번호")

    def test_ai_output_words_are_not_masked(self):
        data = {"subject": "Basic Authentication 지원 추가", "body": ["token.py: 리팩터링", "secret_store.py: 리팩터링"]}
        self.assertEqual(mask_data(data), (data, 0))

    def test_changed_file_paths_are_masked(self):
        report = SafeModeReport(enabled=True)
        files = [PreparedFile(source=FileDiff(path="docs/kim@corp.com.md", change="A", additions=1))]
        text = prompts.build_user_prompt("commit", "main", "x", "git diff", files, report)
        self.assertNotIn("kim@corp.com", text)
        self.assertEqual(report.masked.get("EMAIL"), 1)


class FinalRoundCliTests(base.CliTestBase):
    def test_all_plain_has_section_labels(self):
        self.stage_change()
        self.call_mock.return_value = base.make_response(ALL_DATA)
        code, out, err = self.run_cli(["all", "--plain"])
        self.assertEqual(code, 0, err)
        for label in ("[Commit Message]", "[PR Title]", "[PR Body]"):
            self.assertIn(label, out)
        self.assertNotIn("검증 결과", out)


class FinalRoundMiscTests(unittest.TestCase):
    def test_proxy_url_error_hint_mentions_proxy(self):
        post = _post_returning(requests.exceptions.InvalidProxyURL("bad proxy"))
        with self.assertRaises(AIClientError) as ctx:
            _call(post)
        self.assertIn("PROXY", ctx.exception.hint)
        self.assertEqual(len(post.calls), 1)

    def test_windows_pipe_uses_console_codepage(self):
        """PowerShell/cmd 에서 파이프로 출력하면 콘솔 출력 코드페이지(예: 65001)로 인코딩한다."""
        from gitgen import console

        class FakeStream:
            def __init__(self):
                self.calls = []

            def isatty(self):
                return False

            def reconfigure(self, **kwargs):
                self.calls.append(kwargs)

        out, err = FakeStream(), FakeStream()
        env = {k: v for k, v in os.environ.items() if k not in ("MSYSTEM", "TERM", "PYTHONIOENCODING", "PYTHONUTF8")}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(console.sys, "platform", "win32"), \
                mock.patch.object(console.sys, "stdout", out), \
                mock.patch.object(console.sys, "stderr", err), \
                mock.patch.object(console, "_console_output_codepage", return_value=65001):
            console.setup_stdio()
        self.assertEqual(out.calls, [{"encoding": "cp65001", "errors": "replace"}])


# ---------------------------------------------------------------------------
# formatter / prompts
# ---------------------------------------------------------------------------
class FormatterFixTests(unittest.TestCase):
    def test_three_generic_bullets_without_file_mention_trimmed_to_two(self):
        data = {"type": "fix", "scope": "", "subject": "로그인 오류 수정",
                "body": ["입력 검증 추가", "에러 메시지 개선", "재시도 로직 추가"]}
        message, checks = formatter.build_commit_message(data, ["auth/login.py", "auth/errors.py"])
        self.assertEqual(len(message.bullets), 2)
        self.assertTrue(any("2개로 줄임" in c.message for c in checks))

    def test_validate_warns_when_body_has_no_file_and_many_bullets(self):
        text = "fix: 오류 수정\n\n- 하나\n- 둘\n- 셋"
        checks = formatter.validate_commit_text(text, ["auth/login.py"])
        self.assertTrue(any(c.level == formatter.WARN for c in checks))


class PromptFixTests(unittest.TestCase):
    def test_changed_files_list_is_capped(self):
        files = [PreparedFile(source=FileDiff(path=f"node_modules/p{i}.js", change="A", additions=1))
                 for i in range(prompts.MAX_LISTED_FILES + 30)]
        text = prompts.build_user_prompt("commit", "main", "x", "git diff", files, SafeModeReport(enabled=True))
        self.assertEqual(text.count("- [추가] node_modules/"), prompts.MAX_LISTED_FILES)
        self.assertIn("외 30개 파일", text)
        self.assertIn(f'count="{prompts.MAX_LISTED_FILES + 30}"', text)

    def test_all_schema_has_nested_commit_and_pr(self):
        schema = prompts.schema_for("all")
        self.assertEqual(schema["required"], ["summary", "commit", "pr"])
        self.assertFalse(schema["properties"]["commit"]["additionalProperties"])
        self.assertIn("how_to_test", schema["properties"]["pr"]["properties"])


if __name__ == "__main__":
    unittest.main()
