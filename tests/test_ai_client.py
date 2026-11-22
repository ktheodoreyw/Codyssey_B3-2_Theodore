"""gitgen.ai_client 단위 테스트.

실제 Anthropic API 는 절대 호출하지 않는다. call_messages 에 가짜 post 함수(FakePost)와
아무것도 하지 않는 sleep 을 주입해, REST 요청 구성 → 응답/예외 처리 → 재시도 흐름을
빠르고 결정적으로 검증한다.

실행: python -m unittest tests.test_ai_client -v
"""

import json
import os
import unittest
from unittest import mock

import requests

from gitgen import config
from gitgen.ai_client import (
    STATUS_HINTS,
    AIClientError,
    AIResponse,
    build_headers,
    build_payload,
    call_messages,
    estimate_cost,
    get_api_key,
    supports_temperature,
)
from gitgen.prompts import COMMIT_SCHEMA, PR_SCHEMA

FAKE_KEY = "test-key-not-real"


# ---------------------------------------------------------------------------
# 가짜 HTTP 계층
# ---------------------------------------------------------------------------
class FakeResponse:
    """requests.Response 중 call_messages 가 쓰는 속성만 흉내 낸다."""

    def __init__(self, status_code, body=None, *, text=None, headers=None, reason=""):
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.reason = reason
        if text is None:
            text = json.dumps(body, ensure_ascii=False) if body is not None else ""
        self.text = text

    def json(self):
        # requests 와 마찬가지로 JSON 이 아니면 ValueError(JSONDecodeError) 를 던진다.
        return json.loads(self.text)


