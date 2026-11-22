# ai-gitgen — 내가 고친 코드 설명을 AI가 대신 써주는 도우미

`git status` / `git diff` 로 수집한 변경 사항을 Claude API(REST)에 보내 **커밋 메시지**와 **Pull Request 제목/본문 초안**을 만들어 터미널에 출력하는 Python CLI 입니다.

```
python main.py commit   →  변경 요약 + 커밋 메시지(제목 1줄 + 불릿 본문) + 검증 결과
python main.py pr       →  변경 요약 + PR 제목 + PR 본문(Why / What / How to Test) + 검증 결과
python main.py all      →  변경 요약 + 커밋 메시지 + PR 초안을 한 번에 (API 1회 호출)
```

API 를 호출하는 데서 끝나지 않고 **입력(프롬프트·컨텍스트) 설계 → 구조화 출력 → 규칙 검증·후처리**로 결과 품질을 코드에서 직접 제어합니다.

```
 git status ─┐
             ├─▶ 변경 파일/diff 수집 ─▶ safe-mode ─▶ 프롬프트 구성 ─▶ Claude API (1회) ─▶ 검증·후처리 ─▶ 터미널 출력
 git diff  ──┘     (git_collector)     (마스킹/제한)    (prompts)        (ai_client)       (formatter)      (cli)
```

## 목차

