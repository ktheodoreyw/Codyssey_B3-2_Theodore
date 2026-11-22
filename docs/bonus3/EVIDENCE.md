# 보너스 과제 3 — 안전 모드 고도화 증빙 (자동 생성)

> `scripts/bonus3_evidence.py` 가 생성한 문서입니다. 직접 수정하지 말고 스크립트를 다시 실행하세요.  
> 생성: 2026-11-22 19:00 · 고도화 후 코드: `bef4efd + 작업 중 변경` · Python 3.14.6 · Windows 11
>  
> 고도화 전 기준: `bef4efd` (이 커밋을 `git archive` 로 꺼낸 코드로 0번 조건을 실행)

모든 민감정보는 **가짜 값**입니다. 같은 샘플 변경(파일 10개)을 조건만 바꿔 `commit --dry-run` 으로 실행했고, 표의 판정은 **실제로 API 에 전송될 user 프롬프트**에서 각 값을 검색한 결과입니다. 판정할 수 없는 경우가 하나라도 있으면 스크립트가 실패하도록 되어 있습니다(fail-closed).

## 1. 실행 조건

| 기호 | 조건 | 실행한 코드 | 명령 |
|---|---|---|---|
| 0 | 고도화 전 코드 | 고도화 전 (`bef4efd`) | `python main.py commit --dry-run` |
| 1 | safe-mode OFF | 고도화 후 | `python main.py commit --dry-run --no-safe-mode` |
| 2 | ON 기본 정책 | 고도화 후 | `python main.py commit --dry-run` |
| 3 | ON 사용자 정책 | 고도화 후 | `python main.py commit --dry-run --safe-mode-config docs/bonus3/ai-gitgen.sample.json` |

## 2. 민감정보 노출 매트릭스

✅ 마스킹 = 줄은 전송되지만 값은 `[MASKED:..]` 로 치환 · 🚫 파일 제외 = 파일 내용 자체를 전송하지 않음 · ✂️ = 전송량 제한으로 해당 줄이 잘림 · ❌ 노출 = 값이 그대로 전송됨

| 민감정보 | 파일 | 잡는 규칙 | 0 고도화 전 | 1 OFF | 2 ON 기본 | 3 ON 사용자 |
|---|---|---|---|---|---|---|
| Anthropic API Key | `app/config.py` | 내장 ANTHROPIC_KEY | ✅ 마스킹 | ❌ 노출 | ✅ 마스킹 | ✅ 마스킹 |
| DB 비밀번호 (URL 안) | `app/config.py` | 내장 URL_CREDENTIAL | ✅ 마스킹 | ❌ 노출 | ✅ 마스킹 | ✅ 마스킹 |
| 관리자 이메일 | `app/config.py` | 내장 EMAIL | ✅ 마스킹 | ❌ 노출 | ✅ 마스킹 | ✅ 마스킹 |
| 고객센터 전화번호 | `app/config.py` | 내장 KR_PHONE | ✅ 마스킹 | ❌ 노출 | ✅ 마스킹 | ✅ 마스킹 |
| 사내 DB 호스트 | `app/config.py` | 사용자 INTERNAL_HOST | ❌ 노출 | ❌ 노출 | ❌ 노출 | ✅ 마스킹 |
| 라이선스 시리얼 (값만) | `app/config.py` | 사용자 LICENSE_SERIAL (?P<value>) | ❌ 노출 | ❌ 노출 | ❌ 노출 | ✅ 마스킹 |
| 결제 서버 호스트 | `app/orders.py` | 사용자 INTERNAL_HOST | ❌ 노출 | ❌ 노출 | ❌ 노출 | ✅ 마스킹 |
| 담당자 사번 | `app/orders.py` | 사용자 EMPLOYEE_ID | ❌ 노출 | ❌ 노출 | ❌ 노출 | ✅ 마스킹 |
| 사내 인증 토큰 (헤더) | `app/orders.py` | 사용자 SHOP_TOKEN | ❌ 노출 | ❌ 노출 | ❌ 노출 | ✅ 마스킹 |
| 비공개 프로젝트 코드명 | `app/orders.py` | 사용자 CODENAME | ❌ 노출 | ❌ 노출 | ❌ 노출 | ✅ 마스킹 |
| 고객 이름 (SQL 시드) | `db/seed_customers.sql` | 사용자 exclude_files *.sql | ❌ 노출 | ❌ 노출 | ❌ 노출 | 🚫 파일 제외 |
| 고객 주민등록번호 (SQL) | `db/seed_customers.sql` | 내장 KR_RRN | ✅ 마스킹 | ❌ 노출 | ✅ 마스킹 | 🚫 파일 제외 |
| .env DB 비밀번호 | `.env` | 내장 민감 파일(.env*) | 🚫 파일 제외 | ❌ 노출 | 🚫 파일 제외 | 🚫 파일 제외 |
| **노출 개수** | | | **7/13** | **13/13** | **7/13** | **0/13** |