class FakePost:
    """미리 정한 응답/예외를 순서대로 돌려주고, 호출 내역을 기록한다."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.outcomes:
            raise AssertionError("예상보다 많은 HTTP 요청이 발생했습니다")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def ok_response(data, *, stop_reason="end_turn", usage=None, request_id="req_ok", content=None, **body_extra):
    """구조화 출력(JSON 문자열)을 text 블록에 담은 200 응답."""
    if content is None:
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        content = [{"type": "text", "text": text}]
    body = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-6",
        "content": content,
        "stop_reason": stop_reason,
        "usage": usage if usage is not None else {"input_tokens": 1234, "output_tokens": 56},
    }
    body.update(body_extra)
    return FakeResponse(200, body, headers={"request-id": request_id})


def error_response(status, error_type, message, *, request_id="req_err", headers=None):
    """Anthropic 형식의 오류 응답 {"type":"error","error":{...},"request_id":...}."""
    body = {"type": "error", "error": {"type": error_type, "message": message}, "request_id": request_id}
    return FakeResponse(status, body, headers=headers or {}, reason="Error")


COMMIT_DATA = {
    "summary": "요약",
    "type": "feat",
    "scope": "cli",
    "subject": "커밋 메시지 생성 명령 추가",
    "body": ["cli.py: commit 명령 추가"],
}


def make_payload(**overrides):
    kwargs = dict(
        model=config.DEFAULT_MODEL,
        system="system prompt",
        user_content="user prompt",
        max_tokens=config.DEFAULT_MAX_TOKENS,
        temperature=config.DEFAULT_TEMPERATURE,
        effort=config.DEFAULT_EFFORT,
        schema=COMMIT_SCHEMA,
    )
    kwargs.update(overrides)
    return build_payload(**kwargs)


class _CallTestBase(unittest.TestCase):
    """sleep / on_retry 호출을 기록하는 공통 도우미."""

    def setUp(self):
        self.sleeps = []
        self.retries = []
        self.payload = make_payload()

    def call(self, post, **kwargs):
        kwargs.setdefault("sleep", self.sleeps.append)  # 실제로 잠들지 않음
        kwargs.setdefault("on_retry", lambda attempt, reason: self.retries.append((attempt, reason)))
        return call_messages(FAKE_KEY, self.payload, post=post, **kwargs)

    def call_expect_error(self, post, **kwargs):
        with self.assertRaises(AIClientError) as ctx:
            self.call(post, **kwargs)
        return ctx.exception


# ---------------------------------------------------------------------------
# 요청 구성
# ---------------------------------------------------------------------------
class BuildPayloadTest(unittest.TestCase):
    def test_full_payload_structure(self):
        """model/max_tokens/system/messages/temperature/output_config 가 모두 들어간다."""
        payload = make_payload()
        self.assertEqual(payload["model"], "claude-opus-4-6")
        self.assertEqual(payload["max_tokens"], 2048)
        self.assertEqual(payload["system"], "system prompt")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "user prompt"}])
        self.assertEqual(payload["temperature"], 0.2)
        self.assertEqual(payload["output_config"]["effort"], "medium")
        self.assertEqual(payload["output_config"]["format"], {"type": "json_schema", "schema": COMMIT_SCHEMA})

    def test_temperature_omitted_when_none(self):
        """temperature=None 이면 temperature 키 자체가 없다(미지원 모델용)."""
        payload = make_payload(temperature=None)
        self.assertNotIn("temperature", payload)

    def test_temperature_zero_is_kept(self):
        """temperature=0.0 은 falsy 지만 None 이 아니므로 그대로 전송된다."""
        payload = make_payload(temperature=0.0)
        self.assertIn("temperature", payload)
        self.assertEqual(payload["temperature"], 0.0)

    def test_effort_omitted_when_empty(self):
        """effort 가 없으면 output_config 에 format 만 남는다."""
        payload = make_payload(effort=None)
        self.assertEqual(set(payload["output_config"]), {"format"})

    def test_pr_schema_is_used_as_is(self):
        """PR 스키마를 주면 그 스키마가 json_schema 로 실린다."""
        payload = make_payload(schema=PR_SCHEMA)
        self.assertIs(payload["output_config"]["format"]["schema"], PR_SCHEMA)

    def test_payload_is_json_serializable(self):
        """requests 의 json= 으로 보낼 수 있도록 JSON 직렬화가 가능해야 한다."""
        json.dumps(make_payload())


class BuildHeadersTest(unittest.TestCase):
    def test_headers(self):
        """x-api-key, anthropic-version 2023-06-01, content-type 헤더를 만든다."""
        self.assertEqual(
            build_headers(FAKE_KEY),
            {"x-api-key": FAKE_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        )


class GetApiKeyTest(unittest.TestCase):
    def test_anthropic_key_takes_precedence(self):
        """ANTHROPIC_API_KEY 가 AI_API_KEY 보다 우선한다."""
        env = {"ANTHROPIC_API_KEY": "key-a", "AI_API_KEY": "key-b"}
        self.assertEqual(get_api_key(env), "key-a")

    def test_falls_back_to_ai_api_key(self):
        """ANTHROPIC_API_KEY 가 없으면 AI_API_KEY 를 쓴다."""
        self.assertEqual(get_api_key({"AI_API_KEY": "key-b"}), "key-b")

    def test_blank_value_is_ignored(self):
        """공백뿐인 ANTHROPIC_API_KEY 는 무시하고 다음 후보로 넘어간다."""
        self.assertEqual(get_api_key({"ANTHROPIC_API_KEY": "   ", "AI_API_KEY": "key-b"}), "key-b")

    def test_value_is_stripped(self):
        """앞뒤 공백/줄바꿈은 잘라낸다."""
        self.assertEqual(get_api_key({"ANTHROPIC_API_KEY": "  key-a\n"}), "key-a")

    def test_none_when_missing(self):
        """둘 다 없거나 비어 있으면 None."""
        self.assertIsNone(get_api_key({}))
        self.assertIsNone(get_api_key({"ANTHROPIC_API_KEY": "", "AI_API_KEY": " "}))

    def test_reads_os_environ_by_default(self):
        """인자를 생략하면 os.environ 을 읽는다(테스트에서는 가짜 값으로 바꿔치기)."""
        with mock.patch.dict(os.environ, {"AI_API_KEY": "key-from-env"}, clear=True):
            self.assertEqual(get_api_key(), "key-from-env")


class SupportsTemperatureTest(unittest.TestCase):
    def test_supported_models(self):
        """claude-opus-4-6 등 이전 세대 모델은 temperature 를 지원한다."""
        for model in ("claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5", config.DEFAULT_MODEL):
            with self.subTest(model=model):
                self.assertTrue(supports_temperature(model))

    def test_unsupported_models(self):
        """Opus 4.7+/5.x, Sonnet 5.x 는 temperature 를 받지 않는다."""
        for model in ("claude-opus-4-7", "claude-opus-5-5", "claude-sonnet-5-5"):
            with self.subTest(model=model):
                self.assertFalse(supports_temperature(model))


# ---------------------------------------------------------------------------
# 호출: 성공
# ---------------------------------------------------------------------------
class CallMessagesSuccessTest(_CallTestBase):
    def test_success_parses_structured_json(self):
        """200 응답의 text 블록 JSON 을 파싱하고 토큰 사용량/시도 횟수를 돌려준다."""
        post = FakePost(ok_response(COMMIT_DATA, request_id="req_123"))
        resp = self.call(post)

        self.assertIsInstance(resp, AIResponse)
        self.assertEqual(resp.data, COMMIT_DATA)
        self.assertEqual(json.loads(resp.raw_text), COMMIT_DATA)
        self.assertEqual(resp.model, "claude-opus-4-6")
        self.assertEqual(resp.stop_reason, "end_turn")
        self.assertEqual(resp.input_tokens, 1234)
        self.assertEqual(resp.output_tokens, 56)
        self.assertEqual(resp.request_id, "req_123")
        self.assertEqual(resp.attempts, 1)
        self.assertGreaterEqual(resp.elapsed, 0.0)
        self.assertEqual(len(post.calls), 1)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.retries, [])

    def test_request_is_built_correctly(self):
        """POST 대상 URL, 헤더, JSON 본문, (연결, 응답) 타임아웃이 올바르게 전달된다."""
        post = FakePost(ok_response(COMMIT_DATA))
        self.call(post, timeout=30)
        url, kwargs = post.calls[0]
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(kwargs["headers"]["x-api-key"], FAKE_KEY)
        self.assertEqual(kwargs["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(kwargs["headers"]["content-type"], "application/json")
        self.assertIs(kwargs["json"], self.payload)
        self.assertEqual(kwargs["timeout"], (10, 30))

    def test_only_text_blocks_are_used(self):
        """thinking 등 다른 블록은 무시하고 text 블록만 이어 붙여 파싱한다."""
        content = [
            {"type": "thinking", "thinking": "생각 중..."},
            {"type": "text", "text": json.dumps(COMMIT_DATA)},
        ]
        resp = self.call(FakePost(ok_response(None, content=content)))
        self.assertEqual(resp.data, COMMIT_DATA)

    def test_missing_usage_defaults_to_zero(self):
        """usage 가 비어 있어도 토큰 수는 0 으로 처리한다."""
        resp = self.call(FakePost(ok_response(COMMIT_DATA, usage={})))
        self.assertEqual((resp.input_tokens, resp.output_tokens), (0, 0))


# ---------------------------------------------------------------------------
# 호출: HTTP 오류
# ---------------------------------------------------------------------------
class CallMessagesHttpErrorTest(_CallTestBase):
    def test_401_fails_immediately_without_retry(self):
        """401 인증 실패는 다시 보내도 같으므로 재시도 없이 즉시 AIClientError."""
        post = FakePost(error_response(401, "authentication_error", "invalid x-api-key", request_id="req_401"))
        err = self.call_expect_error(post)

        self.assertEqual(err.kind, "http")
        self.assertEqual(err.status, 401)
        self.assertEqual(err.error_type, "authentication_error")
        self.assertEqual(err.request_id, "req_401")
        self.assertEqual(err.message, "invalid x-api-key")
        self.assertEqual(err.hint, STATUS_HINTS[401])
        self.assertIn("API Key", err.hint)
        self.assertEqual(err.attempts, 1)
        self.assertEqual(len(post.calls), 1)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.retries, [])

    def test_describe_includes_status_hint_and_request_id(self):
        """describe() 는 HTTP 상태/오류 타입, 안내(→), request_id 를 사람이 읽을 형태로 보여 준다."""
        err = self.call_expect_error(FakePost(error_response(401, "authentication_error", "invalid x-api-key")))
        text = err.describe()
        self.assertIn("HTTP 401 authentication_error", text)
        self.assertIn("invalid x-api-key", text)
        self.assertIn("→ " + STATUS_HINTS[401], text)
        self.assertIn("request_id: req_err", text)

    def test_400_temperature_gives_temperature_hint(self):
        """temperature 관련 400 은 '모델이 temperature 미지원' 안내로 바뀌고 재시도하지 않는다."""
        post = FakePost(
            error_response(400, "invalid_request_error", "temperature: is not supported for this model")
        )
        self.payload = make_payload(model="claude-opus-5-5")
        err = self.call_expect_error(post)
        self.assertEqual(err.status, 400)
        self.assertEqual(err.error_type, "invalid_request_error")
        self.assertIn("temperature", err.hint)
        self.assertIn("claude-opus-5-5", err.hint)
        self.assertIn(config.DEFAULT_MODEL, err.hint)
        self.assertEqual(err.attempts, 1)
        self.assertEqual(len(post.calls), 1)

    def test_400_effort_gives_effort_hint(self):
        """effort 관련 400 은 --effort 안내."""
        err = self.call_expect_error(
            FakePost(error_response(400, "invalid_request_error", "output_config.effort: invalid value 'max'"))
        )
        self.assertIn("--effort", err.hint)

    def test_400_max_tokens_gives_max_tokens_hint(self):
        """max_tokens 관련 400 은 --max-tokens 안내."""
        err = self.call_expect_error(
            FakePost(error_response(400, "invalid_request_error", "max_tokens: 999999 > 128000"))
        )
        self.assertIn("--max-tokens", err.hint)

    def test_other_400_gets_generic_hint(self):
        """특정 키워드가 없는 400 은 일반 안내(STATUS_HINTS[400])."""
        err = self.call_expect_error(FakePost(error_response(400, "invalid_request_error", "messages: required")))
        self.assertEqual(err.hint, STATUS_HINTS[400])

    def test_404_model_not_found_hint(self):
        """404 는 --model 확인 안내."""
        err = self.call_expect_error(FakePost(error_response(404, "not_found_error", "model: claude-x")))
        self.assertEqual(err.hint, STATUS_HINTS[404])
        self.assertEqual(err.attempts, 1)

    def test_429_then_200_succeeds_on_second_attempt(self):
        """429 후 200 이면 한 번 재시도해 성공하고 attempts=2, on_retry 1회."""
        post = FakePost(
            error_response(429, "rate_limit_error", "rate limited", headers={"retry-after": "3"}),
            ok_response(COMMIT_DATA),
        )
        resp = self.call(post)
        self.assertEqual(resp.attempts, 2)
        self.assertEqual(resp.data, COMMIT_DATA)
        self.assertEqual(len(post.calls), 2)
        self.assertEqual(len(self.retries), 1)
        self.assertEqual(self.retries[0][0], 1)
        self.assertIn("rate limited", self.retries[0][1])
        self.assertEqual(self.sleeps, [3.0])  # retry-after 헤더 값 사용

    def test_retry_delay_is_clamped_and_defaults(self):
        """retry-after 는 1~10초로 제한되고, 숫자가 아니거나 없으면 2초."""
        cases = [("100", 10.0), ("0", 1.0), ("abc", 2.0), (None, 2.0)]
        for header, expected in cases:
            with self.subTest(retry_after=header):
                self.sleeps = []
                headers = {"retry-after": header} if header is not None else {}
                post = FakePost(
                    error_response(429, "rate_limit_error", "rate limited", headers=headers),
                    ok_response(COMMIT_DATA),
                )
                self.call(post)
                self.assertEqual(self.sleeps, [expected])

    def test_two_529_overloaded_gives_up_after_second_attempt(self):
        """529 가 두 번 연속이면 2번째에서 포기한다(3번째 요청은 절대 없음)."""
        post = FakePost(
            error_response(529, "overloaded_error", "Overloaded"),
            error_response(529, "overloaded_error", "Overloaded", request_id="req_529_2"),
        )
        err = self.call_expect_error(post)
        self.assertEqual(err.kind, "http")
        self.assertEqual(err.status, 529)
        self.assertEqual(err.error_type, "overloaded_error")
        self.assertEqual(err.request_id, "req_529_2")
        self.assertEqual(err.hint, STATUS_HINTS[529])
        self.assertEqual(err.attempts, 2)
        self.assertEqual(len(post.calls), 2)
        self.assertEqual(len(self.retries), 1)
        self.assertEqual(len(self.sleeps), 1)

    def test_max_attempts_one_disables_retry(self):
        """max_attempts=1 이면 429 라도 재시도하지 않는다."""
        post = FakePost(error_response(429, "rate_limit_error", "rate limited"))
        err = self.call_expect_error(post, max_attempts=1)
        self.assertEqual(err.attempts, 1)
        self.assertEqual(len(post.calls), 1)
        self.assertEqual(self.retries, [])

    def test_unlisted_5xx_uses_server_error_hint(self):
        """STATUS_HINTS 에 없는 5xx(503) 도 서버 오류 안내를 받고, 재시도 대상이다."""
        post = FakePost(
            error_response(503, "api_error", "unavailable"),
            error_response(503, "api_error", "unavailable"),
        )
        err = self.call_expect_error(post)
        self.assertEqual(err.hint, STATUS_HINTS[500])
        self.assertEqual(err.attempts, 2)

    def test_non_json_error_body_is_handled(self):
        """오류 응답 본문이 HTML 등 JSON 이 아니어도 본문/헤더로 오류를 만든다."""
        html = "<html><body><h1>502 Bad Gateway</h1></body></html>"
        post = FakePost(
            FakeResponse(502, text=html, headers={"request-id": "req_html"}, reason="Bad Gateway"),
            FakeResponse(502, text=html, headers={"request-id": "req_html"}, reason="Bad Gateway"),
        )
        err = self.call_expect_error(post)
        self.assertEqual(err.kind, "http")
        self.assertEqual(err.status, 502)
        self.assertIsNone(err.error_type)
        self.assertEqual(err.request_id, "req_html")
        self.assertIn("502 Bad Gateway", err.message)
        self.assertEqual(err.hint, STATUS_HINTS[500])
        self.assertEqual(err.attempts, 2)

    def test_empty_error_body_uses_reason(self):
        """오류 본문이 비어 있으면 HTTP reason 문구를 메시지로 쓴다."""
        post = FakePost(FakeResponse(403, text="", reason="Forbidden"))
        err = self.call_expect_error(post)
        self.assertEqual(err.message, "Forbidden")
        self.assertEqual(err.hint, STATUS_HINTS[403])
        self.assertEqual(len(post.calls), 1)

    # 회귀 테스트: 프록시/게이트웨이가 객체가 아닌 JSON 본문을 보내도 AIClientError 로 정리되는지 확인한다.
    def test_json_but_not_object_error_body_is_handled(self):
        """오류 본문이 JSON 배열/문자열이어도 AIClientError 로 정리되어야 한다."""
        post = FakePost(FakeResponse(400, text='["Service Unavailable"]', reason="Bad Request"))
        err = self.call_expect_error(post)
        self.assertEqual(err.status, 400)


# ---------------------------------------------------------------------------
# 호출: 네트워크/타임아웃 예외
# ---------------------------------------------------------------------------
class CallMessagesNetworkErrorTest(_CallTestBase):
    def test_connection_error_twice_gives_network_error(self):
        """ConnectionError 가 두 번 나면 kind=network, attempts=2 로 포기한다."""
        post = FakePost(
            requests.exceptions.ConnectionError("dns failure"),
            requests.exceptions.ConnectionError("dns failure"),
        )
        err = self.call_expect_error(post)
        self.assertEqual(err.kind, "network")
        self.assertIsNone(err.status)
        self.assertEqual(err.attempts, 2)
        self.assertIn("ConnectionError", err.message)
        self.assertIn("인터넷", err.hint)
        self.assertIsInstance(err.__cause__, requests.exceptions.ConnectionError)
        self.assertEqual(len(post.calls), 2)
        self.assertEqual(len(self.retries), 1)
        self.assertEqual(self.sleeps, [2.0])

    def test_connection_error_then_success(self):
        """일시적 네트워크 오류 후 성공하면 결과를 돌려주고 attempts=2."""
        post = FakePost(requests.exceptions.ConnectionError("reset"), ok_response(COMMIT_DATA))
        resp = self.call(post)
        self.assertEqual(resp.attempts, 2)
        self.assertEqual(resp.data, COMMIT_DATA)

    def test_timeout_gives_timeout_error(self):
        """Timeout 이 계속되면 kind=timeout 이고 --timeout 안내가 붙는다."""
        post = FakePost(requests.exceptions.ReadTimeout("read timed out"), requests.exceptions.Timeout("timed out"))
        err = self.call_expect_error(post, timeout=45)
        self.assertEqual(err.kind, "timeout")
        self.assertEqual(err.attempts, 2)
        self.assertIn("45초", err.message)
        self.assertIn("--timeout", err.hint)
        self.assertIsInstance(err.__cause__, requests.exceptions.Timeout)
        self.assertEqual(len(post.calls), 2)

    def test_connect_timeout_is_classified_as_timeout(self):
        """ConnectTimeout(Timeout 이자 ConnectionError)은 timeout 으로 분류된다."""
        post = FakePost(requests.exceptions.ConnectTimeout("connect timed out"))
        err = self.call_expect_error(post, max_attempts=1)
        self.assertEqual(err.kind, "timeout")
        self.assertEqual(err.attempts, 1)

    def test_unexpected_exception_is_not_swallowed(self):
        """requests 예외가 아닌 버그성 예외는 AIClientError 로 감추지 않고 그대로 올라온다."""
        post = FakePost(KeyError("boom"))
        with self.assertRaises(KeyError):
            self.call(post)
        self.assertEqual(len(post.calls), 1)


# ---------------------------------------------------------------------------
# 호출: 200 이지만 쓸 수 없는 응답
# ---------------------------------------------------------------------------
class CallMessagesBadResponseTest(_CallTestBase):
    def test_stop_reason_max_tokens(self):
        """stop_reason=max_tokens 면 잘린 JSON 을 파싱하지 않고 --max-tokens 안내 오류."""
        post = FakePost(
            ok_response('{"summary": "잘린', stop_reason="max_tokens", usage={"input_tokens": 10, "output_tokens": 2048})
        )
        err = self.call_expect_error(post)
        self.assertEqual(err.kind, "response")
        self.assertIn("max_tokens", err.message)
        self.assertIn("2048", err.message)
        self.assertIn("--max-tokens", err.hint)
        self.assertEqual(err.attempts, 1)
        self.assertEqual(len(post.calls), 1)  # 응답 문제는 재시도하지 않음

    def test_stop_reason_refusal(self):
        """stop_reason=refusal 이면 거절 오류(카테고리 포함)."""
        post = FakePost(ok_response("", stop_reason="refusal", stop_details={"category": "cyber"}))
        err = self.call_expect_error(post)
        self.assertEqual(err.kind, "response")
        self.assertIn("거절", err.message)
        self.assertIn("cyber", err.message)
        self.assertIsNotNone(err.hint)
        self.assertEqual(len(post.calls), 1)

    def test_non_json_text(self):
        """text 블록이 JSON 이 아니면 response 오류."""
        err = self.call_expect_error(FakePost(ok_response("커밋 메시지: feat: 무언가 추가")))
        self.assertEqual(err.kind, "response")
        self.assertIn("JSON", err.message)
        self.assertIsInstance(err.__cause__, ValueError)

    def test_json_array_is_rejected(self):
        """JSON 이지만 최상위가 객체가 아니면 오류."""
        err = self.call_expect_error(FakePost(ok_response("[1, 2, 3]")))
        self.assertEqual(err.kind, "response")
        self.assertIn("객체", err.message)

    def test_empty_content(self):
        """text 블록이 없으면 '텍스트 결과 없음' 오류."""
        err = self.call_expect_error(FakePost(ok_response(None, content=[])))
        self.assertEqual(err.kind, "response")
        self.assertIn("텍스트", err.message)

    def test_non_json_http_body_on_200(self):
        """200 인데 HTTP 본문 자체가 JSON 이 아니면 response 오류(request-id 헤더 보존)."""
        post = FakePost(FakeResponse(200, text="<html>proxy</html>", headers={"request-id": "req_proxy"}))
        err = self.call_expect_error(post)
        self.assertEqual(err.kind, "response")
        self.assertEqual(err.request_id, "req_proxy")
        self.assertEqual(err.attempts, 1)


# ---------------------------------------------------------------------------
# 비용 추정
# ---------------------------------------------------------------------------
class EstimateCostTest(unittest.TestCase):
    def test_known_model(self):
        """알려진 모델은 (입력, 출력) 단가로 USD 비용을 계산한다."""
        self.assertAlmostEqual(estimate_cost("claude-opus-4-6", 1_000_000, 1_000_000), 30.0)
        self.assertAlmostEqual(estimate_cost("claude-opus-4-6", 1000, 500), 0.005 + 0.0125)
        self.assertEqual(estimate_cost("claude-opus-4-6", 0, 0), 0.0)

    def test_unknown_model(self):
        """가격표에 없는 모델은 None."""
        self.assertIsNone(estimate_cost("claude-unknown-model", 1000, 1000))


if __name__ == "__main__":
    unittest.main()
