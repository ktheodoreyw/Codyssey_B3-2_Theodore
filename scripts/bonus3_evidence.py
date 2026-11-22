"""보너스 과제 3(안전 모드 고도화) 증빙 자료 생성 스크립트.

가짜 민감정보가 들어 있는 샘플 저장소를 임시로 만들고, '같은 변경'을 아래 조건으로 실행해
외부 API 로 '전송되는 내용'을 비교한다. 기본은 --dry-run 이라 API 를 호출하지 않는다(비용 0).

  0) 고도화 전 코드 (--baseline-ref 로 지정한 커밋, safe-mode ON)
  1) safe-mode OFF                    (--no-safe-mode)
  2) safe-mode ON  + 기본 정책          (고도화 후 코드, 설정 없음)
  3) safe-mode ON  + 사용자 정책        (--safe-mode-config docs/bonus3/ai-gitgen.sample.json)

결과물 (기본 위치: docs/bonus3/)
  EVIDENCE.md                 비교 보고서 (민감정보 노출 매트릭스, 전송량 표, 전/후 diff 요약)
  ai-gitgen.sample.json       3) 에서 쓴 사용자 정책 파일
  raw/*.txt, raw/*.diff       각 실행의 원본 출력과 전송 프롬프트 diff

사용법 (ai-gitgen 폴더에서)
  python scripts/bonus3_evidence.py                          # dry-run 비교만 (비용 0)
  python scripts/bonus3_evidence.py --baseline-ref bef4efd   # 고도화 전 코드와도 비교
  python scripts/bonus3_evidence.py --with-api               # 실제 AI 결과도 비교 (API 3회 호출, .env 의 키 사용)
"""

from __future__ import annotations

import argparse
import datetime
import difflib
import io
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

TOOL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUT = TOOL_DIR / "docs" / "bonus3"

# ---------------------------------------------------------------------------
# 샘플 저장소 (모든 값은 가짜)
# ---------------------------------------------------------------------------
SAMPLE_POLICY = {
    "safe_mode": {
        "max_files": 5,
        "max_lines": 120,
        "max_lines_per_file": 30,
        "exclude_files": ["*.sql", "db/"],
        "mask_rules": [
            {"name": "EMPLOYEE_ID", "pattern": r"\bEMP-\d{6}\b", "label": "EMPLOYEE"},
            {"name": "INTERNAL_HOST", "pattern": r"\b[\w-]+\.corp\.example\.com\b"},
            {"name": "SHOP_TOKEN", "pattern": r"\bshoptok_[0-9a-f]{12}\b", "label": "TOKEN"},
            {"name": "CODENAME", "pattern": r"(?i)\bfalcon-x\b"},
            {"name": "LICENSE_SERIAL", "pattern": 'LICENSE_SERIAL = "(?P<value>[A-Z0-9-]{12,})"', "label": "LICENSE"},
        ],
    }
}


@dataclass
class Secret:
    label: str  # 표에 쓸 이름
    value: str  # 노출 여부를 확인할 문자열
    file: str
    marker: str  # 같은 줄에 있는 '민감하지 않은' 표식 (이 줄이 전송됐는지 판단)
    caught_by: str  # 이 값을 잡는 규칙 (설명용)


