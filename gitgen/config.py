"""도구 전역 설정값.

CLI 옵션의 기본값과 safe-mode 정책, API 접속 정보를 한 곳에 모아 둔다.
값을 바꿀 때 코드 여기저기를 찾아다니지 않도록 하기 위함이다.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# API Key 환경변수
# ---------------------------------------------------------------------------
# 앞에 있는 이름부터 차례로 확인한다. ANTHROPIC_API_KEY 는 Anthropic 공식 이름,
# AI_API_KEY 는 과제 설명서 예시에서 사용한 이름이다. 둘 다 지원한다.
API_KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "AI_API_KEY")

# 실행 시 환경변수로 불러올 .env 파일 위치: ai-gitgen 폴더(main.py 가 있는 곳)
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(PROJECT_DIR, ".env")

# ---------------------------------------------------------------------------
# Claude Messages API (REST)
# ---------------------------------------------------------------------------
API_URL = "https://api.anthropic.com/v1/messages"

# anthropic-version 헤더 값.
# 2026-10-21 버전은 모든 모델에서 temperature 를 거부하므로(400),
# temperature 파라미터를 실제로 전달하기 위해 2023-06-01 버전을 사용한다.
ANTHROPIC_VERSION = "2023-06-01"

# ---------------------------------------------------------------------------
# CLI 기본값
# ---------------------------------------------------------------------------
# Claude Opus 4.6: temperature 를 지원하는 모델 중 가장 성능이 높은 모델.
# (Opus 4.7 이후 모델은 temperature 를 받지 않는다.)
DEFAULT_MODEL = "claude-opus-4-6"

# 커밋/PR 문구는 '창의성'보다 '일관성'과 '형식 준수'가 중요하므로 낮게 둔다.
DEFAULT_TEMPERATURE = 0.2

# 출력(JSON) 길이의 상한. 실제 사용량만 과금되므로 여유 있게 두되,
# 비정상적으로 긴 응답이 나와도 이 값에서 끊겨 비용이 제한된다.
DEFAULT_MAX_TOKENS = 2048

# effort: 모델이 응답에 들이는 노력(토큰 사용량) 수준.
# Opus 4.6 은 low / medium / high / max 를 지원한다.
EFFORT_CHOICES = ("low", "medium", "high", "max")
DEFAULT_EFFORT = "medium"

# 한 번의 HTTP 요청에 대한 타임아웃(초)
DEFAULT_TIMEOUT = 120.0

# 일시적 오류(429, 5xx, 네트워크) 시 최대 요청 횟수(첫 요청 포함).
# 과제 제약: 1회 실행당 요청 횟수는 1~2회 이내.
MAX_API_ATTEMPTS = 2

# temperature 를 받지 않는(400 오류가 나는) 모델 ID 접두사.
# 이 모델들을 --model 로 지정하면 temperature 를 요청에서 자동으로 제외한다.
TEMPERATURE_UNSUPPORTED_PREFIXES = (
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-5",
    "claude-fable",
    "claude-mythos",
)

# effort 를 받지 않는(400 오류가 나는) 모델 ID 접두사. 지정하면 effort 를 요청에서 제외한다.
EFFORT_UNSUPPORTED_PREFIXES = (
    "claude-3",
    "claude-haiku-4-5",
    "claude-sonnet-4-5",
    "claude-sonnet-4-0",
    "claude-sonnet-4-2",  # claude-sonnet-4-20250514
    "claude-opus-4-0",
    "claude-opus-4-1",
    "claude-opus-4-2",  # claude-opus-4-20250514
)

# 비용 추정용 정가(USD / 1M tokens). (입력, 출력)
MODEL_PRICES = {
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-5-5": (0.10, 0.50),
}

# ---------------------------------------------------------------------------
# safe-mode 정책
# ---------------------------------------------------------------------------
# safe-mode ON: 아래 한도까지만 diff 를 전송한다. (과제 예시 기준: 10개 파일, 200줄)
SAFE_MAX_FILES = 10
SAFE_MAX_LINES = 200

# safe-mode OFF 여도 비용 폭주를 막기 위한 하드 상한(문자 수, 약 4만 토큰 내외).
HARD_MAX_CHARS = 150_000

# ---------------------------------------------------------------------------
# 출력 형식 규칙
# ---------------------------------------------------------------------------
COMMIT_TITLE_RECOMMENDED = 50
COMMIT_TITLE_MAX = 72
PR_TITLE_MAX = 80
COMMIT_BULLETS_MAX = 3
PR_SECTION_BULLETS_MAX = 5
