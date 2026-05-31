from contextlib import contextmanager
from pathlib import Path
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from lark_agent_bridge.knowledge import KnowledgeService
from lark_agent_bridge.knowledge.models import KnowledgeChunk, SearchHit
from lark_agent_bridge.knowledge import source_investigation as source_investigation_module
from lark_agent_bridge.knowledge.source_investigation import SourceInvestigationRunner
from lark_agent_bridge.models import BridgeConfig, KnowledgeOptions, KnowledgeSourceOptions
from lark_agent_bridge.models import SourceInvestigationOptions


class _KnowledgeTestBase(unittest.TestCase):
    pass


__all__ = [
    'contextmanager',
    'Path',
    'json',
    'os',
    'subprocess',
    'tempfile',
    'time',
    'unittest',
    'patch',
    'KnowledgeService',
    'KnowledgeChunk',
    'SearchHit',
    'source_investigation_module',
    'SourceInvestigationRunner',
    'BridgeConfig',
    'KnowledgeOptions',
    'KnowledgeSourceOptions',
    'SourceInvestigationOptions',
    '_write_minimal_guideengine_sources',
    '_KnowledgeTestBase',
]



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
// 上下电  0下电 1上电 2远程上电 SIGNAL_McuIGStatus
SIGNAL_MCU_IG_ST = 36001;
// SR调试信号 190000 - 199999
SIGNAL_DEBUG_SHOW_LD_TILE = 190014;
// 展示 LD debug 信息
SIGNAL_DEBUG_SHOW_LD_DEBUG_INFO = 190015;
SIGNAL_DEBUG_LD_TILES_INFO = 190016; // C++层LD调试信息
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
