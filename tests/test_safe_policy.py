"""safe-mode 정책(보너스 3: 안전 모드 고도화) 테스트.

- 정규식 기반 사용자 마스킹 규칙 추가 (설정 파일 / --mask-pattern)
- diff 전송 제한 정책(파일 수 / 전체 줄 수 / 파일당 줄 수) 조정
- 추가 제외 파일, 내장 규칙 끄기, 설정 오류 검증, 우선순위(기본값 ← 설정 파일 ← CLI)
- 정책을 지정하지 않으면 고도화 전과 똑같이 동작하는지(하위 호환)

실제 API 는 호출하지 않는다. 실행: python -m unittest discover -s tests -v
"""

import json
import os
import shutil
import tempfile
import unittest

from gitgen import config
from gitgen.git_collector import FileDiff
from gitgen.safe_mode import mask_data, mask_text, prepare_files
from gitgen.safe_policy import (
    POLICY_FILE_NAME,
    SOURCE_CLI,
    SOURCE_DEFAULT,
    SOURCE_FILE,
    PolicyError,
    default_policy,
    load_policy,
)

try:  # discover -s tests 로 실행하면 최상위 모듈, -m unittest tests.xxx 로 실행하면 패키지 모듈
    from tests import test_cli as base
except ImportError:  # pragma: no cover
    import test_cli as base


def setUpModule():
    base.setUpModule()


def tearDownModule():
    base.tearDownModule()


def _diff(path, n_lines, prefix="+line", old_path=None, change="M"):
    lines = [f"diff --git a/{path} b/{path}", "@@ -0,0 +1 @@"] + [f"{prefix} {i}" for i in range(n_lines - 2)]
    return FileDiff(path=path, old_path=old_path, change=change, additions=n_lines - 2, lines=lines)


class PolicyTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gitgen_policy_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write_policy(self, section, name=POLICY_FILE_NAME, raw=None):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(raw if raw is not None else json.dumps({"safe_mode": section}, ensure_ascii=False))
        return path


# ---------------------------------------------------------------------------
# 불러오기와 우선순위
# ---------------------------------------------------------------------------
class LoadPolicyTests(PolicyTestBase):
    def test_no_file_no_options_is_default_policy(self):
        policy = load_policy(cwd=self.tmp)
        self.assertTrue(policy.is_default())
        self.assertIsNone(policy.config_path)
        self.assertEqual((policy.max_files, policy.max_lines, policy.max_lines_per_file),
                         (config.SAFE_MAX_FILES, config.SAFE_MAX_LINES, 0))
        self.assertEqual(policy.source_of("max_files"), SOURCE_DEFAULT)

    def test_file_values_are_applied_with_source(self):
        self.write_policy({
            "max_files": 5, "max_lines": 120, "max_lines_per_file": 40,
            "exclude_files": ["*.sql"], "disable_rules": ["KR_PHONE"],
            "mask_rules": [{"name": "employee_id", "pattern": r"\bEMP-\d{6}\b", "label": "EMPLOYEE"}],
        })
        policy = load_policy(cwd=self.tmp)
        self.assertEqual((policy.max_files, policy.max_lines, policy.max_lines_per_file), (5, 120, 40))
        self.assertEqual(policy.exclude_files, ["*.sql"])
        self.assertEqual(policy.disabled_rules, ["KR_PHONE"])
        self.assertEqual([r.name for r in policy.custom_rules], ["EMPLOYEE_ID"])  # 이름은 대문자로 정규화
        self.assertEqual(policy.custom_rules[0].label, "EMPLOYEE")
        self.assertEqual(policy.source_of("max_lines"), SOURCE_FILE)
        self.assertFalse(policy.is_default())

    def test_cli_options_override_file(self):
        self.write_policy({"max_files": 5, "max_lines": 120, "exclude_files": ["*.sql"]})
        policy = load_policy(cwd=self.tmp, max_lines=30, mask_patterns=[r"TICKET=JIRA-\d+"], exclude_files=["*.csv"])
        self.assertEqual(policy.max_files, 5)
        self.assertEqual(policy.max_lines, 30)
        self.assertEqual(policy.source_of("max_lines"), SOURCE_CLI)
        self.assertEqual(policy.exclude_files, ["*.sql", "*.csv"])
        self.assertEqual(policy.custom_rules[0].name, "TICKET")

    def test_mask_pattern_requires_explicit_name(self):
        """'NAME=REGEX' 형식이 아니면 모호하게 해석하지 않고 오류로 알린다."""
        for spec in (r"JIRA-\d+", r"internal-host=[\w-]+\.corp\.local", r"사번=EMP-\d{6}", r"1ID=EMP-\d{6}"):
            with self.assertRaises(PolicyError, msg=spec) as ctx:
                load_policy(cwd=self.tmp, mask_patterns=[spec])
            self.assertIn("NAME=REGEX", str(ctx.exception))

    def test_explicit_config_path(self):
        path = self.write_policy({"max_files": 2}, name="team-policy.json")
        policy = load_policy(cwd=self.tmp, config_path=path)
        self.assertEqual(policy.max_files, 2)

    def test_file_without_safe_mode_section_is_fine(self):
        self.write_policy(None, raw=json.dumps({"convention": {"types": ["feat"]}}))
        policy = load_policy(cwd=self.tmp)
        self.assertEqual(policy.max_files, config.SAFE_MAX_FILES)

    def test_utf8_bom_is_accepted(self):
        path = os.path.join(self.tmp, POLICY_FILE_NAME)
        with open(path, "w", encoding="utf-8-sig") as fh:
            json.dump({"safe_mode": {"max_lines": 77}}, fh)
        self.assertEqual(load_policy(cwd=self.tmp).max_lines, 77)


