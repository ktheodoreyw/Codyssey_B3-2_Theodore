# 보너스 과제 2 — 커밋/PR 템플릿 커스터마이징 증빙 (자동 생성)

> `scripts/bonus2_evidence.py` 가 생성한 문서입니다. 직접 수정하지 말고 스크립트를 다시 실행하세요.  
> 생성: 2026-11-22 20:52 · 도구 커밋: `5ce5fc2 + 작업 중 변경` · 분석 저장소: `On-Premise-Sky/B2-2` @ `3423f2f` · Python 3.14.6 · Windows 11

## 1. 컨벤션 분석 근거 (B2-2 실제 기록 집계)

### 커밋 메시지 (머지 커밋 제외 64개, 모든 브랜치)

| 항목 | 결과 | 컨벤션에 반영 |
|---|---|---|
| `type: 설명` 형식 | 63/64 (예외 1개: `Update docs/conflict-resolution.md` — GitHub 웹 편집 기본 메시지) | 형식 `{type}: {subject}` |
| 사용한 타입 | docs 40, feat 10, fix 8, chore 4, refactor 1 | CONTRIBUTING 의 5종만 허용 |
| scope 사용 | 0/64 | scope 사용 안 함 |
| 제목 길이 | 평균 32.6자, 최대 55자, 50자 초과 1개 | 50자 이내 |
| 한글 제목 / 마침표 | 한글 63개 / 마침표 0개 | 한글, 마침표 없음 |
| 제목 끝맺음 상위 | '추가' 26, '수정' 9, '명시' 4, '분리' 4, '변경' 3, '작성' 2 | 명사형으로 끝맺음 |
| 본문 | 6개만 있음, 불릿 0개, 존댓말 문장 4개 | 필요할 때만, 불릿 없는 존댓말 문장 |

### Pull Request (25개)

| 항목 | 결과 | 컨벤션에 반영 |
|---|---|---|
| 제목 `type: 설명` | 25/25 (scope 0개), 평균 32.6자·최대 47자 | 커밋과 같은 형식, 50자 이내 |
| 템플릿 섹션 순서 그대로 | 23/25 | 관련 이슈 → What → Why → How → 체크리스트 |
| `Closes #번호` | 24/25 | `--issue N` 으로 채움 |
| 체크리스트 | 24/25 | 템플릿의 3개 항목 |
| 섹션 제목 빈도 | 관련 이슈 25, What (무엇을 바꿨나요) 24, Why (왜 바꿨나요) 24, How (어떻게 확인했나요) 24, 체크리스트 24, 참고 2, 기준 시점 1, What 1 | |

### 정의한 컨벤션 (`python main.py convention --convention b2-2`)

```
====================== 커밋/PR 컨벤션 ======================
컨벤션 : B2-2 팀 컨벤션
출처   : 프리셋 b2-2 (conventions/b2-2.json)
설명   : On-Premise-Sky/B2-2 의 CONTRIBUTING, PR 템플릿, 실제 커밋 64개·PR 25개를 분석해 정의

[커밋 메시지]
  제목 형식 : {type}: {subject}
  예시      : docs: CONTRIBUTING에 리뷰 최소 기준 추가
  타입      : feat(새 기능 / 결과물 추가), fix(오류 수정), docs(문서 추가·수정), refactor(동작 변화 없는 구조 개선), chore(설정, 폴더 구조 등 기타)
  scope     : 사용 안 함
  제목 길이 : 50자 이내 (최대 72자)
  제목 작성 : 무엇을 바꿨는지 대상이 드러나게 한글로 쓰고, '~ 추가', '~ 수정', '~ 분리', '~ 명시'처럼 명사형으로 끝내며 마침표를 찍지 않는다
  금지 제목 : update, fix, temp, wip, final, bug fix, edit file
  본문      : 문장 1~3개 (필요할 때만) — 무엇을 어떻게 바꿨고 왜 바꿨는지 '~했습니다' 존댓말 평서문으로, 바꾼 파일 경로를 포함해 쓴다

[Pull Request]
  제목      : 커밋과 같은 형식, 50자 이내
  문체      : What 은 '`파일 경로` 변경 내용' 형태의 짧은 항목, Why 는 '~했습니다', '~위해서입니다' 처럼 존댓말 평서문. How 는 '~를 확인합니다' 처럼 쓰고, 실행하지 않은 결과를 '확인했습니다'로 단정하지 않는다
  본문 템플릿 (이 순서로 생성):
    ## 관련 이슈
    Closes #<이슈 번호>   ← --issue N 으로 채움
    ## What (무엇을 바꿨나요)
    - (1~5개) 변경한 파일과 주요 변경 사항. 파일 경로는 백틱(`)으로 감싼다
    ## Why (왜 바꿨나요)
    - (1~3개) 변경이 필요한 이유
    ## How (어떻게 확인했나요)
    - (1~5개) 작성자가 수행할 검증 방법과 확인할 결과(실행 명령은 백틱). 실행 후 작성자가 실제 결과로 고친다
    ## 체크리스트
    - [ ] 커밋 메시지가 컨벤션(`type: 설명`)을 따릅니다
    - [ ] 리뷰어를 지정했습니다
    - [ ] 리뷰 코멘트에 답글 또는 수정으로 응답했습니다