## 3. 전송량 비교

| 항목 | 0 고도화 전 | 1 OFF | 2 ON 기본 | 3 ON 사용자 |
|---|---|---|---|---|
| 원본 diff 줄 수 | 335 | 335 | 335 | 335 |
| 전송 diff 줄 수 | 200 | 335 | 200 | 120 |
| 마스킹 건수 | 8 | 0 | 8 | 10 |
| 민감 파일 제외(내장) | 1 | 0 | 1 | 1 |
| 정책 제외(exclude_files) | 0 | 0 | 0 | 1 |
| 파일 수 제한으로 생략 | 0 | 0 | 0 | 3 |
| 전송 프롬프트 크기(문자) | 7,927 | 13,058 | 7,927 | 5,192 |
| 마스킹 상세 | ANTHROPIC_KEY 1, EMAIL 1, KR_PHONE 3, KR_RRN 2, URL_CREDENTIAL 1 | - | ANTHROPIC_KEY 1, EMAIL 1, KR_PHONE 3, KR_RRN 2, URL_CREDENTIAL 1 | ANTHROPIC_KEY 1, CODENAME 1, EMAIL 1, EMPLOYEE_ID 1, INTERNAL_HOST 2, KR_PHONE 1, LICENSE_SERIAL 1, SHOP_TOKEN 1, URL_CREDENTIAL 1 |

### 파일별 전송 결과

| 파일 | 0 고도화 전 | 1 OFF | 2 ON 기본 | 3 ON 사용자 |
|---|---|---|---|---|
| `.env` | 민감 파일로 판단되어 내용 제외 | 7줄 | 민감 파일로 판단되어 내용 제외 | 민감 파일로 판단되어 내용 제외 |
| `app/config.py` | 14줄 | 14줄 | 14줄 | 14줄 |
| `app/orders.py` | 18줄 | 18줄 | 18줄 | 18줄 |
| `app/services/service_01.py` | 27줄 (diff 48줄 중 27줄만 전송) | 48줄 | 27줄 (diff 48줄 중 27줄만 전송) | 30줄 (diff 48줄 중 30줄만 전송) |
| `app/services/service_02.py` | 27줄 (diff 48줄 중 27줄만 전송) | 48줄 | 27줄 (diff 48줄 중 27줄만 전송) | 29줄 (diff 48줄 중 29줄만 전송) |
| `app/services/service_03.py` | 27줄 (diff 48줄 중 27줄만 전송) | 48줄 | 27줄 (diff 48줄 중 27줄만 전송) | 29줄 (diff 48줄 중 29줄만 전송) |
| `app/services/service_04.py` | 27줄 (diff 48줄 중 27줄만 전송) | 48줄 | 27줄 (diff 48줄 중 27줄만 전송) | 파일 수 제한(5개) 초과: 내용 생략 |
| `app/services/service_05.py` | 26줄 (diff 48줄 중 26줄만 전송) | 48줄 | 26줄 (diff 48줄 중 26줄만 전송) | 파일 수 제한(5개) 초과: 내용 생략 |
| `app/services/service_06.py` | 26줄 (diff 48줄 중 26줄만 전송) | 48줄 | 26줄 (diff 48줄 중 26줄만 전송) | 파일 수 제한(5개) 초과: 내용 생략 |
| `db/seed_customers.sql` | 8줄 | 8줄 | 8줄 | 사용자 정책(exclude_files)으로 내용 제외 |

## 4. 전/후 비교 (전송 프롬프트 diff)

### 0 → 2: 고도화 전 코드 vs 고도화 후 기본 정책 (하위 호환)

정책을 지정하지 않으면 고도화 전과 **완전히 같은** 내용이 전송되는지 확인. 바뀐 줄 **0줄** (전체 diff: `raw/diff_0_vs_2.diff`)