SECRETS = [
    Secret("Anthropic API Key", "sk-ant-api03-FAKE0000example0000key", "app/config.py", "ANTHROPIC_API_KEY =",
           "내장 ANTHROPIC_KEY"),
    Secret("DB 비밀번호 (URL 안)", "Sh0pPassw0rd!", "app/config.py", "DATABASE_URL =", "내장 URL_CREDENTIAL"),
    Secret("관리자 이메일", "kim.admin@corp.example.com", "app/config.py", "ADMIN_EMAIL =", "내장 EMAIL"),
    Secret("고객센터 전화번호", "010-1234-5678", "app/config.py", "SUPPORT_PHONE =", "내장 KR_PHONE"),
    Secret("사내 DB 호스트", "db1.corp.example.com", "app/config.py", "DATABASE_URL =", "사용자 INTERNAL_HOST"),
    Secret("라이선스 시리얼 (값만)", "SHOP-7F3K-92QX-11ZD", "app/config.py", "LICENSE_SERIAL =",
           "사용자 LICENSE_SERIAL (?P<value>)"),
    Secret("결제 서버 호스트", "billing.corp.example.com", "app/orders.py", "BILLING_HOST =", "사용자 INTERNAL_HOST"),
    Secret("담당자 사번", "EMP-204817", "app/orders.py", "# 담당:", "사용자 EMPLOYEE_ID"),
    Secret("사내 인증 토큰 (헤더)", "shoptok_9f8e7d6c5b4a", "app/orders.py", '"X-Shop-Auth":', "사용자 SHOP_TOKEN"),
    Secret("비공개 프로젝트 코드명", "FALCON-X", "app/orders.py", "프로젝트", "사용자 CODENAME"),
    Secret("고객 이름 (SQL 시드)", "홍길동", "db/seed_customers.sql", "INSERT INTO customers", "사용자 exclude_files *.sql"),
    Secret("고객 주민등록번호 (SQL)", "900101-1234567", "db/seed_customers.sql", "INSERT INTO customers",
           "내장 KR_RRN"),
    Secret(".env DB 비밀번호", "env-only-pass-1234", ".env", "DB_PASSWORD=", "내장 민감 파일(.env*)"),
]

BASE_FILES = {
    "README.md": "# sample-shop\n\n주문/결제 데모 서비스\n",
    "app/__init__.py": "",
    "app/config.py": 'DEBUG = True\nTIMEOUT = 10\n',
    "app/orders.py": 'def create_order(items):\n    return {"items": items}\n',
}


def _service(n: int) -> str:
    lines = [f'"""서비스 {n:02d}: 주문 처리 단계 {n}"""', "", "", f"def step_{n:02d}(order):"]
    for i in range(1, 38):
        lines.append(f"    order['step_{n:02d}_{i:02d}'] = {i} * {n}  # 처리 {i}")
    lines.append("    return order")
    return "\n".join(lines) + "\n"


CHANGED_FILES = {
    "app/config.py": (
        "DEBUG = False\n"
        "TIMEOUT = 30\n"
        'ANTHROPIC_API_KEY = "sk-ant-api03-FAKE0000example0000key"\n'
        'DATABASE_URL = "postgres://shop_admin:Sh0pPassw0rd!@db1.corp.example.com:5432/shop"\n'
        'ADMIN_EMAIL = "kim.admin@corp.example.com"\n'
        'SUPPORT_PHONE = "010-1234-5678"\n'
        'LICENSE_SERIAL = "SHOP-7F3K-92QX-11ZD"\n'
    ),
    "app/orders.py": (
        'BILLING_HOST = "billing.corp.example.com"\n'
        "\n"
        "\n"
        "def create_order(items, user):\n"
        '    """주문을 만들고 결제 서버에 요청한다. (프로젝트 FALCON-X 결제 연동, 출시 전 비공개)"""\n'
        "    # 담당: EMP-204817 (결제 연동)\n"
        '    headers = {"X-Shop-Auth": "shoptok_9f8e7d6c5b4a"}\n'
        "    total = sum(item['price'] * item['qty'] for item in items)\n"
        "    if total <= 0:\n"
        '        raise ValueError("주문 금액이 0 이하입니다")\n'
        '    return {"items": items, "user": user, "total": total, "headers": headers}\n'
    ),
    "db/seed_customers.sql": (
        "INSERT INTO customers (name, rrn, phone) VALUES ('홍길동', '900101-1234567', '010-9876-5432');\n"
        "INSERT INTO customers (name, rrn, phone) VALUES ('김철수', '850505-2345678', '010-1111-2222');\n"
    ),
    ".env": "DB_PASSWORD=env-only-pass-1234\n",
}
CHANGED_FILES.update({f"app/services/service_{n:02d}.py": _service(n) for n in range(1, 7)})


