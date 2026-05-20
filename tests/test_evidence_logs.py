from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from lark_agent_bridge.evidence_logs import preserve_evidence_log_bundle


class EvidenceLogBundleTests(unittest.TestCase):
    def test_preserves_focused_navigation_logd_and_vehicle_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "extracted"
            log0_nav = root / "Log" / "log0" / "app" / "com.xiaopeng.montecarlo" / "nav.log"
            log1_nav = root / "Log" / "log1" / "app" / "com.xiaopeng.montecarlo" / "nav.log"
            log1_logd = root / "Log" / "log1" / "logd" / "main.txt"
            log1_vehicle = root / "Log" / "log1" / "app" / "vehicle" / "vehicle.log"
            for path in (log0_nav, log1_nav, log1_logd, log1_vehicle):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(path.name, encoding="utf-8")

            report_json = Path(tmp) / "startup.json"
            report_json.write_text(
                json.dumps(
                    {
                        "events": [
                            {
                                "file": str(log1_nav),
                                "line": 42,
                                "message": "首帧卡在导航日志中",
                            }
                        ],
                        "ignored": "/etc/passwd",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            output_dir = Path(tmp) / "job" / "output"

            bundle = preserve_evidence_log_bundle(
                output_dir=output_dir,
                source_roots=[root],
                reference_files=[report_json],
                reference_texts=[f"结论补充引用同级 vehicle 日志：{log1_vehicle}"],
            )

            assert bundle is not None
            manifest = json.loads(Path(bundle["manifest_path"]).read_text(encoding="utf-8"))
            copied = {entry["relative_path"] for entry in manifest["files"]}

        self.assertEqual(manifest["focus_logs"], ["log1"])
        self.assertIn("Log/log1/app/com.xiaopeng.montecarlo/nav.log", copied)
        self.assertIn("Log/log1/logd/main.txt", copied)
        self.assertIn("Log/log1/app/vehicle/vehicle.log", copied)
        self.assertNotIn("Log/log0/app/com.xiaopeng.montecarlo/nav.log", copied)
        self.assertFalse((output_dir / "evidence_logs" / "etc" / "passwd").exists())


if __name__ == "__main__":
    unittest.main()
