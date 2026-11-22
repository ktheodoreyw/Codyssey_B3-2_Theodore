"""Claude Messages API REST 클라이언트 (requests 사용).

흐름: 요청 구성(build_payload) → HTTP POST(call_messages) → 응답/예외 처리 → JSON 파싱

  POST https://api.anthropic.com/v1/messages
  Headers:
    x-api-key: <환경변수의 API Key>
    anthropic-version: 2023-06-01
    content-type: application/json
  Body:
    model, max_tokens, temperature, system, messages,
    output_config: { effort, format: { type: json_schema, schema } }

오류는 원인별로 AIClientError 하나로 모아, 사용자에게 '무엇이 왜 실패했고 어떻게
고치면 되는지'를 담아 보여 준다.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

import requests

from . import config

# 재시도할 가치가 있는(일시적인) HTTP 상태 코드
RETRYABLE_STATUS = {429, 500, 502, 503, 504, 529}

# 요청 자체가 잘못되어 다시 보내도 결과가 같은 requests 예외
NON_RETRYABLE_REQUEST_ERRORS = (
    requests.exceptions.InvalidHeader,
    requests.exceptions.InvalidURL,  # InvalidProxyURL 포함
    requests.exceptions.MissingSchema,
    requests.exceptions.InvalidSchema,
)

# 서버와 TCP 연결을 맺기까지 기다리는 시간(초). 응답 대기 시간은 --timeout 으로 따로 정한다.
CONNECT_TIMEOUT = 10.0

# HTTP 상태 코드별 사용자 안내
STATUS_HINTS = {
    400: "요청 형식이나 파라미터가 올바르지 않습니다. --model / --max-tokens / --effort 값을 확인하세요.",
    401: "인증에 실패했습니다. API Key 가 올바른지 확인하세요. (환경변수 ANTHROPIC_API_KEY 또는 AI_API_KEY)",
    402: "결제/크레딧 문제입니다. Anthropic Console 의 결제 상태를 확인하세요.",
    403: "이 API Key 로는 해당 요청(모델)을 사용할 권한이 없습니다.",
    404: "모델을 찾을 수 없습니다. --model 값(예: claude-opus-4-6)을 확인하세요.",
    413: "요청이 너무 큽니다. safe-mode 를 켜서(--safe-mode) 전송 diff 를 줄이세요.",
    429: "요청 한도(rate limit)를 초과했습니다. 잠시 후 다시 실행하세요.",
    500: "Anthropic 서버 내부 오류입니다. 잠시 후 다시 실행하세요.",
    529: "Anthropic API 가 일시적으로 과부하 상태입니다. 잠시 후 다시 실행하세요.",
}


class AIClientError(Exception):
    """API 호출 실패. 사용자에게 보여 줄 원인/안내를 담는다."""

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        status: Optional[int] = None,
        error_type: Optional[str] = None,
        request_id: Optional[str] = None,
        hint: Optional[str] = None,
        attempts: int = 0,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind  # network / timeout / http / response
        self.status = status
        self.error_type = error_type
        self.request_id = request_id
        self.hint = hint
        self.attempts = attempts

    def describe(self) -> str:
        head = "AI API 호출 실패"
        if self.status is not None:
            head += f" (HTTP {self.status}{' ' + self.error_type if self.error_type else ''})"
        lines = [f"{head}: {self.message}"]
        if self.hint:
            lines.append(f"→ {self.hint}")
        if self.request_id:
            lines.append(f"request_id: {self.request_id}")
        return "\n".join(lines)


@dataclass
class AIResponse:
    data: Dict[str, Any]  # 구조화 출력(JSON) 파싱 결과
    raw_text: str
    model: str
    stop_reason: Optional[str]
    input_tokens: int
    output_tokens: int
    request_id: Optional[str]
    attempts: int
    elapsed: float
    extra: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# API Key
# ---------------------------------------------------------------------------
def get_api_key(environ: Optional[Dict[str, str]] = None) -> Optional[str]:
    env = os.environ if environ is None else environ
    for name in config.API_KEY_ENV_VARS:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return None


def api_key_problem(key: str) -> Optional[str]:
    """API Key 형식 문제를 사람이 읽을 수 있는 문장으로 돌려준다(문제 없으면 None).

    PDF·메신저에서 복사할 때 섞여 들어오는 공백/줄바꿈/보이지 않는 문자/전각 하이픈은
    HTTP 헤더에 넣을 수 없어 requests 내부에서 알아보기 힘든 예외가 난다. 미리 걸러 안내한다.
    키 값 자체는 절대 출력하지 않는다.
    """
    if any(ch.isspace() for ch in key):
        return "API Key 중간에 공백 또는 줄바꿈이 있습니다."
    if not all(33 <= ord(ch) <= 126 for ch in key):
        return "API Key 에 영문/숫자/기호가 아닌 문자(보이지 않는 문자, 전각 하이픈 등)가 섞여 있습니다."
    return None


def supports_temperature(model: str) -> bool:
    return not model.startswith(config.TEMPERATURE_UNSUPPORTED_PREFIXES)


def supports_effort(model: str) -> bool:
    return not model.startswith(config.EFFORT_UNSUPPORTED_PREFIXES)


# ---------------------------------------------------------------------------
# 요청 구성
# ---------------------------------------------------------------------------
def build_payload(
    *,
    model: str,
    system: str,
    user_content: str,
    max_tokens: int,
    temperature: Optional[float],
    effort: Optional[str],
    schema: Dict[str, Any],
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user_content}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if effort:
        payload["output_config"]["effort"] = effort
    return payload


def build_headers(api_key: str) -> Dict[str, str]:
    return {
        "x-api-key": api_key,
        "anthropic-version": config.ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


# ---------------------------------------------------------------------------
# 호출
# ---------------------------------------------------------------------------
def call_messages(
    api_key: str,
    payload: Dict[str, Any],
    *,
    timeout: float = config.DEFAULT_TIMEOUT,
    max_attempts: int = config.MAX_API_ATTEMPTS,
    post: Callable[..., requests.Response] = requests.post,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Optional[Callable[[int, str], None]] = None,
) -> AIResponse:
    """Messages API 를 호출하고 구조화 출력(JSON)을 파싱해 돌려준다.

    일시적 오류(네트워크, 타임아웃, 429/5xx)는 최대 max_attempts 회까지 시도한다.
    인증 실패(401)처럼 다시 보내도 결과가 같은 오류는 즉시 실패시킨다.
    """
    headers = build_headers(api_key)
    started = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = post(
                config.API_URL,
                headers=headers,
                json=payload,
                timeout=(CONNECT_TIMEOUT, timeout),  # (연결 타임아웃, 응답 대기 타임아웃)
            )
        except (requests.exceptions.ConnectTimeout, requests.exceptions.Timeout) as exc:
            if isinstance(exc, requests.exceptions.ConnectTimeout):
                message = f"API 서버 연결 시간({CONNECT_TIMEOUT:.0f}초)을 초과했습니다."
                hint = "인터넷 연결, 프록시/방화벽 설정을 확인하세요."
            else:
                message = f"응답 대기 시간({timeout:.0f}초)을 초과했습니다."
                hint = "네트워크 상태를 확인하거나 --timeout 값을 늘려 다시 실행하세요."
            err = AIClientError(message, kind="timeout", hint=hint, attempts=attempt)
            if _can_retry(attempt, max_attempts, on_retry, err.message):
                sleep(2.0)
                continue
            raise err from exc
        except requests.exceptions.SSLError as exc:
            # 인증서 검증 실패는 다시 보내도 같으므로 재시도하지 않는다.
            raise AIClientError(
                "HTTPS 인증서 검증에 실패했습니다. (SSLError)",
                kind="network",
                hint="회사/학교 프록시가 HTTPS 를 가로채는 환경인지 확인하세요.",
                attempts=attempt,
            ) from exc
        except NON_RETRYABLE_REQUEST_ERRORS as exc:
            # 잘못된 헤더(API Key 에 줄바꿈 등)·URL 처럼 요청 자체가 잘못된 경우: 재시도해도 같다.
            if isinstance(exc, requests.exceptions.InvalidHeader):
                hint = "API Key 환경변수 값에 공백/줄바꿈/특수문자가 없는지 확인하세요."
            else:
                hint = "프록시 환경변수(HTTPS_PROXY / HTTP_PROXY)의 URL 형식을 확인하세요."
            raise AIClientError(
                f"요청을 만들 수 없습니다. ({type(exc).__name__})",
                kind="request",
                hint=hint,
                attempts=attempt,
            ) from exc
        except requests.exceptions.RequestException as exc:
            err = AIClientError(
                f"네트워크 오류로 API 서버에 연결하지 못했습니다. ({type(exc).__name__})",
                kind="network",
                hint="인터넷 연결, 프록시/방화벽 설정을 확인하세요.",
                attempts=attempt,
            )
            if _can_retry(attempt, max_attempts, on_retry, err.message):
                sleep(2.0)
                continue
            raise err from exc
        except (UnicodeError, ValueError) as exc:
            # 예) 헤더에 넣을 수 없는 문자가 섞인 API Key → http.client 의 UnicodeEncodeError
            raise AIClientError(
                f"요청을 만들 수 없습니다. ({type(exc).__name__})",
                kind="request",
                hint="API Key 환경변수 값에 보이지 않는 문자/전각 문자가 섞이지 않았는지 확인하세요.",
                attempts=attempt,
            ) from exc

        if resp.status_code == 200:
            return _parse_success(resp, attempt, time.monotonic() - started)

        err = _http_error(resp, attempt, payload)
        if resp.status_code in RETRYABLE_STATUS and _can_retry(attempt, max_attempts, on_retry, err.message):
            sleep(_retry_delay(resp))
            continue
        raise err


def _can_retry(attempt: int, max_attempts: int, on_retry: Optional[Callable[[int, str], None]], reason: str) -> bool:
    if attempt >= max_attempts:
        return False
    if on_retry:
        on_retry(attempt, reason)
    return True


def _retry_delay(resp: requests.Response) -> float:
    try:
        return min(10.0, max(1.0, float(resp.headers.get("retry-after", "2"))))
    except ValueError:
        return 2.0


def _http_error(resp: requests.Response, attempt: int, payload: Dict[str, Any]) -> AIClientError:
    """오류 응답 본문 {"type":"error","error":{"type":..,"message":..},"request_id":..} 을 해석한다."""
    error_type = None
    message = (resp.text or "").strip()[:300] or resp.reason or "알 수 없는 오류"
    request_id = resp.headers.get("request-id")
    try:
        body = resp.json()
    except ValueError:
        body = None
    # 프록시/게이트웨이가 보낸 비표준 응답({"error": "Forbidden"}, 리스트 등)도 견딘다.
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            error_type = err.get("type") if isinstance(err.get("type"), str) else None
            if isinstance(err.get("message"), str) and err["message"]:
                message = err["message"]
        elif isinstance(err, str) and err:
            message = err
        if isinstance(body.get("request_id"), str):
            request_id = body["request_id"]

    hint = STATUS_HINTS.get(resp.status_code)
    if hint is None and resp.status_code >= 500:
        hint = STATUS_HINTS[500]
    lowered = message.lower()
    if resp.status_code == 400 and "temperature" in lowered:
        hint = (
            f"'{payload.get('model')}' 모델 또는 API 버전이 temperature 를 지원하지 않습니다. "
            f"temperature 를 지원하는 모델(예: {config.DEFAULT_MODEL})을 사용하세요."
        )
    elif resp.status_code == 400 and "effort" in lowered:
        hint = (
            f"'{payload.get('model')}' 모델이 effort 옵션(또는 해당 값)을 지원하지 않을 수 있습니다. "
            f"--effort 값을 낮추거나 effort 를 지원하는 모델(예: {config.DEFAULT_MODEL})을 사용하세요."
        )
    elif resp.status_code == 400 and "max_tokens" in lowered:
        hint = "--max-tokens 값이 모델 허용 범위를 벗어났습니다."

    return AIClientError(
        message,
        kind="http",
        status=resp.status_code,
        error_type=error_type,
        request_id=request_id,
        hint=hint,
        attempts=attempt,
    )


def _parse_success(resp: requests.Response, attempt: int, elapsed: float) -> AIResponse:
    request_id = resp.headers.get("request-id")
    try:
        body = resp.json()
    except ValueError as exc:
        raise AIClientError(
            "API 응답이 JSON 형식이 아닙니다.", kind="response", request_id=request_id, attempts=attempt
        ) from exc
    if not isinstance(body, dict):
        raise AIClientError(
            "API 응답 JSON 의 최상위 구조가 객체가 아닙니다.", kind="response", request_id=request_id, attempts=attempt
        )

    request_id = body.get("request_id") or request_id
    stop_reason = body.get("stop_reason")
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}

    if stop_reason == "refusal":
        details = body.get("stop_details") if isinstance(body.get("stop_details"), dict) else {}
        raise AIClientError(
            f"모델이 요청 처리를 거절했습니다. (category={details.get('category')})",
            kind="response",
            request_id=request_id,
            hint="diff 에 민감하거나 정책에 걸리는 내용이 있는지 확인하세요.",
            attempts=attempt,
        )
    if stop_reason == "max_tokens":
        raise AIClientError(
            f"응답이 max_tokens 한도에서 잘렸습니다. (출력 {usage.get('output_tokens', '?')} 토큰)",
            kind="response",
            request_id=request_id,
            hint="--max-tokens 값을 늘려 다시 실행하세요.",
            attempts=attempt,
        )

    # content 는 블록 배열이다. 구조화 출력은 text 블록에 JSON 문자열로 들어 있다.
    # (thinking 등 다른 종류의 블록은 건너뛴다)
    blocks = body.get("content") if isinstance(body.get("content"), list) else []
    text = "".join(
        block["text"]
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ).strip()
    if not text:
        raise AIClientError(
            "API 응답에 텍스트 결과가 없습니다.", kind="response", request_id=request_id, attempts=attempt
        )
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise AIClientError(
            "AI 응답을 JSON 으로 해석하지 못했습니다.",
            kind="response",
            request_id=request_id,
            hint="다시 실행하거나 --max-tokens 값을 늘려 보세요.",
            attempts=attempt,
        ) from exc
    if not isinstance(data, dict):
        raise AIClientError(
            "AI 응답 JSON 의 최상위 구조가 객체가 아닙니다.", kind="response", request_id=request_id, attempts=attempt
        )

    return AIResponse(
        data=data,
        raw_text=text,
        model=body.get("model") or "",
        stop_reason=stop_reason,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        request_id=request_id,
        attempts=attempt,
        elapsed=elapsed,
    )


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> Optional[float]:
    price = config.MODEL_PRICES.get(model)
    if price is None:
        return None
    in_price, out_price = price
    return input_tokens / 1_000_000 * in_price + output_tokens / 1_000_000 * out_price