class PolicyErrorTests(PolicyTestBase):
    def assert_policy_error(self, contains, **kwargs):
        with self.assertRaises(PolicyError) as ctx:
            load_policy(cwd=self.tmp, **kwargs)
        self.assertIn(contains, str(ctx.exception))

    def test_invalid_json(self):
        self.write_policy(None, raw='{"safe_mode": {"max_files": 5,}}')
        self.assert_policy_error("JSON 형식 오류")

    def test_unknown_key(self):
        self.write_policy({"max_file": 5})
        self.assert_policy_error("알 수 없는 항목 max_file")

    def test_number_type_and_range(self):
        for value, message in (("5", "정수여야"), (True, "정수여야"), (0, "1 ~ 1000"), (100000, "1 ~ 1000")):
            self.write_policy({"max_files": value})
            self.assert_policy_error(message)

    def test_per_file_zero_is_allowed_but_negative_is_not(self):
        self.write_policy({"max_lines_per_file": 0})
        self.assertEqual(load_policy(cwd=self.tmp).max_lines_per_file, 0)
        self.write_policy({"max_lines_per_file": -1})
        self.assert_policy_error("0 ~ 20000")

    def test_bad_regex(self):
        self.write_policy({"mask_rules": [{"name": "BAD", "pattern": "(abc"}]})
        self.assert_policy_error("정규식 오류")

    def test_regex_matching_empty_or_zero_width(self):
        for pattern in ("a*", r"\b", r"(?=\d)", r"x?\b"):
            self.write_policy({"mask_rules": [{"name": "ZW", "pattern": pattern}]})
            self.assert_policy_error("길이 0")

    def test_anchors_without_multiline_are_rejected(self):
        for pattern in (r"^INTERNAL_URL=.*", r"\d{6}$", r"(?i)^secret"):
            self.write_policy({"mask_rules": [{"name": "ANCHOR", "pattern": pattern}]})
            self.assert_policy_error("(?m)")
        self.write_policy({"mask_rules": [{"name": "OK", "pattern": r"(?m)^\+INTERNAL_URL=.*"}]})
        self.assertEqual(len(load_policy(cwd=self.tmp).custom_rules), 1)
        self.write_policy({"mask_rules": [{"name": "DOLLAR", "pattern": r"PRICE\$"}]})  # 이스케이프된 $ 는 허용
        self.assertEqual(len(load_policy(cwd=self.tmp).custom_rules), 1)

    def test_label_validation(self):
        for label in ("A\nB", "a@b.co", "x" * 41, ""):
            self.write_policy({"mask_rules": [{"name": "L", "pattern": "EMP-1", "label": label}]})
            self.assert_policy_error("label")

    def test_huge_repetition_is_policy_error(self):
        self.assert_policy_error("정규식 오류", mask_patterns=["BIG=a{4294967296}"])

    def test_keys_outside_section_or_misspelled_section(self):
        self.write_policy(None, raw=json.dumps({"exclude_files": ["*.sql"], "max_files": 3}))
        self.assert_policy_error('"safe_mode": {...} 안에')
        self.write_policy(None, raw=json.dumps({"safe-mode": {"exclude_files": ["*.sql"]}}))
        self.assert_policy_error("safe-mode")

    def test_builtin_name_clash_and_duplicate(self):
        self.write_policy({"mask_rules": [{"name": "EMAIL", "pattern": "x@y"}]})
        self.assert_policy_error("내장 규칙 이름")
        self.write_policy({"mask_rules": [{"name": "A", "pattern": "x1"}, {"name": "a", "pattern": "x2"}]})
        self.assert_policy_error("중복")

    def test_unknown_disable_rule(self):
        self.write_policy({"disable_rules": ["NOPE"]})
        self.assert_policy_error("알 수 없는 내장 규칙 NOPE")

    def test_rule_missing_fields_and_extra_keys(self):
        self.write_policy({"mask_rules": [{"name": "A"}]})
        self.assert_policy_error("name 과 pattern")
        self.write_policy({"mask_rules": [{"name": "A", "pattern": "x1", "flags": "i"}]})
        self.assert_policy_error("알 수 없는 항목 flags")

    def test_exclude_files_must_be_string_list(self):
        self.write_policy({"exclude_files": "*.sql"})
        self.assert_policy_error("문자열의 배열")

    def test_missing_explicit_config(self):
        self.assert_policy_error("찾을 수 없습니다", config_path=os.path.join(self.tmp, "nope.json"))