```
(차이 없음 — 두 실행의 user 프롬프트가 글자 하나까지 동일)
system 프롬프트 동일: 예 / 요청 파라미터 동일: 예
```

### 0 → 3: 고도화 전 코드 vs 고도화 후 사용자 정책 (전/후 비교)

고도화로 새로 가능해진 정규식 규칙·제외 파일·전송 한도 조정이 실제 전송 내용을 어떻게 바꾸는지. 바뀐 줄 **127줄** (전체 diff: `raw/diff_0_vs_3.diff`)

```diff
-- [추가] app/services/service_01.py (+42/-0)  ※ diff 48줄 중 27줄만 전송
-- [추가] app/services/service_02.py (+42/-0)  ※ diff 48줄 중 27줄만 전송
-- [추가] app/services/service_03.py (+42/-0)  ※ diff 48줄 중 27줄만 전송
-- [추가] app/services/service_04.py (+42/-0)  ※ diff 48줄 중 27줄만 전송
-- [추가] app/services/service_05.py (+42/-0)  ※ diff 48줄 중 26줄만 전송
-- [추가] app/services/service_06.py (+42/-0)  ※ diff 48줄 중 26줄만 전송
+- [추가] app/services/service_01.py (+42/-0)  ※ diff 48줄 중 30줄만 전송
+- [추가] app/services/service_02.py (+42/-0)  ※ diff 48줄 중 29줄만 전송
+- [추가] app/services/service_03.py (+42/-0)  ※ diff 48줄 중 29줄만 전송
+- [추가] app/services/service_04.py (+42/-0)  ※ 파일 수 제한(5개) 초과: 내용 생략(safe-mode)
+- [추가] app/services/service_05.py (+42/-0)  ※ 파일 수 제한(5개) 초과: 내용 생략(safe-mode)
+- [추가] app/services/service_06.py (+42/-0)  ※ 파일 수 제한(5개) 초과: 내용 생략(safe-mode)
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
-+INSERT INTO customers (name, rrn, phone) VALUES ('홍길동', '[MASKED:RRN]', '[MASKED:PHONE]');
-+INSERT INTO customers (name, rrn, phone) VALUES ('김철수', '[MASKED:RRN]', '[MASKED:PHONE]');
-- 민감정보로 의심되는 값 8건이 [MASKED:종류] 로 가려져 있다.
+- 민감정보로 의심되는 값 10건이 [MASKED:종류] 로 가려져 있다.
```

### 1 → 2: safe-mode OFF vs ON (기본 정책)

OFF 에서 그대로 나가던 값이 ON 에서 어떻게 바뀌는지. 바뀐 줄 **171줄** (전체 diff: `raw/diff_1_vs_2.diff`)

```diff
+- [추가] .env (+1/-0)  ※ 민감 파일로 판단되어 내용 제외(safe-mode)
+- [추가] app/services/service_01.py (+42/-0)  ※ diff 48줄 중 27줄만 전송
+- [추가] app/services/service_02.py (+42/-0)  ※ diff 48줄 중 27줄만 전송
+- [추가] app/services/service_03.py (+42/-0)  ※ diff 48줄 중 27줄만 전송
+- [추가] app/services/service_04.py (+42/-0)  ※ diff 48줄 중 27줄만 전송
+- [추가] app/services/service_05.py (+42/-0)  ※ diff 48줄 중 26줄만 전송
+- [추가] app/services/service_06.py (+42/-0)  ※ diff 48줄 중 26줄만 전송
-+DB_PASSWORD=env-only-pass-1234
-+ANTHROPIC_API_KEY = "sk-ant-api03-FAKE0000example0000key"
-+DATABASE_URL = "postgres://shop_admin:Sh0pPassw0rd!@db1.corp.example.com:5432/shop"
-+ADMIN_EMAIL = "kim.admin@corp.example.com"
-+SUPPORT_PHONE = "010-1234-5678"
++ANTHROPIC_API_KEY = "[MASKED:API_KEY]"
++DATABASE_URL = "postgres://[MASKED:CREDENTIAL]@db1.corp.example.com:5432/shop"
++ADMIN_EMAIL = "[MASKED:EMAIL]"
++SUPPORT_PHONE = "[MASKED:PHONE]"
-+INSERT INTO customers (name, rrn, phone) VALUES ('홍길동', '900101-1234567', '010-9876-5432');
++INSERT INTO customers (name, rrn, phone) VALUES ('홍길동', '[MASKED:RRN]', '[MASKED:PHONE]');
++INSERT INTO customers (name, rrn, phone) VALUES ('김철수', '[MASKED:RRN]', '[MASKED:PHONE]');
+<notes>
+- 민감정보로 의심되는 값 8건이 [MASKED:종류] 로 가려져 있다.
```

