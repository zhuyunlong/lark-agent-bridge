from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile


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

    def test_choose_focus_session_ignores_stale_before_session_when_after_is_near(self):
        stale = self.mod.Session(
            index=1,
            events=[],
            first_by_node={},
            all_by_node={},
            status="partial",
            diagnosis="stale",
            missing_critical=[],
            primary_pid=2468,
        )
        near_after = self.mod.Session(
            index=2,
            events=[],
            first_by_node={},
            all_by_node={},
            status="complete",
            diagnosis="near",
            missing_critical=[],
            primary_pid=2468,
        )
        stale.events = [self._fake_event(self.mod, "2026-04-30 14:52:49.770", 2468)]
        stale.process_begin = self.mod.dt.datetime.strptime("2026-05-13 16:14:15", "%Y-%m-%d %H:%M:%S")
        near_after.events = [self._fake_event(self.mod, "2026-05-13 16:14:26.462", 2468)]
        near_after.process_begin = self.mod.dt.datetime.strptime("2026-05-13 16:14:15", "%Y-%m-%d %H:%M:%S")
        target_time = self.mod.parse_target_time("2026-05-13 16:12")

        focus, reason = self.mod.choose_focus_session([stale, near_after], target_time)

        self.assertIsNotNone(focus)
        self.assertEqual(focus.index, 2)
        self.assertIn("之后最近", reason)

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

    def test_collect_inputs_skips_raw_alog_when_decoded_companion_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            target_dir = root / "data" / "Log" / "log0" / "app" / "com.xiaopeng.montecarlo"
            target_dir.mkdir(parents=True)
            raw_log = target_dir / "main_2026-04-26_17-00.alog"
            decoded_log = target_dir / "main_2026-04-26_17-00.alog.log"
            raw_log.write_bytes(b"touch Max size of zip file!")
            decoded_log.write_text("decoded text log\n", encoding="utf-8")
            workspace = Path(tmp) / "workspace"

            copied, warnings = self.mod.collect_and_materialize_inputs(root, workspace)

        rel_paths = {path.relative_to(workspace / "collected").as_posix() for path in copied}
        self.assertFalse(warnings)
        self.assertIn("data/Log/log0/app/com.xiaopeng.montecarlo/main_2026-04-26_17-00.alog.log", rel_paths)
        self.assertNotIn("data/Log/log0/app/com.xiaopeng.montecarlo/main_2026-04-26_17-00.alog", rel_paths)

    def test_collect_inputs_keeps_raw_alog_when_decoded_companion_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            target_dir = root / "data" / "Log" / "log0" / "app" / "com.xiaopeng.montecarlo"
            target_dir.mkdir(parents=True)
            raw_log = target_dir / "main_2026-04-26_16-00.alog"
            raw_log.write_bytes(b"encrypted")
            workspace = Path(tmp) / "workspace"

            copied, warnings = self.mod.collect_and_materialize_inputs(root, workspace)

        rel_paths = {path.relative_to(workspace / "collected").as_posix() for path in copied}
        self.assertFalse(warnings)
        self.assertIn("data/Log/log0/app/com.xiaopeng.montecarlo/main_2026-04-26_16-00.alog", rel_paths)

    def test_collect_inputs_skips_raw_alog_zip_member_when_decoded_companion_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "logs.zip"
            raw_member = "data/Log/log0/app/com.xiaopeng.montecarlo/main_2026-04-26_17-00.alog"
            decoded_member = f"{raw_member}.log"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr(raw_member, b"touch Max size of zip file!")
                zf.writestr(decoded_member, "decoded text log\n")
            workspace = Path(tmp) / "workspace"

            copied, warnings = self.mod.collect_and_materialize_inputs(zip_path, workspace)

        rel_paths = {path.relative_to(workspace / "collected").as_posix() for path in copied}
        self.assertFalse(warnings)
        self.assertIn(decoded_member, rel_paths)
        self.assertNotIn(raw_member, rel_paths)

    def test_decode_warning_uses_collected_relative_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            raw_log = (
                workspace
                / "collected"
                / "data"
                / "Log"
                / "log1"
                / "app"
                / "com.xiaopeng.montecarlo"
                / "main_2026-04-26_17-00.alog"
            )
            raw_log.parent.mkdir(parents=True)
            raw_log.write_bytes(b"touch Max size of zip file!")
            decoder = Path(tmp) / "decoder.py"
            decoder.write_text("lastseq = 0\n\ndef ParseFile(src, dst):\n    return False\n", encoding="utf-8")

            decoded, warnings = self.mod.decode_raw_logs([raw_log], decoder)

        self.assertEqual(decoded, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("data/Log/log1/app/com.xiaopeng.montecarlo/main_2026-04-26_17-00.alog", warnings[0])

    def test_select_report_sessions_prefers_focus_pid_and_nearby_window(self):
        stale = self.mod.Session(
            index=1,
            events=[self._fake_event(self.mod, "2026-01-01 08:00:38.398", 2468)],
            first_by_node={},
            all_by_node={},
            status="partial",
            diagnosis="stale",
            missing_critical=[],
            primary_pid=2468,
        )
        near_focus = self.mod.Session(
            index=2,
            events=[self._fake_event(self.mod, "2026-05-13 16:14:26.462", 2468)],
            first_by_node={},
            all_by_node={},
            status="complete",
            diagnosis="focus",
            missing_critical=[],
            primary_pid=2468,
        )
        nearby_same_pid = self.mod.Session(
            index=3,
            events=[self._fake_event(self.mod, "2026-05-13 16:18:00.000", 2468)],
            first_by_node={},
            all_by_node={},
            status="complete",
            diagnosis="nearby",
            missing_critical=[],
            primary_pid=2468,
        )
        nearby_other_pid = self.mod.Session(
            index=4,
            events=[self._fake_event(self.mod, "2026-05-13 16:15:00.000", 7412)],
            first_by_node={},
            all_by_node={},
            status="complete",
            diagnosis="other",
            missing_critical=[],
            primary_pid=7412,
        )

        target_time = self.mod.parse_target_time("2026-05-13 16:12")
        selected = self.mod.select_report_sessions(
            [stale, near_focus, nearby_same_pid, nearby_other_pid],
            near_focus,
            target_time,
        )

        self.assertEqual([session.index for session in selected], [2, 3])

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
        self.assertEqual(markers[0].timestamp.strftime("%Y-%m-%d %H:%M:%S"), "2026-05-11 23:11:29")

    def test_scan_text_log_parses_line_without_sequence_number(self):
        """LOG_LINE_RE must match standard logcat format (no sequence field)."""
        with tempfile.TemporaryDirectory() as tmp:
            # logd/main.txt style: no sequence number field
            log_path = Path(tmp) / "main.txt"
            log_path.write_text(
                "05-11 23:06:40.123  1462  1480 I SrSM_SrUnityPlayer: preloadNativeLibrary: start loading\n",
                encoding="utf-8",
            )
            events = self.mod.scan_text_log(log_path)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].node_id, "unity_preload_start")
        self.assertEqual(events[0].pid, 1462)

    def test_scan_text_log_parses_line_with_sequence_number(self):
        """LOG_LINE_RE must also match decoded alog format (sequence number present)."""
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "main_2026-05-11_23-00.alog.log"
            log_path.write_text(
                "05-11 23:06:40.123  1462  1480 42 I SrSM_SrUnityPlayer: preloadNativeLibrary: start loading\n",
                encoding="utf-8",
            )
            events = self.mod.scan_text_log(log_path)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].node_id, "unity_preload_start")

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