# ---------------------------------------------------------------------------
# 마스킹 규칙 확장
# ---------------------------------------------------------------------------
class CustomMaskingTests(PolicyTestBase):
    def test_custom_rule_masks_and_counts(self):
        policy = load_policy(cwd=self.tmp, mask_patterns=[r"EMPLOYEE_ID=\bEMP-\d{6}\b"])
        text, counts = mask_text("담당: EMP-123456", policy)
        self.assertEqual(text, "담당: [MASKED:EMPLOYEE_ID]")
        self.assertEqual(counts, {"EMPLOYEE_ID": 1})

    def test_value_group_masks_only_value(self):
        policy = load_policy(cwd=self.tmp, mask_patterns=[r"DB_HOST=DB_HOST=(?P<value>\S+)"])
        self.assertEqual(mask_text("+DB_HOST=db1.internal", policy)[0], "+DB_HOST=[MASKED:DB_HOST]")

    def test_custom_rules_never_reduce_builtin_protection(self):
        """사용자 규칙은 내장 규칙 '다음에' 적용된다: 규칙을 추가해서 기존에 가려지던 값이 드러나면 안 된다."""
        cases = [
            ("CODENAME=(?i)acme", "+ACME_DB_PASSWORD=hunter2xyz", "hunter2xyz"),
            ("CODENAME=(?i)acme", "+export ACME_API_KEY=abcd1234efgh", "abcd1234efgh"),
            ("SHOP=shoptok_[0-9a-f]{12}", "+SHOP_TOKEN=shoptok_0123456789abZZsecretTAILvalue", "ZZsecretTAILvalue"),
            ("CORP=corp\\.example\\.com", "alice@corp.example.com", "alice"),
            ("INTERNAL_HOST=\\b[\\w-]+\\.corp\\.example\\.com\\b", "+notify: alice@mail.corp.example.com", "alice"),
        ]
        for spec, line, secret in cases:
            policy = load_policy(cwd=self.tmp, mask_patterns=[spec])
            default_masked = mask_text(line)[0]
            custom_masked = mask_text(line, policy)[0]
            self.assertNotIn(secret, default_masked, line)
            self.assertNotIn(secret, custom_masked, f"{spec} 추가 후 {secret!r} 가 드러남: {custom_masked}")

    def test_custom_rule_adds_on_top_of_builtin(self):
        policy = load_policy(cwd=self.tmp, mask_patterns=[r"CODENAME=(?i)acme"])
        text, counts = mask_text("+ACME_DB_PASSWORD=hunter2xyz  # acme 출시", policy)
        self.assertEqual(text, "+[MASKED:CODENAME]_DB_PASSWORD=[MASKED:SECRET]  # [MASKED:CODENAME] 출시")
        self.assertEqual(counts, {"ENV_SECRET": 1, "CODENAME": 2})

    def test_value_group_inside_lookaround_does_not_garble(self):
        policy = load_policy(cwd=self.tmp, mask_patterns=[r"TOK=svc_token(?=:\s*(?P<value>\S+))"])
        text = mask_text("+svc_token: Zx9secretVALUE", policy)[0]
        self.assertNotIn("Zx9secretVALUE", text)  # 내장 ENV_SECRET 이 먼저 값을 가린다
        self.assertNotIn("[MASKED:TOK]:", text.replace("[MASKED:TOK]: [MASKED:SECRET]", ""))

    def test_disable_builtin_rule(self):
        self.write_policy({"disable_rules": ["KR_PHONE"]})
        policy = load_policy(cwd=self.tmp)
        self.assertEqual(mask_text("010-1234-5678", policy), ("010-1234-5678", {}))
        self.assertTrue(mask_text("010-1234-5678")[1])  # 기본 정책에서는 가려진다

    def test_case_insensitive_flag_in_pattern(self):
        policy = load_policy(cwd=self.tmp, mask_patterns=[r"PROJECT=(?i)\bproject-x\b"])
        self.assertEqual(mask_text("PROJECT-X 출시", policy)[1], {"PROJECT": 1})

    def test_mask_data_uses_policy(self):
        policy = load_policy(cwd=self.tmp, mask_patterns=[r"TICKET=JIRA-\d+"])
        data, n = mask_data({"subject": "JIRA-42 해결", "body": ["JIRA-43 관련"]}, policy)
        self.assertEqual(n, 2)
        self.assertEqual(data["subject"], "[MASKED:TICKET] 해결")


