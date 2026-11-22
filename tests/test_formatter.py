"""gitgen.formatter 단위 테스트.

AI 응답(JSON dict)을 후처리해 실무 규칙(제목 길이, 템플릿, 표현 제한)에 맞는
커밋 메시지 / PR 초안으로 만드는지 확인한다. API 호출은 전혀 하지 않는다.

실행: python -m unittest tests.test_formatter -v
"""

import unittest

from gitgen import config
from gitgen.formatter import (
    FIXED,
    OK,
    WARN,
    CommitMessage,
    PRDraft,
    build_commit_message,
    build_pr_draft,
    clean_bullets,
    has_masked_token,
    parse_pr_sections,
    truncate,
    validate_commit_text,
    validate_pr,
)


def _commit_data(**overrides):
    """정상적인 커밋용 AI 응답(dict) 기본값에 일부 필드만 덮어쓴다."""
    data = {
        "summary": "커밋 메시지 생성 기능 추가",
        "type": "feat",
        "scope": "cli",
        "subject": "커밋 메시지 생성 명령 추가",
        "body": ["cli.py: commit 명령 추가", "formatter.py: 제목 길이 검증"],
    }
    data.update(overrides)
    return data


def _pr_data(**overrides):
    """정상적인 PR용 AI 응답(dict) 기본값에 일부 필드만 덮어쓴다."""
    data = {
        "summary": "PR 초안 생성 기능 추가",
        "type": "feat",
        "subject": "PR 초안 자동 생성 기능 추가",
        "why": ["PR 설명 작성 시간을 줄이기 위함"],
        "what": ["cli.py: pr 명령 추가", "formatter.py: PR 템플릿 조립"],
        "how_to_test": ["`python main.py pr` 실행 후 출력 확인"],
    }
    data.update(overrides)
    return data


def _levels(checks, level):
    return [c for c in checks if c.level == level]


def _messages(checks, level=None):
    return [c.message for c in checks if level is None or c.level == level]