[브랜치 이름]
  규칙      : ^(feature|fix|docs)/[A-Za-z0-9_-]+-[a-z0-9][a-z0-9-]*$  (어긋나면 검증 결과에 [경고])
```

## 2. 컨벤션 적용 전/후 생성 결과 비교

B2-2 에 실제로 있었던 변경을 그 시점 상태로 재현해(커밋은 직전 커밋 + 해당 변경 스테이징, PR 은 머지 직전 브랜치), **같은 diff** 로 `--convention default`(적용 전)와 `--convention b2-2`(적용 후)를 실행했습니다. 채점은 팀 문서(CONTRIBUTING·PR 템플릿)에서 뽑은 기준으로, 도구의 검증 함수가 아닌 이 스크립트의 독립 검사입니다.

### 요약

| 대상 | 팀원이 실제로 쓴 것 | 적용 전 (default) | 적용 후 (b2-2) |
|---|---|---|---|
| C1 커밋 — `docs: CONTRIBUTING에 히스토리 정리 기준 추가` | 4/4 | 4/5 | **5/5** |
| C2 커밋 — `refactor: 팀원 데이터를 members.json으로 분리` | 5/5 | 4/5 | **5/5** |
| P1 PR #27 — `refactor: 팀원 데이터를 members.json으로 분리` | 6/6 | 1/6 | **5/6** |

### C1 커밋 — `docs: CONTRIBUTING에 히스토리 정리 기준 추가`

- 고른 이유: B2-2 커밋의 62%를 차지하는 docs 타입, 본문 없는 짧은 커밋
- 재현: `32638ff` 의 부모로 이동한 뒤 그 커밋의 변경을 스테이징 (브랜치 `feature/ktheodoreyw-history-cleanup`)
- 명령: 적용 전 `python main.py commit --convention default` / 적용 후 `python main.py commit --convention b2-2`

**팀원이 실제로 쓴 것**

```
docs: CONTRIBUTING에 히스토리 정리 기준 추가
```

**적용 전 (default 컨벤션)**

```
docs: 히스토리 정리 가이드라인 섹션 추가

- docs/CONTRIBUTING.md: squash, fixup, reword 등 rebase -i 사용 기준 및 백업·검증 절차 안내 추가
- docs/CONTRIBUTING.md: 기존 주의 문구에 6장 참조 링크 추가
```

**적용 후 (b2-2 컨벤션)**

```
docs: CONTRIBUTING에 히스토리 정리 기준 추가

docs/CONTRIBUTING.md에 6장 '히스토리 정리(git rebase -i)' 절을 추가하여 squash, reword, 백업, force-with-lease 사용 기준을 명시했습니다.
기존 주의 문구에 6장 참조 안내를 덧붙였습니다.
```

| 팀 컨벤션 기준 | 실제 | 적용 전 | 적용 후 |
|---|---|---|---|
| 형식 `type: 설명` | ✅ | ✅ | ✅ |
| 팀 타입 5종 중 하나 | ✅ | ✅ | ✅ |
| 제목 50자 이내 | ✅ | ✅ | ✅ |
| 한글 제목·마침표 없음 | ✅ | ✅ | ✅ |
| 본문은 불릿 없는 존댓말 문장 | – | ❌ | ✅ |

`–` 는 해당 없음입니다 (본문이 없는 메시지는 본문 기준을 채점하지 않습니다).

토큰/비용 — 적용 전: 입력 2,291 / 출력 159 · $0.0154 · 적용 후: 입력 2,250 / 출력 197 · $0.0162

### C2 커밋 — `refactor: 팀원 데이터를 members.json으로 분리`

- 고른 이유: 파일 이동·삭제가 섞인 구조 변경, 팀원이 본문(존댓말 문장)까지 쓴 커밋
- 재현: `ecb3797` 의 부모로 이동한 뒤 그 커밋의 변경을 스테이징 (브랜치 `feature/SIUUU1-members-json`)
- 명령: 적용 전 `python main.py commit --convention default` / 적용 후 `python main.py commit --convention b2-2`

**팀원이 실제로 쓴 것**

```
refactor: 팀원 데이터를 members.json으로 분리

