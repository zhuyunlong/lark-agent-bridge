from pathlib import Path
import subprocess


MAX_PYTHON_FILE_LINES = 2000


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
        if line_count > MAX_PYTHON_FILE_LINES:
            oversized.append(f"{relative}: {line_count}")

    assert oversized == []