# ---------------------------------------------------------------------------
# 커밋 메시지
# ---------------------------------------------------------------------------
class BuildCommitMessageTest(unittest.TestCase):
    def test_renders_title_blank_line_and_dash_bullets(self):
        """정상 입력은 'type(scope): subject' + 빈 줄 + '- ' 불릿으로 조립된다."""
        message, checks = build_commit_message(_commit_data(), ["gitgen/cli.py", "gitgen/formatter.py"])
        self.assertIsInstance(message, CommitMessage)
        self.assertEqual(
            message.text,
            "feat(cli): 커밋 메시지 생성 명령 추가\n\n- cli.py: commit 명령 추가\n- formatter.py: 제목 길이 검증",
        )
        self.assertEqual(message.summary, "커밋 메시지 생성 기능 추가")
        # 정상 입력이면 '보정' 항목이 없어야 한다.
        self.assertEqual(_levels(checks, FIXED), [])
        self.assertIn("제목 형식 'type(scope): subject' 준수", _messages(checks, OK))

    def test_empty_scope_renders_type_colon_subject(self):
        """scope 가 빈 문자열이면 괄호 없이 'type: subject' 로 만든다."""
        message, _ = build_commit_message(_commit_data(scope=""), [])
        self.assertEqual(message.title, "feat: 커밋 메시지 생성 명령 추가")

    def test_scope_special_characters_are_removed(self):
        """scope 의 공백/특수문자는 제거되고 영문·숫자·./-_ 만 남는다."""
        message, _ = build_commit_message(_commit_data(scope="c l i!@#"), [])
        self.assertEqual(message.title, "feat(cli): 커밋 메시지 생성 명령 추가")

    def test_type_is_case_insensitive(self):
        """'FEAT' 처럼 대문자로 와도 소문자 타입으로 정규화되고 보정 항목은 생기지 않는다."""
        message, checks = build_commit_message(_commit_data(type="FEAT"), [])
        self.assertTrue(message.title.startswith("feat(cli): "))
        self.assertFalse(any("커밋 타입" in m for m in _messages(checks, FIXED)))

    def test_unknown_type_falls_back_to_chore_with_fixed_check(self):
        """허용 목록에 없는 타입은 'chore' 로 바뀌고 '보정' 항목이 남는다."""
        message, checks = build_commit_message(_commit_data(type="feature"), [])
        self.assertTrue(message.title.startswith("chore(cli): "), message.title)
        fixed = _messages(checks, FIXED)
        self.assertTrue(any("feature" in m and "chore" in m for m in fixed), fixed)

    def test_missing_type_falls_back_to_chore(self):
        """type 필드가 아예 없어도 예외 없이 'chore' 로 대체된다."""
        data = _commit_data()
        del data["type"]
        message, checks = build_commit_message(data, [])
        self.assertTrue(message.title.startswith("chore"))
        self.assertTrue(any("chore" in m for m in _messages(checks, FIXED)))

    def test_subject_repeating_type_prefix_is_cleaned(self):
        """AI 가 subject 에 'feat: ' 를 또 넣어도 최종 제목은 'feat: feat: ...' 가 되지 않는다."""
        message, _ = build_commit_message(_commit_data(scope="", subject="feat: 로그인 기능 추가"), [])
        self.assertEqual(message.title, "feat: 로그인 기능 추가")
        self.assertNotIn("feat: feat:", message.text)

    def test_subject_with_other_type_and_scope_prefix_is_cleaned(self):
        """subject 앞의 'fix(api): ' 같은 다른 타입/스코프 접두사도 제거된다."""
        message, _ = build_commit_message(_commit_data(scope="", subject="fix(api): 토큰 갱신 오류 수정"), [])
        self.assertEqual(message.title, "feat: 토큰 갱신 오류 수정")

    # 회귀 테스트: subject 에 타입 접두사가 여러 번 들어와도 모두 제거되는지 확인한다.
    def test_subject_with_doubled_prefix_is_fully_cleaned(self):
        """subject 가 'feat: feat: x' 처럼 접두사를 두 번 포함해도 최종 제목은 'feat: x' 여야 한다."""
        message, _ = build_commit_message(_commit_data(scope="", subject="feat: feat: 로그인 기능 추가"), [])
        self.assertEqual(message.title, "feat: 로그인 기능 추가")

    def test_trailing_period_is_removed(self):
        """제목 끝의 마침표는 제거된다."""
        message, _ = build_commit_message(_commit_data(scope="", subject="README 사용법 보완."), [])
        self.assertEqual(message.title, "feat: README 사용법 보완")

    def test_emoji_is_removed(self):
        """제목의 이모지(✨, 🎉)는 제거된다."""
        message, _ = build_commit_message(_commit_data(scope="", subject="✨ 로그인 기능 추가 🎉"), [])
        self.assertEqual(message.title, "feat: 로그인 기능 추가")

    def test_newlines_in_subject_are_collapsed(self):
        """제목 안의 줄바꿈은 공백 하나로 합쳐 한 줄로 만든다."""
        message, _ = build_commit_message(_commit_data(scope="", subject="로그인 기능\n추가\r\n."), [])
        self.assertEqual(message.title, "feat: 로그인 기능 추가")
        self.assertNotIn("\n", message.title)

    def test_empty_subject_gets_placeholder_and_warning(self):
        """subject 가 비면 기본 문구로 채우고 '경고' 를 남긴다."""
        message, checks = build_commit_message(_commit_data(scope="", subject="  "), [])
        self.assertEqual(message.title, "feat: 변경 사항 반영")
        self.assertTrue(any("비어" in m for m in _messages(checks, WARN)))

    def test_long_title_drops_scope_first(self):
        """scope 포함 시 72자를 넘지만 scope 를 빼면 들어가는 경우, 자르지 않고 scope 만 생략한다."""
        scope = "abcdefghijklmnopqrst"  # 20자 (scope 최대 길이)
        subject = "retry logic added for flaky network calls in api client"
        self.assertGreater(len(f"feat({scope}): {subject}"), config.COMMIT_TITLE_MAX)
        self.assertLessEqual(len(f"feat: {subject}"), config.COMMIT_TITLE_MAX)

        message, checks = build_commit_message(_commit_data(scope=scope, subject=subject), [])
        self.assertEqual(message.title, f"feat: {subject}")
        fixed = _messages(checks, FIXED)
        self.assertTrue(any("scope" in m for m in fixed), fixed)
        self.assertFalse(any("자름" in m for m in fixed), "scope 생략만으로 충분하면 자르면 안 된다")

    def test_very_long_title_is_truncated_to_72(self):
        """scope 를 빼도 72자를 넘으면 단어 경계에서 잘라 72자 이하 + '…' 로 만든다."""
        subject = " ".join(["word%02d" % i for i in range(30)])  # 약 200자
        message, checks = build_commit_message(_commit_data(scope="cli", subject=subject), [])
        self.assertLessEqual(len(message.title), config.COMMIT_TITLE_MAX)
        self.assertTrue(message.title.startswith("feat: word00"), message.title)
        self.assertTrue(message.title.endswith("…"))
        self.assertNotIn("(cli)", message.title)
        fixed = _messages(checks, FIXED)
        self.assertTrue(any("scope" in m for m in fixed), fixed)
        self.assertTrue(any("자름" in m for m in fixed), fixed)

    def test_bullets_capped_at_three(self):
        """본문 불릿이 3개를 넘으면 앞의 3개만 남기고 '보정' 항목을 남긴다."""
        body = ["a.py: 하나", "b.py: 둘", "c.py: 셋", "d.py: 넷", "e.py: 다섯"]
        message, checks = build_commit_message(_commit_data(body=body), [])
        self.assertEqual(message.bullets, ["a.py: 하나", "b.py: 둘", "c.py: 셋"])
        self.assertEqual(message.text.count("\n- "), config.COMMIT_BULLETS_MAX)
        self.assertTrue(any("3개" in m for m in _messages(checks, FIXED)))

    def test_bullet_markers_stripped_but_double_dash_content_kept(self):
        """'- ', '1. ', '* ' 같은 머리 기호는 떼지만 '--context 옵션' 같은 내용은 보존한다."""
        body = ["- cli.py: 옵션 추가", "1. git_collector.py: diff 파싱", "--context 옵션 지원"]
        message, _ = build_commit_message(_commit_data(body=body), [])
        self.assertEqual(
            message.bullets, ["cli.py: 옵션 추가", "git_collector.py: diff 파싱", "--context 옵션 지원"]
        )
        self.assertIn("\n- --context 옵션 지원", message.text)
        self.assertNotIn("- - ", message.text)

    def test_duplicate_and_blank_bullets_removed(self):
        """빈 문자열·중복(대소문자 무시) 불릿은 제거되고 끝 마침표도 지운다."""
        body = ["", "  ", "Add parser.", "add parser", "README 수정"]
        message, _ = build_commit_message(_commit_data(body=body), [])
        self.assertEqual(message.bullets, ["Add parser", "README 수정"])

    def test_empty_body_falls_back_to_changed_files(self):
        """본문이 비면 변경 파일 목록으로 불릿 하나를 채우고 '보정' 항목을 남긴다."""
        message, checks = build_commit_message(_commit_data(body=[]), ["gitgen/cli.py", "README.md"])
        self.assertEqual(message.bullets, ["변경 파일: gitgen/cli.py, README.md"])
        self.assertTrue(message.text.endswith("\n\n- 변경 파일: gitgen/cli.py, README.md"))
        self.assertTrue(any("변경 파일" in m for m in _messages(checks, FIXED)))

    def test_empty_body_fallback_lists_at_most_three_files(self):
        """변경 파일이 3개를 넘으면 앞 3개만 쓰고 '외 N개' 로 줄인다."""
        paths = ["a.py", "b.py", "c.py", "d.py", "e.py"]
        message, _ = build_commit_message(_commit_data(body=None), paths)
        self.assertEqual(message.bullets, ["변경 파일: a.py, b.py, c.py 외 2개"])

    def test_empty_body_without_changed_files_gives_title_only(self):
        """본문도 변경 파일도 없으면 제목 한 줄만 출력한다."""
        message, _ = build_commit_message(_commit_data(body=[]), [])
        self.assertEqual(message.bullets, [])
        self.assertEqual(message.text, message.title)

    def test_mentioned_changed_files_are_reported(self):
        """본문에 변경 파일 이름이 나오면 '변경 파일 언급' OK 항목이 생긴다."""
        _, checks = build_commit_message(_commit_data(), ["gitgen/cli.py"])
        self.assertTrue(any("변경 파일 언급 1개" in m and "cli.py" in m for m in _messages(checks, OK)))