# ---------------------------------------------------------------------------
# 전송 제한 조정 + 제외 파일
# ---------------------------------------------------------------------------
class TransmissionLimitTests(PolicyTestBase):
    def test_default_policy_matches_previous_behavior(self):
        """정책 없이 호출한 결과와 기본 정책 결과가 같다(하위 호환)."""
        files = [_diff(f"f{i}.py", 50) for i in range(12)]
        old_prepared, old_report = prepare_files(files, True)
        new_prepared, new_report = prepare_files(files, True, default_policy())
        self.assertEqual([p.text for p in old_prepared], [p.text for p in new_prepared])
        self.assertEqual(old_report.sent_lines, new_report.sent_lines)
        self.assertEqual(new_report.sent_lines, config.SAFE_MAX_LINES)
        self.assertEqual(len(new_report.skipped_over_file_limit), 2)

    def test_max_files_and_max_lines(self):
        policy = load_policy(cwd=self.tmp, max_files=3, max_lines=60)
        files = [_diff(f"f{i}.py", 50) for i in range(5)]
        prepared, report = prepare_files(files, True, policy)
        self.assertEqual(report.skipped_over_file_limit, ["f3.py", "f4.py"])
        self.assertEqual(report.sent_lines, 60)
        self.assertEqual([p.sent_lines for p in prepared], [20, 20, 20, 0, 0])
        self.assertIn("파일 수 제한(3개)", prepared[3].note)

    def test_max_lines_per_file_caps_each_file(self):
        policy = load_policy(cwd=self.tmp, max_lines_per_file=15)
        files = [_diff("small.py", 10), _diff("big.py", 300)]
        prepared, report = prepare_files(files, True, policy)
        self.assertEqual([p.sent_lines for p in prepared], [10, 15])
        self.assertIn("300줄 중 15줄만 전송", prepared[1].note)

    def test_exclude_files_by_name_path_and_old_path(self):
        policy = load_policy(cwd=self.tmp, exclude_files=["*.sql", "fixtures/customers/*"])
        files = [
            _diff("db/dump.sql", 10),
            _diff("fixtures/customers/kim.json", 10),
            _diff("renamed.txt", 10, old_path="seed.sql", change="R"),
            _diff("app.py", 10),
        ]
        prepared, report = prepare_files(files, True, policy)
        self.assertEqual(report.excluded_by_policy, ["db/dump.sql", "fixtures/customers/kim.json", "renamed.txt"])
        self.assertEqual([bool(p.text) for p in prepared], [False, False, False, True])
        self.assertIn("사용자 정책(exclude_files)", prepared[0].note)

    def test_gitignore_style_exclude_patterns(self):
        from gitgen.safe_policy import match_exclude

        cases = [
            ("**/*.sql", "dump.sql", True), ("**/*.sql", "db/dump.sql", True),
            ("secrets/", "secrets/prod.yaml", True), ("secrets/", "app/secrets/x.yaml", True),
            ("secrets/", "secrets", False),
            ("fixtures/customers/", "fixtures/customers/a.csv", True),
            ("./db/*", "db/seed.sql", True), ("/db/*", "db/seed.sql", True),
            ("db/*", "other/db/seed.sql", False),  # 슬래시가 있는 패턴은 루트 기준
            ("customers", "data/customers/list.csv", True),
            ("*.SQL", "Db/Dump.sql", True),  # 대소문자 무시
            ("*.sql", "app/main.py", False),
        ]
        for pattern, path, expected in cases:
            self.assertEqual(match_exclude(path, pattern), expected, f"{pattern!r} vs {path!r}")

    def test_unmatched_exclude_patterns_are_reported(self):
        policy = load_policy(cwd=self.tmp, exclude_files=["*.sql", "*.sqll"])
        self.assertEqual(policy.unmatched_excludes(["db/a.sql", "app.py"]), ["*.sqll"])

    def test_policy_file_itself_is_not_sent(self):
        policy = load_policy(cwd=self.tmp, mask_patterns=[r"HOST=\bcorp\.example\.com\b"])
        files = [_diff(POLICY_FILE_NAME, 10, prefix='+"pattern": "\\\\bcorp\\\\.example\\\\.com"')]
        prepared, report = prepare_files(files, True, policy)
        self.assertEqual(prepared[0].text, "")
        self.assertIn("정책 파일", prepared[0].note)

    def test_policy_is_ignored_when_safe_mode_off(self):
        policy = load_policy(cwd=self.tmp, max_files=1, exclude_files=["*.py"], mask_patterns=[r"SECRET_WORD=line"])
        files = [_diff("a.py", 10), _diff("b.py", 10)]
        prepared, report = prepare_files(files, False, policy)
        self.assertEqual(report.sent_lines, 20)
        self.assertEqual(report.masked, {})
        self.assertIsNone(report.policy)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
