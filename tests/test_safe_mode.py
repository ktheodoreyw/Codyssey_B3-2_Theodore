"""gitgen/safe_mode.py 테스트.

- mask_text        : MASK_RULES 의 모든 규칙이 실제와 비슷한 샘플을 가리는지, 그리고
                     플레이스홀더/숫자/일반 파이썬 코드는 건드리지 않는지
- is_sensitive_file: .env / 키 파일 판단과 예시 파일 허용 목록
- allocate_line_budget: water-filling 줄 예산의 성질
- prepare_files    : safe-mode ON/OFF, 파일 10개·200줄 제한, 민감/lock/바이너리 파일 처리, 문자 수 하드 상한

테스트용 키 문자열은 실제 키가 아니며, 비밀 스캐너 오탐을 피하려고 조각을 이어 붙여 만든다.

실행: python -m unittest tests.test_safe_mode -v
"""

from __future__ import annotations

import random
import unittest
from typing import List, Optional
from unittest import mock

from gitgen import config
from gitgen.git_collector import FileDiff
from gitgen.safe_mode import (
    MASK_RULES,
    allocate_line_budget,
    is_sensitive_file,
    mask_output,
    mask_text,
    prepare_files,
)


def _k(*parts: str) -> str:
    return "".join(parts)


# 가짜 샘플 값 (형식만 실제와 같다)
ANTHROPIC_KEY = _k("sk-", "ant-", "api03-", "Zx9Kq2Lm7Np4Rs8Tv1Wy3Ab")
OPENAI_KEY = _k("sk-", "proj-", "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8")
AWS_KEY = _k("AKIA", "IOSFODNN7EXAMPLE")
GITHUB_CLASSIC = _k("gh", "p_", "a1B2c3D4e5" * 4)
GITHUB_FINE_GRAINED = _k("github", "_pat_", "11ABCDEFG0", "_", "Xy9" * 10)
GOOGLE_KEY = _k("AI", "za", "Sy", "B1c2D3e4F5" * 3, "g6H")  # 'AIza' + 35자
SLACK_TOKEN = _k("xo", "xb-", "123456789012-", "AbCdEfGhIjKl")
JWT_TOKEN = _k(
    "eyJhbGciOiJIUzI1NiJ9", ".", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", ".", "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
)
PRIVATE_KEY_BODY = "MIIEowIBAAKCAQEAfakeKeyMaterialOnlyForTests"


