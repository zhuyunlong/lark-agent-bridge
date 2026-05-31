from pathlib import Path

from lark_agent_bridge.models import DownloadResource
from lark_agent_bridge.replay import (
    ReplayResourceBundle,
    normalize_replay_mode,
    serialize_resource_status,
)


def test_normalize_replay_mode_accepts_known_analysis_modes():
    assert normalize_replay_mode("bug_reanalysis") == "bug"
    assert normalize_replay_mode("bug_analysis") == "bug"
    assert normalize_replay_mode("direct_analysis") == "direct_analysis"
    assert normalize_replay_mode("signal_lifecycle") == "signal_lifecycle"
    assert normalize_replay_mode("perception_summary") == "perception_summary"
    assert normalize_replay_mode("addr2line_resolve") == "addr2line_resolve"
    assert normalize_replay_mode("rom_version_lookup") == "rom_version_lookup"


def test_resource_bundle_prefers_existing_local_paths(tmp_path):
    prepared = tmp_path / "prepared"
    selected = tmp_path / "selected"
    prepared.mkdir()
    selected.mkdir()
    missing = tmp_path / "missing"

    bundle = ReplayResourceBundle.from_candidates(
        current=[],
        reply_chain=[],
        session=[
            DownloadResource(kind="local", value=str(missing)),
            DownloadResource(kind="local", value=str(prepared)),
            DownloadResource(kind="local", value=str(selected)),
        ],
    )

    assert [Path(item.value) for item in bundle.local_existing] == [
        prepared.resolve(),
        selected.resolve(),
    ]
    assert bundle.best_effort
    assert bundle.missing_local_values == [str(missing)]


def test_resource_status_serialization_is_generic_and_stable(tmp_path):
    prepared = tmp_path / "prepared"
    prepared.mkdir()

    bundle = ReplayResourceBundle.from_candidates(
        current=[
            DownloadResource(
                kind="url",
                value="https://example.test/log.zip",
                source_message_id="om_current",
                display_name="log.zip",
            )
        ],
        reply_chain=[],
        session=[DownloadResource(kind="local", value=str(prepared))],
    )

    assert serialize_resource_status(bundle) == {
        "local_existing": [
            {
                "kind": "local",
                "value": str(prepared.resolve()),
                "source_message_id": "",
                "display_name": "",
            }
        ],
        "remote": [
            {
                "kind": "url",
                "value": "https://example.test/log.zip",
                "source_message_id": "om_current",
                "display_name": "log.zip",
            }
        ],
        "missing_local_values": [],
    }