### 1 → 3: safe-mode OFF vs ON (사용자 정책)

OFF 와 고도화된 safe-mode 의 차이. 바뀐 줄 **252줄** (전체 diff: `raw/diff_1_vs_3.diff`)

```diff
+- [추가] .env (+1/-0)  ※ 민감 파일로 판단되어 내용 제외(safe-mode)
+- [추가] app/services/service_01.py (+42/-0)  ※ diff 48줄 중 30줄만 전송
+- [추가] app/services/service_02.py (+42/-0)  ※ diff 48줄 중 29줄만 전송
+- [추가] app/services/service_03.py (+42/-0)  ※ diff 48줄 중 29줄만 전송
+- [추가] app/services/service_04.py (+42/-0)  ※ 파일 수 제한(5개) 초과: 내용 생략(safe-mode)
+- [추가] app/services/service_05.py (+42/-0)  ※ 파일 수 제한(5개) 초과: 내용 생략(safe-mode)
+- [추가] app/services/service_06.py (+42/-0)  ※ 파일 수 제한(5개) 초과: 내용 생략(safe-mode)
+- [추가] db/seed_customers.sql (+2/-0)  ※ 사용자 정책(exclude_files)으로 내용 제외(safe-mode)
-+DB_PASSWORD=env-only-pass-1234
-+ANTHROPIC_API_KEY = "sk-ant-api03-FAKE0000example0000key"
-+DATABASE_URL = "postgres://shop_admin:Sh0pPassw0rd!@db1.corp.example.com:5432/shop"
-+ADMIN_EMAIL = "kim.admin@corp.example.com"
-+SUPPORT_PHONE = "010-1234-5678"
-+LICENSE_SERIAL = "SHOP-7F3K-92QX-11ZD"
++ANTHROPIC_API_KEY = "[MASKED:API_KEY]"
++DATABASE_URL = "postgres://[MASKED:CREDENTIAL]@[MASKED:INTERNAL_HOST]:5432/shop"
++ADMIN_EMAIL = "[MASKED:EMAIL]"
++SUPPORT_PHONE = "[MASKED:PHONE]"
++LICENSE_SERIAL = "[MASKED:LICENSE]"
-+BILLING_HOST = "billing.corp.example.com"
++BILLING_HOST = "[MASKED:INTERNAL_HOST]"
-+    """주문을 만들고 결제 서버에 요청한다. (프로젝트 FALCON-X 결제 연동, 출시 전 비공개)"""
-+    # 담당: EMP-204817 (결제 연동)
-+    headers = {"X-Shop-Auth": "shoptok_9f8e7d6c5b4a"}
++    """주문을 만들고 결제 서버에 요청한다. (프로젝트 [MASKED:CODENAME] 결제 연동, 출시 전 비공개)"""
++    # 담당: [MASKED:EMPLOYEE] (결제 연동)
++    headers = {"X-Shop-Auth": "[MASKED:TOKEN]"}
-+INSERT INTO customers (name, rrn, phone) VALUES ('홍길동', '900101-1234567', '010-9876-5432');
+<notes>
+- 민감정보로 의심되는 값 10건이 [MASKED:종류] 로 가려져 있다.
```

## 5. 적용 정책 (`python main.py policy`)

### 기본 정책