# (규칙 이름, 입력, 사라져야 하는 비밀 조각, 결과에 있어야 하는 표시)
POSITIVE_CASES = [
    (
        "PRIVATE_KEY",
        "+-----BEGIN RSA PRIVATE KEY-----\n+" + PRIVATE_KEY_BODY + "\n+-----END RSA PRIVATE KEY-----\n+after = 1",
        PRIVATE_KEY_BODY,
        "+[MASKED:PRIVATE_KEY]\n+after = 1",
    ),
    ("PRIVATE_KEY", "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjE\n-----END OPENSSH PRIVATE KEY-----",
     "b3BlbnNzaC1rZXktdjE", "[MASKED:PRIVATE_KEY]"),
    ("ANTHROPIC_KEY", 'headers = {"x-api-key": "' + ANTHROPIC_KEY + '"}', ANTHROPIC_KEY, "[MASKED:API_KEY]"),
    ("OPENAI_KEY", "OPENAI_API_KEY=" + OPENAI_KEY, OPENAI_KEY, "OPENAI_API_KEY=[MASKED:API_KEY]"),
    ("AWS_ACCESS_KEY", "aws_access_key_id = " + AWS_KEY, AWS_KEY, "aws_access_key_id = [MASKED:AWS_KEY]"),
    ("GITHUB_TOKEN", "GH_AUTH " + GITHUB_CLASSIC, GITHUB_CLASSIC, "GH_AUTH [MASKED:TOKEN]"),
    ("GITHUB_TOKEN", "pat = " + GITHUB_FINE_GRAINED, GITHUB_FINE_GRAINED, "pat = [MASKED:TOKEN]"),
    ("GOOGLE_API_KEY", "maps.js?key=" + GOOGLE_KEY + "&callback=init", GOOGLE_KEY, "key=[MASKED:API_KEY]&callback"),
    ("SLACK_TOKEN", "slack_bot: " + SLACK_TOKEN, SLACK_TOKEN, "slack_bot: [MASKED:TOKEN]"),
    ("JWT", "jwt: " + JWT_TOKEN, JWT_TOKEN, "jwt: [MASKED:JWT]"),
    ("BEARER", "Authorization: Bearer abcDEF123456ghiJKL789xyz", "abcDEF123456ghiJKL789xyz",
     "Authorization: [MASKED:TOKEN]"),
    ("URL_CREDENTIAL", "DATABASE_URL=postgres://admin:s3cretPass@db.example.com:5432/app", "s3cretPass",
     "DATABASE_URL=postgres://[MASKED:CREDENTIAL]@db.example.com:5432/app"),
    ("EMAIL", "Maintainer: hong.gildong@example.co.kr", "hong.gildong@example.co.kr", "Maintainer: [MASKED:EMAIL]"),
    ("KR_RRN", "주민등록번호: 900101-1234567", "900101-1234567", "주민등록번호: [MASKED:RRN]"),
    ("KR_PHONE", "연락처: 010-1234-5678", "1234-5678", "연락처: [MASKED:PHONE]"),
    ("KR_PHONE", "phone=01098765432", "01098765432", "phone=[MASKED:PHONE]"),
    ("CARD_NUMBER", "카드: 4111-1111-1111-1111", "4111-1111-1111-1111", "카드: [MASKED:CARD]"),
    ("CARD_NUMBER", "card 5500 0000 0000 0004", "5500 0000 0000 0004", "card [MASKED:CARD]"),
    ("SECRET_ASSIGNMENT", 'password = "hunter2hunter2"', "hunter2hunter2", 'password = "[MASKED:SECRET]"'),
    ("SECRET_ASSIGNMENT", "db_password: 'Pa55word!'", "Pa55word!", "db_password: '[MASKED:SECRET]'"),
    ("SECRET_ASSIGNMENT", '"apiKey": "abcd1234efgh"', "abcd1234efgh", '"apiKey": "[MASKED:SECRET]"'),
    ("SECRET_ASSIGNMENT", 'client_secret => "s3cr3t-value"', "s3cr3t-value", 'client_secret => "[MASKED:SECRET]"'),
    ("SECRET_ASSIGNMENT", 'DB_PASSWORD="abc123def"', "abc123def", 'DB_PASSWORD="[MASKED:SECRET]"'),
    ("ENV_SECRET", "DB_PASSWORD=abc123", "abc123", "DB_PASSWORD=[MASKED:SECRET]"),
    ("ENV_SECRET", "+export API_KEY=abcd1234efgh", "abcd1234efgh", "+export API_KEY=[MASKED:SECRET]"),
    ("ENV_SECRET", "-GITHUB_TOKEN=tok_abcdef", "tok_abcdef", "-GITHUB_TOKEN=[MASKED:SECRET]"),
    # 따옴표 없는 설정값 (YAML / .properties / INI / docker-compose / *_PASS 변수)
    ("ENV_SECRET", "+  password: SuperSecret123", "SuperSecret123", "+  password: [MASKED:SECRET]"),
    ("ENV_SECRET", "+spring.datasource.password=Sup3rS3cret", "Sup3rS3cret",
     "+spring.datasource.password=[MASKED:SECRET]"),
    ("ENV_SECRET", "+aws_secret_access_key = wJalrXUtnFEMI/K7MDENG", "wJalrXUtnFEMI/K7MDENG",
     "+aws_secret_access_key = [MASKED:SECRET]"),
    ("ENV_SECRET", "+  - POSTGRES_PASSWORD=pg_secret_99", "pg_secret_99", "+  - POSTGRES_PASSWORD=[MASKED:SECRET]"),
    ("ENV_SECRET", "+SMTP_PASS=mailpass77", "mailpass77", "+SMTP_PASS=[MASKED:SECRET]"),
    ("STRIPE_KEY", "STRIPE=sk_live_" + "a1" * 12, "sk_live_" + "a1" * 12, "STRIPE=[MASKED:API_KEY]"),
    ("GITLAB_TOKEN", "tok = glpat-" + "b2" * 10, "glpat-" + "b2" * 10, "tok = [MASKED:TOKEN]"),
    ("HUGGINGFACE_TOKEN", "hf = hf_" + "c3" * 17, "hf_" + "c3" * 17, "hf = [MASKED:TOKEN]"),
    ("BASIC_AUTH", "Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA==",
     "Authorization: [MASKED:CREDENTIAL]"),
    ("QUERY_TOKEN", "url = https://api.x.com/v1?access_token=abcdef123456&x=1", "abcdef123456",
     "?access_token=[MASKED:TOKEN]&x=1"),
]