class ValidateCommitTextTest(unittest.TestCase):
    @staticmethod
    def _title(length):
        prefix = "feat: "
        return prefix + "a" * (length - len(prefix))

    def test_title_up_to_50_is_ok(self):
        """제목 50자 이하는 OK."""
        title = self._title(50)
        self.assertEqual(len(title), 50)
        check = validate_commit_text(title)[0]
        self.assertEqual(check.level, OK)
        self.assertIn("50자", check.message)

    def test_title_51_to_72_is_warning_but_allowed(self):
        """제목 51~72자는 '권장 초과' 경고지만 최대 길이 이내라 허용으로 표시된다."""
        for n in (51, 60, 72):
            with self.subTest(length=n):
                check = validate_commit_text(self._title(n))[0]
                self.assertEqual(check.level, WARN)
                self.assertIn(f"{n}자", check.message)
                self.assertIn("허용", check.message)
                self.assertNotIn("최대 72자 초과", check.message)

    def test_title_over_72_is_max_exceeded_warning(self):
        """제목 73자 이상은 '최대 72자 초과' 경고."""
        check = validate_commit_text(self._title(73))[0]
        self.assertEqual(check.level, WARN)
        self.assertIn("최대 72자 초과", check.message)
        self.assertNotIn("허용", check.message)

    def test_title_format_check(self):
        """'type: subject' 형식이 아니면 경고, 맞으면 OK."""
        self.assertIn(OK, [c.level for c in validate_commit_text("fix(api): 버그 수정")[1:2]])
        bad = validate_commit_text("그냥 수정함")
        self.assertTrue(any(c.level == WARN and "형식" in c.message for c in bad))

    def test_missing_blank_line_between_title_and_body_warns(self):
        """제목 바로 다음 줄에 본문이 붙어 있으면 '빈 줄 필요' 경고."""
        checks = validate_commit_text("feat: 기능 추가\n- 본문")
        self.assertTrue(any(c.level == WARN and "빈 줄" in c.message for c in checks))

    def test_well_formed_message_counts_bullets(self):
        """빈 줄 + 불릿 본문이면 빈 줄 경고 없이 불릿 개수를 OK 로 보고한다."""
        checks = validate_commit_text("feat: 기능 추가\n\n- 하나\n- 둘")
        self.assertFalse(any("빈 줄" in c.message for c in checks))
        self.assertIn("본문 핵심 변경 불릿 2개", _messages(checks, OK))

    def test_title_only_message_has_no_body_checks(self):
        """제목만 있는 메시지는 본문 관련 검사를 하지 않는다."""
        checks = validate_commit_text("feat: 기능 추가")
        self.assertEqual(len(checks), 2)


