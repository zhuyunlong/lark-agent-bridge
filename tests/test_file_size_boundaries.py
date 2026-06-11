from pathlib import Path
import subprocess


MAX_PYTHON_FILE_LINES = 2000
# 2026-06 架构重构后收紧：源码包内单文件不得回退为千行单体。
# 当前唯一例外集：单方法巨型函数（见 docs/refactor/steps/S10-S13 技术债记录）。
MAX_SOURCE_FILE_LINES = 1200


def test_tracked_python_files_stay_below_large_file_boundary() -> None:
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.py"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    oversized: list[str] = []
    for relative in completed.stdout.splitlines():
        path = root / relative
        if not path.exists():
            continue
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        limit = (
            MAX_SOURCE_FILE_LINES
            if relative.startswith("lark_agent_bridge/")
            else MAX_PYTHON_FILE_LINES
        )
        if line_count > limit:
            oversized.append(f"{relative}: {line_count} (limit {limit})")

    assert oversized == []
