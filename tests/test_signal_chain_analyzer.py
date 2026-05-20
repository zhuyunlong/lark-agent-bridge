import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
import zipfile


SCRIPT_PATH = Path("/Users/zhuyl/Documents/workspace/.ai/skills/signal-chain-analyzer/scripts/analyze_signal_chain.py")


def _load_analyzer(repo_root: Path):
    old_repo = os.environ.get("GUIDEENGINE_REPO")
    os.environ["GUIDEENGINE_REPO"] = str(repo_root)
    module_name = f"signal_chain_analyzer_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load signal-chain analyzer")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        if old_repo is None:
            os.environ.pop("GUIDEENGINE_REPO", None)
        else:
            os.environ["GUIDEENGINE_REPO"] = old_repo


class SignalChainAnalyzerTests(unittest.TestCase):
    def test_maybe_unzip_extracts_7z_archive_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            proto = repo / "module_floorcenter/module_proto/src/main/proto/signal.proto"
            proto.parent.mkdir(parents=True, exist_ok=True)
            proto.write_text("enum SignalCode { SIGNAL_SAMPLE = 1; }\n", encoding="utf-8")
            archive = Path(tmp) / "logs.7z"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("Log/main.txt", "signal log")

            analyzer = _load_analyzer(repo)
            extracted = analyzer.maybe_unzip(archive)

            self.assertTrue(extracted.is_dir())
            self.assertTrue((extracted / "Log/main.txt").exists())

    def test_android_datacenter_on_next_data_signal_is_linked_to_business_consumer(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            proto = repo / "module_floorcenter/module_proto/src/main/proto/signal.proto"
            helper = repo / (
                "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/"
                "helper/carcontrol/CarCtlPowerCenterHelper.kt"
            )
            data_center = repo / (
                "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/center/DataCenter.kt"
            )
            action = repo / (
                "module_core/manager_carscene/src/main/java/com/xiaopeng/manager_carscene/power/"
                "action/external_discharge/CarSceneExternalDischargeAction.kt"
            )
            for path in (proto, helper, data_center, action):
                path.parent.mkdir(parents=True, exist_ok=True)
            proto.write_text(
                "enum SignalCode {\n"
                "  SIGNAL_CTL_POWERCENTER_SYNTHESIS_REMAIN_DIS_CHANGE = 16042;// 综合续航\n"
                "}\n",
                encoding="utf-8",
            )
            helper.write_text(
                "class CarCtlPowerCenterHelper {\n"
                "  fun callback() {\n"
                "    onNextData(SignalCode.SIGNAL_CTL_POWERCENTER_SYNTHESIS_REMAIN_DIS_CHANGE, SignalFormat.Float, synthesisRemainingDis)\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            data_center.write_text(
                "class DataCenter {\n"
                "  override fun dispatchSignal(xDataPropertyValue: XDataPropertyValue) {}\n"
                "}\n",
                encoding="utf-8",
            )
            action.write_text(
                "class CarSceneExternalDischargeAction {\n"
                "  fun getRegisterSignalCode(): MutableSet<SignalCode> = mutableSetOf(\n"
                "    SignalCode.SIGNAL_CTL_POWERCENTER_SYNTHESIS_REMAIN_DIS_CHANGE,\n"
                "  )\n"
                "  fun update(xDataPropertyValue: XDataPropertyValue) {\n"
                "    when (xDataPropertyValue.code) {\n"
                "      SignalCode.SIGNAL_CTL_POWERCENTER_SYNTHESIS_REMAIN_DIS_CHANGE -> emit()\n"
                "    }\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )

            analyzer = _load_analyzer(repo)
            by_code, by_name = analyzer.parse_signal_proto()
            edges = analyzer.build_static_edges(by_name)
            chain = analyzer.collect_chain_edges("16042", edges)

        self.assertTrue(any(edge.target == "16042" and "onNextData" in edge.note for edge in chain))
        self.assertTrue(any(edge.source == "16042" and edge.target == "ANDROID:DataCenter Flow/Observer" for edge in chain))
        self.assertTrue(
            any(
                edge.source == "ANDROID:DataCenter Flow/Observer"
                and "CarSceneExternalDischargeAction" in edge.target
                for edge in chain
            )
        )
        context = analyzer.infer_android_provider_context(by_code["16042"], chain)
        self.assertIsNotNone(context)
        self.assertEqual(context["helper_class"], "CarCtlPowerCenterHelper")


if __name__ == "__main__":
    unittest.main()