# ---------------------------------------------------------------------------
# PR 초안
# ---------------------------------------------------------------------------
class BuildPRDraftTest(unittest.TestCase):
    def test_renders_title_and_three_sections(self):
        """정상 입력은 'type: subject' 제목과 Why/What/How to Test 템플릿 본문이 된다."""
        draft, checks = build_pr_draft(_pr_data(), ["gitgen/cli.py"])
        self.assertIsInstance(draft, PRDraft)
        self.assertEqual(draft.title, "feat: PR 초안 자동 생성 기능 추가")
        self.assertEqual(
            draft.body,
            "## Why\n- PR 설명 작성 시간을 줄이기 위함\n\n"
            "## What\n- cli.py: pr 명령 추가\n- formatter.py: PR 템플릿 조립\n\n"
            "## How to Test\n- `python main.py pr` 실행 후 출력 확인",
        )
        self.assertEqual(_levels(checks, FIXED), [])
        self.assertEqual(_levels(checks, WARN), [])

    def test_empty_sections_get_fallback_bullets(self):
        """AI 가 빈 목록을 주면 세 섹션 모두 기본 불릿으로 채워지고 각각 '보정' 항목이 남는다."""
        data = _pr_data(why=[], what=[], how_to_test=[])
        draft, checks = build_pr_draft(data, ["gitgen/cli.py", "README.md"])
        sections = parse_pr_sections(draft.body)
        for heading in ("why", "what", "how to test"):
            with self.subTest(section=heading):
                self.assertIn(heading, sections)
                self.assertGreaterEqual(len(sections[heading]), 1)
                self.assertTrue(all(b.startswith("- ") for b in sections[heading]))
        self.assertEqual(draft.sections["what"], ["변경 파일: gitgen/cli.py, README.md"])
        self.assertIn("(작성 필요)", draft.sections["why"][0])
        self.assertIn("(작성 필요)", draft.sections["how_to_test"][0])
        self.assertEqual(len([m for m in _messages(checks, FIXED) if "섹션에 불릿이 없어" in m]), 3)
        # 보정 후 본문은 검증을 통과해야 한다.
        self.assertFalse(any("누락" in m for m in _messages(checks, WARN)))

    def test_missing_section_keys_and_no_changed_files(self):
        """섹션 키가 없고 변경 파일도 없어도 예외 없이 기본 불릿으로 채운다."""
        draft, _ = build_pr_draft({"type": "fix", "subject": "버그 수정"}, [])
        self.assertEqual(draft.sections["what"], ["변경 파일: (알 수 없음)"])
        for heading in ("## Why\n- ", "## What\n- ", "## How to Test\n- "):
            self.assertIn(heading, draft.body)

    def test_section_bullets_are_cleaned_and_capped(self):
        """섹션 불릿도 머리 기호 제거·중복 제거 후 최대 5개로 제한된다."""
        what = ["- a.py: 1", "* b.py: 2", "c.py: 3", "c.py: 3", "d.py: 4", "e.py: 5", "f.py: 6"]
        draft, _ = build_pr_draft(_pr_data(what=what), [])
        self.assertEqual(draft.sections["what"], ["a.py: 1", "b.py: 2", "c.py: 3", "d.py: 4", "e.py: 5"])
        self.assertEqual(len(draft.sections["what"]), config.PR_SECTION_BULLETS_MAX)

    def test_string_section_is_split_into_lines(self):
        """섹션 값이 목록이 아니라 여러 줄 문자열이어도 줄 단위 불릿으로 나눈다."""
        draft, _ = build_pr_draft(_pr_data(why="- 이유 하나\n- 이유 둘"), [])
        self.assertEqual(draft.sections["why"], ["이유 하나", "이유 둘"])

    def test_long_pr_title_is_truncated_to_80(self):
        """PR 제목이 80자를 넘으면 80자 이하로 자르고 '보정' 항목을 남긴다."""
        subject = " ".join(["항목%02d" % i for i in range(40)])
        draft, checks = build_pr_draft(_pr_data(subject=subject), [])
        self.assertLessEqual(len(draft.title), config.PR_TITLE_MAX)
        self.assertTrue(draft.title.startswith("feat: 항목00"))
        self.assertTrue(any("PR 제목" in m and "자름" in m for m in _messages(checks, FIXED)))
        self.assertTrue(any(c.level == OK and "PR 제목" in c.message for c in checks))

    def test_pr_title_is_single_line_without_prefix_duplication(self):
        """PR 제목의 줄바꿈·중복 접두사·끝 마침표가 정리된다."""
        draft, checks = build_pr_draft(_pr_data(subject="feat: 로그인\n기능 추가."), [])
        self.assertEqual(draft.title, "feat: 로그인 기능 추가")
        self.assertFalse(any("한 줄이 아님" in m for m in _messages(checks)))

    def test_unknown_pr_type_falls_back_to_chore(self):
        """PR 타입이 허용 목록 밖이면 'chore' 로 대체한다."""
        draft, checks = build_pr_draft(_pr_data(type="improvement"), [])
        self.assertTrue(draft.title.startswith("chore: "))
        self.assertTrue(any("improvement" in m for m in _messages(checks, FIXED)))