# ---------------------------------------------------------------------------
# 실행 도우미
# ---------------------------------------------------------------------------
# 사용자 PC 의 전역 git 설정(전역 .gitignore 에 .env, 전역 hook 등)이 샘플 결과를 바꾸지 않도록 격리한다.
GIT_ENV: Dict[str, str] = {}
_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_CEILING_DIRECTORIES")


def make_isolated_env(tmp: Path) -> Dict[str, str]:
    empty_config = tmp / "empty-gitconfig"
    empty_config.write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    env.update({"GIT_CONFIG_GLOBAL": str(empty_config), "GIT_CONFIG_NOSYSTEM": "1", "PYTHONIOENCODING": "utf-8"})
    return env


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "core.quotepath=false", *args],
        cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
        errors="replace", timeout=120,
        env=GIT_ENV or None,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 실패: {proc.stderr.strip()}")
    return proc.stdout


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def make_sample_repo(root: Path) -> Path:
    repo = root / "sample-shop"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "bonus3")
    _git(repo, "config", "user.email", "bonus3@example.invalid")
    _git(repo, "config", "commit.gpgsign", "false")
    for rel, content in BASE_FILES.items():
        _write(repo, rel, content)
    _git(repo, "add", "-A", "-f")
    _git(repo, "commit", "-q", "--no-verify", "-m", "init")
    for rel, content in CHANGED_FILES.items():
        _write(repo, rel, content)
    _git(repo, "add", "-A", "-f")  # .env 도 반드시 스테이징 (전역 ignore 무시)
    staged = sorted(_git(repo, "diff", "--cached", "--name-only").split())
    if staged != sorted(CHANGED_FILES):
        raise RuntimeError(f"샘플 스테이징이 예상과 다릅니다: {staged}")
    return repo


@dataclass
class Run:
    key: str
    title: str
    args: List[str]
    main_py: Path
    short: str = ""  # 표 머리글용 짧은 이름
    stdout: str = ""
    stderr: str = ""
    code: int = 0
    prompt: str = ""
    stats: Dict[str, str] = field(default_factory=dict)


def run_cli(run: Run, repo: Path, expect_prompt: bool = True) -> None:
    proc = subprocess.run(
        [sys.executable, str(run.main_py), *run.args],
        cwd=repo, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
        errors="replace",
        env=GIT_ENV or None, timeout=300,
    )
    run.stdout, run.stderr, run.code = proc.stdout, proc.stderr, proc.returncode
    run.prompt = _extract_block(proc.stdout, "User Prompt")
    run.stats = _parse_stats(proc.stderr)
    # 판정이 '안전'으로 잘못 기울지 않도록(fail-closed), 프롬프트를 못 찾으면 바로 중단한다.
    if expect_prompt and (not run.prompt or "<diff>" not in run.prompt or "</diff>" not in run.prompt):
        raise RuntimeError(f"{run.title}: 전송 프롬프트(User Prompt)를 찾지 못했습니다.\n{proc.stderr}")


def _extract_block(text: str, title: str) -> str:
    m = re.search(rf"^--- {re.escape(title)} ---\n(.*?)\n-{{60}}$", text, re.S | re.M)
    return m.group(1) if m else ""


def _parse_stats(stderr: str) -> Dict[str, str]:
    stats: Dict[str, str] = {}
    m = re.search(r"전송 diff:? (\d+)줄 / 원본 (\d+)줄", stderr)
    if m:
        stats["sent_lines"], stats["original_lines"] = m.group(1), m.group(2)
    m = re.search(r"마스킹 (\d+)건", stderr)
    stats["masked"] = m.group(1) if m else "0"
    m = re.search(r"마스킹 상세: (.+)", stderr)
    stats["masked_detail"] = m.group(1).strip() if m else "-"
    for key, pattern in (("excluded", r"민감 파일 제외 (\d+)개"), ("policy_excluded", r"정책 제외 (\d+)개"),
                         ("file_limited", r"파일 수 제한으로 생략 (\d+)개")):
        m = re.search(pattern, stderr)
        stats[key] = m.group(1) if m else "0"
    m = re.search(r"safe-mode 정책: (.+)", stderr)
    stats["policy"] = m.group(1).strip() if m else ""
    return stats


