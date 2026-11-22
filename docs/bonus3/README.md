# 보너스 과제 3 — 안전 모드(safe-mode) 고도화

> **과제 요구**: safe-mode 에서 마스킹 규칙을 확장하거나(정규표현식 기반), diff 전송 제한 정책(파일/줄 기준)을 조정 가능하게 만든다.
> **제출(증빙)**: safe-mode 정책(숫자 기준 포함) + safe-mode ON/OFF 결과 차이 비교

두 가지를 **모두** 구현했습니다. ① 정규식 마스킹 규칙 추가, ② 전송 제한(파일 수 / 전체 줄 수 / 파일당 줄 수) 조정입니다. 증빙으로는 ON/OFF 비교에 더해 **고도화 전 코드와 후 코드의 실제 전송 내용 비교**까지 만들었습니다.

| 문서 | 내용 |
|---|---|
| 이 문서 | 제출용 요약: 구현 내용, 정책(숫자 기준), 핵심 비교 결과 |
| [EVIDENCE.md](EVIDENCE.md) | 스크립트가 자동 생성한 상세 증빙: 노출 매트릭스, 전송량, 파일별 결과, 전/후 diff, 정책 출력, 실제 AI 결과 |
| [ai-gitgen.sample.json](ai-gitgen.sample.json) | 비교에 사용한 사용자 정책 파일 |
| [raw/](raw/) | 각 실행의 원본 출력(stdout+stderr)과 전송 프롬프트 diff |
| [../../scripts/bonus3_evidence.py](../../scripts/bonus3_evidence.py) | 증빙 생성 스크립트 (누구나 같은 결과를 재현 가능) |

---

## 1. 요구사항 → 구현 → 증빙

| 요구 | 구현 | 증빙 위치 |
|---|---|---|
| 마스킹 규칙 확장 (정규식 기반) | `.ai-gitgen.json` 의 `mask_rules` / `--mask-pattern NAME=REGEX`. `(?P<value>...)` 로 값만 가리기, 내장 규칙 끄기(`disable_rules`) | EVIDENCE §2 매트릭스의 "사용자 …" 행, §4 0→3 diff |
| 전송 제한 정책 조정 (파일/줄 기준) | `max_files`, `max_lines`, `max_lines_per_file` (+ `exclude_files` 로 파일 제외) — 설정 파일 또는 CLI 옵션 | EVIDENCE §3 전송량·파일별 표 |
| safe-mode 정책 (숫자 기준 포함) | `python main.py policy` 가 적용될 정책을 숫자·출처와 함께 출력 | 아래 §2, EVIDENCE §5, [raw/policy_custom.txt](raw/policy_custom.txt) |
| safe-mode ON/OFF 결과 차이 비교 | 같은 변경을 OFF / ON(기본) / ON(사용자 정책)으로 실행해 비교 | 아래 §4, EVIDENCE §2·§3·§4·§6 |
| (추가) 고도화 전/후 비교 | 고도화 전 커밋(`bef4efd`) 코드를 `git archive` 로 꺼내 같은 조건으로 실행 | 아래 §5, EVIDENCE §4 (0→2, 0→3) |

## 2. safe-mode 정책 (숫자 기준)

### 기본 정책과 비교에 쓴 사용자 정책

| 항목 | 기본 정책 (= 고도화 전 동작) | 사용자 정책 ([ai-gitgen.sample.json](ai-gitgen.sample.json)) | 허용 범위 |
|---|---|---|---|
| 최대 파일 수 (`max_files`) | **10개** | **5개** | 1 ~ 1000 |
| 전체 최대 줄 수 (`max_lines`) | **200줄** | **120줄** | 1 ~ 20000 |
| 파일당 최대 줄 수 (`max_lines_per_file`) | **제한 없음 (0)** | **30줄** | 0 ~ 20000 (0 = 제한 없음) |
| 전송 하드 상한 (문자 수) | 150,000자 (고정, OFF 에도 적용) | 150,000자 | 변경 불가 |
| 마스킹 규칙 | 내장 21개 | 내장 21개 + **사용자 5개** | 이름 영문 40자, 정규식 500자 이내 |
| 내용 제외 파일 | 내장 패턴 31개 (`.env*`, `*.pem`, `id_rsa*` …) | 내장 31개 + **`*.sql`, `db/`** | glob 문자열 |