class ValidatePRTest(unittest.TestCase):
    GOOD_BODY = "## Why\n- 이유\n\n## What\n- 변경\n\n## How to Test\n- `python -m unittest`"

    def test_good_title_and_body_pass(self):
        """올바른 제목/본문은 경고 없이 OK 만 나온다."""
        checks = validate_pr("feat: 기능 추가", self.GOOD_BODY)
        self.assertEqual(_levels(checks, WARN), [])
        self.assertTrue(any("Why 1" in m and "What 1" in m and "How to Test 1" in m for m in _messages(checks, OK)))

    def test_missing_section_and_empty_section_are_flagged(self):
        """손으로 쓴 잘못된 본문: 섹션 누락과 '불릿 없음' 섹션을 모두 경고한다."""
        bad_body = "## Why\n- 이유\n\n## What\n변경 내용을 문장으로만 씀\n"
        checks = validate_pr("feat: 기능 추가", bad_body)
        warns = _messages(checks, WARN)
        self.assertEqual(len(warns), 1, warns)
        self.assertIn("누락", warns[0])
        self.assertIn("What(불릿 없음)", warns[0])
        self.assertIn("How to Test", warns[0])
        self.assertNotIn("Why", warns[0])

    def test_body_without_any_headers_flags_all_sections(self):
        """헤더가 하나도 없는 본문은 세 섹션 모두 누락으로 보고된다."""
        warns = _messages(validate_pr("feat: x", "- 그냥 불릿\n- 또 불릿"), WARN)
        self.assertEqual(len(warns), 1)
        for heading in ("Why", "What", "How to Test"):
            self.assertIn(heading, warns[0])

    def test_headers_are_case_insensitive(self):
        """'## why', '### How To Test' 처럼 대소문자/레벨이 달라도 섹션으로 인정한다."""
        body = "## why\n- a\n## WHAT\n* b\n### How To Test\n- c"
        self.assertEqual(_levels(validate_pr("feat: x", body), WARN), [])

    def test_title_over_80_and_multiline_title_warn(self):
        """80자 초과 제목과 여러 줄 제목은 각각 경고."""
        long_checks = validate_pr("feat: " + "a" * 80, self.GOOD_BODY)
        self.assertTrue(any(c.level == WARN and "PR 제목 86자" in c.message for c in long_checks))
        multi_checks = validate_pr("feat: a\nb", self.GOOD_BODY)
        self.assertTrue(any(c.level == WARN and "한 줄이 아님" in c.message for c in multi_checks))

    def test_title_exactly_80_is_ok(self):
        """정확히 80자 제목은 OK."""
        title = "feat: " + "a" * 74
        self.assertEqual(len(title), 80)
        checks = validate_pr(title, self.GOOD_BODY)
        self.assertEqual(checks[0].level, OK)


