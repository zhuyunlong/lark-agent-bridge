from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPT_PATH = (
    Path("/Users/zhuyl/Documents/workspace/.ai/skills/unity-startup-lifecycle-check/scripts/analyze_unity_startup.py")
)


def load_module():
    spec = importlib.util.spec_from_file_location("unity_startup_skill_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class UnityStartupSkillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_module()

    def test_parse_target_time_accepts_fullwidth_colon(self):
        parsed = self.mod.parse_target_time("2026-5-11 23：10")

        self.assertEqual(parsed.strftime("%Y-%m-%d %H:%M:%S"), "2026-05-11 23:10:00")

    def test_choose_focus_session_prefers_latest_session_before_target(self):
        session1 = self.mod.Session(
            index=1,
            events=[],
            first_by_node={},
            all_by_node={},
            status="ready-no-frame",
            diagnosis="s1",
            missing_critical=[],
            primary_pid=2577,
        )
        session2 = self.mod.Session(
            index=2,
            events=[],
            first_by_node={},
            all_by_node={},
            status="complete",
            diagnosis="s2",
            missing_critical=[],
            primary_pid=7412,
        )
        session1.events = [self._fake_event(self.mod, "2026-05-11 23:06:20.956", 2577)]
        session2.events = [self._fake_event(self.mod, "2026-05-11 23:11:29.313", 7412)]
        target_time = self.mod.parse_target_time("2026-05-11 23:10")

        focus, reason = self.mod.choose_focus_session([session1, session2], target_time)

        self.assertIsNotNone(focus)
        self.assertEqual(focus.index, 1)
        self.assertIn("之前最近", reason)

    def test_select_target_logs_falls_back_to_main_logs_without_package_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            decoded_log = Path(tmp) / "main_2026-05-11_23-00.alog.log"
            decoded_log.write_text("", encoding="utf-8")
            support_log = Path(tmp) / "main.txt"
            support_log.write_text("", encoding="utf-8")

            selected, warnings = self.mod.select_target_logs([decoded_log, support_log])

        self.assertEqual(selected, [decoded_log])
        self.assertTrue(warnings)
        self.assertIn("main_*.log", warnings[0])

    def test_scan_system_load_snapshots_parses_logd_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "main.txt"
            log_path.write_text(
                "\n".join(
                    [
                        "05-11 23:06:35.164   380   443 I DFX-SystemMonitor: Total 75%, User 24%, System 37%, iow 10%, irq 3%, sirq 1%",
                        "05-11 23:06:35.611   380   443 I DFX-SystemMonitor: com.xiaopeng.montecarlo(2577), CPU:0.00%, MEM:(221596/263685) K, IO:2540/2540 K, T/F:255/284",
                    ]
                ),
                encoding="utf-8",
            )

            snapshots = self.mod.scan_system_load_snapshots(log_path)

        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].process_pid, 2577)
        self.assertEqual(snapshots[0].total_cpu, 75)
        self.assertEqual(snapshots[0].iow_cpu, 10)

    def test_scan_process_markers_reads_pid_from_process_begin_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "main_2026-05-11_23-00.alog.log"
            log_path.write_text(
                "process begin^^^^^^^^^^Mar 11 2026^^^20:04:15^^^^^^^^^^[7412,7577][2026-05-11 +0800 23:11:29]\n",
                encoding="utf-8",
            )

            markers = self.mod.scan_process_markers(log_path)

        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0].pid, 7412)

    @staticmethod
    def _fake_event(mod, timestamp_text: str, pid: int):
        timestamp = mod.dt.datetime.strptime(timestamp_text, "%Y-%m-%d %H:%M:%S.%f")
        return mod.Event(
            node_id="app_attach_base_context",
            phase="Application",
            title="start",
            importance="critical",
            source="test",
            why="test",
            timestamp=timestamp,
            timestamp_text=timestamp_text,
            file_path="/tmp/main.log",
            line_no=1,
            tag="TAG",
            level="I",
            message="msg",
            excerpt="excerpt",
            pid=pid,
            tid=pid,
        )


if __name__ == "__main__":
    unittest.main()
