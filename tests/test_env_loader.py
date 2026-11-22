""".env 파일 → 환경변수 로딩 테스트.

- gitgen.env_loader.load_env_file 단위 테스트 (임시 .env 파일 사용)
- CLI 에서 .env 의 API Key 가 실제로 쓰이는지, OS 환경변수가 우선하는지, 안내 문구가 맞는지

프로젝트 폴더의 실제 .env 는 읽지 않는다(경로를 임시 폴더로 바꿔서 테스트). 실제 API 호출도 없다.

실행: python -m unittest discover -s tests -v
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from gitgen import config
from gitgen.env_loader import load_env_file

try:  # discover -s tests 로 실행하면 최상위 모듈, -m unittest tests.xxx 로 실행하면 패키지 모듈
    from tests import test_cli as base
except ImportError:  # pragma: no cover
    import test_cli as base


def setUpModule():
    base.setUpModule()


def tearDownModule():
    base.tearDownModule()


KEY_NAME = "GITGEN_TEST_ONLY_KEY"  # 실제 환경과 겹치지 않는 테스트 전용 변수 이름


class LoadEnvFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gitgen_env_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, ".env")
        env_patch = mock.patch.dict(os.environ)  # 테스트가 끝나면 환경변수를 원래대로 되돌린다
        env_patch.start()
        self.addCleanup(env_patch.stop)
        os.environ.pop(KEY_NAME, None)

    def write(self, content, encoding="utf-8"):
        with open(self.path, "w", encoding=encoding, newline="\n") as fh:
            fh.write(content)

    def test_missing_file_is_a_no_op(self):
        result = load_env_file(self.path)
        self.assertFalse(result.found)
        self.assertEqual((result.loaded, result.skipped, result.error), ([], [], None))

    def test_values_are_registered_as_environment_variables(self):
        self.write(f"# 주석\n{KEY_NAME}=sk-test-123\n")
        result = load_env_file(self.path)
        self.assertTrue(result.found)
        self.assertEqual(result.loaded, [KEY_NAME])
        self.assertEqual(os.environ[KEY_NAME], "sk-test-123")

    def test_existing_environment_variable_wins(self):
        os.environ[KEY_NAME] = "from-os"
        self.write(f"{KEY_NAME}=from-dotenv\n")
        result = load_env_file(self.path)
        self.assertEqual(result.skipped, [KEY_NAME])
        self.assertEqual(os.environ[KEY_NAME], "from-os")

    def test_quotes_export_and_inline_comment(self):
        self.write(f'export {KEY_NAME}="sk-quoted"  # 주석\n')
        load_env_file(self.path)
        self.assertEqual(os.environ[KEY_NAME], "sk-quoted")

    def test_utf8_bom_does_not_break_first_name(self):
        """메모장이 붙이는 BOM 이 있어도 첫 줄 변수 이름이 그대로 인식된다."""
        self.write(f"{KEY_NAME}=sk-bom\n", encoding="utf-8-sig")
        load_env_file(self.path)
        self.assertEqual(os.environ.get(KEY_NAME), "sk-bom")

    def test_empty_value_is_not_loaded(self):
        self.write(f"{KEY_NAME}=\n")
        result = load_env_file(self.path)
        self.assertEqual(result.loaded, [])
        self.assertNotIn(KEY_NAME, os.environ)

    def test_non_utf8_file_reports_error(self):
        self.write(f"# 한글 주석\n{KEY_NAME}=x\n", encoding="cp949")
        result = load_env_file(self.path)
        self.assertIsNotNone(result.error)
        self.assertIn("UTF-8", result.error)

    def test_missing_python_dotenv_reports_error(self):
        self.write(f"{KEY_NAME}=x\n")
        with mock.patch.dict(sys.modules, {"dotenv": None}):
            result = load_env_file(self.path)
        self.assertIn("python-dotenv", result.error)
        self.assertNotIn(KEY_NAME, os.environ)

    def test_default_location_is_project_folder(self):
        """기본 .env 위치는 main.py 가 있는 ai-gitgen 폴더다."""
        project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertEqual(os.path.normcase(config.ENV_FILE), os.path.normcase(os.path.join(project, ".env")))
        self.assertTrue(os.path.isfile(os.path.join(project, "main.py")))


class CliEnvFileTests(base.CliTestBase):
    """CliTestBase 는 .env 위치를 self.env_file(임시 폴더)로 바꿔 둔다."""

    def write_env(self, content):
        with open(self.env_file, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)

    def test_key_from_env_file_is_used(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.write_env("ANTHROPIC_API_KEY=dotenv-test-key\n")
        self.stage_change()
        code, out, err = self.run_cli(["commit"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.call_mock.call_args[0][0], "dotenv-test-key")
        self.assertIn(".env 파일에서 환경변수 1개를 불러왔습니다 (ANTHROPIC_API_KEY)", err)
        self.assertNotIn("dotenv-test-key", out + err)  # 키 값은 출력하지 않는다

    def test_os_environment_variable_takes_precedence(self):
        self.write_env("ANTHROPIC_API_KEY=dotenv-test-key\n")
        self.stage_change()
        code, _, err = self.run_cli(["commit"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.call_mock.call_args[0][0], base.DUMMY_KEY)
        self.assertIn("덮어쓰지 않습니다", err)

    def test_no_env_file_and_no_variable_explains_both_ways(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.stage_change()
        code, _, err = self.run_cli(["commit"])
        self.assertEqual(code, 1)
        self.assertIn(".env 파일이 없습니다", err)
        self.assertIn("cp .env.example .env", err)
        self.assertIn("export ANTHROPIC_API_KEY", err)
        self.call_mock.assert_not_called()

    def test_env_file_without_key_is_reported(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.write_env("OTHER_SETTING=1\n")
        self.stage_change()
        code, _, err = self.run_cli(["commit"])
        self.assertEqual(code, 1)
        self.assertIn(".env 파일은 찾았지만 API Key 항목이 없습니다", err)

    def test_placeholder_key_is_rejected(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.write_env("ANTHROPIC_API_KEY=YOUR_KEY\n")
        self.stage_change()
        code, _, err = self.run_cli(["commit"])
        self.assertEqual(code, 1)
        self.assertIn("예시 값", err)
        self.call_mock.assert_not_called()

    # --- 리뷰에서 확인된 문제의 회귀 테스트 ------------------------------------
    def test_os_ai_api_key_beats_dotenv_anthropic_key(self):
        """OS 에 AI_API_KEY 만 있어도 API Key 는 OS 값이 우선한다(이름이 달라도 한 묶음)."""
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ["AI_API_KEY"] = "os-ai-key"
        self.write_env("ANTHROPIC_API_KEY=dotenv-test-key\n")
        self.stage_change()
        code, _, err = self.run_cli(["commit"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.call_mock.call_args[0][0], "os-ai-key")
        self.assertIn("덮어쓰지 않습니다", err)

    def test_os_ai_api_key_with_copied_example_still_works(self):
        """.env.example 을 그대로 복사해 둔 상태(YOUR_KEY)여도 OS 의 AI_API_KEY 로 실행된다."""
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ["AI_API_KEY"] = "os-ai-key"
        self.write_env("ANTHROPIC_API_KEY=YOUR_KEY\n")
        self.stage_change()
        code, _, err = self.run_cli(["commit"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.call_mock.call_args[0][0], "os-ai-key")

    def test_blank_os_variable_is_filled_from_dotenv(self):
        os.environ["ANTHROPIC_API_KEY"] = "   "
        self.write_env("ANTHROPIC_API_KEY=dotenv-test-key\n")
        self.stage_change()
        code, _, err = self.run_cli(["commit"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.call_mock.call_args[0][0], "dotenv-test-key")

    def test_unreadable_env_file_is_not_reported_as_missing(self):
        """UTF-16 으로 저장된 .env (PowerShell 5.1 의 '>' 결과)는 '없다'가 아니라 '읽지 못했다'로 안내한다."""
        os.environ.pop("ANTHROPIC_API_KEY", None)
        with open(self.env_file, "w", encoding="utf-16") as fh:
            fh.write("ANTHROPIC_API_KEY=dotenv-test-key\n")
        self.stage_change()
        code, _, err = self.run_cli(["commit"])
        self.assertEqual(code, 1)
        self.assertIn(".env 파일을 읽지 못했습니다", err)
        self.assertNotIn(".env 파일이 없습니다", err)

    def test_hint_names_the_tool_folder(self):
        """다른 프로젝트에서 실행해도 'cp .env.example .env' 를 어느 폴더에서 할지 알려 준다."""
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.stage_change()
        _, _, err = self.run_cli(["commit"])
        self.assertIn(f"ai-gitgen 폴더({config.PROJECT_DIR})", err)

    def test_env_backup_copies_are_sensitive_files(self):
        from gitgen.safe_mode import is_sensitive_file

        for name in (".env~", ".env copy", ".env - Copy", ".env.bak", ".env.local"):
            self.assertTrue(is_sensitive_file(name), name)
        self.assertFalse(is_sensitive_file(".env.example"))

    def test_example_file_in_repository_has_no_real_key(self):
        """저장소에 올라가는 .env.example 에는 예시 값만 있어야 한다."""
        project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(project, ".env.example"), encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("ANTHROPIC_API_KEY=YOUR_KEY", content)
        self.assertNotIn("sk-ant-", content)


if __name__ == "__main__":
    unittest.main()
