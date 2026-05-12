import unittest

from lark_agent_bridge.parser import (
    build_basic_chat_reply,
    parse_bug_request,
    parse_claude_skill_request,
    parse_direct_analysis_request,
    parse_perception_summary_request,
    parse_signal_request,
    should_use_omlx_chat,
)


class ParserTests(unittest.TestCase):
    def test_parse_chinese_signal_url_and_time(self):
        request = parse_signal_request("@bot /signal 132002 日志 https://example.com/log.zip 13-14 点")

        self.assertEqual(request.signal, "132002")
        self.assertEqual(request.since, "13-14")
        self.assertEqual(request.resources[0].kind, "url")
        self.assertEqual(request.resources[0].value, "https://example.com/log.zip")

    def test_parse_enum_and_file_attachment(self):
        request = parse_signal_request("调查 SIGNAL_X3D_LD_NORMAL_OVER_ALL_DATA，日志见附件 file_abc123")

        self.assertEqual(request.signal, "SIGNAL_X3D_LD_NORMAL_OVER_ALL_DATA")
        self.assertEqual(request.resources[0].kind, "file")

    def test_parse_alias(self):
        request = parse_signal_request("帮我看 LD normal 有没有到 Unity，日志 https://e.test/a.log")

        self.assertEqual(request.signal, "SIGNAL_X3D_LD_NORMAL_OVER_ALL_DATA")
        self.assertTrue(request.triggered)

    def test_missing_signal_on_triggered_request(self):
        request = parse_signal_request("/signal 日志 https://example.com/log.zip")

        self.assertEqual(request.error, "missing_signal")
        self.assertIsNone(request.signal)

    def test_basic_chat_identity_reply(self):
        reply = build_basic_chat_reply("你好，你是谁？")

        self.assertIsNotNone(reply)
        self.assertIn("Lark Agent Bridge", reply)
        self.assertIn("当前感知数据总结", reply)

    def test_basic_chat_help_reply(self):
        reply = build_basic_chat_reply("帮助")

        self.assertIsNotNone(reply)
        self.assertIn("/signal", reply)
        self.assertIn("/perception-summary", reply)
        self.assertIn("分析启动和卡顿 file_xxx", reply)

    def test_parse_claude_skill_prefix(self):
        request = parse_claude_skill_request("@bot /skill 请分析这个日志排查流程")

        self.assertTrue(request.triggered)
        self.assertEqual(request.prompt, "请分析这个日志排查流程")

    def test_parse_claude_skill_prefix_without_slash(self):
        request = parse_claude_skill_request("@bot skill 请分析这个日志排查流程")

        self.assertTrue(request.triggered)
        self.assertEqual(request.prompt, "请分析这个日志排查流程")

    def test_parse_claude_skill_keyword_must_be_first(self):
        request = parse_claude_skill_request("@bot 帮我 skill 请分析这个日志排查流程")

        self.assertFalse(request.triggered)

    def test_parse_bug_request(self):
        request = parse_bug_request(
            "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722 调查3D启动时序"
        )

        self.assertTrue(request.triggered)
        self.assertEqual(
            request.bug_url,
            "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6987292722",
        )
        self.assertEqual(request.prompt, "调查3D启动时序")

    def test_parse_perception_summary_prefix(self):
        request = parse_perception_summary_request("@bot perception-summary 总结当前感知数据")

        self.assertTrue(request.triggered)
        self.assertEqual(request.prompt, "总结当前感知数据")

    def test_parse_perception_summary_prefix_without_slash(self):
        request = parse_perception_summary_request("@bot perception 总结当前感知数据")

        self.assertTrue(request.triggered)
        self.assertEqual(request.prompt, "总结当前感知数据")

    def test_parse_perception_summary_extracts_file_resource(self):
        request = parse_perception_summary_request("@bot /perception-summary 总结当前感知数据 file_abc123")

        self.assertTrue(request.triggered)
        self.assertEqual(request.resources[0].kind, "file")

    def test_parse_direct_analysis_request(self):
        request = parse_direct_analysis_request("@bot 分析启动和卡顿 file_abc123 11:30")

        self.assertTrue(request.triggered)
        self.assertEqual(request.resources[0].kind, "file")
        self.assertIn("11:30", request.prompt)

    def test_omlx_chat_candidate_for_simple_question(self):
        self.assertTrue(should_use_omlx_chat("帮我解释一下什么是 token？"))
        self.assertFalse(should_use_omlx_chat("这条消息当前没有实现对应能力"))
        self.assertFalse(should_use_omlx_chat("/signal 132002 日志 https://example.com/log.zip"))


if __name__ == "__main__":
    unittest.main()