**줄 수 계산 방식**: diff 본문 줄(파일 헤더 포함)을 셉니다. 먼저 파일당 상한을 적용하고, 남은 전체 예산을 파일마다 **공평하게** 나눕니다(water-filling: 작은 파일은 전부, 큰 파일들은 나머지를 똑같이). 파일 수 제한을 넘거나 줄 예산을 못 받은 파일도 **이름과 +/- 통계는** 전송합니다.

### 사용자 마스킹 규칙 (샘플)

| 이름 | 정규식 | 가린 결과 | 내장 규칙으로 잡히나? |
|---|---|---|---|
| `EMPLOYEE_ID` | `\bEMP-\d{6}\b` | `[MASKED:EMPLOYEE]` | ✗ (사내 형식) |
| `INTERNAL_HOST` | `\b[\w-]+\.corp\.example\.com\b` | `[MASKED:INTERNAL_HOST]` | ✗ |
| `SHOP_TOKEN` | `\bshoptok_[0-9a-f]{12}\b` | `[MASKED:TOKEN]` | ✗ (키 이름에 secret/token 이 없음) |
| `CODENAME` | `(?i)\bfalcon-x\b` | `[MASKED:CODENAME]` | ✗ |
| `LICENSE_SERIAL` | `LICENSE_SERIAL = "(?P<value>[A-Z0-9-]{12,})"` | `LICENSE_SERIAL = "[MASKED:LICENSE]"` (**값만**) | ✗ |

### 정책이 정해지는 순서와 규칙

1. **기본값**(config.py) → 2. **대상 프로젝트 루트의 `.ai-gitgen.json`**(또는 `--safe-mode-config`) → 3. **CLI 옵션** 순서로 합쳐집니다. 숫자 항목은 뒤의 값이 덮어쓰고, 목록 항목(`mask_rules`, `exclude_files`)은 뒤의 값이 추가됩니다.
- **규칙 적용 순서는 내장 규칙 → 사용자 규칙입니다.** 사용자 규칙을 먼저 적용하면, 그 규칙이 일부를 바꾼 탓에 내장 규칙이 원래 잡던 비밀값을 놓치는 문제가 코드 리뷰에서 확인되었습니다. 예를 들어 `ACME` 를 가리는 규칙을 넣으면 `ACME_DB_PASSWORD=...` 의 값이 노출됐습니다. 그래서 사용자 규칙은 **기존 보호를 줄이지 않고 더하기만** 하도록 설계했습니다.
- 규칙은 diff 본문(줄마다 `+`/`-`/공백으로 시작)에 적용되므로 `^`, `$` 는 `(?m)` 없이 쓸 수 없습니다. 길이 0 과 일치하는 정규식(`a*`, `\b`), 잘못된 label, `safe_mode` 섹션 밖에 쓴 항목 등은 실행 전에 원인과 함께 거부합니다(종료 코드 2).
- `exclude_files` 는 gitignore 와 비슷한 문법을 씁니다: `*.sql`(어느 폴더든), `db/`(폴더 전체), `db/*.sql`(루트 기준), `**/*.sql`. 이름이 바뀐 파일은 원래 경로도 검사하고, 이번 변경과 일치하지 않는 패턴은 오타일 수 있어 로그로 알려 줍니다.
- 정책 파일(`.ai-gitgen.json`) 자체는 내용을 보내지 않습니다. 정규식 원문에 가리려는 값(사내 도메인, 코드명)이 들어 있기 때문입니다.
- safe-mode OFF 에서는 정책이 적용되지 않고 경고가 출력됩니다. 정책을 지정하지 않으면 **고도화 전과 완전히 같게** 동작합니다(§5에서 증명).

## 3. 사용 방법