class ParsePRSectionsTest(unittest.TestCase):
    def test_groups_bullets_under_headers(self):
        """헤더별로 '-'/'*' 불릿만 모으고, 일반 문장과 헤더 이전 줄은 무시한다."""
        body = (
            "서론 문장\n- 헤더 이전 불릿\n"
            "## Why\n- 이유 하나\n  * 이유 둘\n설명 문장\n"
            "## What\n\n"
            "## How to Test\n- `python main.py commit`\n-\n"
        )
        sections = parse_pr_sections(body)
        self.assertEqual(
            sections,
            {
                "why": ["- 이유 하나", "* 이유 둘"],
                "what": [],
                "how to test": ["- `python main.py commit`"],
            },
        )

    def test_empty_body(self):
        """빈 본문은 빈 dict."""
        self.assertEqual(parse_pr_sections(""), {})

    def test_round_trip_with_pr_draft_body(self):
        """PRDraft.body 를 다시 파싱하면 섹션별 불릿 수가 그대로 나온다."""
        draft = PRDraft(title="feat: x", sections={"why": ["a"], "what": ["b", "c"], "how_to_test": ["d"]})
        sections = parse_pr_sections(draft.body)
        self.assertEqual({k: len(v) for k, v in sections.items()}, {"why": 1, "what": 2, "how to test": 1})