팀원 배열을 src/data/members.json으로 옮기고, 렌더링 코드는
src/js/render.js에서 fetch로 읽도록 바꿨습니다.
src/js/members.js는 삭제했습니다.

데이터와 렌더링 코드를 분리해 팀원 정보를 고칠 때 JS를
건드리지 않게 하는 것이 목적입니다.
```

**적용 전 (default 컨벤션)**

```
refactor: 팀원 데이터를 JSON 파일로 분리하고 fetch 기반 렌더링으로 전환

- members.json: 팀원 데이터를 독립 JSON 파일로 추출
- render.js: fetch로 JSON을 읽어 카드를 렌더링하도록 members.js 대체
- index.html: 스크립트 참조를 render.js로 변경하고 주석 갱신
```

**적용 후 (b2-2 컨벤션)**

```
refactor: 팀원 데이터를 members.json으로 분리

src/js/members.js에서 데이터와 렌더링을 함께 관리하던 구조를 src/data/members.json과 src/js/render.js로 분리했습니다.
render.js는 fetch로 JSON을 읽어 카드를 그리며, file:// 환경에서의 실패 시 안내 문구를 표시하도록 했습니다.
src/index.html의 스크립트 참조와 주석을 새 파일 구조에 맞게 수정했습니다.
```

| 팀 컨벤션 기준 | 실제 | 적용 전 | 적용 후 |
|---|---|---|---|
| 형식 `type: 설명` | ✅ | ✅ | ✅ |
| 팀 타입 5종 중 하나 | ✅ | ✅ | ✅ |
| 제목 50자 이내 | ✅ | ✅ | ✅ |
| 한글 제목·마침표 없음 | ✅ | ✅ | ✅ |
| 본문은 불릿 없는 존댓말 문장 | ✅ | ❌ | ✅ |

토큰/비용 — 적용 전: 입력 4,765 / 출력 215 · $0.0292 · 적용 후: 입력 4,724 / 출력 269 · $0.0303

### P1 PR #27 — `refactor: 팀원 데이터를 members.json으로 분리`

- 고른 이유: C2 와 같은 변경의 PR, 템플릿 + 추가 섹션까지 쓴 PR
- 재현: 머지 직전 상태: 브랜치 `feature/SIUUU1-members-json` = `6a92a96`, 기준 = 머지 시점 main `1b520eb`
- 명령: 적용 전 `python main.py pr --base pr-base --convention default` / 적용 후 `python main.py pr --base pr-base --convention b2-2 --issue 26`

**팀원이 실제로 쓴 것**

```
refactor: 팀원 데이터를 members.json으로 분리

팀원 배열을 src/data/members.json으로 옮기고, 렌더링 코드는
src/js/render.js에서 fetch로 읽도록 바꿨습니다.
src/js/members.js는 삭제했습니다.

데이터와 렌더링 코드를 분리해 팀원 정보를 고칠 때 JS를
건드리지 않게 하는 것이 목적입니다.

## 관련 이슈

Closes #26

## What (무엇을 바꿨나요)

- `src/js/members.js` 삭제
- `src/data/members.json` 생성 — 팀원 4명 데이터
- `src/js/render.js` 생성 — 기존 렌더링 로직을 그대로 옮기고 fetch로 데이터 읽기
- `src/index.html` — 스크립트 참조와 주석 수정

## Why (왜 바꿨나요)

- 데이터와 렌더링 코드를 분리해, 팀원 정보를 고칠 때 JS를 건드리지 않게 했습니다.
- 계획표 5번(members.js 수정 PR)과 함께 "파일 삭제 vs 내용 수정" 충돌을
  만드는 실습이기도 합니다. 이 PR이 먼저 머지되어야 합니다.

## How (어떻게 확인했나요)

- Live Server로 `src/index.html`을 열어 팀원 카드 4개와 링크 동작을 확인했습니다.
- 개인 사이트 링크 4개가 각 `../team/<id>/`로 이동하는 것을 확인했습니다.
- members.json의 역할·소개 8개 항목이 루트 README와 team/README.md와
  일치하는지 대조했습니다.
- `members.js`를 가리키는 참조가 남아 있지 않은지 확인했습니다.

## 바뀐 동작 하나

`fetch`는 `file://`로 열면 브라우저가 차단합니다.
이전에는 데이터가 JS에 박혀 있어 파일을 직접 열어도 카드가 보였지만,
이제는 로컬 서버가 필요합니다.