```bash
# 1) 적용될 정책 확인 (API 호출 없음) + 특정 문자열이 어떻게 가려지는지 시험
python main.py policy --safe-mode-config docs/bonus3/ai-gitgen.sample.json --check "담당 EMP-204817 / 프로젝트 FALCON-X"

# 2) 팀 정책 파일로 실행: 대상 프로젝트 루트에 .ai-gitgen.json 을 두면 자동 적용
python main.py commit --dry-run

# 3) 이번 실행에만 CLI 로 조정
python main.py commit --max-files 5 --max-lines 120 --max-lines-per-file 30 \
  --mask-pattern "EMPLOYEE_ID=\bEMP-\d{6}\b" --exclude-file "*.sql"
```

`policy --check` 출력 (발췌, 전체는 [raw/policy_custom.txt](raw/policy_custom.txt)):

```
================= 정책 적용 확인 (--check) =================
입력 : 담당 EMP-204817 / 프로젝트 FALCON-X
결과 : 담당 [MASKED:EMPLOYEE] / 프로젝트 [MASKED:CODENAME]
적용 : EMPLOYEE_ID 1, CODENAME 1  (diff 의 추가 줄 '+...' 로 넣어 실제 전송과 같은 조건에서 확인)

================= 정책 적용 확인 (--check) =================
입력 : LICENSE_SERIAL = "SHOP-7F3K-92QX-11ZD"
결과 : LICENSE_SERIAL = "[MASKED:LICENSE]"
적용 : LICENSE_SERIAL 1  (diff 의 추가 줄 '+...' 로 넣어 실제 전송과 같은 조건에서 확인)
```

## 4. 증빙 ① — safe-mode ON / OFF 결과 차이

가짜 민감정보 13종이 들어 있는 같은 변경(파일 10개, diff 335줄)을 조건만 바꿔 실행했습니다. 판정은 **실제로 API 에 전송될 user 프롬프트**에서 각 값을 검색한 결과입니다.

| | 1 OFF | 2 ON · 기본 정책 | 3 ON · 사용자 정책 |
|---|---|---|---|
| **민감정보 노출** | **13/13** | **7/13** | **0/13** |
| 전송 diff 줄 수 | 335 | 200 | 120 |
| 마스킹 건수 | 0 | 8 | 10 |
| 내용 제외 파일 | 0 | 1 (`.env`) | 2 (`.env`, `*.sql`) |
| 파일 수 제한으로 생략 | 0 | 0 | 3 |
| 전송 프롬프트 크기 | 13,058자 | 7,927자 | 5,192자 |
| 실제 API 입력 토큰 / 비용 | 8,824 / $0.0518 | 5,990 / $0.0357 | 4,461 / $0.0284 |

- **OFF**: API Key, DB 비밀번호, `.env`, 고객 정보까지 13종이 **모두 그대로** 전송됩니다.
- **ON · 기본 정책**: 형식이 알려진 6종은 가리지만, 사내 고유 형식 7종(사내 호스트 2, 사번, 사내 토큰, 코드명, 라이선스 시리얼, SQL 속 고객 이름)은 **노출**됩니다.
- **ON · 사용자 정책**: 정규식 규칙 5개와 `*.sql` 제외로 **13종 모두 차단**합니다. 전송량은 OFF 대비 **64% 감소**(335→120줄)했고, 실제 API 비용은 **45% 감소**($0.0518→$0.0284)했습니다.
- 세 조건 모두에서 AI 가 만든 커밋 메시지 품질은 비슷했습니다(EVIDENCE §6). 마스킹과 전송 제한을 해도 결과물 품질이 떨어지지 않았습니다.