1. [설치](#1-설치)
2. [환경변수(API Key) 설정 — .env](#2-환경변수api-key-설정)
3. [사용법](#3-사용법)
4. [출력 예시](#4-출력-예시)
5. [민감정보 대응 (safe-mode)](#5-민감정보-대응-safe-mode)
6. [팀 커밋/PR 컨벤션 (보너스 2)](#6-팀-커밋pr-컨벤션-보너스-2)
7. [비용과 요청 횟수](#7-비용과-요청-횟수)
8. [API 파라미터와 모델 선택](#8-api-파라미터와-모델-선택)
9. [REST API 연동 흐름](#9-rest-api-연동-흐름-요청--응답--예외)
10. [프롬프트 설계](#10-프롬프트-설계)
11. [출력 검증 규칙](#11-출력-검증-규칙-후처리)
12. [프로젝트 구조](#12-프로젝트-구조)
13. [테스트](#13-테스트)
14. [주의사항](#14-주의사항)

---

## 1. 설치

**준비물**: Python 3.10 이상, Git, Claude API Key

```bash
git clone https://github.com/ktheodoreyw/Codyssey_B3-2_Theodore.git ai-gitgen
cd ai-gitgen
pip install -r requirements.txt      # 외부 의존성: requests(HTTP 호출), python-dotenv(.env 읽기)
cp .env.example .env                 # API Key 설정 파일 만들기 → 2장 참고
python main.py --help                # 설치 확인
```

> Windows 에서 `python` 명령이 없다고 나오면 `py` 를 사용하세요. (예: `py main.py commit`, `py -m pip install -r requirements.txt`)

가상환경을 쓰려면:

```bash
python -m venv .venv
source .venv/Scripts/activate        # Windows Git Bash (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
```

### 다른 프로젝트에서 사용하기

이 도구는 **커밋/PR 초안을 만들 프로젝트의 루트 디렉토리(.git 이 있는 곳)** 에서 실행해야 합니다. 도구 경로를 지정해 실행하면 됩니다.

```bash
cd ~/my-project                      # .git 이 있는 프로젝트 루트
python ~/ai-gitgen/main.py commit
```

## 2. 환경변수(API Key) 설정

API Key 는 코드에 적지 않고 **환경변수로만** 읽습니다(`ANTHROPIC_API_KEY` → 없으면 `AI_API_KEY`). 매번 터미널에서 `export` 하지 않도록, 프로그램이 시작할 때 **`.env` 파일의 값을 환경변수로 불러옵니다.**

```
 .env (ai-gitgen 폴더, main.py 옆)            환경변수(os.environ)              ai_client.get_api_key()
 ANTHROPIC_API_KEY=sk-ant-...      ──로드──▶  ANTHROPIC_API_KEY=sk-ant-...  ──▶  x-api-key 헤더로 전송
```

### 방법 1) `.env` 파일 (권장)

```bash
cp .env.example .env     # 예시 파일을 복사해 .env 를 만든다
```

`.env` 를 열어 `YOUR_KEY` 를 실제 키로 바꿉니다. (따옴표는 있어도 되고 없어도 됩니다)

```dotenv
ANTHROPIC_API_KEY=sk-ant-...
```

실행하면 로그에 **변수 이름만** 표시됩니다(키 값은 출력하지 않음).

```
[INFO] .env 파일에서 환경변수 1개를 불러왔습니다 (ANTHROPIC_API_KEY)
```

| 규칙 | 내용 |
|---|---|
| 파일 위치 | ai-gitgen 폴더의 `.env` (main.py 옆). 다른 프로젝트 폴더에서 `python ~/ai-gitgen/main.py commit` 으로 실행해도 이 파일을 읽습니다 |
| 우선순위 | 이미 설정된 OS 환경변수가 있으면 `.env` 값으로 **덮어쓰지 않습니다** (`이미 설정된 환경변수(...)는 .env 값으로 덮어쓰지 않습니다.` 로그 표시). API Key 는 이름이 달라도 한 묶음으로 봐서, OS 에 `ANTHROPIC_API_KEY` 나 `AI_API_KEY` 중 하나라도 있으면 `.env` 의 API Key 는 쓰지 않습니다. 공백뿐인 OS 값은 설정되지 않은 것으로 봅니다 |
| Git 관리 | `.gitignore` 의 `.env*` 규칙으로 `.env` 와 그 복사본(`.env.bak`, `.env~`, `.env copy` 등)은 **커밋되지 않습니다**. 저장소에는 예시 파일 `.env.example`(값: `YOUR_KEY`)만 올라갑니다 |
| 파일 형식 | `KEY=값`, `KEY="값"`, `export KEY=값`, `# 주석` 모두 가능. UTF-8 로 저장 (메모장의 BOM 도 처리) |
| 예시 값 검사 | `YOUR_KEY` 를 바꾸지 않고 실행하면 API 를 호출하기 전에 알려 줍니다 |

### 방법 2) OS 환경변수 직접 설정

`.env` 대신 터미널/OS 에 직접 설정해도 됩니다. (CI 서버처럼 파일을 둘 수 없는 환경에 적합)

| 터미널 | 현재 터미널에서만 | 영구 설정 |
|---|---|---|
| Git Bash / macOS / Linux | `export ANTHROPIC_API_KEY="sk-ant-..."` | `~/.bashrc`(또는 `~/.zshrc`)에 같은 줄 추가 |
| PowerShell | `$env:ANTHROPIC_API_KEY = "sk-ant-..."` | `[Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-...", "User")` |
| cmd | `set ANTHROPIC_API_KEY=sk-ant-...` | `setx ANTHROPIC_API_KEY "sk-ant-..."` (새 창부터 적용) |

### 키가 없거나 잘못됐을 때

```
$ python main.py commit
[ERROR] AI API Key 환경변수가 설정되지 않았습니다. (ANTHROPIC_API_KEY 또는 AI_API_KEY)
        .env 파일이 없습니다: C:\Users\me\ai-gitgen\.env
        방법 1) .env 파일(권장): ai-gitgen 폴더(C:\Users\me\ai-gitgen)에서 cp .env.example .env 후 ANTHROPIC_API_KEY=YOUR_KEY 의 YOUR_KEY 를 실제 키로 수정
        방법 2) 환경변수 직접 설정: export ANTHROPIC_API_KEY="YOUR_KEY"
```

`.env` 는 있지만 키 항목이 없으면 `.env 파일은 찾았지만 API Key 항목이 없습니다`, UTF-8 이 아니라서(예: PowerShell 5.1 의 `>` 로 만든 UTF-16 파일) 읽지 못하면 `.env 파일을 읽지 못했습니다`, 예시 값 그대로면 `API Key 가 아직 예시 값(YOUR_KEY) 그대로입니다` 로 알려 줍니다. PDF·메신저에서 키를 복사하다 공백·줄바꿈·보이지 않는 문자가 섞여도 API 를 호출하기 전에 알려 줍니다(키 값은 출력하지 않음).

```
[ERROR] API Key 에 영문/숫자/기호가 아닌 문자(보이지 않는 문자, 전각 하이픈 등)가 섞여 있습니다. (ANTHROPIC_API_KEY 또는 AI_API_KEY)
        .env 파일(또는 환경변수)의 키를 공백/줄바꿈/특수문자 없이 다시 복사해 설정하세요.
```

## 3. 사용법

```bash
# 커밋 메시지: 스테이징된 변경(없으면 작업 트리 변경)으로 생성
git add app.py test_app.py
python main.py commit

# 변경 이유를 함께 알려 주면 결과 품질이 좋아진다
python main.py commit -c "평문 비밀번호 비교를 해시 비교로 바꿔 보안 강화"

# PR 초안: 기준 브랜치(main → master 자동 탐색) 대비 커밋된 변경으로 생성
python main.py pr
python main.py pr --base develop

# 변경 요약 + 커밋 메시지 + PR 초안을 한 번에 (API 1회 호출)
python main.py all

# API 를 호출하지 않고 실제로 전송될 프롬프트만 확인 (API Key 불필요, 비용 0)
python main.py commit --dry-run

# 파라미터 바꾸기
python main.py commit --temperature 0.5 --max-tokens 1024 --effort low

# safe-mode 정책 보기 / 바꾸기 (5장 참고)
python main.py policy
python main.py commit --max-files 5 --max-lines 120 --mask-pattern "EMPLOYEE_ID=\bEMP-\d{6}\b"

# 팀 컨벤션 보기 / 적용하기 (6장 참고)
python main.py convention --convention b2-2
python main.py pr --convention b2-2 --issue 26
```

### 어떤 diff 를 분석하나요?

| 명령 | 분석 대상 (위에서부터 먼저 있는 것) |
|---|---|
| `commit` | ① 스테이징된 변경 `git diff --cached` → ② 작업 트리 변경 `git diff` |
| `pr` | ① 기준 브랜치 대비 커밋된 변경 `git diff <base>...HEAD` → ② `commit` 과 같은 순서 |
| `all` | ① `commit` 과 같은 순서 → ② 기준 브랜치 대비 커밋된 변경 |

추적되지 않은 새 파일(untracked)은 `git diff` 에 나타나지 않으므로 `git add` 후 실행하세요. 변경이 없으면 아무것도 호출하지 않고 종료합니다.

`--base` 로 직접 지정한 브랜치와 비교할 수 없으면(없는 브랜치 등) ②로 넘어가지 않고 오류(종료 코드 1)로 끝납니다. ②로 넘어가는 것은 자동 탐색(main → master)일 때뿐입니다.

### 옵션 (commit / pr / all 공통)

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--model` | `claude-opus-4-6` | 사용할 모델 ID |
| `--temperature` | `0.2` | 0.0~1.0. 낮을수록 일관된 문구, 높을수록 다양한 표현 |
| `--max-tokens` | `2048` | 응답 최대 토큰 수 (출력 길이·비용 상한) |
| `--effort` | `medium` | `low` / `medium` / `high` / `max`. 모델이 응답에 들이는 노력(토큰 사용량) |
| `--timeout` | `120` | API 응답 대기 시간(초) |
| `--safe-mode` / `--no-safe-mode` | 켜짐 | 민감정보 마스킹 + 민감 파일 제외 + 전송량 제한 ([5장](#5-민감정보-대응-safe-mode)) |
| `-c`, `--context` | 없음 | 변경 이유/요구사항 등 AI 에게 줄 추가 맥락 |
| `--dry-run` | 꺼짐 | API 호출 없이 전송될 요청 파라미터와 프롬프트만 출력 |
| `--plain` | 꺼짐 | 구획/검증 결과 없이 결과 텍스트만 출력 (파일 저장·파이프용) |
| `--base` (`pr`, `all`) | `main` → `master` | PR 비교 기준 브랜치 |

### safe-mode 정책 옵션 (commit / pr / all / policy 공통)

설정 파일(`.ai-gitgen.json`)보다 우선합니다. 자세한 내용은 [5장](#safe-mode-정책-바꾸기-마스킹-규칙-추가--전송-한도-조정)을 참고하세요.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--safe-mode-config PATH` | 현재 폴더의 `.ai-gitgen.json` | 정책 파일 경로 |
| `--max-files N` | `10` | 내용을 보낼 최대 파일 수 (1~1000) |
| `--max-lines N` | `200` | 보낼 diff 전체 최대 줄 수 (1~20000) |
| `--max-lines-per-file N` | `0` (제한 없음) | 파일 하나에서 보낼 최대 줄 수 (0~20000) |
| `--mask-pattern NAME=REGEX` | 없음 | 마스킹 규칙(정규식) 추가. 여러 번 지정 가능 |
| `--exclude-file GLOB` | 없음 | 내용을 보내지 않을 파일 패턴 추가. 여러 번 지정 가능 |

### 팀 컨벤션 옵션 (commit / pr / all / convention)

설정 파일(`.ai-gitgen.json` 의 `convention`)보다 우선합니다. 자세한 내용은 [6장](#6-팀-커밋pr-컨벤션-보너스-2)을 참고하세요.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--convention NAME\|PATH` | 현재 폴더 `.ai-gitgen.json` 의 `convention`, 없으면 `default` | `default`(기본 컨벤션), 프리셋 이름(`b2-2`), 또는 JSON 파일 경로 |
| `--issue N` (`pr`, `all`) | 없음 | PR 본문 '관련 이슈' 에 `Closes #N` 으로 채울 번호 |

### 결과를 실제 커밋에 적용하기

생성된 문구는 **초안**입니다. 터미널에서 복사해 쓰거나, 파일로 저장한 뒤 에디터에서 다듬어 커밋하세요.

```bash
python main.py commit --plain > commit_msg.txt   # 로그는 stderr 로 나가므로 파일에는 결과만 저장된다
git commit -e -F commit_msg.txt                  # -e: 커밋 전에 에디터로 검토/수정
```

> Windows PowerShell 5.1 의 `>` 는 UTF-16 으로 저장해 `git commit -F` 에서 한글이 깨질 수 있습니다. 파일로 저장할 때는 Git Bash 를 권장합니다.

### 종료 코드

| 코드 | 의미 |
|---|---|
| 0 | 성공, 또는 변경 사항이 없어 정상 종료 |
| 1 | 오류 (Git 저장소 아님/루트 아님, API Key 없음, API 호출 실패 등) |
| 2 | 잘못된 옵션 (예: `--temperature 1.5`), safe-mode 정책·팀 컨벤션 설정 오류, 또는 명령 없이 실행 |
| 130 | Ctrl+C 로 실행 중단 |

## 4. 출력 예시

아래는 실제 실행 결과입니다. (Windows 11 + Git Bash, `claude-opus-4-6`, API Key 는 `.env` 에서 로드)

### 커밋 메시지 — `python main.py commit`

```
$ git add app.py test_app.py
$ python main.py commit -c "평문 비밀번호 비교를 해시 비교로 바꿔 보안 강화"
[INFO] .env 파일에서 환경변수 1개를 불러왔습니다 (ANTHROPIC_API_KEY)
[INFO] 현재 브랜치: feature/password-hash
[INFO] Git status 수집 완료: 2개 파일 변경 감지 (staged 2, unstaged 0, untracked 0)
[INFO] Git diff 수집 완료: 42줄, 2개 파일 (+27/-4) - 스테이징된 변경 (git diff --cached)
[INFO] safe-mode ON: 마스킹 1건, 전송 diff 42줄 / 원본 42줄
[INFO] 마스킹 상세: EMAIL 1
[INFO] AI API 요청 중... (model=claude-opus-4-6, temperature=0.2, max_tokens=2048, effort=medium)
[INFO] AI API 호출 횟수: 1회 | 토큰 입력 2,224 / 출력 171 | 5.7초 | 예상 비용 $0.0154
[DONE] 커밋 메시지 생성 완료

======================== 변경 요약 ========================
브랜치    : feature/password-hash
비교 대상 : 스테이징된 변경 (git diff --cached)
변경 파일 : 2개 (+27 / -4)
  M  app.py  (+16/-4)
  A  test_app.py  (+11/-0)
AI 요약   : 로그인 시 평문 비밀번호 비교를 SHA-256 해시 비교로 교체하여 보안을 강화했다.

--- Commit Message ---
feat(auth): 비밀번호 해시 비교 방식으로 로그인 보안 강화

- app.py: hash_password 함수 추가 및 login 에서 hmac.compare_digest 를 이용한 해시 비교로 변경
- test_app.py: 로그인 성공·실패 케이스에 대한 단위 테스트 추가
------------------------------------------------------------

======================== 검증 결과 ========================
[OK] 커밋 제목 37자 (권장 50자 이내)
[OK] 제목 형식 'type(scope): subject' 준수
[OK] 본문 핵심 변경 불릿 2개
[OK] 변경 파일 언급 2개 (app.py, test_app.py)
※ 생성된 문구는 초안입니다. 내용을 검토한 뒤 적용하세요.
```

### PR 초안 — `python main.py pr`

```
$ python main.py pr --base main
[INFO] .env 파일에서 환경변수 1개를 불러왔습니다 (ANTHROPIC_API_KEY)
[INFO] 현재 브랜치: feature/password-hash
[INFO] Git status 수집 완료: 1개 파일 변경 감지 (staged 0, unstaged 0, untracked 1)
[INFO] Git diff 수집 완료: 42줄, 2개 파일 (+27/-4) - 'main' 브랜치 대비 커밋된 변경 (git diff main...HEAD)
[WARN] 커밋되지 않은 변경 1개 파일은 PR 초안에 포함되지 않습니다.
[INFO] safe-mode ON: 마스킹 1건, 전송 diff 42줄 / 원본 42줄
[INFO] 마스킹 상세: EMAIL 1
[INFO] AI API 요청 중... (model=claude-opus-4-6, temperature=0.2, max_tokens=2048, effort=medium)
[INFO] AI API 호출 횟수: 1회 | 토큰 입력 2,331 / 출력 405 | 9.0초 | 예상 비용 $0.0218
[DONE] PR 초안 생성 완료

======================== 변경 요약 ========================
브랜치    : feature/password-hash
비교 대상 : 'main' 브랜치 대비 커밋된 변경 (git diff main...HEAD)
변경 파일 : 2개 (+27 / -4)
  M  app.py  (+16/-4)
  A  test_app.py  (+11/-0)
AI 요약   : 평문 비밀번호 비교를 SHA-256 해시 기반 인증으로 교체하고, 로그인 성공·실패 테스트를 추가했다.

--- PR Title ---
feat: 비밀번호 해시 기반 인증으로 전환
------------------------------------------------------------

--- PR Body ---
## Why
- 기존 login 함수가 평문 비밀번호를 직접 비교하여 보안 취약점이 존재
- salt + 해시 방식으로 전환하여 비밀번호 유출 위험을 줄이기 위함

## What
- app.py: `hash_password` 함수 추가 — salt 와 SHA-256 을 사용한 비밀번호 해시 생성
- app.py: `login` 함수를 users 딕셔너리 기반으로 변경하고 `hmac.compare_digest`로 타이밍 공격 방지
- test_app.py: 로그인 성공·실패 케이스를 검증하는 테스트 2건 추가

## How to Test
- `python -m pytest test_app.py -v` 실행 후 test_login_success, test_login_wrong_password 통과 확인
- Python REPL 에서 `from app import hash_password; hash_password('pw', 's1')` 호출 시 동일 입력에 동일 해시가 반환되는지 확인
------------------------------------------------------------

======================== 검증 결과 ========================
[OK] PR 제목 24자 (최대 80자)
[OK] PR 본문 Why/What/How to Test 섹션과 불릿 확인 (Why 2, What 3, How to Test 2)
※ 생성된 문구는 초안입니다. 내용을 검토한 뒤 적용하세요.
```

### 한 번에 — `python main.py all`

```
$ python main.py all
[INFO] .env 파일에서 환경변수 1개를 불러왔습니다 (ANTHROPIC_API_KEY)
...
[INFO] AI API 호출 횟수: 1회 | 토큰 입력 2,685 / 출력 211 | 7.4초 | 예상 비용 $0.0187
[DONE] 커밋 메시지와 PR 초안 생성 완료

======================== 변경 요약 ========================
브랜치    : feature/password-hash
비교 대상 : 스테이징되지 않은 작업 트리 변경 (git diff)
변경 파일 : 1개 (+5 / -0)
  M  app.py  (+5/-0)
AI 요약   : 세션을 만료시키는 logout 함수를 app.py에 추가했다.

--- Commit Message ---
feat(auth): logout 함수 추가

- app.py: 세션을 초기화하는 logout(session) 함수 추가
------------------------------------------------------------

--- PR Title ---
feat: logout 함수 추가
------------------------------------------------------------

--- PR Body ---
## Why
- 사용자 로그아웃 시 세션을 만료시키는 기능이 필요

## What
- app.py: session.clear()를 호출하는 logout 함수 추가

## How to Test
- 세션 객체를 생성한 뒤 `logout(session)` 호출 후 세션이 비어 있는지 확인
------------------------------------------------------------

======================== 검증 결과 ========================
[OK] 커밋 제목 24자 (권장 50자 이내)
[OK] 제목 형식 'type(scope): subject' 준수
[OK] 본문 핵심 변경 불릿 1개
[OK] 변경 파일 언급 1개 (app.py)
[OK] PR 제목 18자 (최대 80자)
[OK] PR 본문 Why/What/How to Test 섹션과 불릿 확인 (Why 1, What 1, How to Test 1)
※ 생성된 문구는 초안입니다. 내용을 검토한 뒤 적용하세요.
```

### 변경 사항이 없을 때

```
$ python main.py commit
[INFO] 현재 브랜치: main
[INFO] Git status 수집 완료: 0개 파일 변경 감지 (staged 0, unstaged 0, untracked 0)
[INFO] 변경 사항이 없습니다. 커밋 메시지를 생성하지 않고 종료합니다.
```

새 파일만 있고 아직 `git add` 하지 않았다면:

```
[WARN] 추적되지 않은 새 파일 1개는 git diff 에 나타나지 않습니다.
       `git add <파일>` 로 스테이징한 뒤 다시 실행하면 새 파일 내용까지 분석합니다.
[INFO] 변경 사항이 없습니다. 커밋 메시지를 생성하지 않고 종료합니다.
```

### Git 저장소가 아니거나 루트가 아닐 때

```
[ERROR] Git 저장소가 아닙니다. Git 이 초기화된 프로젝트 루트 디렉토리에서 실행하세요.
        예) git init

[ERROR] 프로젝트 루트 디렉토리가 아닙니다. .git 폴더가 있는 최상위 디렉토리로 이동한 뒤 실행하세요.
```

### API 호출 실패 (원인 + 해결 방법 + request_id)

```
[INFO] AI API 호출 횟수: 1회
[ERROR] AI API 호출 실패 (HTTP 401 authentication_error): invalid x-api-key
        → 인증에 실패했습니다. API Key 가 올바른지 확인하세요. (.env 파일 또는 환경변수 ANTHROPIC_API_KEY / AI_API_KEY)
        request_id: req_011ChFtgXxyXrX3WqoVXhnx5

[ERROR] AI API 호출 실패 (HTTP 404 not_found_error): model: claude-nope-1
        → 모델을 찾을 수 없습니다. --model 값(예: claude-opus-4-6)을 확인하세요.

[ERROR] AI API 호출 실패: 응답이 max_tokens 한도에서 잘렸습니다. (출력 20 토큰)
        → --max-tokens 값을 늘려 다시 실행하세요.

[WARN] 일시적 오류로 다시 시도합니다 (2/2회차): 네트워크 오류로 API 서버에 연결하지 못했습니다. (ProxyError)
[INFO] AI API 호출 횟수: 2회
[ERROR] AI API 호출 실패: 네트워크 오류로 API 서버에 연결하지 못했습니다. (ProxyError)
        → 인터넷 연결, 프록시/방화벽 설정을 확인하세요.
```

## 5. 민감정보 대응 (safe-mode)

`git diff` 에는 API Key, 비밀번호, 이메일 같은 민감정보가 섞일 수 있고, 이 내용은 **외부 AI API 로 전송**됩니다. 그래서 safe-mode 를 **기본으로 켜 둡니다.**

| 단계 | 정책 | 내용 |
|---|---|---|
| ① 민감 파일 제외 | 파일 내용 자체를 보내지 않음 (파일명·통계만 전송) | `.env` 로 시작하는 모든 파일(`.env.local`, `.env~`, `.envrc` 등), `*.env`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa*` 등 SSH 키, `credentials`, `credentials*.json`, `service-account*.json`, `*secret*`, `.npmrc`, `.pypirc`, `.netrc`, `.pgpass`, `.git-credentials`, `kubeconfig`, `*.tfstate`, `*.tfvars`, `.aws/*`, `.ssh/*`, `.kube/config`, `.docker/config.json`, `*.ppk`, `*.jks`, `*.keystore`, `*.kdbx` · 이름이 바뀐 경우 **원래 이름**까지 검사 · `*.example`, `*.sample`, `*.template`, `*.dist` 는 제외 대상 아님(내용이 마스킹되어 전송) |
| ② 패턴 마스킹 | 값을 `[MASKED:종류]` 로 치환 | Anthropic/OpenAI/Stripe/AWS/GitHub/GitLab/HuggingFace/Google/Slack 키·토큰, JWT, `Bearer`/`Basic` 인증, URL 속 계정(`postgres://user:pw@`), URL 쿼리 토큰, 개인 키 블록, 이메일, 주민등록번호, 휴대전화 번호, 카드 번호, `password = "..."`·`DB_PASSWORD=...`·`password: ...` 같은 비밀 값 대입 (코드 참조인 `self.api_key = api_key` 는 유지) |
| ③ 전송량 제한 | 기본 **최대 10개 파일, 최대 200줄** (정책으로 조정 가능) | 줄 예산은 파일마다 공평하게 나눔(작은 파일은 전부, 큰 파일들은 나머지를 똑같이). 생략된 파일도 이름과 `+/-` 통계는 전송 (`<changed_files>` 목록은 최대 50개, 나머지는 `... 외 N개 파일` 합계 한 줄) |
| 추가 | `--context` 텍스트와 브랜치 이름 | diff 와 같은 규칙으로 마스킹 |
| 추가 | AI 응답 | 결과에 민감정보 패턴이 섞여 나오면 다시 가리고 `[경고]` 표시 |

lock 파일(`package-lock.json` 등)과 바이너리 파일은 safe-mode 와 무관하게 내용을 생략합니다. safe-mode 를 꺼도 비용 폭주를 막기 위한 하드 상한(150,000자)은 항상 적용됩니다.

### safe-mode ON / OFF 비교 (`--dry-run` 으로 확인)

`.env` 와 키가 하드코딩된 코드가 스테이징된 상태 (예시의 키·비밀번호·이메일은 데모용 가짜 값):

```
$ python main.py commit --dry-run                  # safe-mode ON (기본)
[INFO] safe-mode ON: 마스킹 2건, 민감 파일 제외 1개, 전송 diff 31줄 / 원본 38줄
[INFO] 마스킹 상세: ANTHROPIC_KEY 1, EMAIL 1
...
- [추가] .env (+1/-0)  ※ 민감 파일로 판단되어 내용 제외(safe-mode)
+API_KEY = "[MASKED:API_KEY]"
+    """사용자 비밀번호를 해시로 비교한다. 문의: [MASKED:EMAIL]"""

$ python main.py commit --dry-run --no-safe-mode   # safe-mode OFF
[WARN] safe-mode 가 꺼져 있습니다. diff 가 마스킹 없이 외부 AI API 로 전송됩니다.
[INFO] 전송 diff: 38줄 / 원본 38줄
...
+DB_PASSWORD=supersecret123
+API_KEY = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
+    """사용자 비밀번호를 해시로 비교한다. 문의: admin@example.com"""
```

> 정규식 마스킹은 완벽하지 않습니다. 민감한 저장소에서는 먼저 `--dry-run` 으로 실제 전송 내용을 확인하고, 비밀 값은 애초에 `.gitignore` 로 커밋 대상에서 빼 두세요.

### safe-mode 정책 바꾸기 (마스킹 규칙 추가 · 전송 한도 조정)

내장 규칙은 범용 형식(API Key, 이메일 등)만 압니다. 사번·사내 도메인·사내 토큰처럼 **팀마다 다른 민감정보**는 정규식 규칙으로 추가하고, 저장소 성격에 맞게 **전송 한도(파일 수/줄 수)** 를 조정할 수 있습니다.

정책은 아래 순서로 합쳐지며, 뒤의 것이 앞의 값을 덮어씁니다.

| 순서 | 출처 | 예 |
|---|---|---|
| 1 | 기본값 (`gitgen/config.py`) | 10개 파일 · 200줄 · 파일당 제한 없음 · 내장 규칙 21개 · 내장 제외 패턴 31개 |
| 2 | 대상 프로젝트 루트의 `.ai-gitgen.json` (또는 `--safe-mode-config`) | 팀 공통 정책을 저장소에 커밋해 공유 |
| 3 | CLI 옵션 (`--max-files` 등) | 이번 실행에만 임시로 조정 |

**`.ai-gitgen.json` 예시** ([docs/bonus3/ai-gitgen.sample.json](docs/bonus3/ai-gitgen.sample.json))

```json
{
  "safe_mode": {
    "max_files": 5,
    "max_lines": 120,
    "max_lines_per_file": 30,
    "exclude_files": ["*.sql", "db/"],
    "mask_rules": [
      {"name": "EMPLOYEE_ID", "pattern": "\\bEMP-\\d{6}\\b", "label": "EMPLOYEE"},
      {"name": "INTERNAL_HOST", "pattern": "\\b[\\w-]+\\.corp\\.example\\.com\\b"},
      {"name": "SHOP_TOKEN", "pattern": "\\bshoptok_[0-9a-f]{12}\\b", "label": "TOKEN"},
      {"name": "CODENAME", "pattern": "(?i)\\bfalcon-x\\b"},
      {"name": "LICENSE_SERIAL", "pattern": "LICENSE_SERIAL = \"(?P<value>[A-Z0-9-]{12,})\"", "label": "LICENSE"}
    ]
  }
}
```

| 항목 | 허용 값 | 의미 |
|---|---|---|
| `max_files` | 정수 1~1000 (기본 10) | 내용을 보낼 최대 파일 수. 넘는 파일은 이름·통계만 전송 |
| `max_lines` | 정수 1~20000 (기본 200) | 보낼 diff 전체 줄 수. 파일마다 공평하게 나눔 |
| `max_lines_per_file` | 정수 0~20000 (기본 0 = 제한 없음) | 파일 하나가 받을 수 있는 최대 줄 수. 큰 파일 하나가 예산을 독차지하지 못하게 함 |
| `exclude_files` | glob 문자열 배열 | 내용을 보내지 않을 파일. gitignore 와 비슷하게 `*.sql`(어느 폴더든), `db/`(폴더 전체), `db/*.sql`(루트 기준), `**/*.sql` 를 쓸 수 있음. 이름이 바뀐 파일은 원래 경로도 검사 |
| `disable_rules` | 내장 규칙 이름 배열 | 오탐이 많은 내장 규칙 끄기 (이름은 `python main.py policy` 로 확인) |
| `mask_rules` | `{name, pattern, label?}` 배열 | 정규식 마스킹 규칙. `(?P<value>...)` 그룹이 있으면 그 부분만 가림, `(?i)` 로 대소문자 무시. 가린 자리는 `[MASKED:label]` (label 생략 시 name) |

- **규칙 적용 순서**: 내장 규칙 → 사용자 규칙. 사용자 규칙이 먼저 일부를 바꿔 버리면 내장 규칙이 원래 잡던 비밀값을 놓칠 수 있어서(예: `ACME` 를 가리는 규칙 때문에 `ACME_DB_PASSWORD=...` 의 값이 노출), 사용자 규칙은 **기존 보호를 줄이지 않고 더하기만** 합니다.
- **규칙이 보는 텍스트**: diff 본문(줄마다 `+`, `-`, 공백으로 시작하는 여러 줄)입니다. 그래서 `^`, `$` 는 `(?m)` 없이 쓸 수 없습니다(줄 단위로 쓰려면 `(?m)^\+...`).
- **목록 합치기**: 숫자 항목은 CLI 옵션이 설정 파일 값을 덮어쓰고, 목록 항목(`mask_rules`, `exclude_files`)은 CLI 옵션이 설정 파일 값에 **추가**됩니다.
- **정책 파일 자체**(`.ai-gitgen.json`)는 내용을 보내지 않습니다. 정규식 원문에 가리려는 값(사내 도메인, 코드명)이 그대로 들어 있기 때문입니다.
- 잘못된 설정(알 수 없는 항목, `safe_mode` 섹션 밖에 쓴 항목, 범위를 벗어난 숫자, 정규식 오류, 길이 0 과 일치하는 정규식, 내장 규칙과 같은 이름, 허용되지 않는 label 등)은 실행 전에 원인과 함께 알려 주고 종료 코드 2로 끝납니다. 이번 변경과 일치하는 파일이 없는 제외 패턴은 오타일 수 있어 로그로 알려 줍니다.
- safe-mode 를 끄면(`--no-safe-mode`) 정책은 적용되지 않고 경고가 출력됩니다.

**적용될 정책 확인** — `policy` 명령은 API 를 호출하지 않고, 숫자 기준과 규칙 목록, 각 값의 출처를 보여 줍니다. `--check` 로 특정 문자열이 어떻게 가려지는지 바로 시험할 수 있습니다.

```
$ python main.py policy --safe-mode-config docs/bonus3/ai-gitgen.sample.json --check "담당 EMP-204817 / 프로젝트 FALCON-X"
====================== safe-mode 정책 ======================
설정 파일 : docs/bonus3/ai-gitgen.sample.json

[전송 제한]  safe-mode ON 일 때 외부 API 로 보내는 diff 의 양
  최대 파일 수      : 5개  (설정 파일, 기본 10개)
  전체 최대 줄 수   : 120줄  (설정 파일, 기본 200줄)
  파일당 최대 줄 수 : 30줄  (설정 파일, 기본 제한 없음)
  전송 하드 상한    : 150,000자  (고정, safe-mode OFF 에도 적용)

[민감 파일 제외]  내용은 보내지 않고 파일명과 +/- 통계만 전송
  내장 패턴 31개 : .env*, *.env, *.pem, *.key, ... (생략)
  사용자 패턴 2개 (목록 항목은 설정 파일 + CLI 옵션이 합쳐짐)
    - *.sql  (설정 파일)
    - db/  (설정 파일)
  정책 파일(.ai-gitgen.json) 자체도 내용을 보내지 않음 (가리려는 값이 정규식 원문에 들어 있으므로)

[마스킹 규칙]  위에서부터 순서대로 적용, 가린 자리는 [MASKED:라벨]
  1) 내장 규칙 21개 (전체 21개)
     PRIVATE_KEY, ANTHROPIC_KEY, OPENAI_KEY, ... (생략)
  2) 사용자 규칙 5개 (내장 규칙 다음에 적용 → 기존 보호를 줄이지 않고 추가만 함)
     1. EMPLOYEE_ID  /\bEMP-\d{6}\b/  → 일치 부분 전체 [MASKED:EMPLOYEE]  (설정 파일)
     2. INTERNAL_HOST  /\b[\w-]+\.corp\.example\.com\b/  → 일치 부분 전체 [MASKED:INTERNAL_HOST]  (설정 파일)
     3. SHOP_TOKEN  /\bshoptok_[0-9a-f]{12}\b/  → 일치 부분 전체 [MASKED:TOKEN]  (설정 파일)
     4. CODENAME  /(?i)\bfalcon-x\b/  → 일치 부분 전체 [MASKED:CODENAME]  (설정 파일)
     5. LICENSE_SERIAL  /LICENSE_SERIAL = "(?P<value>[A-Z0-9-]{12,})"/  → value 그룹만 [MASKED:LICENSE]  (설정 파일)

================= 정책 적용 확인 (--check) =================
입력 : 담당 EMP-204817 / 프로젝트 FALCON-X
결과 : 담당 [MASKED:EMPLOYEE] / 프로젝트 [MASKED:CODENAME]
적용 : EMPLOYEE_ID 1, CODENAME 1  (diff 의 추가 줄 '+...' 로 넣어 실제 전송과 같은 조건에서 확인)
```

정책을 지정하지 않으면 고도화 전과 **완전히 같은** 내용이 전송됩니다. 같은 변경으로 비교한 결과(가짜 민감정보 13종)는 아래와 같습니다. 자세한 증빙은 [docs/bonus3/README.md](docs/bonus3/README.md)(요약)와 [docs/bonus3/EVIDENCE.md](docs/bonus3/EVIDENCE.md)(자동 생성 상세)에 있습니다.

| 조건 | 민감정보 노출 | 전송 diff |
|---|---|---|
| safe-mode OFF | 13/13 | 335줄 |
| ON · 기본 정책 (= 고도화 전) | 7/13 | 200줄 |
| ON · 사용자 정책 (`ai-gitgen.sample.json`) | **0/13** | 120줄 |

## 6. 팀 커밋/PR 컨벤션 (보너스 2)

기본 결과(`type(scope): subject` + 불릿, PR 은 Why/What/How to Test)는 일반적인 형식일 뿐, 팀마다 커밋 타입·문체·PR 템플릿이 다릅니다. 그래서 **이전 미션 저장소 [On-Premise-Sky/B2-2](https://github.com/On-Premise-Sky/B2-2) 의 실제 기록을 분석해 팀 컨벤션을 정의**하고, 도구가 그 컨벤션대로 커밋 메시지와 PR 초안을 만들도록 했습니다.

증빙: [docs/bonus2/README.md](docs/bonus2/README.md)(요약) · [docs/bonus2/EVIDENCE.md](docs/bonus2/EVIDENCE.md)(스크립트가 만든 상세 보고서) · [docs/bonus2/raw/](docs/bonus2/raw/)(실행 원본 출력)

### 이전 미션(B2-2) 분석

팀 문서(`docs/CONTRIBUTING.md`, `.github/pull_request_template.md`)와 **실제로 쌓인 기록**을 함께 봤습니다. 문서에 적힌 규칙이 실제로 지켜졌는지, 문서에 없는 습관(문체 등)은 무엇인지 확인하기 위해서입니다. 숫자는 `scripts/bonus2_evidence.py` 가 저장소를 clone 해서 다시 계산합니다.

| 대상 | 관찰한 것 | 컨벤션에 반영 |
|---|---|---|
| 커밋 64개 (머지 제외) | 63개가 `type: 설명` (예외 1개는 GitHub 웹 편집 기본 메시지), scope 사용 0개 | `{type}: {subject}`, scope 없음 |
| | 타입: docs 40 · feat 10 · fix 8 · chore 4 · refactor 1 | CONTRIBUTING 의 5종만 허용 |
| | 제목 평균 32.6자(50자 초과 1개), 한글 63개, 마침표 0개, '~ 추가' '~ 수정' '~ 분리' 같은 명사형 끝맺음 | 한글, 50자 이내, 명사형, 마침표 없음 |
| | 본문은 6개에만 있고 불릿 0개, '~했습니다' 존댓말 문장 | 필요할 때만, 불릿 없는 존댓말 문장 |
| PR 25개 | 제목 25개 모두 커밋과 같은 `type: 설명`, 최대 47자 | 커밋과 같은 형식, 50자 이내 |
| | 23개가 템플릿 순서(관련 이슈 → What → Why → How → 체크리스트) 그대로, `Closes #번호` 24개, 체크리스트 24개 | 이 순서의 본문 템플릿 + `Closes #N` + 체크리스트 |
| 브랜치 | CONTRIBUTING: `<type>/<GitHub ID>-<작업 내용>` (예: `feature/SIUUU1-members-json`) | 브랜치 이름 검사 (어긋나면 경고) |

### 정의한 팀 컨벤션 — `conventions/b2-2.json`

**커밋 메시지**

```
<type>: <무엇을 바꿨는지>          ← 한글, 50자 이내, 명사형으로 끝맺음, 마침표 없음

<필요할 때만: 무엇을 어떻게, 왜 바꿨는지 '~했습니다' 문장 1~3개 (불릿 없이, 파일 경로 포함)>
```

| type | 언제 |
|---|---|
| `feat` | 새 기능 / 결과물 추가 |
| `fix` | 오류 수정 |
| `docs` | 문서 추가·수정 |
| `refactor` | 동작 변화 없는 구조 개선 |
| `chore` | 설정, 폴더 구조 등 기타 |

- 금지 제목: `update`, `fix`, `temp`, `wip`, `final`, `bug fix`, `edit file` 처럼 무엇을 바꿨는지 알 수 없는 제목 (CONTRIBUTING 의 나쁜 예)
- 예: `docs: CONTRIBUTING에 리뷰 최소 기준 추가`, `refactor: 팀원 데이터를 members.json으로 분리`

**Pull Request**

- 제목: 커밋과 같은 형식, 50자 이내
- 본문: 팀 PR 템플릿의 섹션과 순서를 그대로 따릅니다.

```markdown
## 관련 이슈

Closes #<이슈 번호>                ← --issue N 으로 채움 (없으면 'Closes #' 로 비워 두고 경고)

## What (무엇을 바꿨나요)

- `파일 경로` 변경 내용            ← 1~5개

## Why (왜 바꿨나요)

- ~하기 위해서입니다.              ← 1~3개, 존댓말 평서문

## How (어떻게 확인했나요)

- ~를 확인합니다.                  ← 1~5개, 실행하지 않은 결과를 '확인했습니다'로 단정하지 않음

## 체크리스트

- [ ] 커밋 메시지가 컨벤션(`type: 설명`)을 따릅니다
- [ ] 리뷰어를 지정했습니다
- [ ] 리뷰 코멘트에 답글 또는 수정으로 응답했습니다
```

- How 를 '~를 확인합니다'로 쓰게 한 이유: 도구는 diff 만 보고 테스트를 실행하지 않습니다. 실행하지도 않은 결과를 '확인했습니다'로 적으면 사실이 아닌 PR 이 되므로, 작성자가 실행 후 실제 결과로 고치도록 했습니다.
- 체크리스트는 사람이 직접 확인할 항목이라 모두 빈 칸(`[ ]`)으로 둡니다.

**브랜치 이름**: `^(feature|fix|docs)/[A-Za-z0-9_-]+-[a-z0-9][a-z0-9-]*$` — 현재 브랜치가 어긋나면 검증 결과에 `[경고]` 를 표시합니다(생성은 계속).

### 컨벤션 적용 방법

세 가지 방법을 지원하며, 위에 있는 것이 우선합니다.

| 우선순위 | 방법 | 예 | 용도 |
|---|---|---|---|
| 1 | `--convention` 옵션 | `--convention b2-2` (프리셋) · `--convention team.json` (파일) · `--convention default` | 이번 실행만 바꾸기 |
| 2 | 대상 프로젝트 루트의 `.ai-gitgen.json` 의 `convention` 섹션 | 아래 예시 | 팀 저장소에 커밋해 팀원 모두 같은 컨벤션 사용 |
| 3 | 기본 컨벤션 | — | 설정이 없으면 지금까지와 똑같이 동작 |

```bash
python main.py convention --convention b2-2   # 적용될 컨벤션 확인 (API 호출 없음)
python main.py commit --convention b2-2
python main.py pr --convention b2-2 --issue 26
python main.py all --convention b2-2 --issue 26
```

**`.ai-gitgen.json` 예시** — safe-mode 정책(`safe_mode`)과 같은 파일에 함께 둘 수 있습니다. 프리셋 파일 [conventions/b2-2.json](conventions/b2-2.json) 을 복사해 시작하면 편합니다.

```json
{
  "convention": {
    "name": "우리 팀 컨벤션",
    "commit": {
      "format": "{type}: {subject}",
      "types": {"feat": "새 기능", "fix": "오류 수정", "docs": "문서"},
      "scope": "none",
      "subject_max": 50,
      "forbidden": ["update", "wip"],
      "body": "prose"
    },
    "pr": {
      "title_max": 50,
      "issue": {"heading": "관련 이슈", "keyword": "Closes"},
      "sections": [
        {"key": "what", "heading": "What (무엇을 바꿨나요)", "min": 1, "max": 5},
        {"key": "why", "heading": "Why (왜 바꿨나요)", "min": 1, "max": 3}
      ],
      "checklist": {"heading": "체크리스트", "items": ["리뷰어를 지정했습니다"]}
    },
    "branch_pattern": "^(feature|fix)/.+$"
  }
}
```

| 항목 | 허용 값 | 의미 |
|---|---|---|
| `commit.format` | `{type}`, `{subject}` 각 1번 필수, `{scope}` 는 `({scope})` 형태로만 | 제목 형식. 예: `{type}: {subject}`, `[{type}] {subject}`, `{type}({scope}): {subject}`. scope 가 비면 괄호째 빠짐 |
| `commit.types` | `{"타입": "설명"}` (타입은 영문 소문자) | 허용 타입. 모델은 이 중에서만 고를 수 있음(응답 스키마의 enum) |
| `commit.scope` | `none` / `optional` / `required` | scope 사용 여부. 생략하면 `format` 에 `({scope})` 가 있을 때 `optional`, 없을 때 `none`. PR 제목에도 같이 적용 |
| `commit.subject_max` | 20~72 (기본 50) | 제목 권장 길이 |
| `commit.subject_guide`, `body_guide` | 200자 이하 문자열 | 제목·본문 작성 요령 (프롬프트에 그대로 들어감) |
| `commit.forbidden` | 문자열 배열 | 설명(subject) 전체가 이 말뿐이면 안 되는 말 (대소문자 무시) |
| `commit.body` / `body_max` | `bullets` / `prose` / `none`, 1~5 | 본문을 불릿으로, 문장으로, 또는 쓰지 않기 |
| `commit.examples` | 팀 형식에 맞는 문자열 배열 | 팀의 좋은 커밋 제목 예시 (few-shot) |
| `pr.title_max`, `pr.tone` | 20~80, 문자열 | PR 제목 길이, 문체 |
| `pr.issue` | `{heading, keyword}` | 이슈 연결 섹션 (`--issue N` → `Closes #N`) |
| `pr.sections` | 1~6개 `{key, heading, guide?, min?, max?}` | 본문 섹션과 순서, 섹션별 항목 수. key·heading 은 서로 달라야 함 |
| `pr.checklist` | `{heading, items[1개 이상]}` | 본문 끝 체크리스트 |
| `pr.examples` | 응답 스키마와 같은 키(`type`, `subject`, 섹션 key)의 객체 배열 | 팀의 좋은 PR 예시 (few-shot) |
| `branch_pattern` | 정규식 | 브랜치 이름 규칙 (어긋나면 경고) |

잘못된 설정은 API 를 호출하기 전에 원인과 함께 알려 주고 종료 코드 2로 끝납니다. 예: 알 수 없는 항목(중첩된 `issue`·`checklist` 안 포함), `convention` 섹션 밖에 쓴 항목, 틀린 섹션 이름(`conventions` 등), 형식의 알 수 없는 자리표시자(`{ticket}`)나 중복, 범위를 벗어난 숫자, 중복된 섹션 key·heading, 팀 형식과 다른 예시, 정규식 오류. 프롬프트에 들어가는 예시까지 검사하는 이유는, 규칙과 어긋난 예시가 모델을 오히려 헷갈리게 만들기 때문입니다.

### 도구 안에서 컨벤션이 반영되는 곳

**프롬프트 템플릿 교체 + 응답 스키마 + 조립 + 검증** 네 곳에 모두 반영합니다. 프롬프트만 바꾸면 모델이 규칙을 어길 때 그대로 출력되기 때문입니다.

| 단계 | 기본 컨벤션 | 팀 컨벤션 (`b2-2`) |
|---|---|---|
| 시스템 프롬프트 (`convention.system_prompt`) | 기본 규칙 | 팀 타입과 설명, 제목 작성 요령·금지 제목, 본문 문체, PR 섹션·문체, 팀의 실제 예시로 교체 |
| 응답 스키마 (`convention.schema`) | 고정 스키마 | `type` 은 팀 타입만(enum), scope 없으면 `scope` 필드 자체를 빼고, PR 은 팀 섹션 key(`what`/`why`/`how`)로 생성 |
| 조립 (`formatter.*_conv`) | `type(scope): subject` + 불릿 | `format` 대로 제목을 만들고(subject 앞에 중복으로 붙은 머리말은 제거) 본문은 문장으로, PR 은 템플릿 순서 + `Closes #N` + 체크리스트 |
| 검증 (`formatter.validate_*_conv`) | 기본 규칙 | 커밋·PR 제목의 형식·길이·금지 제목, 섹션 순서·항목 수, 이슈 번호, 체크리스트 개수, 브랜치 이름. 줄이거나 기본 문구로 채운 곳은 `[보정]`·`[경고]` 로 표시 |

- 기본 컨벤션일 때 프롬프트·스키마·출력은 **보너스 2 이전과 한 글자도 다르지 않습니다**. `tests/test_convention.py` 가 보너스 2 이전 코드의 프롬프트·스키마 해시와 출력 결과를 고정해 두고 비교합니다.
- `--dry-run` 으로 팀 프롬프트와 실제로 보낼 응답 스키마(`Response Schema` 구획)를 API 호출 없이 볼 수 있습니다.
- 팀 컨벤션을 쓸 때는 로그에 `[INFO] 팀 컨벤션: B2-2 팀 컨벤션 (프리셋 b2-2 ...)` 가 표시됩니다.

### 적용 전/후 비교

B2-2 에 실제로 있었던 변경 3건을 그 시점 상태로 재현해(커밋: 직전 커밋 + 그 변경을 스테이징, PR: 머지 직전 브랜치) **같은 diff** 로 `--convention default`(적용 전)와 `--convention b2-2`(적용 후)를 실제 API 로 생성했습니다. 팀원이 실제로 쓴 메시지와 함께, 팀 문서(CONTRIBUTING·PR 템플릿)에서 뽑은 기준으로 채점했습니다. 채점은 도구의 검증 함수가 아니라 증빙 스크립트가 따로 합니다.

| 대상 | 팀원이 실제로 쓴 것 | 적용 전 (`default`) | 적용 후 (`b2-2`) |
|---|---|---|---|
| C1 커밋 — docs, 본문 없음 | 4/4 | 4/5 | **5/5** |
| C2 커밋 — refactor, 파일 이동·삭제 | 5/5 | 4/5 | **5/5** |
| P1 PR #27 — C2 와 같은 변경의 PR | 6/6 | 1/6 | **5/6** |

**C2 커밋** — 같은 diff, 컨벤션만 다름

```text
[적용 전]
refactor: 팀원 데이터를 JSON 파일로 분리하고 fetch 기반 렌더링으로 전환

- members.json: 팀원 데이터를 독립 JSON 파일로 추출
- render.js: fetch로 JSON을 읽어 카드를 렌더링하도록 members.js 대체
- index.html: 스크립트 참조를 render.js로 변경하고 주석 갱신

[적용 후]
refactor: 팀원 데이터를 members.json으로 분리

src/js/members.js에서 데이터와 렌더링을 함께 관리하던 구조를 src/data/members.json과 src/js/render.js로 분리했습니다.
render.js는 fetch로 JSON을 읽어 카드를 그리며, file:// 환경에서의 실패 시 안내 문구를 표시하도록 했습니다.
src/index.html의 스크립트 참조와 주석을 새 파일 구조에 맞게 수정했습니다.

[팀원이 실제로 쓴 제목]
refactor: 팀원 데이터를 members.json으로 분리
```

- 제목: 적용 후 제목(35자)은 팀원이 실제로 쓴 제목과 **같습니다**. 적용 전 제목(49자)은 두 가지 변경을 이어 붙였습니다.
- 본문: 적용 전은 도구 기본값인 "파일명: 내용" 불릿, 적용 후는 B2-2 팀의 습관대로 불릿 없는 존댓말 문장 + 파일 경로입니다.

**P1 PR #27** — 본문 구조 (발췌)

| | 적용 전 (`default`) | 적용 후 (`b2-2`) |
|---|---|---|
| 섹션 | `## Why` → `## What` → `## How to Test` | `## 관련 이슈` → `## What (무엇을 바꿨나요)` → `## Why (왜 바꿨나요)` → `## How (어떻게 확인했나요)` → `## 체크리스트` (팀 템플릿 그대로) |
| 관련 이슈 | 없음 | `Closes #26` (`--issue 26`) |
| What | `src/data/members.json 추가: 팀원 4명의 …` | `` `src/data/members.json` 팀원 4명의 데이터를 담은 JSON 파일 추가 `` (경로를 백틱으로) |
| Why 문체 | `… 관심사 분리가 필요` (개조식) | `… 건드리지 않도록 하기 위해서입니다` (팀의 존댓말) |
| How 문체 | `… 정상 렌더링되는지 확인` | `… 정상 렌더링되는지 확인합니다` (실행하지 않은 결과를 '확인했습니다'로 단정하지 않음) |
| 체크리스트 | 없음 | 팀 템플릿의 3개 항목 (`- [ ]`) |

전문, 채점표, 토큰/비용은 [docs/bonus2/EVIDENCE.md](docs/bonus2/EVIDENCE.md) 에 있습니다. 6회 호출 비용은 모두 합쳐 약 $0.17 이고, 적용 전과 후의 비용은 거의 같습니다(프롬프트만 바꾸므로 추가 호출이 없음).

**남은 한계와 개선 과정** — 모델이 가장 자주 어긴 규칙은 **제목 길이**였습니다. 리뷰 반영 중 실행한 2회차에서 C2·P1 제목이 56자·55자로 넘쳐, '제목 전체 50자'만 알려 주던 프롬프트를 형식 머리말을 뺀 **subject 한도를 타입별 숫자로** 알려 주도록 고쳤습니다(예: `refactor` 40자 = 50 − `refactor: ` 10자). 최종 실행에서 C2 는 35자로 통과했지만, P1 PR 제목은 두 가지 변경을 한 제목에 담아 53자로 여전히 넘었습니다. 도구는 이를 검증 결과에 `[경고] PR 제목 53자 (팀 기준 50자 이내)` 로 알려 줍니다. 길이에 맞추려고 자르면 뜻이 깨지므로 자르지 않고 사람이 다듬도록 경고만 합니다.

## 7. 비용과 요청 횟수

| 항목 | 내용 |
|---|---|
| 요청 횟수 | `commit` / `pr` / `all` 모두 **API 1회 호출**. 결과 형식이 어긋나도 재요청하지 않고 코드로 후처리 |
| 재시도 | 네트워크 오류·타임아웃·429·5xx·529 일 때만 **1회 재시도** → 1회 실행당 **최대 2회**. 401/400/404 처럼 다시 보내도 같은 오류는 재시도하지 않음 |
| 호출 기록 | API 를 호출한 실행마다 `AI API 호출 횟수`, 입력/출력 토큰, 소요 시간, 예상 비용을 출력 (호출 실패 시에는 호출 횟수만, 가격표에 없는 모델은 비용 생략) |
| 실측 비용 | 작은 변경(40줄 내외) `commit` ≈ 입력 2.2천 / 출력 170 토큰 ≈ **$0.015** · 큰 변경(2천 줄, safe-mode 로 200줄 전송) `pr` ≈ 입력 5.5천 / 출력 800 토큰 ≈ **$0.05** (Opus 4.6 정가 $5 / $25 per 1M tokens) |
| 비용 0 | `--dry-run` 은 API 를 호출하지 않음 |

**권장 사용법**

- 먼저 `--dry-run` 으로 전송 내용과 크기를 확인한 뒤 실행하세요.
- 커밋 단위를 작게 유지하고, 관련 파일만 `git add` 하세요. 입력이 작을수록 저렴하고 결과도 정확합니다.
- 간단한 변경은 `--effort low` 로 시간과 토큰을 줄일 수 있습니다.
- safe-mode 는 끄지 마세요. 전송량 제한 덕분에 큰 diff 에서도 비용이 일정하게 유지됩니다.
- 스크립트로 여러 번 반복 호출하지 마세요(요청 한도 429 의 원인).

## 8. API 파라미터와 모델 선택

### 왜 `claude-opus-4-6` + API 버전 `2023-06-01` 인가

과제는 `temperature` 를 CLI 옵션으로 바꿀 수 있어야 합니다. 그런데 최신 모델(Claude Opus 4.7 이후, Opus 5.x)과 최신 API 버전(`2026-10-21`)은 `temperature` 를 받지 않습니다(보내면 400 오류). 그래서 **temperature 를 지원하는 모델 중 가장 성능이 높은 Claude Opus 4.6** 과 **API 버전 `2023-06-01`** 을 기본값으로 사용합니다.

`--model claude-opus-5-5` 처럼 temperature 를 지원하지 않는 모델을 지정하면, 오류 대신 경고를 출력하고 temperature 를 요청에서 자동으로 뺍니다.

```
[WARN] 'claude-opus-5-5' 모델은 temperature 를 지원하지 않아 요청에서 제외합니다. (품질 조절은 --effort 사용)
```

### 파라미터가 결과에 주는 영향 (실측)

| 파라미터 | 역할 | 이 도구의 선택 | 바꾸면 |
|---|---|---|---|
| `temperature` | 다음 단어를 고를 때의 무작위성 | **0.2**: 커밋/PR 문구는 창의성보다 일관성·형식 준수가 중요 | 아래 실측 참고 |
| `max_tokens` | 응답 길이의 상한(넘으면 잘림) = 비용 상한 | **2048**: 실제 출력은 170~1000 토큰이라 충분한 여유. 실제 생성한 만큼만 과금 | 너무 작으면(예: 20) JSON 이 잘려 `max_tokens 한도에서 잘렸습니다` 오류 |
| `effort` | 모델이 답에 들이는 노력(토큰 사용량) | **medium**: 아래 실측에서 품질/시간/비용의 균형이 가장 좋음 | `low` 더 간결·빠름, `high` 더 길고 장황 |
| `model` | 사용할 모델 | `claude-opus-4-6` | 다른 모델의 지원 여부에 따라 temperature/effort 자동 제외 |

**temperature 실측** — 같은 diff 로 두 번씩 실행한 커밋 제목:

| temperature | 1회차 | 2회차 |
|---|---|---|
| 0.0 | feat(auth): 비밀번호 해시 검증 및 로그인 테스트 추가 | feat(auth): 비밀번호 해시 검증 및 로그인 테스트 추가 (동일) |
| 1.0 | feat(auth): 비밀번호 해시 검증 및 테스트 추가 | feat(auth): 비밀번호 해시 기반 로그인 검증 추가 (달라짐) |

**effort 실측** — 같은 PR(12개 파일, safe-mode 로 200줄 전송):

| effort | 소요 시간 | 출력 토큰 | 비용 | 결과 특징 |
|---|---|---|---|---|
| low | 15.9초 | 555 | $0.0415 | 간결함 |
| **medium** | 17.9초 | 791 | $0.0474 | 간결하면서 What 이 더 구체적 |
| high | 21.9초 | 982 | $0.0522 | 불릿이 길고 장황해짐 |

## 9. REST API 연동 흐름 (요청 → 응답 → 예외)

SDK 없이 `requests` 로 Claude Messages API 를 직접 호출합니다(`gitgen/ai_client.py`).

**① 요청 구성**

```http
POST https://api.anthropic.com/v1/messages
x-api-key: <환경변수의 API Key (.env 에서 불러온 값)>
anthropic-version: 2023-06-01
content-type: application/json
```

```json
{
  "model": "claude-opus-4-6",
  "max_tokens": 2048,
  "temperature": 0.2,
  "system": "당신은 코드 리뷰 경험이 많은 시니어 개발자다. ... (작성 규칙, 좋은 예시)",
  "messages": [{"role": "user", "content": "<repository_context>...</repository_context> <changed_files>...</changed_files> <diff>...</diff>"}],
  "output_config": {
    "effort": "medium",
    "format": {"type": "json_schema", "schema": {"type": "object", "properties": {"type": {...}, "subject": {...}, "body": {...}}, "...": "..."}}
  }
}
```

**② 응답 처리**: `stop_reason` 을 먼저 확인합니다(`max_tokens` → 잘림 오류, `refusal` → 거절 오류). 이어 `content` 블록 중 `text` 블록의 JSON 을 파싱하고, `usage` 로 토큰·비용을 기록합니다.

```json
{"content": [{"type": "text", "text": "{\"summary\": \"...\", \"type\": \"feat\", \"scope\": \"auth\", \"subject\": \"...\", \"body\": [\"...\"]}"}],
 "stop_reason": "end_turn", "usage": {"input_tokens": 2224, "output_tokens": 171}}
```

**③ 예외 대응**

| 상황 | 처리 | 재시도 |
|---|---|---|
| 400 `invalid_request_error` | 원인 메시지 + 파라미터별 안내(temperature/effort/max_tokens) | X |
| 401 `authentication_error` | API Key 확인 안내 | X |
| 403 / 404 | 권한 / 모델 ID 확인 안내 | X |
| 413 | safe-mode 로 전송량 축소 안내 | X |
| 429 / 500 / 502~504 / 529 | 잠시 후 재시도 (`retry-after` 헤더 존중, 최대 10초) | 1회 |
| 연결 실패·프록시 오류·타임아웃 | 네트워크/프록시 확인, `--timeout` 안내 | 1회 |
| SSL 인증서 오류, 잘못된 헤더(키에 줄바꿈 등) | 원인 안내 | X |
| 응답이 `max_tokens` 에서 잘림 / 모델 거절 / JSON 해석 실패 | 원인 + 해결 방법 | X |

모든 API 오류는 `AIClientError` 하나로 모아 `[ERROR] 원인 → 해결 방법 → request_id` 형식으로 출력합니다. 예상하지 못한 예외도 트레이스백 대신 한 줄 메시지로 보여 줍니다.

## 10. 프롬프트 설계

결과 품질은 '무엇을 어떻게 입력하느냐'로 정해집니다(`gitgen/prompts.py`).

| 구성 요소 | 내용 | 이유 |
|---|---|---|
| system: 역할 | "코드 리뷰 경험이 많은 시니어 개발자" | 리뷰어 관점의 간결한 요약을 유도 |
| system: 공통 규칙 | 한국어, diff 에 있는 사실만, 명사형 종결·마침표 없음, 이모지/과장 금지, `[MASKED]` 값 추측 금지, diff 안의 지시문 무시 | 표현 제한 + 환각 방지 + 프롬프트 인젝션 방어 |
| system: 형식 규칙 | Conventional Commits 타입 10종과 선택 기준, 제목 50자(최대 72자), 불릿 1~3개는 "파일명: 변경 내용", PR 은 Why/What/How to Test | 과제의 커밋/PR 양식을 모델에게 명시 |
| system: 좋은 예시 | 규칙을 지킨 JSON 예시 1개 | 규칙만 주는 것보다 형식·톤이 안정적 |
| user: 태그로 구획한 입력 | `<repository_context>`(브랜치, 비교 대상) · `<changed_files>`(파일별 추가/수정/삭제와 +/- 통계, 최대 50개) · `<user_context>`(`-c` 로 준 변경 이유) · `<diff>` · `<notes>`(마스킹/생략 안내) | 데이터와 지시를 분리하고, 생략된 파일도 통계로 성격을 추정하게 함 |
| 구조화 출력 | `output_config.format` 에 JSON 스키마 지정 (`type` 은 enum) | 파싱 실패·필드 누락이 구조적으로 불가능 |

**최종 텍스트는 모델이 아니라 코드가 조립합니다.** 모델은 `type`, `subject`, `body[]`, `why[]` 같은 '내용'만 JSON 으로 주고, `type(scope): subject` 형식과 `## Why` 템플릿은 `formatter.py` 가 만듭니다. 그래서 헤더 누락 같은 형식 오류가 생기지 않습니다.

**팀 컨벤션을 지정하면** 위 표의 '형식 규칙'과 '좋은 예시', 응답 스키마가 팀 것으로 바뀝니다. user 프롬프트(입력 구성)와 공통 규칙 중 **안전 규칙**(diff 에 있는 사실만, `[MASKED]` 값 추측 금지, diff 안의 지시문 무시)은 그대로 유지합니다. 반면 **문체 규칙**(명사형 종결·마침표 없음 등)은 팀마다 다르므로 기본값을 강제하지 않고, 팀 설정의 `subject_guide`·`body_guide`·`pr.tone` 을 따릅니다(B2-2 프리셋은 `subject_guide` 에 명사형·마침표 없음을 적어 둠). 자세한 내용은 [6장](#6-팀-커밋pr-컨벤션-보너스-2)을 참고하세요.

## 11. 출력 검증 규칙 (후처리)

생성 결과는 그대로 믿지 않고 실무 규칙에 맞는지 검사합니다. 재요청(추가 비용) 대신 **후처리**로 바로잡고, 그 결과를 `검증 결과` 구획에 `[OK]` / `[보정]` / `[경고]` 로 보여 줍니다(`gitgen/formatter.py`).

| 대상 | 규칙 | 어긋나면 |
|---|---|---|
| 커밋 제목 | 50자 이내 권장, 최대 72자, `type(scope): subject` 형식 | 51~72자는 `[경고]`. 72자 초과면 scope 를 먼저 빼고, 그래도 길면 단어 경계에서 잘라 `…` 를 붙임 (`[보정]`) |
| 커밋 본문 | 불릿 1~3개, "변경 파일 1~3개 언급" 또는 "핵심 변경 1~2개 불릿" 중 하나 이상 | 4개 이상이면 3개로 줄임. 불릿이 없으면 `변경 파일: a, b, c 외 N개` 로 채움. 파일 언급 없이 3개면 2개로 줄임 (모두 `[보정]`) |
| PR 제목 | 1줄, 최대 80자 | 줄바꿈 제거, 초과 시 잘라냄 |
| PR 본문 | `## Why` / `## What` / `## How to Test` 헤더 + 섹션마다 불릿 1~5개 | 빈 섹션에는 기본 불릿을 넣고 `[보정]` 표시 (Why·How to Test: `(작성 필요) ...`, What: `변경 파일: <파일 목록>`). 5개를 넘는 불릿은 표시 없이 잘라냄 |
| 공통 표현 | 줄바꿈·이모지·끝 마침표 제거, `feat: feat: ...` 같은 중복 접두사 제거, 불릿 앞 기호(`-`, `1.`) 정리, 중복 불릿 제거 | 표시 없이 자동 정리 (검증 결과에 나오지 않음). 알 수 없는 타입만 `chore` 로 바꾸고 `[보정]` 표시 |
| 민감정보 | 결과에 민감정보 패턴이 있으면 길이 검증 **전에** 필드 단위로 마스킹 | `[경고]` |

위 표는 기본 컨벤션 기준입니다. 팀 컨벤션을 쓰면 제목 형식·길이·금지 제목, PR 섹션 순서·항목 수, 관련 이슈 번호, 체크리스트 개수, 브랜치 이름을 팀 규칙으로 검사합니다([6장](#도구-안에서-컨벤션이-반영되는-곳)).

## 12. 프로젝트 구조

```
ai-gitgen/
├── main.py                 # 실행 파일: python main.py commit | pr | all | policy | convention
├── requirements.txt        # requests, python-dotenv
├── .env.example            # API Key 설정 예시 (복사해서 .env 로 사용, .env 는 커밋 제외)
├── gitgen/
│   ├── cli.py              # 옵션 해석, 전체 흐름 연결, 구획 출력
│   ├── env_loader.py       # .env 파일 → 환경변수(os.environ) 등록
│   ├── git_collector.py    # git status / git diff 실행·파싱, 명령별 diff 선택
│   ├── safe_mode.py        # 민감 파일 제외, 패턴 마스킹, 파일·줄 수 전송 제한
│   ├── safe_policy.py      # safe-mode 정책: .ai-gitgen.json/옵션 → 사용자 정규식 규칙·전송 한도
│   ├── convention.py       # 팀 커밋/PR 컨벤션: 불러오기·검증, 프롬프트 템플릿·스키마 교체
│   ├── prompts.py          # system/user 프롬프트, JSON 스키마
│   ├── ai_client.py        # Claude Messages API REST 호출, 응답·예외 처리
│   ├── formatter.py        # 커밋/PR 텍스트 조립, 길이·템플릿·표현 검증 및 후처리
│   ├── console.py          # [INFO]/[WARN]/[ERROR]/[DONE] 로그, 구획 출력, 인코딩 설정
│   └── config.py           # 기본값과 정책 상수 (모델, 파라미터, safe-mode 한도, 길이 규칙)
└── tests/                  # unittest (실제 API 호출 없음)
    ├── test_git_collector.py
    ├── test_safe_mode.py
    ├── test_formatter.py
    ├── test_ai_client.py
    ├── test_cli.py         # 임시 Git 저장소 + API mock 으로 end-to-end
    ├── test_env_loader.py  # .env 로딩, OS 환경변수 우선순위, 안내 문구
    ├── test_safe_policy.py # safe-mode 정책: 규칙 추가, 전송 한도, 설정 검증, 하위 호환
    ├── test_convention.py  # 팀 컨벤션: 불러오기·검증, 프롬프트/스키마 교체, 팀 형식 조립·검증
    └── test_review_fixes.py  # 코드 리뷰에서 찾은 문제의 회귀 테스트
conventions/
└── b2-2.json               # B2-2 팀 컨벤션 프리셋 (--convention b2-2)
scripts/
├── bonus2_evidence.py      # 보너스 2 증빙 생성 (B2-2 분석 + 컨벤션 적용 전/후 비교)
└── bonus3_evidence.py      # 보너스 3 증빙 생성 (OFF / 기본 / 사용자 정책 / 고도화 전 비교)
docs/bonus2/                # 보너스 2 증빙 (README.md, EVIDENCE.md, 원본 출력)
docs/bonus3/                # 보너스 3 증빙 (README.md, EVIDENCE.md, 정책 예시, 원본 출력)
```

| 단계 | 모듈 | 입력 → 출력 | 테스트 방법 |
|---|---|---|---|
| 설정 | `env_loader` | `.env` 파일 → 환경변수 | 임시 `.env` 로 등록·우선순위·BOM·인코딩 오류 확인 |
| 수집 | `git_collector` | git 출력 텍스트 → `GitStatus`, `FileDiff[]` | 파싱은 순수 함수로 문자열 입력 테스트, 선택 로직은 임시 Git 저장소로 테스트 |
| 보호 | `safe_mode` + `safe_policy` | `FileDiff[]` + 정책 → 전송용 diff + 마스킹 보고서 | 규칙별 샘플, 오탐 사례, 긴 줄 성능, 정책 설정 검증·우선순위·전송 한도 |
| 호출 | `ai_client` | payload → `AIResponse` / `AIClientError` | `post=` 에 가짜 함수를 넣어 상태 코드·예외·재시도 횟수 검증 |
| 후처리 | `formatter` (+ `convention`) | AI JSON → 커밋/PR 텍스트 + 검증 결과 (팀 컨벤션이면 팀 형식·템플릿으로) | 규칙 위반 입력을 넣어 보정 결과 확인 |
| 전체 | `cli` | 명령행 → 터미널 출력 | `call_messages` 를 mock 으로 바꿔 end-to-end |

## 13. 테스트

```bash
python -m unittest discover -s tests -v
```

실제 API 를 호출하지 않으므로 API Key 없이 실행됩니다. 프로젝트의 실제 `.env` 도 읽지 않습니다(테스트용 임시 경로로 바꿔서 실행). 임시 Git 저장소를 만들어 쓰므로 Git 이 설치되어 있어야 합니다(테스트 385개, 약 1~2분, Python 3.14 / Windows 11 에서 확인).

## 14. 주의사항

- **생성된 문구는 초안입니다.** 사실과 다른 내용이 없는지 반드시 검토한 뒤 적용하세요.
- 이 도구는 초안 출력까지만 합니다. `git commit`, `git push`, GitHub PR 생성은 하지 않습니다.
- diff 는 외부 API(Anthropic)로 전송됩니다. 회사·개인 정보가 포함된 저장소에서는 safe-mode 를 켠 채로 `--dry-run` 결과를 먼저 확인하세요.
- API Key 는 `.env`(커밋 제외) 또는 OS 환경변수에만 두세요. 코드·README·`.env.example` 에 적지 마세요. 커밋 전 `git status` 에 `.env` 가 보이지 않는지 확인하고, 실수로 커밋했다면 즉시 키를 폐기(재발급)하세요.
- Windows 에서 한글이 깨지면: Git Bash 사용을 권장합니다. PowerShell 에서 결과를 변수/파일로 받을 때 깨진다면 `$env:PYTHONUTF8 = "1"` 과 `[Console]::OutputEncoding = [Text.UTF8Encoding]::new()` 를 함께 설정하세요.