차단되는 경우 "Live Server 등 로컬 서버로 열어 주세요" 안내가 표시되도록
처리했습니다. 루트 README의 결과물 섹션에도 이미 Live Server 실행이
안내되어 있어 큰 문제는 없다고 판단했지만, 받아들이기 어려우면
데이터를 JS 모듈(`export const members`)로 두는 방식도 가능합니다.
의견 주세요.

## 체크리스트

- [x] 커밋 메시지가 컨벤션(`type: 설명`)을 따릅니다
- [x] 리뷰어를 지정했습니다
- [x] 리뷰 코멘트에 답글 또는 수정으로 응답했습니다
```

**적용 전 (default 컨벤션)**

```
refactor: 팀원 데이터를 members.json으로 분리하고 fetch 기반 렌더링으로 전환

## Why
- 팀원 데이터와 렌더링 로직이 members.js 한 파일에 혼재되어 관심사 분리가 필요
- 데이터를 JSON으로 관리하면 JS 코드 수정 없이 팀원 정보를 변경할 수 있음

## What
- src/data/members.json 추가: 팀원 4명의 id, name, role, intro, hasSite 데이터를 JSON으로 관리
- src/js/members.js 삭제: 하드코딩된 데이터 배열과 렌더링 로직 제거
- src/js/render.js 추가: fetch로 members.json을 읽어 카드를 렌더링하며, 실패 시 안내 문구 표시 로직 포함
- src/index.html 수정: script 참조를 members.js에서 render.js로 변경하고 주석 갱신

## How to Test
- 로컬 서버(예: `npx live-server src`)로 index.html을 열어 팀원 카드 4장이 정상 렌더링되는지 확인
- 브라우저 DevTools Network 탭에서 data/members.json 요청이 200으로 응답하는지 확인
- members.json의 hasSite를 false로 변경 후 해당 카드에 '사이트 준비 중' 문구가 표시되는지 확인
- file:// 프로토콜로 직접 열었을 때 로컬 서버 안내 문구가 노출되는지 확인
```

**적용 후 (b2-2 컨벤션)**

```
refactor: 팀원 데이터를 members.json으로 분리하고 fetch 기반 렌더링 적용

## 관련 이슈

Closes #26

## What (무엇을 바꿨나요)

- `src/data/members.json` 팀원 4명의 데이터를 담은 JSON 파일 추가
- `src/js/members.js` 삭제 — 데이터 배열과 렌더링 로직이 합쳐져 있던 파일 제거
- `src/js/render.js` 추가 — fetch로 members.json을 읽어 카드를 그리는 렌더링 전용 스크립트
- `src/index.html` 스크립트 참조를 members.js에서 render.js로 변경하고 주석 갱신

## Why (왜 바꿨나요)

- 팀원 데이터와 렌더링 로직의 관심사를 분리하여 데이터 수정 시 JS 파일을 건드리지 않도록 하기 위해서입니다
- JSON 파일로 분리하면 데이터 형식이 명확해지고, 다른 도구나 스크립트에서도 재사용할 수 있기 위해서입니다

## How (어떻게 확인했나요)

- 로컬 서버(Live Server 등)로 `src/index.html`을 열어 팀원 카드 4장이 정상 렌더링되는지 확인합니다
- 브라우저 개발자 도구 Network 탭에서 `data/members.json` 요청이 200으로 응답하는지 확인합니다
- `members.json`을 임시로 삭제하거나 경로를 틀리게 바꾼 뒤, 목록 자리에 에러 안내 문구가 표시되는지 확인합니다

## 체크리스트

- [ ] 커밋 메시지가 컨벤션(`type: 설명`)을 따릅니다
- [ ] 리뷰어를 지정했습니다
- [ ] 리뷰 코멘트에 답글 또는 수정으로 응답했습니다
```

| 팀 컨벤션 기준 | 실제 | 적용 전 | 적용 후 |
|---|---|---|---|
| 제목 형식 `type: 설명` | ✅ | ✅ | ✅ |
| 제목 50자 이내 | ✅ | ❌ | ❌ |
| 템플릿 섹션 5개를 순서대로 | ✅ | ❌ | ✅ |
| 관련 이슈 `Closes #번호` | ✅ | ❌ | ✅ |
| 체크리스트 3개 항목 | ✅ | ❌ | ✅ |
| Why 를 존댓말 문장으로 | ✅ | ❌ | ✅ |

토큰/비용 — 적용 전: 입력 4,928 / 출력 585 · $0.0393 · 적용 후: 입력 4,889 / 출력 569 · $0.0387

## 원본 출력

`raw/` 폴더에 각 실행의 stdout+stderr 전체와 `convention_b2-2.txt` 가 있습니다.

