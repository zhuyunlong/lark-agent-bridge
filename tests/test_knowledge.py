from pathlib import Path
import json
import tempfile
import unittest

from lark_agent_bridge.knowledge import KnowledgeService
from lark_agent_bridge.models import BridgeConfig, KnowledgeOptions, KnowledgeSourceOptions


class KnowledgeServiceTests(unittest.TestCase):
    def test_syncs_local_adb_json_and_searches_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adb_path = root / "adb_data.json"
            adb_path.write_text(
                json.dumps(
                    {
                        "commands": [
                            {
                                "name": "开启I&D级别日志",
                                "command": "adb shell am broadcast -a com.xiaopeng.montecarlo.TEST_ENABLE_DETAIL_NAVI_LOG --ei log_enable 2",
                                "group": "开发调试",
                                "description": "打开导航详细日志",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            config = BridgeConfig(
                data_dir=root,
                knowledge=KnowledgeOptions(
                    enabled=True,
                    storage=root / "knowledge.sqlite",
                    sources=[
                        KnowledgeSourceOptions(
                            id="guideengine-adb",
                            type="local_json",
                            path=str(adb_path),
                        )
                    ],
                ),
            )

            service = KnowledgeService(config)
            sync_result = service.sync_all()
            hits = service.search("怎么开启导航详细日志")

        self.assertEqual(sync_result["total_chunks"], 1)
        self.assertEqual(hits[0].source_id, "guideengine-adb")
        self.assertIn("开启I&D级别日志", hits[0].title)
        self.assertIn("TEST_ENABLE_DETAIL_NAVI_LOG", hits[0].content)

    def test_signal_simulation_questions_are_routed_without_kb_prefix(self):
        service = KnowledgeService(BridgeConfig(knowledge=KnowledgeOptions(enabled=True)))

        self.assertTrue(service.should_handle("OTA信号如何模拟"))
        self.assertTrue(service.should_handle("主题信号怎么模拟"))
        self.assertTrue(service.should_handle("PB对象怎么ADB模拟"))

    def test_answers_ota_signal_with_deterministic_adb_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guideengine = _write_minimal_guideengine_sources(root / "guideengine")
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=guideengine,
                knowledge=KnowledgeOptions(
                    enabled=True,
                    storage=root / "knowledge.sqlite",
                    sources=[
                        KnowledgeSourceOptions(id="guideengine-signals", type="guideengine_signal"),
                    ],
                ),
            )

            service = KnowledgeService(config)
            service.sync_all()
            answer = service.answer("SIGNAL_OTA_ST 四种组合指令")

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["mode"], "knowledge_qa")
        self.assertIn("SIGNAL_OTA_ST 四种组合指令", answer.message)
        self.assertIn("--ei code 105003 --ei format 7 --es value \"[0, 0]\"", answer.message)
        self.assertIn("OTA_CAMPAIGN_SHOW", answer.message)
        self.assertIn("OTA_UPGRADE_AFTER_VIDEO", answer.message)
        self.assertGreaterEqual(len(answer.details["knowledge_hits"]), 3)

    def test_answers_fuzzy_ota_signal_question_with_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guideengine = _write_minimal_guideengine_sources(root / "guideengine")
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=guideengine,
                knowledge=KnowledgeOptions(
                    enabled=True,
                    storage=root / "knowledge.sqlite",
                    sources=[
                        KnowledgeSourceOptions(id="guideengine-signals", type="guideengine_signal"),
                    ],
                ),
            )

            service = KnowledgeService(config)
            service.sync_all()
            answer = service.answer("OTA信号如何模拟")

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "adb_signal_template")
        self.assertIn("SIGNAL_OTA_ST 四种组合指令", answer.message)
        self.assertIn("--ei code 105003 --ei format 7", answer.message)

    def test_theme_signal_question_lists_simulation_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guideengine = _write_minimal_guideengine_sources(root / "guideengine")
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=guideengine,
                knowledge=KnowledgeOptions(
                    enabled=True,
                    storage=root / "knowledge.sqlite",
                    sources=[
                        KnowledgeSourceOptions(id="guideengine-signals", type="guideengine_signal"),
                    ],
                ),
            )

            service = KnowledgeService(config)
            service.sync_all()
            answer = service.answer("主题信号如何模拟")

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "adb_signal_candidates")
        self.assertIn("命中多个可能的主题模拟信号", answer.message)
        self.assertIn("SIGNAL_SR_XTHEME", answer.message)
        self.assertIn("105004", answer.message)
        self.assertIn("--ei format 18", answer.message)
        self.assertIn("timePeriod,themeMode", answer.message)
        self.assertIn("SIGNAL_SR_XTHEME_MSG", answer.message)
        self.assertIn("继续发：知识库 模拟 SIGNAL_SR_XTHEME", answer.message)

    def test_exact_xtheme_followup_returns_adb_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guideengine = _write_minimal_guideengine_sources(root / "guideengine")
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=guideengine,
                knowledge=KnowledgeOptions(
                    enabled=True,
                    storage=root / "knowledge.sqlite",
                    sources=[
                        KnowledgeSourceOptions(id="guideengine-signals", type="guideengine_signal"),
                    ],
                ),
            )

            service = KnowledgeService(config)
            service.sync_all()
            answer = service.answer("知识库 模拟 SIGNAL_SR_XTHEME")

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "adb_signal_template")
        self.assertIn("SIGNAL_SR_XTHEME 常见组合指令", answer.message)
        self.assertIn("--ei code 105004 --ei format 18 --es value \"1,0\"", answer.message)

    def test_complex_pb_question_explains_custom_factory_boundary(self):
        service = KnowledgeService(BridgeConfig(knowledge=KnowledgeOptions(enabled=True)))

        answer = service.answer("PB对象怎么ADB模拟")

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "adb_simulation_policy")
        self.assertIn("基础类型", answer.message)
        self.assertIn("mockDataFactory", answer.message)
        self.assertIn("record 回放", answer.message)
        self.assertIn("不要生成看似通用的 PB ADB 命令", answer.message)

    def test_exact_unsupported_complex_signal_does_not_emit_fake_adb_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guideengine = _write_minimal_guideengine_sources(root / "guideengine")
            config = BridgeConfig(
                data_dir=root,
                guideengine_repo=guideengine,
                knowledge=KnowledgeOptions(
                    enabled=True,
                    storage=root / "knowledge.sqlite",
                    sources=[
                        KnowledgeSourceOptions(id="guideengine-signals", type="guideengine_signal"),
                    ],
                ),
            )

            service = KnowledgeService(config)
            service.sync_all()
            answer = service.answer("SIGNAL_SR_PROPERTY 如何模拟")

        self.assertTrue(answer.success)
        self.assertEqual(answer.details["answer_type"], "adb_signal_custom_required")
        self.assertIn("SIGNAL_SR_PROPERTY", answer.message)
        self.assertIn("ByteArray/PB", answer.message)
        self.assertIn("新增 mockDataFactory", answer.message)
        self.assertIn("record 回放", answer.message)
        self.assertNotIn("--ei code 105007 --ei format 10", answer.message)