```
====================== safe-mode 정책 ======================
설정 파일 : 없음 (현재 폴더에 .ai-gitgen.json 이 없어 기본 정책 사용)

[전송 제한]  safe-mode ON 일 때 외부 API 로 보내는 diff 의 양
  최대 파일 수      : 10개  (기본값)
  전체 최대 줄 수   : 200줄  (기본값)
  파일당 최대 줄 수 : 제한 없음  (기본값)
  전송 하드 상한    : 150,000자  (고정, safe-mode OFF 에도 적용)

[민감 파일 제외]  내용은 보내지 않고 파일명과 +/- 통계만 전송
  내장 패턴 31개 : .env*, *.env, *.pem, *.key, *.p12, *.pfx, *.ppk, *.jks, *.keystore, id_rsa*, id_dsa*, id_ecdsa*, id_ed25519*, *.kdbx, credentials, credentials*.json, service-account*.json, *secret*, .npmrc, .pypirc, .netrc, .pgpass, .git-credentials, kubeconfig, *.tfstate, *.tfstate.*, *.tfvars, .aws/*, .ssh/*, .kube/config, .docker/config.json
    (이름이 .example / .sample / .template / .dist 로 끝나는 예시 파일은 제외하지 않고 마스킹 후 전송)
  사용자 패턴 : 없음
  정책 파일(.ai-gitgen.json) 자체도 내용을 보내지 않음 (가리려는 값이 정규식 원문에 들어 있으므로)

[마스킹 규칙]  위에서부터 순서대로 적용, 가린 자리는 [MASKED:라벨]
  1) 내장 규칙 21개 (전체 21개)
     PRIVATE_KEY, ANTHROPIC_KEY, OPENAI_KEY, STRIPE_KEY, AWS_ACCESS_KEY, GITHUB_TOKEN
     GITLAB_TOKEN, HUGGINGFACE_TOKEN, GOOGLE_API_KEY, SLACK_TOKEN, JWT, BEARER
     BASIC_AUTH, URL_CREDENTIAL, QUERY_TOKEN, EMAIL, KR_RRN, KR_PHONE
     CARD_NUMBER, SECRET_ASSIGNMENT, ENV_SECRET
  2) 사용자 규칙 : 없음

================= 정책 적용 확인 (--check) =================
입력 : 담당 EMP-204817 / 프로젝트 FALCON-X
결과 : 담당 EMP-204817 / 프로젝트 FALCON-X
적용 : 가려진 값 없음  (diff 의 추가 줄 '+...' 로 넣어 실제 전송과 같은 조건에서 확인)

================= 정책 적용 확인 (--check) =================
입력 : headers = {"X-Shop-Auth": "shoptok_9f8e7d6c5b4a"}
결과 : headers = {"X-Shop-Auth": "shoptok_9f8e7d6c5b4a"}
적용 : 가려진 값 없음  (diff 의 추가 줄 '+...' 로 넣어 실제 전송과 같은 조건에서 확인)

================= 정책 적용 확인 (--check) =================
입력 : LICENSE_SERIAL = "SHOP-7F3K-92QX-11ZD"
결과 : LICENSE_SERIAL = "SHOP-7F3K-92QX-11ZD"
적용 : 가려진 값 없음  (diff 의 추가 줄 '+...' 로 넣어 실제 전송과 같은 조건에서 확인)
```

### 사용자 정책 (ai-gitgen.sample.json)