# ---------------------------------------------------------------------------
# 문자열 도우미
# ---------------------------------------------------------------------------
class TruncateTest(unittest.TestCase):
    def test_short_text_unchanged(self):
        """한도 이내 문자열은 그대로 돌려준다."""
        self.assertEqual(truncate("short text", 72), "short text")
        exact = "a" * 72
        self.assertEqual(truncate(exact, 72), exact)

    def test_cuts_at_word_boundary_with_ellipsis(self):
        """한도를 넘으면 단어 경계에서 자르고 '…' 를 붙여 한도 이하로 만든다."""
        text = "alpha beta gamma delta epsilon zeta eta theta"
        result = truncate(text, 20)
        self.assertLessEqual(len(result), 20)
        self.assertEqual(result, "alpha beta gamma…")

    def test_hard_cut_when_no_space(self):
        """공백이 없으면 글자 단위로 잘라 정확히 한도 길이가 된다."""
        result = truncate("a" * 100, 10)
        self.assertEqual(result, "a" * 9 + "…")
        self.assertEqual(len(result), 10)


class CleanBulletsTest(unittest.TestCase):
    def test_none_and_empty(self):
        """None / 빈 목록은 빈 목록."""
        self.assertEqual(clean_bullets(None, 3), [])
        self.assertEqual(clean_bullets([], 3), [])

    def test_strips_markers_emoji_newlines_and_limits(self):
        """머리 기호, 이모지, 줄바꿈, 끝 마침표를 정리하고 개수를 제한한다."""
        items = ["• 첫째 ✅", "2) 둘째.", "셋째\n이어짐", "넷째"]
        self.assertEqual(clean_bullets(items, 3), ["첫째", "둘째", "셋째 이어짐"])

    def test_preserves_option_like_content(self):
        """'--no-safe-mode', '-c 옵션' 처럼 기호 뒤에 공백이 없는 내용은 건드리지 않는다."""
        self.assertEqual(clean_bullets(["--no-safe-mode 지원", "-c 옵션"], 5), ["--no-safe-mode 지원", "-c 옵션"])


class MiscTest(unittest.TestCase):
    def test_has_masked_token(self):
        """'[MASKED' 토큰 포함 여부를 판별한다."""
        self.assertTrue(has_masked_token(["ok", "key=[MASKED:api_key]"]))
        self.assertFalse(has_masked_token(["ok", "nothing here"]))


if __name__ == "__main__":
    unittest.main()