def _write_minimal_guideengine_sources(root: Path) -> Path:
    receiver = (
        root
        / "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/debug/broadcast"
        / "DataCenterBroadcastReceiver.java"
    )
    receiver.parent.mkdir(parents=True, exist_ok=True)
    receiver.write_text(
        """
public class DataCenterBroadcastReceiver {
    public static final String ACTION_MOCK = "com.xiaopeng.guide.action.mock.datacenter";
    private final Map<Integer, Function<String, Object>> mockDataFactory = new HashMap<Integer, Function<String, Object>>() {{
        put(SIGNAL_SR_XTHEME_VALUE, DataCenterBroadcastReceiver.this::mockXTheme);
    }};
    public void onReceive(Intent intent) {
        int code = intent.getIntExtra("code", -1);
        int format = intent.getIntExtra("format", -1);
        String value = intent.getStringExtra("value");
    }
    private XTheme mockXTheme(String value) {
        String[] strings = value.split(",");
        TimePeriod timePeriod = TimePeriod.Companion.fromInt(Integer.parseInt(strings[0]));
        ThemeMode themeMode = ThemeMode.Companion.fromInt(Integer.parseInt(strings[1]));
        return new XTheme(timePeriod, themeMode);
    }
}
""",
        encoding="utf-8",
    )
    ota = root / "module_core/subreality_biz/src/main/java/com/xiaopeng/ainavi/subreality_biz/ota/SrOtaService.kt"
    ota.parent.mkdir(parents=True, exist_ok=True)
    ota.write_text(
        """
private fun sendMsgToUnity(campaign: Int, upgrade: Int) {
    val msg = arrayOf(campaign, upgrade).contentToString()
    unityServiceInstance.sendMsgToUnity(SignalCode.SIGNAL_OTA_ST, SignalFormat.String, msg)
}
enum class OtaCampaign {
    OTA_CAMPAIGN_NONE,
    OTA_CAMPAIGN_SHOW,
}
enum class OtaUpgrade {
    OTA_UPGRADE_NONE,
    OTA_UPGRADE_SHOW,
    OTA_UPGRADE_AFTER_VIDEO,
}
""",
        encoding="utf-8",
    )
    proto = root / "module_floorcenter/module_proto/src/main/proto/signal.proto"
    proto.parent.mkdir(parents=True, exist_ok=True)
    proto.write_text(
        """
enum SignalFormat {
    String = 7;
    ByteArray = 10;
    JavaObject = 18;
    ProtoObject = 19;
}
// OTA状态
SIGNAL_OTA_ST = 105003;
// SR主题是否隐藏
SIGNAL_CLOUD_HIDE_SR_THEME = 102055;
// 车型主题是否隐藏
SIGNAL_CLOUD_HIDE_CAR_MODEL_THEME = 102056;
// XThemeMsg 当前主题及皮肤信息
// themeMode: 0白天，1黑夜
// timePeriod: 0早晨，1白天，2傍晚，3夜晚
SIGNAL_SR_XTHEME = 105004;
SIGNAL_SR_XTHEME_MSG = 105009;
// set_Property PB bytes
SIGNAL_SR_PROPERTY = 105007;
""",
        encoding="utf-8",
    )
    replay_receiver = root / "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/debug/broadcast/ReplayReceiver.kt"
    replay_receiver.write_text(
        """
class ReplayReceiver {
    companion object {
        const val REPLAY_FILE = "com.xiaopeng.guide.action.hmi.replay"
        const val REPLAY_FATH = "com.xiaopeng.guide.action.hmi.replayPath"
        const val START_RECORD = "com.xiaopeng.guide.action.hmi.startRecord"
    }
}
""",
        encoding="utf-8",
    )
    protocol_file = root / "module_floorcenter/module_datacenter/src/main/java/com/xiaopeng/guideengine/debug/protolFile/ProtocolFile.java"
    protocol_file.parent.mkdir(parents=True, exist_ok=True)
    protocol_file.write_text(
        """
public class ProtocolFile {
    private Object convertBytesToMsg(Signal.SignalFormat signalFormat, byte[] raw) {
        if (signalFormat == Signal.SignalFormat.ByteArray) {
            return raw;
        }
        return null;
    }
    private void dispatch(byte[] raw) {
        FloorCenterManager.Companion.getInstance().dataCenter.mockSignal(
            new XDataPropertyValue.Builder().setFormat(Signal.SignalFormat.ByteArray).setValue(raw).build()
        );
    }
}
""",
        encoding="utf-8",
    )
    return root