민감정보별 판정 전체(✅ 마스킹 / 🚫 파일 제외 / ❌ 노출)는 [EVIDENCE.md §2](EVIDENCE.md#2-민감정보-노출-매트릭스)를 보세요.

## 5. 증빙 ② — 고도화 전 / 후 비교

고도화 전 커밋 `bef4efd` 의 코드를 `git archive` 로 꺼내, 같은 샘플을 같은 명령으로 실행해 비교했습니다.

| 비교 | 결과 |
|---|---|
| **0 고도화 전 ↔ 2 고도화 후(기본 정책)** | 전송 프롬프트 **차이 0줄**. system 프롬프트와 요청 파라미터도 동일 → 정책을 쓰지 않으면 동작이 **바뀌지 않음**(하위 호환) |
| **0 고도화 전 ↔ 3 고도화 후(사용자 정책)** | 바뀐 줄 127줄. 노출 **7/13 → 0/13**, 전송 **200 → 120줄** |

0 → 3 전송 프롬프트 diff 발췌 (`-` 고도화 전, `+` 고도화 후 · 전체: [raw/diff_0_vs_3.diff](raw/diff_0_vs_3.diff)):

```diff
+- [추가] app/services/service_04.py (+42/-0)  ※ 파일 수 제한(5개) 초과: 내용 생략(safe-mode)
+- [추가] db/seed_customers.sql (+2/-0)  ※ 사용자 정책(exclude_files)으로 내용 제외(safe-mode)
-+DATABASE_URL = "postgres://[MASKED:CREDENTIAL]@db1.corp.example.com:5432/shop"
++DATABASE_URL = "postgres://[MASKED:CREDENTIAL]@[MASKED:INTERNAL_HOST]:5432/shop"
-+LICENSE_SERIAL = "SHOP-7F3K-92QX-11ZD"
++LICENSE_SERIAL = "[MASKED:LICENSE]"
-+BILLING_HOST = "billing.corp.example.com"
++BILLING_HOST = "[MASKED:INTERNAL_HOST]"
-+    """주문을 만들고 결제 서버에 요청한다. (프로젝트 FALCON-X 결제 연동, 출시 전 비공개)"""
-+    # 담당: EMP-204817 (결제 연동)
-+    headers = {"X-Shop-Auth": "shoptok_9f8e7d6c5b4a"}
++    """주문을 만들고 결제 서버에 요청한다. (프로젝트 [MASKED:CODENAME] 결제 연동, 출시 전 비공개)"""
++    # 담당: [MASKED:EMPLOYEE] (결제 연동)
++    headers = {"X-Shop-Auth": "[MASKED:TOKEN]"}
```

## 6. 재현 방법

```bash
# dry-run 비교만 (API 호출 없음, 비용 0)
python scripts/bonus3_evidence.py --baseline-ref bef4efd

# 실제 AI 결과 비교까지 (API 3회 호출, .env 의 API Key 사용)
python scripts/bonus3_evidence.py --baseline-ref bef4efd --with-api
```

스크립트는 임시 폴더에 가짜 민감정보가 든 샘플 저장소를 만들어 실행하고, 끝나면 지웁니다. 결과는 `docs/bonus3/` 에 씁니다. 사용자의 전역 git 설정(전역 ignore, hook)이 결과에 영향을 주지 않도록 격리합니다. 판정할 수 없는 경우가 있으면 '안전'으로 표시하지 않고 실패합니다(fail-closed). 생성된 문서에 개인 PC 경로가 남으면 역시 실패합니다.

## 7. 테스트

`tests/test_safe_policy.py` 에 **50개** 테스트가 있습니다(실제 API 호출 없음).

- 정책 불러오기·우선순위·항목별 출처, 설정 오류 검증(JSON 형식, 알 수 없는 항목, 섹션 밖 항목, 숫자 타입·범위, 정규식 오류, 길이 0 일치, `^`/`$`, label, 중복·내장 이름, 없는 설정 파일 등)
- 사용자 규칙이 **내장 규칙의 보호를 줄이지 않는지**(`ACME_DB_PASSWORD`, 부분 토큰, 사내 도메인 이메일 등 리뷰에서 재현된 사례)
- `(?P<value>)` 부분 마스킹, 내장 규칙 끄기, gitignore 스타일 제외 패턴 12가지, 정책 파일 자체 제외
- 파일 수·전체 줄 수·파일당 줄 수 한도, 기본 정책 = 고도화 전 결과(하위 호환), safe-mode OFF 시 미적용
- CLI: `policy` 출력, `--check`, 설정 파일 자동 적용, 기준 브랜치 이름 마스킹, AI 결과 마스킹

```bash
python -m unittest tests.test_safe_policy -v
```
