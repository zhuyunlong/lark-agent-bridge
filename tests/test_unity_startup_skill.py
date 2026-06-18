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

    def test_select_target_logs_falls_back_to_variable_prefix_main_logs_without_package_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            decoded_log = Path(tmp) / "user0_main_2026-05-11_23-00.alog.log"
            decoded_log.write_text("", encoding="utf-8")
            support_log = Path(tmp) / "main.txt"
            support_log.write_text("", encoding="utf-8")

            selected, warnings = self.mod.select_target_logs([decoded_log, support_log])

        self.assertEqual(selected, [decoded_log])
        self.assertTrue(warnings)
        self.assertIn("主日志文件", warnings[0])

    def test_collect_single_decoded_log_preserves_package_context_for_target_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs" / "app" / "com.xiaopeng.montecarlo"
            root.mkdir(parents=True)
            decoded_log = root / "user0_main_2026-05-28_11-00.alog.log"
            decoded_log.write_text("decoded text log\n", encoding="utf-8")
            workspace = Path(tmp) / "workspace"

            copied, warnings = self.mod.collect_and_materialize_inputs(decoded_log, workspace)

        rel_paths = {path.relative_to(workspace / "collected").as_posix() for path in copied}
        self.assertFalse(warnings)
        self.assertEqual(
            rel_paths,
            {"app/com.xiaopeng.montecarlo/user0_main_2026-05-28_11-00.alog.log"},
        )

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

    def test_collect_inputs_includes_logd_events_lifecycle_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "logs"
            app_dir = root / "data" / "Log" / "log0" / "app" / "com.xiaopeng.montecarlo"
            logd_dir = root / "data" / "Log" / "log0" / "logd"
            app_dir.mkdir(parents=True)
            logd_dir.mkdir(parents=True)
            app_log = app_dir / "user0_main_2026-05-28_20-00.alog.log"
            events_log = logd_dir / "events.txt.1"
            app_log.write_text("decoded text log\n", encoding="utf-8")
            events_log.write_text("", encoding="utf-8")
            workspace = Path(tmp) / "workspace"

            copied, warnings = self.mod.collect_and_materialize_inputs(root, workspace)

        rel_paths = {path.relative_to(workspace / "collected").as_posix() for path in copied}
        self.assertFalse(warnings)
        self.assertIn("data/Log/log0/app/com.xiaopeng.montecarlo/user0_main_2026-05-28_20-00.alog.log", rel_paths)
        self.assertIn("data/Log/log0/logd/events.txt.1", rel_paths)

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

    def test_extract_unity_runtime_context_parses_bundle_cfg_and_current_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "main_2026-05-28_11-00.alog.log"
            log_path.write_text(
                "\n".join(
                    [
                        '05-28 11:00:59.821 30414 30810 1919746 I Unity: [Napa6][I]FC:[0][HIMIApp]streamingAssets BundleBuildCfg : {"timeVer":"2026-05-26 05:05:06","buildType":2,"buildParam":"{\\"carType\\":\\"XOS5.0\\",\\"Branch\\":\\"6.2.3_release\\",\\"buildTime\\":\\"2026-05-26 04:52:10\\",\\"PlatformVer\\":\\"NapaV5\\",\\"ResourceType\\":\\"outer\\",\\"TargetResource\\":\\"\\"}"}',
                        "05-28 11:01:32.341 30414 30810 1952266 I Unity: [Napa6][I]FC:[894][AnalyseNode] current version:6.2.3_release.2026-05-26 04:52:10 kernel version:v2.5.7 isDebug:False carType:G02S loglev:1 lanType:zh Model:eNone framerate:30 vSyncCount:0 useForceSyncRender:False EnableProfiler:False TargetFrameRate:30 ProtoType:1 dockerInfo:",
                    ]
                ),
                encoding="utf-8",
            )

            context = self.mod.extract_unity_runtime_context(log_path)

        self.assertEqual(context.resource_type, "outer")
        self.assertEqual(context.proto_type, "1")
        self.assertEqual(context.car_type, "G02S")
        self.assertEqual(context.branch, "6.2.3_release")
        self.assertEqual(context.build_time, "2026-05-26 04:52:10")
        self.assertEqual(context.platform_ver, "NapaV5")

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

    def test_scan_text_log_parses_logd_events_activity_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "events.txt.1"
            log_path.write_text(
                "05-28 20:05:01.000  1000  1000 I am_on_resume_called: [0,com.xiaopeng.montecarlo/.AndroidMainActivity,ON_RESUME]\n",
                encoding="utf-8",
            )

            events = self.mod.scan_text_log(log_path)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].node_id, "activity_on_resume")
        self.assertEqual(events[0].tag, "am_on_resume_called")
        self.assertIn("logd/events", events[0].source)

    def test_surface_binding_context_tracks_main_surface_unbind_and_rebind(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "main_2026-05-28_20-00.alog.log"
            log_path.write_text(
                "\n".join(
                    [
                        "05-28 20:04:50.000  3000  3000 I AndroidMainActivity: onResume begin, Version:1 start resume the glSurface view =XPEDriveSurfaceView{abc}",
                        "05-28 20:04:55.000  3000  3100 I SrSM_UnityContext: onUnityDisplaySurfaceSubmit, type: MainSurface, surface: null, scene: 0, status: SurfaceStatusDestroyed, height: 0, width: 0, purpose: UnityRealRenderToThisSurface, mMainSurfaceViewHeight: 1476, mMainSurface: Surface(name=old), surfaceOwnerHashCode:123, mUnityPlayer: com.unity3d.player.UnityPlayer@1",
                        "05-28 20:04:55.001  3000  3100 I SrSM_UnityContext: onUnityDisplaySurfaceSubmit, displayChanged id: 2, surface: null, projectType: 1",
                        "05-28 20:05:04.000  3000  3000 I AndroidMainActivity: onResume begin, Version:1 start resume the glSurface view =XPEDriveSurfaceView{def}",
                        "05-28 20:05:05.000  3000  3100 I SrSM_UnityContext: onUnityDisplaySurfaceSubmit, type: MainSurface, surface: Surface(name=SurfaceView[com.xiaopeng.montecarlo]), scene: 0, status: SurfaceStatusChanged, height: 1476, width: 2880, purpose: UnityRealRenderToThisSurface, mMainSurfaceViewHeight: 0, mMainSurface: null, surfaceOwnerHashCode:456, mUnityPlayer: com.unity3d.player.UnityPlayer@1",
                        "05-28 20:05:05.001  3000  3100 I SrSM_UnityContext: onUnityDisplaySurfaceSubmit, displayChanged id: 2, surface: Surface(name=SurfaceView[com.xiaopeng.montecarlo]), projectType: 1",
                    ]
                ),
                encoding="utf-8",
            )
            target_time = self.mod.parse_target_time("2026-05-28 20:05:00")

            events = self.mod.scan_text_log(log_path)
            context = self.mod.build_surface_binding_context(events, target_time)

        self.assertIsNotNone(context.last_unbind_before_target)
        self.assertIsNotNone(context.nearest_bind_after_target)
        self.assertEqual(context.last_unbind_before_target.surface_type, "MainSurface")
        self.assertEqual(context.nearest_bind_after_target.surface_type, "MainSurface")
        self.assertFalse(context.last_unbind_before_target.is_bound)
        self.assertTrue(context.nearest_bind_after_target.is_bound)
        self.assertIn("AndroidMainActivity onResume", context.summary)

    def test_diagnose_text_does_not_report_complete_when_preload_done_but_player_missing(self):
        def fake(node_id: str):
            event = self._fake_event(self.mod, "2026-05-22 17:29:48.419", 8066)
            event.node_id = node_id
            return event

        diagnosis = self.mod.diagnose_text(
            {
                "app_attach_base_context": fake("app_attach_base_context"),
                "activity_on_create": fake("activity_on_create"),
                "activity_on_resume": fake("activity_on_resume"),
                "xpe_surface_created": fake("xpe_surface_created"),
                "unity_preload_start": fake("unity_preload_start"),
                "unity_preload_success": fake("unity_preload_success"),
            }
        )

        self.assertNotIn("启动链路完整", diagnosis)
        self.assertIn("Unity preload", diagnosis)

    def test_build_overall_verdict_does_not_repeat_complete_for_partial_session(self):
        session = self.mod.Session(
            index=4,
            events=[self._fake_event(self.mod, "2026-05-22 17:29:48.419", 8066)],
            first_by_node={},
            all_by_node={},
            status="partial",
            diagnosis="启动链路完整，已经到达 3D 最终首帧展示。",
            missing_critical=["createUnityPlayerOnMainThread", "SET_READY_PREPARE / UnityReady"],
            primary_pid=8066,
        )

        severity, message = self.mod.build_overall_verdict(
            [session],
            [],
            focus_session=session,
            target_time=self.mod.parse_target_time("2026-05-22 17:29"),
            boot_relation=None,
        )

        self.assertEqual(severity, "red")
        self.assertNotIn("启动链路完整", message)
        self.assertIn("未闭环", message)

    def test_diagnose_text_prefers_napa_resource_failure_chain(self):
        def fake(node_id: str, message: str = "msg"):
            event = self._fake_event(self.mod, "2026-05-28 11:16:35.769", 13379)
            event.node_id = node_id
            event.message = message
            event.excerpt = message
            return event

        diagnosis = self.mod.diagnose_text(
            {
                "app_attach_base_context": fake("app_attach_base_context"),
                "activity_on_create": fake("activity_on_create"),
                "activity_on_resume": fake("activity_on_resume"),
                "xpe_surface_created": fake("xpe_surface_created"),
                "unity_preload_start": fake("unity_preload_start"),
                "unity_preload_success": fake("unity_preload_success"),
                "unity_internal_bundle_cfg": fake("unity_internal_bundle_cfg"),
                "unity_internal_version": fake("unity_internal_version"),
                "unity_internal_asset_dependency_fail": fake(
                    "unity_internal_asset_dependency_fail",
                    "[AddressModel]load fail ... Dependency Exception ... Invalid path in AssetBundleProvider",
                ),
                "unity_internal_display_loader_exception": fake(
                    "unity_internal_display_loader_exception",
                    "[NapaDisplayManager]DisplayLoaderNode Exception at OnAttach:System.NullReferenceException",
                ),
            }
        )

        self.assertIn("资源", diagnosis)
        self.assertIn("BaseCamera", diagnosis)
        self.assertNotIn("SET_READY_PREPARE 没有到达", diagnosis)

    def test_find_nearest_system_load_prefers_exact_pid(self):
        when = self.mod.dt.datetime.strptime("2026-05-28 11:16:24.487", "%Y-%m-%d %H:%M:%S.%f")
        exact_pid = self.mod.SystemLoadSnapshot(
            timestamp=self.mod.dt.datetime.strptime("2026-05-28 11:16:25.000", "%Y-%m-%d %H:%M:%S.%f"),
            total_cpu=30,
            user_cpu=10,
            system_cpu=15,
            iow_cpu=1,
            irq_cpu=2,
            sirq_cpu=2,
            file_path="/tmp/main.txt",
            line_no=10,
            excerpt="exact",
            process_name="com.xiaopeng.montecarlo",
            process_pid=13379,
            process_cpu=5.0,
            process_mem_rss_kb=1,
            process_mem_vss_kb=2,
            process_io_read_kb=3,
            process_io_write_kb=4,
            process_threads=5,
            process_fds=6,
        )
        stale_name_only = self.mod.SystemLoadSnapshot(
            timestamp=self.mod.dt.datetime.strptime("2026-05-28 11:16:20.000", "%Y-%m-%d %H:%M:%S.%f"),
            total_cpu=20,
            user_cpu=5,
            system_cpu=10,
            iow_cpu=0,
            irq_cpu=2,
            sirq_cpu=3,
            file_path="/tmp/main.txt",
            line_no=20,
            excerpt="stale",
            process_name="com.xiaopeng.montecarlo",
            process_pid=10776,
            process_cpu=4.0,
            process_mem_rss_kb=1,
            process_mem_vss_kb=2,
            process_io_read_kb=3,
            process_io_write_kb=4,
            process_threads=5,
            process_fds=6,
        )

        selected = self.mod.find_nearest_system_load([stale_name_only, exact_pid], when=when, pid=13379)

        self.assertIs(selected, exact_pid)

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