# 비밀이 아니므로 그대로 남아야 하는 값 (플레이스홀더 / 환경변수 참조 / 숫자 설정값)
PLACEHOLDER_CASES = [
    "API_KEY=YOUR_KEY",
    "+ANTHROPIC_API_KEY=your-api-key-here",
    "TOKEN=${TOKEN}",
    "export GITHUB_TOKEN=$GITHUB_TOKEN",
    'api_key = "YOUR_API_KEY"',
    'token = "${GITHUB_TOKEN}"',
    'password: "<your-password>"',
    '"apiKey": "%API_KEY%"',
    'secret = "{{ vault_secret }}"',
    "SECRET_KEY=xxxxxxxx",
    "PASSWORD=********",
    "MAX_TOKENS=4096",
    "TOKEN_LIMIT=150000",
    'max_tokens: "2048"',
]

# 일반 파이썬 코드: 비밀 단어가 들어 있어도 가려지면 안 된다
PYTHON_CODE_CASES = [
    "max_tokens = DEFAULT_MAX_TOKENS",
    "MAX_TOKENS = DEFAULT_MAX_TOKENS",
    'author = "John"',
    'model = "claude-opus-4-6"',
    "api_key = get_api_key()",
    'token = os.environ["ANTHROPIC_API_KEY"]',
    'key = os.environ.get("AI_API_KEY")',
    'payload["max_tokens"] = args.max_tokens',
    "def get_api_key() -> Optional[str]:",
    'API_KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "AI_API_KEY")',
    'print(f"tokens: {usage.output_tokens}")',
    "    headers = {\"x-api-key\": api_key, \"anthropic-version\": version}",
]


