""".env 파일 → 환경변수 로더.

API Key 는 코드에 적지 않고 '환경변수'로만 읽는다(ai_client.get_api_key).
매번 터미널에서 export 하지 않아도 되도록, 프로그램 시작 시 .env 파일의 값을
환경변수(os.environ)에 등록한다.

  .env (ai-gitgen 폴더, main.py 옆)          os.environ
  ┌──────────────────────────────┐  로드   ┌──────────────────────┐
  │ ANTHROPIC_API_KEY=sk-ant-... │ ──────▶ │ ANTHROPIC_API_KEY    │ ──▶ get_api_key()
  └──────────────────────────────┘         └──────────────────────┘

규칙
- .env 파일은 도구 폴더(config.ENV_FILE)에서 읽는다. 다른 프로젝트에서 실행해도 같은 파일을 쓴다.
- 이미 설정된 OS 환경변수가 있으면 .env 값으로 덮어쓰지 않는다(OS 환경변수 우선).
- .env 는 .gitignore 에 등록되어 커밋되지 않는다. 저장소에는 예시 파일(.env.example)만 올린다.
- 파일 형식 해석(따옴표, export, 주석 등)은 python-dotenv 가 맡는다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

from . import config


@dataclass
class EnvLoadResult:
    path: str
    found: bool = False  # .env 파일이 있었는지
    loaded: List[str] = field(default_factory=list)  # .env 에서 새로 등록한 변수 이름
    skipped: List[str] = field(default_factory=list)  # 이미 OS 환경변수가 있어 덮어쓰지 않은 이름
    error: Optional[str] = None  # 읽기 실패 사유


def load_env_file(path: Optional[str] = None) -> EnvLoadResult:
    """.env 파일의 값을 환경변수로 등록하고, 무엇을 했는지 돌려준다. 값 자체는 돌려주지 않는다."""
    path = path or config.ENV_FILE
    result = EnvLoadResult(path=path)
    if not os.path.isfile(path):
        return result
    result.found = True

    try:
        from dotenv import dotenv_values
    except ImportError:
        result.error = "python-dotenv 가 설치되어 있지 않아 .env 파일을 읽지 못했습니다. (pip install -r requirements.txt)"
        return result

    try:
        # utf-8-sig: 메모장 등이 붙이는 BOM 을 제거해야 첫 줄의 변수 이름이 깨지지 않는다.
        values = dotenv_values(path, encoding="utf-8-sig")
    except (UnicodeDecodeError, ValueError) as exc:
        result.error = f".env 파일을 읽지 못했습니다. UTF-8 로 저장했는지 확인하세요. ({type(exc).__name__})"
        return result
    except OSError as exc:
        result.error = f".env 파일을 열 수 없습니다. 파일 권한을 확인하세요. ({type(exc).__name__})"
        return result

    # API Key 이름들(ANTHROPIC_API_KEY, AI_API_KEY)은 한 묶음으로 본다.
    # OS 에 어느 하나라도 설정돼 있으면 .env 의 API Key 는 쓰지 않는다(OS 환경변수 우선).
    os_has_api_key = any(_is_set(n) for n in config.API_KEY_ENV_VARS)

    for name, value in values.items():
        if not value or not value.strip():  # 'KEY' / 'KEY=' 처럼 값이 비어 있는 줄
            continue
        if _is_set(name) or (name in config.API_KEY_ENV_VARS and os_has_api_key):
            result.skipped.append(name)
            continue
        os.environ[name] = value
        result.loaded.append(name)
    return result


def _is_set(name: str) -> bool:
    """공백뿐인 값은 설정되지 않은 것으로 본다(get_api_key 와 같은 기준)."""
    return bool((os.environ.get(name) or "").strip())