def file_rows(prompt: str) -> Dict[str, str]:
    """<changed_files> 의 줄을 파일 경로 → 비고 로 정리한다."""
    rows: Dict[str, str] = {}
    for line in prompt.splitlines():
        m = re.match(r"- \[[^\]]+\] (\S+) \(\+\d+/-\d+\)(?:\s+※ (.+))?$", line)
        if m:
            rows[m.group(1)] = m.group(2) or "전체 전송"
    return rows


def sent_body_lines(prompt: str) -> Dict[str, int]:
    """<diff> 안에서 파일별로 실제 전송된 줄 수."""
    body = prompt.split("<diff>", 1)[-1].split("</diff>", 1)[0]
    counts: Dict[str, int] = {}
    current = None
    for line in body.splitlines():
        m = re.match(r"diff --git a/(\S+) b/", line)
        if m:
            current = m.group(1)
            counts[current] = 0
        if current is not None and not line.startswith("... (이하"):
            counts[current] += 1
    return counts


def diff_sections(prompt: str) -> Dict[str, str]:
    """<diff> 본문을 파일별 조각으로 나눈다."""
    body = prompt.split("<diff>", 1)[-1].split("</diff>", 1)[0]
    sections: Dict[str, List[str]] = {}
    current = None
    for line in body.splitlines():
        m = re.match(r"diff --git a/(\S+) b/", line)
        if m:
            current = m.group(1)
            sections[current] = []
        if current is not None:
            sections[current].append(line)
    return {k: "\n".join(v) for k, v in sections.items()}


def secret_status(secret: Secret, run: Run) -> str:
    """전송 프롬프트에서 민감정보 하나의 처리 결과를 판정한다. 판정할 수 없으면 예외(fail-closed)."""
    prompt = run.prompt
    if secret.value in prompt:  # 프롬프트 어디에든 값이 있으면 노출 (가장 보수적인 판정)
        return "❌ 노출"
    rows = file_rows(prompt)
    if secret.file not in rows:
        raise RuntimeError(f"판정 불가: {secret.file} 이 <changed_files> 에 없습니다 ({run.title})")
    note = rows[secret.file]
    if "제외" in note:
        return "🚫 파일 제외"
    section = diff_sections(prompt).get(secret.file, "")
    marker_lines = [line for line in section.splitlines() if secret.marker in line]
    if marker_lines and any("[MASKED:" in line for line in marker_lines):
        return "✅ 마스킹"
    if "생략" in note or "줄만 전송" in note:
        return "✂️ 전송량 제한으로 생략"
    raise RuntimeError(f"판정 불가: {secret.label} ({run.title}) - 값도 마스킹 흔적도 없습니다")


def prompt_diff(a: Run, b: Run, n: int = 0) -> str:
    lines = difflib.unified_diff(
        a.prompt.splitlines(), b.prompt.splitlines(), fromfile=a.title, tofile=b.title, n=n, lineterm=""
    )
    text = "\n".join(lines)
    return text + "\n" if text else ""


def extract_baseline(ref: str, dest: Path) -> Path:
    """git archive 로 고도화 전 코드를 임시 폴더에 꺼낸다(작업 트리/브랜치는 건드리지 않음)."""
    data = subprocess.run(
        ["git", "archive", "--format=tar", ref], cwd=TOOL_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=120,
    )
    if data.returncode != 0:
        raise RuntimeError(f"git archive {ref} 실패: {data.stderr.decode('utf-8', 'replace').strip()}")
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data.stdout)) as tar:
        tar.extractall(dest)
    return dest / "main.py"