# ===========================================================================
# mask_text
# ===========================================================================
class MaskRulesTest(unittest.TestCase):
    def test_every_rule_has_a_positive_sample(self) -> None:
        """MASK_RULES 의 모든 규칙이 아래 샘플 표에서 최소 한 번 검증된다 (규칙 추가 시 누락 방지)."""
        covered = {rule for rule, _, _, _ in POSITIVE_CASES}
        self.assertEqual(covered, {r.name for r in MASK_RULES})

    def test_each_rule_masks_realistic_sample(self) -> None:
        """규칙별 실제와 비슷한 샘플: 비밀 조각이 사라지고 [MASKED:...] 표시가 남으며 해당 규칙으로 집계된다."""
        for rule, text, secret, expected in POSITIVE_CASES:
            with self.subTest(rule=rule, text=text):
                masked, counts = mask_text(text)
                self.assertNotIn(secret, masked)
                self.assertIn(expected, masked)
                self.assertIn(rule, counts)
                self.assertGreaterEqual(counts[rule], 1)

    def test_specific_keys_win_over_generic_rules(self) -> None:
        """규칙 순서: Anthropic 키는 OPENAI_KEY 로, 이미 가린 값은 SECRET/ENV 규칙으로 중복 집계되지 않는다."""
        _, counts = mask_text('headers = {"x-api-key": "' + ANTHROPIC_KEY + '"}')
        self.assertEqual(counts, {"ANTHROPIC_KEY": 1})
        _, counts = mask_text("OPENAI_API_KEY=" + OPENAI_KEY)
        self.assertEqual(counts, {"OPENAI_KEY": 1})

    def test_placeholders_and_numbers_are_kept(self) -> None:
        """'YOUR_KEY', '${TOKEN}', '<...>', 'xxx', 숫자 설정값은 가리지 않고 집계도 하지 않는다."""
        for text in PLACEHOLDER_CASES:
            with self.subTest(text=text):
                self.assertEqual(mask_text(text), (text, {}))

    def test_python_code_is_not_masked(self) -> None:
        """일반 코드(변수 대입, 함수 호출, 환경변수 이름 참조)는 그대로 둔다."""
        for text in PYTHON_CODE_CASES:
            with self.subTest(text=text):
                self.assertEqual(mask_text(text), (text, {}))

    def test_counts_multiple_hits(self) -> None:
        """같은 규칙이 여러 번 맞으면 건수를 모두 센다."""
        text = "a: one@example.com\nb: two@example.org\nc: 010-1111-2222"
        masked, counts = mask_text(text)
        self.assertEqual(counts, {"EMAIL": 2, "KR_PHONE": 1})
        self.assertEqual(masked, "a: [MASKED:EMAIL]\nb: [MASKED:EMAIL]\nc: [MASKED:PHONE]")

    def test_masking_is_idempotent(self) -> None:
        """이미 가린 결과를 다시 넣어도 바뀌지 않는다 ([MASKED 는 플레이스홀더로 취급)."""
        combined = "\n".join(text for _, text, _, _ in POSITIVE_CASES)
        once, counts = mask_text(combined)
        self.assertTrue(counts)
        self.assertEqual(mask_text(once), (once, {}))

    def test_multiline_diff_keeps_other_lines(self) -> None:
        """diff 본문에서 비밀이 있는 줄만 바뀌고 나머지 줄과 줄 수는 그대로다."""
        diff = "\n".join([
            "diff --git a/settings.py b/settings.py",
            "@@ -1,3 +1,3 @@",
            " import os",
            "-API_KEY=abcd1234efgh",
            "+API_KEY=YOUR_KEY",
            "+max_tokens = DEFAULT_MAX_TOKENS",
        ])
        masked, counts = mask_text(diff)
        self.assertEqual(counts, {"ENV_SECRET": 1})
        lines = masked.split("\n")
        self.assertEqual(len(lines), 6)
        self.assertEqual(lines[3], "-API_KEY=[MASKED:SECRET]")
        self.assertEqual(lines[4], "+API_KEY=YOUR_KEY")
        self.assertEqual(lines[5], "+max_tokens = DEFAULT_MAX_TOKENS")

    def test_mask_output_returns_total_count(self) -> None:
        """mask_output 은 (가린 텍스트, 총 치환 건수)를 돌려준다."""
        text, n = mask_output("담당자 dev@example.com / 010-1234-5678")
        self.assertEqual(n, 2)
        self.assertEqual(text, "담당자 [MASKED:EMAIL] / [MASKED:PHONE]")
        self.assertEqual(mask_output("feat: 로그인 추가"), ("feat: 로그인 추가", 0))


# ===========================================================================
# is_sensitive_file
# ===========================================================================
class SensitiveFileTest(unittest.TestCase):
    def test_sensitive_files(self) -> None:
        """.env, 개인키, 인증서, 자격 증명 파일은 민감 파일이다 (경로·대소문자 무관)."""
        for path in [
            ".env",
            "config/.env",
            ".env.local",
            ".env.production",
            ".ENV",
            "id_rsa",
            "home/.ssh/id_ed25519",
            "certs/server.pem",
            "tls/private.key",
            "android/release.keystore",
            "credentials.json",
            "gcp/credentials-prod.json",
            "app/client_secret.json",
            ".npmrc",
            ".netrc",
        ]:
            with self.subTest(path=path):
                self.assertTrue(is_sensitive_file(path))

    def test_non_sensitive_files(self) -> None:
        """예시/템플릿(.env.example 등)과 일반 소스 파일은 민감 파일이 아니다."""
        for path in [
            ".env.example",
            "config/.env.example",
            ".env.sample",
            ".env.template",
            "README.md",
            "src/app.py",
            "environment.yml",
            "keyboard.py",
            "docs/pem-format.md",
            "gitgen/safe_mode.py",
        ]:
            with self.subTest(path=path):
                self.assertFalse(is_sensitive_file(path))


