"""AI 기반 Git 커밋/PR 초안 생성기 실행 파일.

사용 예)
    python main.py commit
    python main.py pr --base main
    python main.py --help
"""

import sys

from gitgen.cli import main

if __name__ == "__main__":
    sys.exit(main())