SAMPLE_POLICY = {
    "max_files": 5,
    "max_lines": 120,
    "max_lines_per_file": 40,
    "exclude_files": ["*.sql"],
    "mask_rules": [
        {"name": "EMPLOYEE_ID", "pattern": r"\bEMP-\d{6}\b"},
        {"name": "INTERNAL_HOST", "pattern": r"\b[\w-]+\.corp\.example\.com\b"},
    ],
}


class CliPolicyTests(base.CliTestBase):
    def write_repo_policy(self, section):
        self.write(POLICY_FILE_NAME, json.dumps({"safe_mode": section}))

    def stage_sensitive_change(self):
        self.write("app.py", "OWNER = 'EMP-123456'\nHOST = 'db1.corp.example.com'\n")
        self.write("dump.sql", "INSERT INTO users VALUES ('kim');\n")
        self.git("add", "app.py", "dump.sql")

    def test_policy_command_prints_numbers_and_sources(self):
        self.write_repo_policy(SAMPLE_POLICY)
        code, out, err = self.run_cli(["policy", "--max-lines", "80", "--check", "EMP-123456"])
        self.assertEqual(code, 0, err)
        self.assertIn("최대 파일 수      : 5개  (설정 파일, 기본 10개)", out)
        self.assertIn("전체 최대 줄 수   : 80줄  (CLI 옵션, 기본 200줄)", out)
        self.assertIn("파일당 최대 줄 수 : 40줄  (설정 파일, 기본 제한 없음)", out)
        self.assertIn("1. EMPLOYEE_ID", out)
        self.assertIn("결과 : [MASKED:EMPLOYEE_ID]", out)
        self.call_mock.assert_not_called()

    def test_policy_command_default(self):
        code, out, _ = self.run_cli(["policy"])
        self.assertEqual(code, 0)
        self.assertIn("기본 정책 사용", out)
        self.assertIn("최대 파일 수      : 10개  (기본값)", out)

    def test_repo_policy_file_is_applied_to_prompt(self):
        self.write_repo_policy(SAMPLE_POLICY)
        self.stage_sensitive_change()
        code, out, err = self.run_cli(["commit", "--dry-run"])
        self.assertEqual(code, 0, err)
        self.assertIn("safe-mode 정책: 최대 5개 파일, 전체 120줄, 파일당 40줄", err)
        self.assertIn("정책 제외 1개", err)
        self.assertIn("[MASKED:EMPLOYEE_ID]", out)
        self.assertIn("[MASKED:INTERNAL_HOST]", out)
        self.assertNotIn("EMP-123456", out)
        self.assertNotIn("INSERT INTO users", out)

    def test_default_policy_does_not_print_policy_line(self):
        """정책을 지정하지 않으면 로그도 고도화 전과 같다."""
        self.stage_change()
        _, _, err = self.run_cli(["commit", "--dry-run"])
        self.assertNotIn("safe-mode 정책:", err)

    def test_cli_mask_pattern_is_applied(self):
        self.stage_sensitive_change()
        code, out, err = self.run_cli(["commit", "--dry-run", "--mask-pattern", r"EMPLOYEE_ID=\bEMP-\d{6}\b"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("EMP-123456", out)
        self.assertIn("EMPLOYEE_ID 1", err)

    def test_policy_warning_when_safe_mode_off(self):
        self.write_repo_policy(SAMPLE_POLICY)
        self.stage_sensitive_change()
        code, out, err = self.run_cli(["commit", "--dry-run", "--no-safe-mode"])
        self.assertEqual(code, 0, err)
        self.assertIn("safe-mode 정책(설정 파일/정책 옵션)은 적용되지 않습니다", err)
        self.assertIn("EMP-123456", out)  # OFF 이므로 그대로 전송

    def test_invalid_policy_file_exits_2(self):
        self.write(POLICY_FILE_NAME, '{"safe_mode": {"max_files": "many"}}')
        self.stage_change()
        code, _, err = self.run_cli(["commit", "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("safe-mode 정책 설정 오류", err)

    def test_policy_check_simulates_diff_line(self):
        code, out, err = self.run_cli(["policy", "--mask-pattern", r"OWNER=\bEMP-\d{6}\b", "--check", "owner EMP-123456"])
        self.assertEqual(code, 0, err)
        self.assertIn("결과 : owner [MASKED:OWNER]", out)
        self.assertIn("diff 의 추가 줄", out)

    def test_policy_command_shows_per_entry_sources(self):
        self.write_repo_policy({"exclude_files": ["*.sql"]})
        code, out, _ = self.run_cli(["policy", "--exclude-file", "*.csv"])
        self.assertEqual(code, 0)
        self.assertIn("- *.sql  (설정 파일)", out)
        self.assertIn("- *.csv  (CLI 옵션)", out)

    def test_base_branch_name_is_masked(self):
        """현재 브랜치뿐 아니라 기준 브랜치 이름(diff 설명/명령)도 같은 규칙으로 가린다."""
        self.git("checkout", "-q", "-b", "release/acme-2026")
        self.git("checkout", "-q", "-b", "feature/acme-login")
        self.write("app.py", "print('x')\n")
        self.git("commit", "-q", "-am", "change")
        code, out, err = self.run_cli(["pr", "--base", "release/acme-2026", "--dry-run",
                                       "--mask-pattern", r"CODENAME=(?i)\bacme\b"])
        self.assertEqual(code, 0, err)
        prompt = out.split("--- User Prompt ---", 1)[1]
        self.assertNotIn("acme", prompt.lower().replace("[masked:codename]", ""))

    def test_unmatched_exclude_pattern_is_logged(self):
        self.stage_change()
        _, _, err = self.run_cli(["commit", "--dry-run", "--exclude-file", "*.sqll"])
        self.assertIn("일치하는 파일이 없는 제외 패턴: *.sqll", err)

    def test_policy_applies_to_ai_output_masking(self):
        self.write_repo_policy({"mask_rules": [{"name": "EMPLOYEE_ID", "pattern": r"\bEMP-\d{6}\b"}]})
        self.stage_change()
        self.call_mock.return_value = base.make_response(
            dict(base.COMMIT_DATA, subject="EMP-123456 담당 기능 추가")
        )
        code, out, err = self.run_cli(["commit"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("EMP-123456", out)
        self.assertIn("[MASKED:EMPLOYEE_ID]", out)


if __name__ == "__main__":
    unittest.main()