# ===========================================================================
# allocate_line_budget
# ===========================================================================
class AllocateLineBudgetTest(unittest.TestCase):
    def test_docstring_example(self) -> None:
        """작은 파일은 전부, 남은 예산은 큰 파일이 똑같이: [10,500,500],200 → [10,95,95]."""
        self.assertEqual(allocate_line_budget([10, 500, 500], 200), [10, 95, 95])

    def test_everything_fits(self) -> None:
        """합계가 예산 이하면 모든 파일을 그대로 보낸다."""
        self.assertEqual(allocate_line_budget([10, 20, 30], 200), [10, 20, 30])
        self.assertEqual(allocate_line_budget([100, 100], 200), [100, 100])

    def test_order_independent_of_input_position(self) -> None:
        """작은 파일이 목록 중간/끝에 있어도 결과는 각 위치에 맞게 돌아온다."""
        self.assertEqual(allocate_line_budget([500, 10, 500], 200), [95, 10, 95])
        self.assertEqual(allocate_line_budget([500, 500, 10], 200), [95, 95, 10])

    def test_remainder_distributed_one_by_one(self) -> None:
        """나누어떨어지지 않는 나머지는 1줄씩 나눠 합계가 정확히 예산과 같다."""
        budgets = allocate_line_budget([500, 500, 500], 200)
        self.assertEqual(sum(budgets), 200)
        self.assertLessEqual(max(budgets) - min(budgets), 1)

    def test_edge_inputs(self) -> None:
        """빈 목록, 예산 0, 파일 수보다 작은 예산."""
        self.assertEqual(allocate_line_budget([], 200), [])
        self.assertEqual(allocate_line_budget([5, 5], 0), [0, 0])
        budgets = allocate_line_budget([7, 8, 9], 2)
        self.assertEqual(sum(budgets), 2)
        self.assertTrue(all(b <= 1 for b in budgets))

    def test_random_properties(self) -> None:
        """무작위 입력(파일 크기 ≥ 1)에 대해 water-filling 성질이 항상 성립한다.

        - 합계 ≤ 예산, 그리고 낭비 없이 min(예산, 전체 줄 수) 를 다 쓴다
        - 어떤 파일도 자기 크기보다 많이 받지 않는다
        - 크기가 예산/파일수 이하인 작은 파일은 전부 보낸다
        - 잘린 파일보다 2줄 이상 더 받는 파일은 없다 (공평성)
        """
        rng = random.Random(20261121)
        for _ in range(500):
            n = rng.randint(1, 12)
            sizes = [rng.randint(1, 600) for _ in range(n)]
            total = rng.randint(0, 400)
            budgets = allocate_line_budget(sizes, total)
            with self.subTest(sizes=sizes, total=total, budgets=budgets):
                self.assertEqual(len(budgets), n)
                self.assertLessEqual(sum(budgets), total)
                self.assertEqual(sum(budgets), min(total, sum(sizes)))
                for size, budget in zip(sizes, budgets):
                    self.assertGreaterEqual(budget, 0)
                    self.assertLessEqual(budget, size)
                    if size <= total // n:
                        self.assertEqual(budget, size)
                truncated = [b for s, b in zip(sizes, budgets) if b < s]
                if truncated:
                    self.assertLessEqual(max(budgets), min(truncated) + 1)

    # 회귀 테스트: 크기 0 인 파일은 줄 예산을 받지 않는다.
    def test_zero_size_file_never_gets_budget(self) -> None:
        """크기 0 파일은 예산을 받지 않고, 남은 줄은 내용이 있는 파일로 간다: [0,10],1 → [0,1]."""
        self.assertEqual(allocate_line_budget([0, 10], 1), [0, 1])


# ===========================================================================
# prepare_files
# ===========================================================================
def make_diff(path: str, body: List[str], *, is_binary: bool = False) -> FileDiff:
    """diff --git 헤더 5줄 + '+' 본문 줄로 이루어진 FileDiff."""
    header = [
        f"diff --git a/{path} b/{path}",
        "index 1111111..2222222 100644",
        f"--- a/{path}",
        f"+++ b/{path}",
        f"@@ -0,0 +1,{len(body)} @@",
    ]
    return FileDiff(path=path, change="M", additions=len(body), lines=header + ["+" + b for b in body],
                    is_binary=is_binary)


def make_sized(path: str, total_lines: int) -> FileDiff:
    """len(lines) 가 정확히 total_lines 인 FileDiff (total_lines ≥ 5)."""
    return make_diff(path, [f"value_{i} = {i}" for i in range(total_lines - 5)])


