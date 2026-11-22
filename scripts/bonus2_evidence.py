"""보너스 과제 2(커밋/PR 템플릿 커스터마이징) 증빙 자료 생성 스크립트.

이전 미션 저장소(On-Premise-Sky/B2-2)를 임시 폴더에 clone 해서

  1) 컨벤션 분석   : 실제 커밋·PR 을 집계해 팀 컨벤션의 근거(통계)를 다시 계산한다
  2) 전/후 비교    : 실제로 있었던 커밋 2개와 PR 1개를 그 시점 상태로 재현하고,
                     같은 변경으로 '컨벤션 적용 전(--convention default)' 과
                     '적용 후(--convention b2-2)' 를 실제 API 로 생성해
                     팀원이 실제로 쓴 메시지와 나란히 비교한다
  3) 준수 채점     : 팀 문서(CONTRIBUTING, PR 템플릿)에서 뽑은 기준으로 세 결과를 똑같이 채점한다
                     (도구 자체의 검증 함수가 아니라 이 스크립트의 독립적인 검사로 채점)

결과물 (기본 위치: docs/bonus2/)
  EVIDENCE.md     비교 보고서
  raw/*.txt       각 실행의 원본 출력, 팀 컨벤션 출력

사용법 (ai-gitgen 폴더에서)
  python scripts/bonus2_evidence.py              # 실제 API 6회 호출 (.env 의 API Key 사용)
  python scripts/bonus2_evidence.py --skip-api   # API 없이 분석 + dry-run 만 (점검용, 결과는 임시 폴더에)
  python scripts/bonus2_evidence.py --reuse-raw  # API 재호출 없이 기존 raw/ 결과로 보고서만 다시 생성

결과는 임시 폴더에서 모두 만든 뒤, 끝까지 성공했을 때만 기존 결과와 바꾼다(중간에 실패해도 기존 증빙은 남는다).
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

TOOL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUT = TOOL_DIR / "docs" / "bonus2"
REPO_URL = "https://github.com/On-Premise-Sky/B2-2.git"
API_BASE = "https://api.github.com/repos/On-Premise-Sky/B2-2"

TEAM_TYPES = ("feat", "fix", "docs", "refactor", "chore")
TEAM_HEADINGS = ["관련 이슈", "What (무엇을 바꿨나요)", "Why (왜 바꿨나요)", "How (어떻게 확인했나요)", "체크리스트"]


@dataclass
class CommitCase:
    key: str
    subject: str  # B2-2 히스토리에서 찾을 실제 커밋 제목
    branch: str  # 그 커밋이 만들어진 작업 브랜치(팀 규칙 형식)
    why: str


@dataclass
class PRCase:
    key: str
    number: int
    branch: str
    issue: int
    why: str


COMMIT_CASES = [
    CommitCase("C1", "docs: CONTRIBUTING에 히스토리 정리 기준 추가", "feature/ktheodoreyw-history-cleanup",
               "B2-2 커밋의 62%를 차지하는 docs 타입, 본문 없는 짧은 커밋"),
    CommitCase("C2", "refactor: 팀원 데이터를 members.json으로 분리", "feature/SIUUU1-members-json",
               "파일 이동·삭제가 섞인 구조 변경, 팀원이 본문(존댓말 문장)까지 쓴 커밋"),
]
PR_CASES = [
    PRCase("P1", 27, "feature/SIUUU1-members-json", 26, "C2 와 같은 변경의 PR, 템플릿 + 추가 섹션까지 쓴 PR"),
]

# ---------------------------------------------------------------------------
# 실행 도우미 (전역 git 설정과 격리)
# ---------------------------------------------------------------------------
ENV: Dict[str, str] = {}


def make_env(tmp: Path) -> Dict[str, str]:
    cfg = tmp / "empty-gitconfig"
    cfg.write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")}
    env.update({"GIT_CONFIG_GLOBAL": str(cfg), "GIT_CONFIG_NOSYSTEM": "1", "PYTHONIOENCODING": "utf-8",
                # 임시 폴더 위로 저장소를 찾아 올라가지 않게 (복사본이 저장소가 아닐 때 상위 저장소를 건드리지 않도록)
                "GIT_CEILING_DIRECTORIES": str(tmp)})
    return env


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.quotepath=false", *args], cwd=repo,
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          encoding="utf-8", errors="replace", timeout=300, env=ENV or None)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 실패: {proc.stderr.strip()}")
    return proc.stdout


@dataclass
class Run:
    title: str
    args: List[str]
    stdout: str = ""
    stderr: str = ""
    code: int = 0
    texts: Dict[str, str] = field(default_factory=dict)


def _parse_texts(run: Run) -> Run:
    for name in ("Commit Message", "PR Title", "PR Body"):
        m = re.search(rf"^--- {re.escape(name)} ---\n(.*?)\n-{{60}}$", run.stdout, re.S | re.M)
        if m:
            run.texts[name] = m.group(1)
    return run


def run_tool(repo: Path, args: List[str]) -> Run:
    run = Run(title=" ".join(args), args=args)
    proc = subprocess.run([sys.executable, str(TOOL_DIR / "main.py"), *args], cwd=repo,
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          encoding="utf-8", errors="replace", timeout=300, env=ENV or None)
    run.stdout, run.stderr, run.code = proc.stdout, proc.stderr, proc.returncode
    _parse_texts(run)
    if proc.returncode != 0:
        raise RuntimeError(f"실행 실패 ({' '.join(args)}): exit {proc.returncode}\n{proc.stderr}")
    return run


def raw_text(run: Run) -> str:
    return f"$ python main.py {run.title}\n\n[stderr]\n{run.stderr}\n[stdout]\n{run.stdout}"


def load_raw(path: Path, args: List[str]) -> Run:
    """--reuse-raw: 이전 실행의 raw 출력을 다시 읽어 API 를 호출하지 않고 보고서만 다시 만든다."""
    if not path.is_file():
        raise RuntimeError(f"{path.name} 이 없습니다. --reuse-raw 없이 한 번 실행하세요.")
    head, _, rest = path.read_text(encoding="utf-8").partition("\n\n[stderr]\n")
    expected = f"$ python main.py {' '.join(args)}"
    if head != expected:
        raise RuntimeError(f"{path.name} 의 명령이 다릅니다: {head!r} != {expected!r}")
    stderr, _, stdout = rest.partition("\n[stdout]\n")
    return _parse_texts(Run(title=" ".join(args), args=args, stdout=stdout, stderr=stderr))


def fetch_json(url: str) -> Optional[object]:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "ai-gitgen-evidence"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # 오프라인 등: 분석/정답 비교만 생략
        print(f"[경고] GitHub API 요청 실패 ({url}): {type(exc).__name__}")
        return None


def _rmtree(path: Path) -> None:
    def onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    shutil.rmtree(path, onerror=onerror)


# ---------------------------------------------------------------------------
# 1) 컨벤션 분석 (통계)
# ---------------------------------------------------------------------------
_CONV_RE = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]+)\))?!?: (?P<desc>.+)$")
_TRAILER_RE = re.compile(r"^(?:Co-authored-by|Signed-off-by|Reviewed-by|Acked-by|Helped-by|Reported-by|Tested-by):\s",
                         re.IGNORECASE)
_WEB_EDIT_RE = re.compile(r"^(?:Update|Create|Delete|Rename|Add files via upload)\b")
_ISSUE_RE = re.compile(r"Closes #\d+")  # 통계와 채점이 같은 기준을 쓰도록 공유


def strip_trailers(body: str) -> str:
    """Co-authored-by 같은 git trailer 는 사람이 쓴 본문이 아니므로 빼고 본다."""
    return "\n".join(line for line in body.splitlines() if not _TRAILER_RE.match(line.strip())).strip()


def analyze_commits(repo: Path) -> Dict[str, object]:
    raw = git(repo, "log", "--all", "--no-merges", "--format=%s%x1f%b%x1e")
    rows = []
    for item in raw.split("\x1e"):
        item = item.strip("\n")
        if not item.strip():
            continue
        subject, _, body = item.partition("\x1f")
        rows.append((subject.strip(), body.strip()))
    types = collections.Counter()
    scoped = period = hangul = with_body = bullet_body = honorific_body = 0
    nonconv: List[str] = []
    endings = collections.Counter()
    lengths = []
    for subject, body in rows:
        lengths.append(len(subject))
        m = _CONV_RE.match(subject)
        if m:
            types[m["type"]] += 1
            scoped += bool(m["scope"])
            endings[re.sub(r"\s*\(.*\)$", "", m["desc"]).strip()[-2:]] += 1
        else:
            nonconv.append(subject)
        period += subject.endswith(".")
        hangul += bool(re.search(r"[가-힣]", subject))
        body = strip_trailers(body)
        if body:
            with_body += 1
            bullet_body += any(line.strip().startswith(("-", "*")) for line in body.splitlines())
            honorific_body += "니다" in body
    return {
        "total": len(rows), "types": types.most_common(), "scoped": scoped, "nonconv": nonconv,
        "period": period, "hangul": hangul, "avg_len": sum(lengths) / max(1, len(lengths)),
        "max_len": max(lengths or [0]), "over_50": sum(1 for n in lengths if n > 50),
        "with_body": with_body, "bullet_body": bullet_body, "honorific_body": honorific_body,
        "endings": endings.most_common(6),
    }


def analyze_prs(prs: List[dict]) -> Dict[str, object]:
    headings = collections.Counter()
    closes = checklist = template_order = 0
    for p in prs:
        body = p.get("body") or ""
        hs = [h.strip() for h in re.findall(r"^#{1,6}\s+(.+?)\s*$", body, re.M)]
        headings.update(hs)
        closes += bool(_ISSUE_RE.search(body))
        checklist += bool(re.search(r"- \[[ xX]\]", body))
        template_order += [h for h in hs if h in TEAM_HEADINGS] == TEAM_HEADINGS
    titles = [p["title"] for p in prs]
    return {
        "total": len(prs), "conv_titles": sum(1 for t in titles if _CONV_RE.match(t)),
        "scoped_titles": sum(1 for t in titles if (_CONV_RE.match(t) or {}) and _CONV_RE.match(t)["scope"]),
        "avg_len": sum(len(t) for t in titles) / max(1, len(titles)), "max_len": max((len(t) for t in titles), default=0),
        "headings": headings.most_common(8), "closes": closes, "checklist": checklist, "template_order": template_order,
    }


# ---------------------------------------------------------------------------
# 3) 팀 컨벤션 준수 채점 (CONTRIBUTING / PR 템플릿 기준, 도구와 독립)
# ---------------------------------------------------------------------------
def score_commit(text: Optional[str]) -> List[Tuple[str, bool]]:
    if text is None:  # 생성되지 않은 결과(dry-run 등)는 채점하지 않는다
        return []
    lines = text.strip().splitlines()
    title = lines[0] if lines else ""
    body = [line for line in strip_trailers("\n".join(lines[1:])).splitlines() if line.strip()]
    m = _CONV_RE.match(title)
    rules = [
        ("형식 `type: 설명`", bool(m) and not m["scope"]),
        ("팀 타입 5종 중 하나", bool(m) and m["type"] in TEAM_TYPES),
        ("제목 50자 이내", len(title) <= 50),
        ("한글 제목·마침표 없음", bool(re.search(r"[가-힣]", title)) and not title.endswith(".")),
    ]
    if body:
        rules.append(("본문은 불릿 없는 존댓말 문장", not any(b.lstrip().startswith(("-", "*")) for b in body)
                      and any("니다" in b for b in body)))
    return rules


def score_pr(title: Optional[str], body: str) -> List[Tuple[str, bool]]:
    if title is None:
        return []
    m = _CONV_RE.match(title)
    headings = [h.strip() for h in re.findall(r"^#{1,6}\s+(.+?)\s*$", body, re.M)]
    template = [h for h in headings if h in TEAM_HEADINGS]

    def section(name: str) -> str:
        part = body.split(f"## {name}", 1)
        return part[1].split("\n## ", 1)[0] if len(part) > 1 else ""

    return [
        ("제목 형식 `type: 설명`", bool(m) and not m["scope"] and m["type"] in TEAM_TYPES),
        ("제목 50자 이내", len(title) <= 50),
        ("템플릿 섹션 5개를 순서대로", template == TEAM_HEADINGS),
        ("관련 이슈 `Closes #번호`", bool(_ISSUE_RE.search(body))),
        ("체크리스트 3개 항목", len(re.findall(r"^- \[[ xX]\] ", body, re.M)) == 3),
        ("Why 를 존댓말 문장으로", "니다" in section("Why (왜 바꿨나요)")),
    ]


def score_text(rules: List[Tuple[str, bool]]) -> str:
    return f"{sum(ok for _, ok in rules)}/{len(rules)}" if rules else "–"


# ---------------------------------------------------------------------------
# 보고서
# ---------------------------------------------------------------------------
def code_block(text: str) -> List[str]:
    return ["```", text.strip() or "(없음)", "```"]


def build_report(meta: Dict[str, str], ca: Dict[str, object], pa: Optional[Dict[str, object]],
                 commit_results: List[dict], pr_results: List[dict], convention_text: str) -> str:
    out: List[str] = []
    w = out.append
    w("# 보너스 과제 2 — 커밋/PR 템플릿 커스터마이징 증빙 (자동 생성)")
    w("")
    w("> `scripts/bonus2_evidence.py` 가 생성한 문서입니다. 직접 수정하지 말고 스크립트를 다시 실행하세요.  ")
    w(f"> 생성: {meta['generated']} · 도구 커밋: `{meta['commit']}` · 분석 저장소: `On-Premise-Sky/B2-2` @ `{meta['b2_head']}` · "
      f"Python {meta['python']} · {meta['os']}")
    w("")
    w("## 1. 컨벤션 분석 근거 (B2-2 실제 기록 집계)")
    w("")
    w(f"### 커밋 메시지 (머지 커밋 제외 {ca['total']}개, 모든 브랜치)")
    w("")
    w("| 항목 | 결과 | 컨벤션에 반영 |")
    w("|---|---|---|")
    types = ", ".join(f"{t} {n}" for t, n in ca["types"])
    nonconv = ca["nonconv"]
    if not nonconv:
        exceptions = "예외 없음"
    else:
        listed = ", ".join(f"`{s}`" for s in nonconv[:3]) + (f" 외 {len(nonconv) - 3}개" if len(nonconv) > 3 else "")
        web = all(_WEB_EDIT_RE.match(s) for s in nonconv)
        exceptions = f"예외 {len(nonconv)}개: {listed}" + (" — GitHub 웹 편집 기본 메시지" if web else "")
    w(f"| `type: 설명` 형식 | {ca['total'] - len(nonconv)}/{ca['total']} ({exceptions}) | 형식 `{{type}}: {{subject}}` |")
    w(f"| 사용한 타입 | {types} | CONTRIBUTING 의 5종만 허용 |")
    w(f"| scope 사용 | {ca['scoped']}/{ca['total']} | scope 사용 안 함 |")
    w(f"| 제목 길이 | 평균 {ca['avg_len']:.1f}자, 최대 {ca['max_len']}자, 50자 초과 {ca['over_50']}개 | 50자 이내 |")
    w(f"| 한글 제목 / 마침표 | 한글 {ca['hangul']}개 / 마침표 {ca['period']}개 | 한글, 마침표 없음 |")
    endings = ", ".join(f"'{e}' {n}" for e, n in ca["endings"])
    w(f"| 제목 끝맺음 상위 | {endings} | 명사형으로 끝맺음 |")
    w(f"| 본문 | {ca['with_body']}개만 있음, 불릿 {ca['bullet_body']}개, 존댓말 문장 {ca['honorific_body']}개 | 필요할 때만, 불릿 없는 존댓말 문장 |")
    w("")
    if pa:
        w(f"### Pull Request ({pa['total']}개)")
        w("")
        w("| 항목 | 결과 | 컨벤션에 반영 |")
        w("|---|---|---|")
        w(f"| 제목 `type: 설명` | {pa['conv_titles']}/{pa['total']} (scope {pa['scoped_titles']}개), 평균 {pa['avg_len']:.1f}자·최대 {pa['max_len']}자 | 커밋과 같은 형식, 50자 이내 |")
        w(f"| 템플릿 섹션 순서 그대로 | {pa['template_order']}/{pa['total']} | 관련 이슈 → What → Why → How → 체크리스트 |")
        w(f"| `Closes #번호` | {pa['closes']}/{pa['total']} | `--issue N` 으로 채움 |")
        w(f"| 체크리스트 | {pa['checklist']}/{pa['total']} | 템플릿의 3개 항목 |")
        heads = ", ".join(f"{h} {n}" for h, n in pa["headings"])
        w(f"| 섹션 제목 빈도 | {heads} | |")
        w("")
    w("### 정의한 컨벤션 (`python main.py convention --convention b2-2`)")
    w("")
    out.extend(code_block(convention_text))
    w("")
    w("## 2. 컨벤션 적용 전/후 생성 결과 비교")
    w("")
    w("B2-2 에 실제로 있었던 변경을 그 시점 상태로 재현해(커밋은 직전 커밋 + 해당 변경 스테이징, PR 은 머지 직전 브랜치), "
      "**같은 diff** 로 `--convention default`(적용 전)와 `--convention b2-2`(적용 후)를 실행했습니다. "
      "채점은 팀 문서(CONTRIBUTING·PR 템플릿)에서 뽑은 기준으로, 도구의 검증 함수가 아닌 이 스크립트의 독립 검사입니다.")
    w("")
    w("### 요약")
    w("")
    w("| 대상 | 팀원이 실제로 쓴 것 | 적용 전 (default) | 적용 후 (b2-2) |")
    w("|---|---|---|---|")
    for r in commit_results + pr_results:
        w(f"| {r['label']} | {r['score_actual']} | {r['score_before']} | **{r['score_after']}** |")
    w("")
    for r in commit_results + pr_results:
        w(f"### {r['label']}")
        w("")
        w(f"- 고른 이유: {r['why']}")
        w(f"- 재현: {r['setup']}")
        w(f"- 명령: 적용 전 `python main.py {r['before_cmd']}` / 적용 후 `python main.py {r['after_cmd']}`")
        w("")
        for title, key in (("팀원이 실제로 쓴 것", "actual"), ("적용 전 (default 컨벤션)", "before"), ("적용 후 (b2-2 컨벤션)", "after")):
            w(f"**{title}**")
            w("")
            out.extend(code_block(r[key]))
            w("")
        w("| 팀 컨벤션 기준 | 실제 | 적용 전 | 적용 후 |")
        w("|---|---|---|---|")
        results = [dict(r[k]) for k in ("rules_actual", "rules_before", "rules_after")]
        names: List[str] = []
        for k in ("rules_actual", "rules_before", "rules_after"):
            names += [name for name, _ in r[k] if name not in names]
        for name in names:
            marks = ["–" if name not in res else "✅" if res[name] else "❌" for res in results]
            w(f"| {name} | {' | '.join(marks)} |")
        if any(name not in res for res in results for name in names):
            w("")
            w("`–` 는 해당 없음입니다 (본문이 없는 메시지는 본문 기준을 채점하지 않습니다).")
        w("")
        if r.get("tokens"):
            w(f"토큰/비용 — {r['tokens']}")
            w("")
    w("## 원본 출력")
    w("")
    w("`raw/` 폴더에 각 실행의 stdout+stderr 전체와 `convention_b2-2.txt` 가 있습니다.")
    w("")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="보너스 과제 2 증빙 자료 생성")
    parser.add_argument("--out", help=f"결과 폴더 (기본: {DEFAULT_OUT.relative_to(TOOL_DIR).as_posix()}, "
                                      "--skip-api 일 때는 임시 폴더)")
    parser.add_argument("--repo", help="이미 clone 한 B2-2 경로 (지정하지 않으면 임시 폴더에 clone)")
    parser.add_argument("--skip-api", action="store_true", help="API 를 호출하지 않고 dry-run 으로만 실행 (점검용)")
    parser.add_argument("--reuse-raw", action="store_true",
                        help="API 를 다시 호출하지 않고 기존 raw/*.txt 의 생성 결과로 보고서만 다시 만든다")
    args = parser.parse_args()
    _setup_stdio()
    if args.skip_api and args.reuse_raw:
        parser.error("--skip-api 와 --reuse-raw 는 함께 쓸 수 없습니다.")

    # --skip-api 는 점검용이므로 기본적으로 실제 증빙(docs/bonus2)을 덮어쓰지 않는다
    if args.out:
        out = Path(args.out).resolve()
    elif args.skip_api:
        out = Path(tempfile.mkdtemp(prefix="bonus2_skipapi_"))
    else:
        out = DEFAULT_OUT
    raw = out / "raw"

    tmp = Path(tempfile.mkdtemp(prefix="bonus2_"))
    staging = tmp / "staging"  # 끝까지 성공했을 때만 out 으로 옮긴다 (실패해도 기존 증빙이 남도록)
    (staging / "raw").mkdir(parents=True)
    global ENV
    ENV = make_env(tmp)
    try:
        repo = tmp / "B2-2"
        if args.repo:
            _copy_standalone_repo(Path(args.repo).resolve(), repo)
        else:
            subprocess.run(["git", "clone", "-q", REPO_URL, str(repo)], check=True, env=ENV, timeout=300,
                           stdin=subprocess.DEVNULL)
        _assert_isolated(repo)  # 아래의 config / reset --hard / checkout 이 다른 저장소를 건드리지 않게
        git(repo, "config", "user.name", "bonus2")
        git(repo, "config", "user.email", "bonus2@example.invalid")
        b2_head = git(repo, "rev-parse", "--short", "origin/main").strip()

        # 1) 분석
        commit_stats = analyze_commits(repo)
        prs = fetch_json(f"{API_BASE}/pulls?state=all&per_page=100")
        if not isinstance(prs, list):
            if not args.skip_api:
                raise RuntimeError("GitHub 에서 PR 목록을 불러오지 못했습니다. 네트워크를 확인하고 다시 실행하세요.")
            print("[경고] PR 목록 없이 계속합니다 (--skip-api 점검용).")
        pr_stats = analyze_prs(prs) if isinstance(prs, list) else None
        pr_by_number = {p["number"]: p for p in prs} if isinstance(prs, list) else {}
        convention_text = run_tool(repo, ["convention", "--convention", "b2-2"]).stdout.strip()
        (staging / "raw" / "convention_b2-2.txt").write_text(convention_text + "\n", encoding="utf-8")

        mode = ["--dry-run"] if args.skip_api else []

        def generate(key: str, tag: str, tool_args: List[str]) -> Run:
            name = f"{key}_{tag}.txt"
            run = load_raw(raw / name, tool_args) if args.reuse_raw else run_tool(repo, tool_args)
            (staging / "raw" / name).write_text(raw_text(run), encoding="utf-8")
            return run

        def generated(run: Run, name: str) -> Optional[str]:
            """생성 결과. dry-run 이면 None (채점하지 않음). 실제 실행인데 없으면 오류."""
            if args.skip_api:
                return None
            if name not in run.texts:
                raise RuntimeError(f"'{run.title}' 출력에서 '--- {name} ---' 구획을 찾지 못했습니다.\n{run.stdout[-500:]}")
            return run.texts[name]

        log_index = {}
        for line in git(repo, "log", "--all", "--no-merges", "--format=%H%x1f%s").splitlines():
            sha, _, subject = line.partition("\x1f")
            log_index.setdefault(subject, sha)

        def reset_tree() -> None:
            git(repo, "reset", "-q", "--hard")
            git(repo, "clean", "-fdq")

        def result(label: str, why: str, setup: str, before: Run, after: Run,
                   actual: Optional[str], texts: Tuple[Optional[str], Optional[str]], rules: tuple) -> dict:
            return {
                "label": label, "why": why, "setup": setup,
                "before_cmd": " ".join(before.args), "after_cmd": " ".join(after.args),
                "actual": actual if actual is not None else "(불러오지 못함)",
                "before": texts[0] if texts[0] is not None else "(dry-run: 생성 안 함)",
                "after": texts[1] if texts[1] is not None else "(dry-run: 생성 안 함)",
                "rules_actual": rules[0], "rules_before": rules[1], "rules_after": rules[2],
                "score_actual": score_text(rules[0]), "score_before": score_text(rules[1]),
                "score_after": score_text(rules[2]), "tokens": _tokens(before, after),
            }

        commit_results = []
        for case in COMMIT_CASES:
            sha = log_index.get(case.subject)
            if not sha:
                raise RuntimeError(f"B2-2 에서 커밋을 찾지 못했습니다: {case.subject}")
            reset_tree()
            git(repo, "checkout", "-q", "-f", "-B", case.branch, sha)
            git(repo, "reset", "-q", "--soft", "HEAD~1")  # 그 커밋의 변경을 스테이징된 상태로 되돌림
            actual = git(repo, "log", "-1", "--format=%B", sha).strip()
            before = generate(case.key, "before", ["commit", "--convention", "default", *mode])
            after = generate(case.key, "after", ["commit", "--convention", "b2-2", *mode])
            b_text, a_text = generated(before, "Commit Message"), generated(after, "Commit Message")
            rules = (score_commit(actual), score_commit(b_text), score_commit(a_text))
            commit_results.append(result(
                f"{case.key} 커밋 — `{case.subject}`", case.why,
                f"`{sha[:7]}` 의 부모로 이동한 뒤 그 커밋의 변경을 스테이징 (브랜치 `{case.branch}`)",
                before, after, actual, (b_text, a_text), rules))
            print(f"[{case.key}] 적용 전 {score_text(rules[1])} → 적용 후 {score_text(rules[2])} (실제 {score_text(rules[0])})")

        pr_results = []
        for case in PR_CASES:
            merge = git(repo, "log", "--all", "--merges", "--format=%H", f"--grep=Merge pull request #{case.number} ").split()
            if not merge:
                raise RuntimeError(f"PR #{case.number} 의 머지 커밋을 찾지 못했습니다.")
            base_sha = git(repo, "rev-parse", f"{merge[0]}^1").strip()
            head_sha = git(repo, "rev-parse", f"{merge[0]}^2").strip()
            reset_tree()
            git(repo, "branch", "-f", "pr-base", base_sha)
            git(repo, "checkout", "-q", "-f", "-B", case.branch, head_sha)
            p = pr_by_number.get(case.number) or fetch_json(f"{API_BASE}/pulls/{case.number}")
            if not isinstance(p, dict) and not args.skip_api:
                raise RuntimeError(f"GitHub 에서 PR #{case.number} 을 불러오지 못했습니다.")
            actual_title = p.get("title") if isinstance(p, dict) else None
            actual_body = ((p or {}).get("body") or "").replace("\r\n", "\n").strip()
            before = generate(case.key, "before", ["pr", "--base", "pr-base", "--convention", "default", *mode])
            after = generate(case.key, "after", ["pr", "--base", "pr-base", "--convention", "b2-2",
                                                 "--issue", str(case.issue), *mode])
            bt, at = generated(before, "PR Title"), generated(after, "PR Title")
            bb = generated(before, "PR Body") or ""
            ab = generated(after, "PR Body") or ""
            rules = (score_pr(actual_title, actual_body), score_pr(bt, bb), score_pr(at, ab))
            pr_results.append(result(
                f"{case.key} PR #{case.number} — `{actual_title or '(불러오지 못함)'}`", case.why,
                f"머지 직전 상태: 브랜치 `{case.branch}` = `{head_sha[:7]}`, 기준 = 머지 시점 main `{base_sha[:7]}`",
                before, after, f"{actual_title}\n\n{actual_body}" if actual_title is not None else None,
                (f"{bt}\n\n{bb}" if bt is not None else None, f"{at}\n\n{ab}" if at is not None else None), rules))
            print(f"[{case.key}] 적용 전 {score_text(rules[1])} → 적용 후 {score_text(rules[2])} (실제 {score_text(rules[0])})")

        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=TOOL_DIR, stdout=subprocess.PIPE,
                                encoding="utf-8").stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "gitgen", "main.py", "conventions"], cwd=TOOL_DIR,
                               stdout=subprocess.PIPE, encoding="utf-8").stdout.strip()
        meta = {
            "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                         + (" (생성 결과는 이전 실행의 raw/ 재사용)" if args.reuse_raw else "")
                         + (" (--skip-api 점검 실행: 생성·채점 없음)" if args.skip_api else ""),
            "commit": commit + (" + 작업 중 변경" if dirty else ""),
            "b2_head": b2_head,
            "python": platform.python_version(),
            "os": f"{platform.system()} {platform.release()}",
        }
        report = build_report(meta, commit_stats, pr_stats, commit_results, pr_results, convention_text)
        (staging / "EVIDENCE.md").write_text(report, encoding="utf-8")
        _privacy_guard(staging, tmp)

        # 모두 성공했을 때만 기존 결과를 교체한다. 새 파일을 먼저 덮어쓴 뒤 남은 옛 파일만 지워서,
        # 교체 도중 실패해도 raw/ 가 비지 않게 한다.
        raw.mkdir(parents=True, exist_ok=True)
        new_names = {new.name for new in (staging / "raw").iterdir()}
        for name in sorted(new_names):
            shutil.copy2(staging / "raw" / name, raw / name)
        shutil.copy2(staging / "EVIDENCE.md", out / "EVIDENCE.md")
        for old in raw.iterdir():
            if old.is_file() and old.name not in new_names:
                old.unlink()
        print(f"\n증빙 보고서: {out / 'EVIDENCE.md'}")
    except BaseException:
        _preserve_partial(staging, args)
        raise
    finally:
        _rmtree(tmp)
    return 0


def _setup_stdio() -> None:
    """도구와 같은 방식으로 출력 인코딩을 맞춘다 (cp949 파이프에서 '–' 같은 문자로 죽지 않도록)."""
    if str(TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(TOOL_DIR))
    from gitgen.console import setup_stdio

    setup_stdio()


def _preserve_partial(staging: Path, args: argparse.Namespace) -> None:
    """실패했을 때 이번 실행에서 이미 받은 API 결과(비용을 낸 결과)를 임시 폴더에 남겨 둔다."""
    generated = [p for p in (staging / "raw").glob("*_*.txt") if p.name != "convention_b2-2.txt"] \
        if (staging / "raw").is_dir() else []
    if args.skip_api or args.reuse_raw or not generated:
        return
    keep = Path(tempfile.mkdtemp(prefix="bonus2_partial_"))
    shutil.copytree(staging, keep, dirs_exist_ok=True)
    print(f"[안내] 실패했지만 이번 실행에서 받은 결과 {len(generated)}개를 보존했습니다: {keep}", file=sys.stderr)


def _copy_standalone_repo(src: Path, dest: Path) -> None:
    """--repo 로 받은 clone 을 임시 폴더로 복사한다.

    linked worktree / submodule 은 .git 이 '원래 저장소를 가리키는 파일'이라 복사본에서 git 을 실행하면
    원래 저장소의 HEAD·index·config 가 바뀐다. 그래서 .git 폴더를 가진 독립 clone 만 받는다.
    """
    if not (src / ".git").is_dir():
        raise RuntimeError(f"--repo 는 .git 폴더가 있는 독립 clone 이어야 합니다 (worktree/submodule/일반 폴더 불가): {src}")
    shutil.copytree(src, dest)


def _assert_isolated(repo: Path) -> None:
    """repo 의 git 저장소가 임시 폴더 안의 자기 .git 인지 확인한다."""
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir").strip()).resolve()
    top = Path(git(repo, "rev-parse", "--show-toplevel").strip()).resolve()
    if git_dir != (repo / ".git").resolve() or top != repo.resolve():
        raise RuntimeError(f"격리 실패: 임시 복사본이 다른 저장소를 가리킵니다 ({git_dir})")


def _tokens(before: Run, after: Run) -> str:
    parts = []
    for name, run in (("적용 전", before), ("적용 후", after)):
        m = re.search(r"토큰 입력 ([\d,]+) / 출력 ([\d,]+) \| [\d.]+초(?: \| 예상 비용 (\$[\d.]+))?", run.stderr)
        if m:
            parts.append(f"{name}: 입력 {m.group(1)} / 출력 {m.group(2)} · {m.group(3) or '-'}")
    return " · ".join(parts)


def _privacy_guard(out: Path, tmp: Path) -> None:
    needles = {str(Path.home()), str(tmp), str(Path.home()).replace("\\", "/"), str(tmp).replace("\\", "/")}
    leaked = [str(p) for p in out.rglob("*") if p.is_file() and p.suffix in (".md", ".txt")
              and any(n in p.read_text(encoding="utf-8", errors="replace") for n in needles)]
    if leaked:
        raise RuntimeError("증빙 파일에 개인 PC 경로가 남아 있습니다: " + ", ".join(leaked))


if __name__ == "__main__":
    sys.exit(main())
