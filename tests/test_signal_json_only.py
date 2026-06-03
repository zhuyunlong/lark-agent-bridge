import subprocess
import sys
from pathlib import Path

SCRIPT = Path("/Users/zhuyl/Documents/workspace/.ai/skills/signal-chain-analyzer/scripts/analyze_signal_chain.py")

# Minimal proto needed by the script to resolve a signal by name
_MINIMAL_PROTO = """\
syntax = "proto3";
enum SignalCode {
  SIGNAL_VCU_ELECTRICIT_PERCENT = 12345;
}
"""


def _make_repo(tmp_path: Path) -> Path:
    """Create a minimal repo with a signal.proto so the script can parse signals."""
    repo = tmp_path / "repo"
    proto_dir = repo / "module_floorcenter/module_proto/src/main/proto"
    proto_dir.mkdir(parents=True)
    (proto_dir / "signal.proto").write_text(_MINIMAL_PROTO, encoding="utf-8")
    return repo


def test_json_only_skips_html(tmp_path):
    repo = _make_repo(tmp_path)
    out_html = tmp_path / "r.html"
    out_json = tmp_path / "r.json"
    logdir = tmp_path / "logs"
    logdir.mkdir()
    (logdir / "empty.log").write_text("", encoding="utf-8")

    import os
    env = {**os.environ, "GUIDEENGINE_REPO": str(repo)}

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--signal-code", "SIGNAL_VCU_ELECTRICIT_PERCENT",
            "--log-path", str(logdir),
            "--output", str(out_html),
            "--json-output", str(out_json),
            "--json-only",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert out_json.exists()
    assert not out_html.exists()