def make_binary(path: str) -> FileDiff:
    return FileDiff(
        path=path,
        is_binary=True,
        lines=[f"diff --git a/{path} b/{path}", f"Binary files a/{path} and b/{path} differ"],
    )


def find(prepared, path: str):
    for pf in prepared:
        if pf.source.path == path:
            return pf
    raise AssertionError(f"{path} not in prepared files")


SECRET_FILE_BODY = ["API_KEY=abcd1234efgh", "owner = 'dev.lead@example.com'", "x = 1"]


class PrepareFilesSafeModeOnTest(unittest.TestCase):
    def test_masks_body_and_reports_counts(self) -> None:
        """safe-mode ON: 전송 본문의 비밀이 가려지고 규칙별 건수가 보고서에 쌓인다. 원본 FileDiff 는 그대로."""
        f = make_diff("app.py", SECRET_FILE_BODY)
        original_lines = list(f.lines)
        (pf,), report = prepare_files([f], safe_mode=True)
        self.assertTrue(report.enabled)
        self.assertEqual(report.masked, {"ENV_SECRET": 1, "EMAIL": 1})
        self.assertEqual(report.masked_total, 2)
        self.assertIn("+API_KEY=[MASKED:SECRET]", pf.text)
        self.assertIn("[MASKED:EMAIL]", pf.text)
        self.assertNotIn("abcd1234efgh", pf.text)
        self.assertNotIn("dev.lead@example.com", pf.text)
        self.assertEqual(f.lines, original_lines)
        self.assertEqual(pf.sent_lines, len(f.lines))

    def test_sensitive_files_excluded(self) -> None:
        """.env / *.pem 은 내용 없이 파일명만 남고, .env.example 과 일반 파일은 전송된다."""
        files = [
            make_diff(".env", ["DB_PASSWORD=abc123"]),
            make_diff("config/server.pem", ["-----BEGIN CERTIFICATE-----"]),
            make_diff(".env.example", ["API_KEY=YOUR_KEY"]),
            make_diff("app.py", ["print('hi')"]),
        ]
        prepared, report = prepare_files(files, safe_mode=True)
        self.assertEqual(report.excluded_sensitive, [".env", "config/server.pem"])
        for path in (".env", "config/server.pem"):
            pf = find(prepared, path)
            self.assertTrue(pf.omitted)
            self.assertIn("민감 파일", pf.note)
            self.assertEqual(pf.sent_lines, 0)
        self.assertIn("+API_KEY=YOUR_KEY", find(prepared, ".env.example").text)
        self.assertFalse(find(prepared, "app.py").omitted)

    def test_binary_and_lockfile_omitted_and_not_counted_for_file_limit(self) -> None:
        """바이너리/lock 파일은 내용 생략, 파일 수 제한 계산에서도 빠진다."""
        files = [make_diff(f"src/m{i}.py", ["x = 1"]) for i in range(config.SAFE_MAX_FILES)]
        files.insert(3, make_binary("assets/logo.png"))
        files.insert(7, make_diff("web/package-lock.json", ['"lockfileVersion": 3'] * 50))
        prepared, report = prepare_files(files, safe_mode=True)
        self.assertIn("바이너리", find(prepared, "assets/logo.png").note)
        self.assertTrue(find(prepared, "assets/logo.png").omitted)
        self.assertIn("lock 파일", find(prepared, "web/package-lock.json").note)
        self.assertTrue(find(prepared, "web/package-lock.json").omitted)
        self.assertEqual(report.skipped_over_file_limit, [])
        sent = [pf.source.path for pf in prepared if not pf.omitted]
        self.assertEqual(len(sent), config.SAFE_MAX_FILES)

    def test_file_limit(self) -> None:
        """본문을 보낼 파일이 10개를 넘으면 11번째부터는 내용을 생략하고 보고한다."""
        n = config.SAFE_MAX_FILES + 2
        files = [make_diff(f"f{i:02d}.py", ["a = 1", "b = 2"]) for i in range(n)]
        prepared, report = prepare_files(files, safe_mode=True)
        self.assertEqual([pf.source for pf in prepared], files)  # 순서·개수 보존
        self.assertEqual(report.skipped_over_file_limit, [f"f{i:02d}.py" for i in range(config.SAFE_MAX_FILES, n)])
        for pf in prepared[: config.SAFE_MAX_FILES]:
            self.assertFalse(pf.omitted)
        for pf in prepared[config.SAFE_MAX_FILES:]:
            self.assertTrue(pf.omitted)
            self.assertIn(f"파일 수 제한({config.SAFE_MAX_FILES}개)", pf.note)
            self.assertEqual(pf.sent_lines, 0)

    def test_line_limit_water_filling(self) -> None:
        """줄 제한 200: 크기 [10,500,500] → [10,95,95] 줄 전송, 잘린 파일에는 생략 안내가 붙는다."""
        small, big1, big2 = make_sized("small.py", 10), make_sized("big1.py", 500), make_sized("big2.py", 500)
        prepared, report = prepare_files([small, big1, big2], safe_mode=True)
        self.assertEqual([pf.sent_lines for pf in prepared], [10, 95, 95])
        self.assertEqual(report.sent_lines, config.SAFE_MAX_LINES)
        self.assertEqual(report.original_lines, 1010)
        self.assertEqual(report.truncated, ["big1.py", "big2.py"])
        self.assertEqual(prepared[0].text, "\n".join(small.lines))
        big_lines = prepared[1].text.split("\n")
        self.assertEqual(big_lines[:95], big1.lines[:95])
        self.assertEqual(big_lines[95:], ["... (이하 405줄 생략)"])
        self.assertEqual(prepared[1].note, "diff 500줄 중 95줄만 전송")

    def test_exactly_at_line_limit_is_not_truncated(self) -> None:
        """합계가 정확히 200줄이면 아무것도 자르지 않는다."""
        files = [make_sized("a.py", 50), make_sized("b.py", 60), make_sized("c.py", 90)]
        prepared, report = prepare_files(files, safe_mode=True)
        self.assertEqual(report.truncated, [])
        self.assertEqual(report.sent_lines, 200)
        self.assertEqual([pf.text for pf in prepared], ["\n".join(f.lines) for f in files])

    def test_file_with_zero_line_budget_is_omitted(self) -> None:
        """줄 예산을 하나도 못 받은 파일은 내용 생략 + truncated 로 보고된다 (SAFE_MAX_LINES 를 작게 패치)."""
        files = [make_sized(f"f{i}.py", 6 + i) for i in range(5)]
        with mock.patch.object(config, "SAFE_MAX_LINES", 3):
            prepared, report = prepare_files(files, safe_mode=True)
        self.assertEqual([pf.sent_lines for pf in prepared], [1, 1, 1, 0, 0])
        self.assertEqual(report.sent_lines, 3)
        for pf in prepared[3:]:
            self.assertTrue(pf.omitted)
            self.assertIn("줄 수 제한(3줄)", pf.note)
        self.assertEqual(report.truncated, [f"f{i}.py" for i in range(5)])


