import importlib.util
from pathlib import Path
import tempfile
import unittest


def _load_perception_module():
    script_path = (
        Path(__file__).resolve().parents[3]
        / ".ai/skills/perception-data-summary/scripts/analyze_perception_data_summary.py"
    )
    if not script_path.exists():
        raise unittest.SkipTest(f"perception-data-summary script not found: {script_path}")
    spec = importlib.util.spec_from_file_location("perception_data_summary_script", script_path)
    if spec is None or spec.loader is None:
        raise unittest.SkipTest(f"cannot load perception-data-summary script: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PerceptionDataSummarySkillTests(unittest.TestCase):
    def test_current_xpd_formats_are_counted(self):
        module = _load_perception_module()
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "log0" / "app" / "com.xiaopeng.montecarlo"
            log_dir.mkdir(parents=True)
            log_path = log_dir / "user0_main_2026-05-22_16-00.alog.txt"
            log_path.write_text(
                "\n".join(
                    [
                        "05-22 16:17:05.659 2715 7005 336651 I XPD_VHALHelper: [41001][597|103ms|30s]onPropertyChanged 41001:597",
                        "05-22 16:17:10.460 2715 7001 341452 I XPD_UnityTransport: [X3DCB-132000][597|108ms|30s]code:132000, length:1168",
                        "05-22 16:17:11.059 2715 7001 342052 I Unity: [Napa6][I]FC:[7963][NativeReceiveService]ReceiveMsg bizCode:132000;fps:19/秒 10000.94/毫秒内收到;收到:199:放弃:10",
                    ]
                ),
                encoding="utf-8",
            )

            files = module.find_candidate_logs(Path(tmp))
            result = module.analyze_logs(files)

        self.assertIn("41001", result["raw_stats"])
        self.assertIn("132000", result["x3d_stats"])
        self.assertIn("132000", result["unity_stats"])
        self.assertEqual(result["raw_stats"]["41001"][0]["count"], 597)
        self.assertEqual(result["x3d_stats"]["132000"][0]["count"], 597)
        self.assertEqual(result["unity_stats"]["132000"][0]["fps"], 19)
        self.assertEqual(result["unity_stats"]["132000"][0]["recv"], 199)
        self.assertEqual(result["unity_stats"]["132000"][0]["drop"], 10)

    def test_target_time_filter_excludes_unrelated_earlier_drop(self):
        module = _load_perception_module()
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "log0" / "app" / "com.xiaopeng.montecarlo"
            log_dir.mkdir(parents=True)
            log_path = log_dir / "user0_main_2026-05-22_16-00.alog.txt"
            log_path.write_text(
                "\n".join(
                    [
                        "05-22 16:12:35.603 2715 7001 66596 I XPD_UnityTransport: [X3DCB-DROP-132000][595|152ms|30s]code:132000, length:1168",
                        "05-22 16:17:05.659 2715 7005 336651 I XPD_VHALHelper: [41001][597|103ms|30s]onPropertyChanged 41001:597",
                        "05-22 16:17:10.460 2715 7001 341452 I XPD_UnityTransport: [X3DCB-132000][597|108ms|30s]code:132000, length:1168",
                        "05-22 16:17:11.059 2715 7001 342052 I Unity: [Napa6][I]FC:[7963][NativeReceiveService]ReceiveMsg bizCode:132000;fps:19/秒 10000.94/毫秒内收到;收到:199:放弃:10",
                    ]
                ),
                encoding="utf-8",
            )

            result = module.analyze_logs(module.find_candidate_logs(Path(tmp)))

        self.assertTrue(hasattr(module, "filter_result_by_target_time"))
        filtered = module.filter_result_by_target_time(result, "2026-05-22 16:17", 90)
        self.assertEqual(filtered["x3d_drop_stats"], {})
        self.assertIn("41001", filtered["raw_stats"])
        self.assertIn("132000", filtered["x3d_stats"])
        self.assertIn("132000", filtered["unity_stats"])