```
====================== safe-mode 정책 ======================
설정 파일 : docs/bonus3/ai-gitgen.sample.json

[전송 제한]  safe-mode ON 일 때 외부 API 로 보내는 diff 의 양
  최대 파일 수      : 5개  (설정 파일, 기본 10개)
  전체 최대 줄 수   : 120줄  (설정 파일, 기본 200줄)
  파일당 최대 줄 수 : 30줄  (설정 파일, 기본 제한 없음)
  전송 하드 상한    : 150,000자  (고정, safe-mode OFF 에도 적용)

[민감 파일 제외]  내용은 보내지 않고 파일명과 +/- 통계만 전송
  내장 패턴 31개 : .env*, *.env, *.pem, *.key, *.p12, *.pfx, *.ppk, *.jks, *.keystore, id_rsa*, id_dsa*, id_ecdsa*, id_ed25519*, *.kdbx, credentials, credentials*.json, service-account*.json, *secret*, .npmrc, .pypirc, .netrc, .pgpass, .git-credentials, kubeconfig, *.tfstate, *.tfstate.*, *.tfvars, .aws/*, .ssh/*, .kube/config, .docker/config.json
    (이름이 .example / .sample / .template / .dist 로 끝나는 예시 파일은 제외하지 않고 마스킹 후 전송)
  사용자 패턴 2개 (목록 항목은 설정 파일 + CLI 옵션이 합쳐짐)
    - *.sql  (설정 파일)
    - db/  (설정 파일)
  정책 파일(.ai-gitgen.json) 자체도 내용을 보내지 않음 (가리려는 값이 정규식 원문에 들어 있으므로)

[마스킹 규칙]  위에서부터 순서대로 적용, 가린 자리는 [MASKED:라벨]
  1) 내장 규칙 21개 (전체 21개)
     PRIVATE_KEY, ANTHROPIC_KEY, OPENAI_KEY, STRIPE_KEY, AWS_ACCESS_KEY, GITHUB_TOKEN
     GITLAB_TOKEN, HUGGINGFACE_TOKEN, GOOGLE_API_KEY, SLACK_TOKEN, JWT, BEARER
     BASIC_AUTH, URL_CREDENTIAL, QUERY_TOKEN, EMAIL, KR_RRN, KR_PHONE
     CARD_NUMBER, SECRET_ASSIGNMENT, ENV_SECRET
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

================= 정책 적용 확인 (--check) =================
입력 : headers = {"X-Shop-Auth": "shoptok_9f8e7d6c5b4a"}
결과 : headers = {"X-Shop-Auth": "[MASKED:TOKEN]"}
적용 : SHOP_TOKEN 1  (diff 의 추가 줄 '+...' 로 넣어 실제 전송과 같은 조건에서 확인)

================= 정책 적용 확인 (--check) =================
입력 : LICENSE_SERIAL = "SHOP-7F3K-92QX-11ZD"
결과 : LICENSE_SERIAL = "[MASKED:LICENSE]"
적용 : LICENSE_SERIAL 1  (diff 의 추가 줄 '+...' 로 넣어 실제 전송과 같은 조건에서 확인)
```

## 6. 실제 AI 결과 비교 (`--with-api`, API 3회 호출)

같은 변경으로 실제 커밋 메시지를 생성했습니다. 마지막 열은 AI 결과에 가짜 민감정보가 그대로 등장했는지입니다.

| 조건 | 생성된 커밋 메시지 | 토큰/비용 | 결과 속 민감정보 |
|---|---|---|---|
| safe-mode OFF | feat(orders): 주문 처리 서비스 모듈 및 결제 연동 추가<br><br>- app/services/service_01~06.py: 주문 처리 단계별 서비스 모듈 6개 추가<br>- app/orders.py: create_order에 user 파라미터·금액 검증·결제 헤더 추가<br>- app/config.py: DEBUG·TIMEOUT 값 변경 및 외부 연동 설정 추가 (민감정보 하드코딩 주의) | 입력 8,824 / 출력 306 · $0.0518 | ✅ 없음 |
| ON 기본 정책 | feat(orders): 주문 처리 단계별 서비스 모듈 및 주문 생성 로직 추가<br><br>- app/services/service_01~06.py: 주문 처리 단계 1~6 서비스 모듈 추가<br>- app/orders.py: create_order에 user 파라미터·총액 계산·금액 검증 로직 추가<br>- app/config.py: DEBUG·TIMEOUT 설정 변경 및 외부 연동 설정값 추가 | 입력 5,990 / 출력 232 · $0.0357 | ✅ 없음 |
| ON 사용자 정책 | feat(orders): 결제 연동 및 주문 처리 서비스 모듈 추가<br><br>- app/orders.py: create_order에 user 파라미터·금액 검증·결제 서버 요청 헤더 추가<br>- app/services/service_01~06.py: 주문 처리 단계별 서비스 모듈 6개 신규 생성<br>- app/config.py: DEBUG/TIMEOUT 값 변경 및 외부 연동용 설정 항목 추가 | 입력 4,461 / 출력 245 · $0.0284 | ✅ 없음 |

## 원본 출력

| 파일 | 내용 |
|---|---|
| `raw/0_baseline.txt` | 고도화 전 코드 실행의 stdout+stderr 전체 |
| `raw/1_off.txt` | safe-mode OFF 실행의 stdout+stderr 전체 |
| `raw/2_on_default.txt` | ON 기본 정책 실행의 stdout+stderr 전체 |
| `raw/3_on_custom.txt` | ON 사용자 정책 실행의 stdout+stderr 전체 |
| `raw/api_1_off.txt` | safe-mode OFF 실제 API 실행 출력 |
| `raw/api_2_on_default.txt` | ON 기본 정책 실제 API 실행 출력 |
| `raw/api_3_on_custom.txt` | ON 사용자 정책 실제 API 실행 출력 |