class PrepareFilesSafeModeOffTest(unittest.TestCase):
    def test_no_masking(self) -> None:
        """safe-mode OFF: 마스킹하지 않고 원문 그대로 보낸다."""
        f = make_diff("app.py", SECRET_FILE_BODY)
        (pf,), report = prepare_files([f], safe_mode=False)
        self.assertFalse(report.enabled)
        self.assertEqual(report.masked, {})
        self.assertEqual(pf.text, "\n".join(f.lines))

    def test_sensitive_files_are_sent(self) -> None:
        """safe-mode OFF: .env 도 제외하지 않는다."""
        prepared, report = prepare_files([make_diff(".env", ["DB_PASSWORD=abc123"])], safe_mode=False)
        self.assertEqual(report.excluded_sensitive, [])
        self.assertIn("+DB_PASSWORD=abc123", prepared[0].text)

    def test_no_file_or_line_limits(self) -> None:
        """safe-mode OFF: 파일 10개·200줄 제한 없이 전부 보낸다."""
        files = [make_sized(f"f{i:02d}.py", 60) for i in range(config.SAFE_MAX_FILES + 3)]
        prepared, report = prepare_files(files, safe_mode=False)
        self.assertEqual(report.skipped_over_file_limit, [])
        self.assertEqual(report.truncated, [])
        self.assertEqual(report.sent_lines, report.original_lines)
        self.assertEqual(report.sent_lines, 60 * len(files))
        self.assertTrue(all(not pf.omitted for pf in prepared))

    def test_binary_and_lockfile_still_omitted(self) -> None:
        """safe-mode 와 무관하게 바이너리/lock 파일 본문은 보내지 않는다."""
        prepared, _ = prepare_files(
            [make_binary("a.png"), make_diff("yarn.lock", ["x"]), make_diff("b.py", ["y = 1"])], safe_mode=False
        )
        self.assertEqual([pf.omitted for pf in prepared], [True, True, False])