def _privacy_guard(out: Path, tmp: Path) -> None:
    """생성된 증빙에 개인 PC 경로(홈 폴더, 임시 폴더)가 남았으면 실패시킨다."""
    needles = {str(Path.home()), str(tmp), str(Path.home()).replace("\\", "/"), str(tmp).replace("\\", "/")}
    leaked = []
    for path in out.rglob("*"):
        if path.is_file() and path.suffix in (".md", ".txt", ".diff", ".json"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if any(n and n in text for n in needles):
                leaked.append(str(path))
    if leaked:
        raise RuntimeError("증빙 파일에 개인 PC 경로가 남아 있습니다: " + ", ".join(leaked))


def _rmtree(path: Path) -> None:
    def onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    shutil.rmtree(path, onerror=onerror)


# ---------------------------------------------------------------------------
# 보고서
# ---------------------------------------------------------------------------
def build_report(runs: List[Run], policy_outputs: Dict[str, str], api_runs: List[Run], meta: Dict[str, str]) -> str:
    by_key = {r.key: r for r in runs}
    out: List[str] = []
    w = out.append
    w("# 보너스 과제 3 — 안전 모드 고도화 증빙 (자동 생성)")
    w("")
    w(f"> `scripts/bonus3_evidence.py` 가 생성한 문서입니다. 직접 수정하지 말고 스크립트를 다시 실행하세요.  ")
    w(f"> 생성: {meta['generated']} · 고도화 후 코드: `{meta['commit']}` · Python {meta['python']} · {meta['os']}")
    if meta.get("baseline"):
        w(f">  \n> 고도화 전 기준: `{meta['baseline']}` (이 커밋을 `git archive` 로 꺼낸 코드로 0번 조건을 실행)")
    w("")
    w("모든 민감정보는 **가짜 값**입니다. 같은 샘플 변경(파일 " f"{len(CHANGED_FILES)}개)을 조건만 바꿔 `commit --dry-run` 으로 실행했고, "
      "표의 판정은 **실제로 API 에 전송될 user 프롬프트**에서 각 값을 검색한 결과입니다. "
      "판정할 수 없는 경우가 하나라도 있으면 스크립트가 실패하도록 되어 있습니다(fail-closed).")
    w("")
    w("## 1. 실행 조건")
    w("")
    w("| 기호 | 조건 | 실행한 코드 | 명령 |")
    w("|---|---|---|---|")
    for r in runs:
        code = f"고도화 전 (`{meta.get('baseline', '-')}`)" if r.key == "0" else "고도화 후"
        w(f"| {r.key} | {r.title} | {code} | `python main.py {' '.join(r.args)}` |")
    w("")
    w("## 2. 민감정보 노출 매트릭스")
    w("")
    w("✅ 마스킹 = 줄은 전송되지만 값은 `[MASKED:..]` 로 치환 · 🚫 파일 제외 = 파일 내용 자체를 전송하지 않음 · "
      "✂️ = 전송량 제한으로 해당 줄이 잘림 · ❌ 노출 = 값이 그대로 전송됨")
    w("")
    w("| 민감정보 | 파일 | 잡는 규칙 | " + " | ".join(r.short or r.key for r in runs) + " |")
    w("|---|---|---|" + "---|" * len(runs))
    for s in SECRETS:
        w(f"| {s.label} | `{s.file}` | {s.caught_by} | " + " | ".join(secret_status(s, r) for r in runs) + " |")
    totals = []
    for r in runs:
        leaked = sum(1 for s in SECRETS if secret_status(s, r).startswith("❌"))
        totals.append(f"**{leaked}/{len(SECRETS)}**")
    w("| **노출 개수** | | | " + " | ".join(totals) + " |")
    w("")
    w("## 3. 전송량 비교")
    w("")
    w("| 항목 | " + " | ".join(r.short or r.key for r in runs) + " |")
    w("|---|" + "---|" * len(runs))
    rows = [
        ("원본 diff 줄 수", "original_lines"),
        ("전송 diff 줄 수", "sent_lines"),
        ("마스킹 건수", "masked"),
        ("민감 파일 제외(내장)", "excluded"),
        ("정책 제외(exclude_files)", "policy_excluded"),
        ("파일 수 제한으로 생략", "file_limited"),
    ]
    for label, key in rows:
        w(f"| {label} | " + " | ".join(r.stats.get(key, "-") for r in runs) + " |")
    w("| 전송 프롬프트 크기(문자) | " + " | ".join(f"{len(r.prompt):,}" for r in runs) + " |")
    w("| 마스킹 상세 | " + " | ".join(r.stats.get("masked_detail", "-") for r in runs) + " |")
    w("")
    w("### 파일별 전송 결과")
    w("")
    w("| 파일 | " + " | ".join(r.short or r.key for r in runs) + " |")
    w("|---|" + "---|" * len(runs))
    files = sorted(file_rows(by_key[runs[-1].key].prompt) or file_rows(runs[0].prompt))
    sent = {r.key: sent_body_lines(r.prompt) for r in runs}
    notes = {r.key: file_rows(r.prompt) for r in runs}
    for path in files:
        cells = []
        for r in runs:
            n = sent[r.key].get(path)
            note = notes[r.key].get(path, "")
            if n:
                cells.append(f"{n}줄" + (f" ({note})" if note != "전체 전송" else ""))
            else:
                cells.append(note.replace("(safe-mode)", "").strip() or "-")
        w(f"| `{path}` | " + " | ".join(cells) + " |")
    w("")

    w("## 4. 전/후 비교 (전송 프롬프트 diff)")
    w("")
    for a, b, title, why in comparison_pairs(by_key):
        diff = prompt_diff(by_key[a], by_key[b])
        changed = [l for l in diff.splitlines() if l[:1] in "+-" and not l.startswith(("+++", "---"))]
        w(f"### {a} → {b}: {title}")
        w("")
        w(f"{why}. 바뀐 줄 **{len(changed)}줄** (전체 diff: `raw/diff_{a}_vs_{b}.diff`)")
        w("")
        if not changed:
            same_system = _extract_block(by_key[a].stdout, "System Prompt") == _extract_block(by_key[b].stdout, "System Prompt")
            same_params = (_extract_block(by_key[a].stdout, "Request Parameters")
                           == _extract_block(by_key[b].stdout, "Request Parameters"))
            w("```")
            w("(차이 없음 — 두 실행의 user 프롬프트가 글자 하나까지 동일)")
            w(f"system 프롬프트 동일: {'예' if same_system else '아니오'} / 요청 파라미터 동일: {'예' if same_params else '아니오'}")
            w("```")
        else:
            w("```diff")
            tokens = [s.value for s in SECRETS] + ["MASKED", "※", "<notes>", "민감정보"]
            interesting = [l for l in changed if any(t in l for t in tokens)]
            shown = (interesting or changed)[:40]
            w("\n".join(shown))
            if len(interesting or changed) > len(shown):
                w(f"... (이하 생략, 전체는 raw/diff_{a}_vs_{b}.diff)")
            w("```")
        w("")

    w("## 5. 적용 정책 (`python main.py policy`)")
    w("")
    for title, text in policy_outputs.items():
        w(f"### {title}")
        w("")
        w("```")
        w(text.strip())
        w("```")
        w("")

    if api_runs:
        w("## 6. 실제 AI 결과 비교 (`--with-api`, API 3회 호출)")
        w("")
        w("같은 변경으로 실제 커밋 메시지를 생성했습니다. 마지막 열은 AI 결과에 가짜 민감정보가 그대로 등장했는지입니다.")
        w("")
        w("| 조건 | 생성된 커밋 메시지 | 토큰/비용 | 결과 속 민감정보 |")
        w("|---|---|---|---|")
        for r in api_runs:
            message = _extract_block(r.stdout, "Commit Message").replace("\n", "<br>") or "(실패)"
            usage = re.search(r"토큰 입력 ([\d,]+) / 출력 ([\d,]+) \| [\d.]+초(?: \| 예상 비용 (\$[\d.]+))?", r.stderr)
            usage_text = f"입력 {usage.group(1)} / 출력 {usage.group(2)} · {usage.group(3) or '-'}" if usage else "-"
            leaked = [s.label for s in SECRETS if s.value in r.stdout]
            w(f"| {r.title} | {message} | {usage_text} | {'❌ ' + ', '.join(leaked) if leaked else '✅ 없음'} |")
        w("")

    w("## 원본 출력")
    w("")
    w("| 파일 | 내용 |")
    w("|---|---|")
    for r in runs:
        w(f"| `raw/{r.key}_{_slug(r.title)}.txt` | {r.title} 실행의 stdout+stderr 전체 |")
    for r in api_runs:
        w(f"| `raw/api_{r.key}_{_slug(r.title)}.txt` | {r.title} 실제 API 실행 출력 |")
    w("")
    return "\n".join(out) + "\n"


def comparison_pairs(by_key: Dict[str, Run]) -> List[Tuple[str, str, str, str]]:
    pairs: List[Tuple[str, str, str, str]] = []
    if "0" in by_key:
        pairs += [
            ("0", "2", "고도화 전 코드 vs 고도화 후 기본 정책 (하위 호환)",
             "정책을 지정하지 않으면 고도화 전과 **완전히 같은** 내용이 전송되는지 확인"),
            ("0", "3", "고도화 전 코드 vs 고도화 후 사용자 정책 (전/후 비교)",
             "고도화로 새로 가능해진 정규식 규칙·제외 파일·전송 한도 조정이 실제 전송 내용을 어떻게 바꾸는지"),
        ]
    pairs += [
        ("1", "2", "safe-mode OFF vs ON (기본 정책)", "OFF 에서 그대로 나가던 값이 ON 에서 어떻게 바뀌는지"),
        ("1", "3", "safe-mode OFF vs ON (사용자 정책)", "OFF 와 고도화된 safe-mode 의 차이"),
    ]
    return pairs


def _slug(text: str) -> str:
    return {"고도화 전 코드": "baseline", "safe-mode OFF": "off", "ON 기본 정책": "on_default",
            "ON 사용자 정책": "on_custom"}.get(text, re.sub(r"\W+", "_", text).strip("_").lower())


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="보너스 과제 3 증빙 자료 생성")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="결과 폴더 (기본: docs/bonus3)")
    parser.add_argument("--baseline-ref", help="고도화 전 코드의 커밋/브랜치 (예: bef4efd). 지정하면 0) 조건을 추가")
    parser.add_argument("--with-api", action="store_true", help="실제 API 로 OFF/기본/사용자 정책 결과도 생성 (3회 호출)")
    parser.add_argument("--keep-sample", action="store_true", help="임시 샘플 저장소를 지우지 않고 경로를 출력")
    args = parser.parse_args()

    out = Path(args.out).resolve()
    raw = out / "raw"
    if raw.exists():  # 이전 실행의 원본 출력이 섞이지 않도록 비운다
        for old in raw.iterdir():
            if old.is_file():
                old.unlink()
    raw.mkdir(parents=True, exist_ok=True)
    policy_file = out / "ai-gitgen.sample.json"
    policy_file.write_text(json.dumps(SAMPLE_POLICY, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        rel_policy = Path(os.path.relpath(policy_file, TOOL_DIR)).as_posix()
        if rel_policy.startswith(".."):
            rel_policy = policy_file.name
    except ValueError:  # 다른 드라이브
        rel_policy = policy_file.name

    tmp = Path(tempfile.mkdtemp(prefix="bonus3_"))
    global GIT_ENV
    GIT_ENV = make_isolated_env(tmp)
    try:
        repo = make_sample_repo(tmp)
        main_py = TOOL_DIR / "main.py"
        runs: List[Run] = []
        if args.baseline_ref:
            base_main = extract_baseline(args.baseline_ref, tmp / "baseline")
            runs.append(Run("0", "고도화 전 코드", ["commit", "--dry-run"], base_main, short="0 고도화 전"))
        runs += [
            Run("1", "safe-mode OFF", ["commit", "--dry-run", "--no-safe-mode"], main_py, short="1 OFF"),
            Run("2", "ON 기본 정책", ["commit", "--dry-run"], main_py, short="2 ON 기본"),
            Run("3", "ON 사용자 정책", ["commit", "--dry-run", "--safe-mode-config", str(policy_file)], main_py,
                short="3 ON 사용자"),
        ]
        for r in runs:
            run_cli(r, repo)
            # 증빙에 개인 PC 경로가 남지 않도록 정책 파일 경로를 상대 경로로 바꾼다.
            r.stdout = r.stdout.replace(str(policy_file), rel_policy)
            r.stderr = r.stderr.replace(str(policy_file), rel_policy)
            r.args = [rel_policy if a == str(policy_file) else a for a in r.args]
            if r.code != 0:
                raise RuntimeError(f"{r.title} 실행 실패 (exit {r.code}):\n{r.stderr}")
            (raw / f"{r.key}_{_slug(r.title)}.txt").write_text(
                f"$ python main.py {' '.join(r.args)}\n\n[stderr]\n{r.stderr}\n[stdout]\n{r.stdout}", encoding="utf-8"
            )
            print(f"[{r.key}] {r.title}: 전송 {r.stats.get('sent_lines')}줄, 마스킹 {r.stats.get('masked')}건")

        by_key = {r.key: r for r in runs}
        for a, b, _title, _why in comparison_pairs(by_key):
            (raw / f"diff_{a}_vs_{b}.diff").write_text(prompt_diff(by_key[a], by_key[b], n=2) or "(차이 없음)\n",
                                                       encoding="utf-8")

        policy_outputs: Dict[str, str] = {}
        checks = [
            "담당 EMP-204817 / 프로젝트 FALCON-X",
            'headers = {"X-Shop-Auth": "shoptok_9f8e7d6c5b4a"}',
            'LICENSE_SERIAL = "SHOP-7F3K-92QX-11ZD"',
        ]
        for title, extra in (("기본 정책", []), ("사용자 정책 (ai-gitgen.sample.json)", ["--safe-mode-config", str(policy_file)])):
            pr = Run("p", title, ["policy", *extra, *sum((["--check", c] for c in checks), [])], main_py)
            run_cli(pr, repo, expect_prompt=False)
            if pr.code != 0:
                raise RuntimeError(f"policy 실행 실패 (exit {pr.code}):\n{pr.stderr}")
            text = pr.stdout.replace(str(policy_file), rel_policy)
            policy_outputs[title] = text
            (raw / f"policy_{'default' if not extra else 'custom'}.txt").write_text(text, encoding="utf-8")

        api_runs: List[Run] = []
        if args.with_api:
            for src in runs[-3:]:
                api_args = [str(policy_file) if a == rel_policy else a for a in src.args if a != "--dry-run"]
                ar = Run(src.key, src.title, api_args, main_py)
                run_cli(ar, repo, expect_prompt=False)
                ar.stdout = ar.stdout.replace(str(policy_file), rel_policy)
                ar.stderr = ar.stderr.replace(str(policy_file), rel_policy)
                ar.args = [rel_policy if a == str(policy_file) else a for a in ar.args]
                (raw / f"api_{ar.key}_{_slug(ar.title)}.txt").write_text(
                    f"$ python main.py {' '.join(ar.args)}\n\n[stderr]\n{ar.stderr}\n[stdout]\n{ar.stdout}",
                    encoding="utf-8",
                )
                print(f"[API {ar.key}] {ar.title}: exit {ar.code}")
                api_runs.append(ar)

        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=TOOL_DIR, stdout=subprocess.PIPE,
                                encoding="utf-8").stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "gitgen", "main.py"], cwd=TOOL_DIR,
                               stdout=subprocess.PIPE, encoding="utf-8").stdout.strip()
        meta = {
            "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "commit": commit + (" + 작업 중 변경" if dirty else ""),
            "python": platform.python_version(),
            "os": f"{platform.system()} {platform.release()}",
        }
        if args.baseline_ref:
            short = subprocess.run(["git", "rev-parse", "--short", args.baseline_ref], cwd=TOOL_DIR,
                                   stdout=subprocess.PIPE, encoding="utf-8").stdout.strip()
            meta["baseline"] = short or args.baseline_ref
        report = build_report(runs, policy_outputs, api_runs, meta)
        (out / "EVIDENCE.md").write_text(report, encoding="utf-8")
        _privacy_guard(out, tmp)
        print(f"\n증빙 보고서: {out / 'EVIDENCE.md'}")
        if args.keep_sample:
            print(f"샘플 저장소: {repo}")
    finally:
        if not args.keep_sample:
            _rmtree(tmp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