class HardCharLimitTest(unittest.TestCase):
    SUFFIX = "\n... (전송 하드 상한으로 이하 생략)"

    def _three_files(self):
        a = make_diff("a.py", [f"alpha_line_{i:03d} = {i}" for i in range(20)])
        b = make_diff("b.py", [f"beta_line_{i:03d} = {i}" for i in range(40)])
        c = make_diff("c.py", [f"gamma_line_{i:03d} = {i}" for i in range(5)])
        return a, b, c

    def test_partial_cut_then_omit(self) -> None:
        """하드 상한: 앞 파일은 그대로, 넘치는 파일은 줄 경계에서 자르고, 이후 파일은 생략한다."""
        a, b, c = self._three_files()
        a_len = len("\n".join(a.lines))
        room = 300
        with mock.patch("gitgen.config.HARD_MAX_CHARS", a_len + room):
            prepared, report = prepare_files([a, b, c], safe_mode=False)
        pa, pb, pc = prepared
        self.assertTrue(report.hard_limited)
        self.assertEqual(pa.text, "\n".join(a.lines))
        # b: 줄 경계에서 잘린 앞부분 + 안내 문구
        self.assertTrue(pb.text.endswith(self.SUFFIX))
        body = pb.text[: -len(self.SUFFIX)]
        self.assertLessEqual(len(body), room)
        self.assertTrue(("\n".join(b.lines)).startswith(body + "\n"))
        self.assertEqual(pb.sent_lines, body.count("\n") + 1)
        self.assertEqual(pb.note, "전송 하드 상한으로 일부만 전송")
        # c: 남은 공간이 없으므로 생략
        self.assertTrue(pc.omitted)
        self.assertEqual(pc.sent_lines, 0)
        self.assertEqual(pc.note, "전송 하드 상한 초과로 내용 생략")
        self.assertEqual(report.sent_lines, pa.sent_lines + pb.sent_lines)

    def test_small_room_omits_whole_file(self) -> None:
        """남은 공간이 200자 미만이면 자르지 않고 그 파일 내용을 통째로 생략한다."""
        a, b, c = self._three_files()
        a_len = len("\n".join(a.lines))
        with mock.patch("gitgen.config.HARD_MAX_CHARS", a_len + 150):
            prepared, report = prepare_files([a, b, c], safe_mode=False)
        self.assertTrue(report.hard_limited)
        self.assertFalse(prepared[0].omitted)
        self.assertTrue(prepared[1].omitted)
        self.assertEqual(prepared[1].note, "전송 하드 상한 초과로 내용 생략")
        self.assertTrue(prepared[2].omitted)
        self.assertEqual(report.sent_lines, len(a.lines))

    def test_hard_limit_applies_in_safe_mode_too(self) -> None:
        """하드 상한은 safe-mode ON 에서도 적용되며 전송 총 문자 수가 상한(+안내 문구) 안에 든다."""
        a, b, c = self._three_files()
        limit = 500
        with mock.patch("gitgen.config.HARD_MAX_CHARS", limit):
            prepared, report = prepare_files([a, b, c], safe_mode=True)
        self.assertTrue(report.hard_limited)
        total_body = sum(len(pf.text[: -len(self.SUFFIX)] if pf.text.endswith(self.SUFFIX) else pf.text)
                         for pf in prepared)
        self.assertLessEqual(total_body, limit)

    def test_not_triggered_under_default_limit(self) -> None:
        """기본 상한(150,000자)에서는 작은 diff 에 영향이 없다."""
        prepared, report = prepare_files(list(self._three_files()), safe_mode=False)
        self.assertFalse(report.hard_limited)
        self.assertTrue(all(not pf.omitted for pf in prepared))
        self.assertEqual(config.HARD_MAX_CHARS, 150_000)


if __name__ == "__main__":
    unittest.main()
