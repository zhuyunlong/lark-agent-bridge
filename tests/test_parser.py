from pathlib import Path
import tempfile
import unittest

from lark_agent_bridge.parser import (
    parse_app_server_investigation_request,
    build_basic_chat_reply,
    find_resources,
    parse_addr2line_request,
    parse_bug_request,
    parse_claude_skill_request,
    parse_direct_analysis_request,
    parse_followup_action,
    parse_perception_summary_request,
    parse_requirement_analysis_request,
    parse_report_followup_request,
    parse_rom_version_lookup_request,
    parse_signal_request,
    parse_source_analysis_request,
    should_use_omlx_chat,
)
from lark_agent_bridge.signal_resolver import SignalResolver


class ParserTests(unittest.TestCase):
    def test_parse_chinese_signal_url_and_time(self):
        request = parse_signal_request("@bot 调查 132002 信号链路 日志 https://example.com/log.zip 13-14 点")

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

    def test_parse_signal_request_ignores_urls_and_css_hex_colors_as_signal_codes(self):
        request = parse_signal_request(
            "信号链路总览 http://10.99.149.127:8765/reports/e1c56e6bb411636da68eea2e0c91f5cc/ "
            "CSS --green:#059669; bug https://project.feishu.cn/xpfailuremgmt/buglo/detail/6979499593"
        )

        self.assertIsNone(request.signal)

    def test_parse_bare_signal_name_without_forcing_signal_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            signal_proto = repo / "module_floorcenter/module_proto/src/main/proto/signal.proto"
            signal_proto.parent.mkdir(parents=True, exist_ok=True)
            signal_proto.write_text(
                "enum SignalCode {\n  SIGNAL_VCU_ELECTRICIT_PERCENT = 40019;\n}\n",
                encoding="utf-8",
            )
            resolver = SignalResolver(repo)

            request = parse_signal_request("帮我看 VCU_ELECTRICIT_PERCENT 为什么不对", signal_resolver=resolver)

        self.assertEqual(request.signal, "SIGNAL_VCU_ELECTRICIT_PERCENT")
        self.assertTrue(request.triggered)

    def test_rom_version_does_not_fuzzy_match_as_signal(self):
        text = "XMARTM3EUD03E5_V6.2.2.6808_20260424002644.3_REV01_USERDEBUG 调用rom-version skill 找下导航版本"

        request = parse_signal_request(text)

        self.assertFalse(request.triggered)
        self.assertIsNone(request.signal)

    def test_parse_rom_version_lookup_request(self):
        text = "XMARTM3EUD03E5_V6.2.2.6808_20260424002644.3_REV01_USERDEBUG 调用rom-version skill 找下导航版本"

        request = parse_rom_version_lookup_request(text)

        self.assertTrue(request.triggered)
        self.assertEqual(
            request.rom_version,
            "XMARTM3EUD03E5_V6.2.2.6808_20260424002644.3_REV01_USERDEBUG",
        )
        self.assertIn("导航版本", request.prompt)

    def test_parse_addr2line_request_with_rom_and_stack(self):
        text = (
            "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release 反解地址\n"
            "#05 pc 0000000000f385e4 /system/app/xp_envirodrive/lib/arm64/libunity.so\n"
            "#06 pc 00000000010fa7f0 /system/app/xp_envirodrive/lib/arm64/libunity.so"
        )

        request = parse_addr2line_request(text)

        self.assertTrue(request.triggered)
        self.assertEqual(
            request.rom_version,
            "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release",
        )
        self.assertEqual(request.target, "auto")
        self.assertIn("#05 pc 0000000000f385e4", request.addr_text)
        self.assertIsNone(request.error)

    def test_parse_addr2line_request_without_symbol_version_still_triggers(self):
        text = (
            "反解地址\n"
            "#05 pc 0000000000f385e4 /system/app/xp_envirodrive/lib/arm64/libunity.so"
        )

        request = parse_addr2line_request(text)

        self.assertTrue(request.triggered)
        self.assertEqual(request.error, "missing_symbol_version")
        self.assertIn("libunity.so", request.addr_text)

    def test_parse_addr2line_request_accepts_bare_hex_so_stack(self):
        text = (
            "0000000000085304 /apex/com.android.runtime/lib64/bionic/libc.so (__memcpy+276)\n"
            "00000000049f7194 /system/app/xp_envirodrive-mainland/lib/arm64/libil2cpp.so\n"
            "0000000004872b6c /system/app/xp_envirodrive-mainland/lib/arm64/libil2cpp.so\n"
            "堆栈解析"
        )

        request = parse_addr2line_request(text)

        self.assertTrue(request.triggered)
        self.assertEqual(request.error, "missing_symbol_version")
        self.assertIn("00000000049f7194", request.addr_text)
        self.assertIn("libil2cpp.so", request.addr_text)

    def test_parse_addr2line_request_accepts_single_line_rom_and_markdown_so_stack(self):
        text = (
            "XMARTQTZHG01E5_V6.2.61.6806_20260529152853.2_REV01_USERDEBUG_Alpha "
            "000000000118520c /system/app/xp_envirodrive-mainland/lib/arm64/"
            "[libunity.so](http://libunity.so/) "
            "00000000011856bc /system/app/xp_envirodrive-mainland/lib/arm64/"
            "[libunity.so](http://libunity.so/) "
            "反解堆栈"
        )

        request = parse_addr2line_request(text)

        self.assertTrue(request.triggered)
        self.assertIsNone(request.error)
        self.assertEqual(
            request.rom_version,
            "XMARTQTZHG01E5_V6.2.61.6806_20260529152853.2_REV01_USERDEBUG_Alpha",
        )
        self.assertIn("000000000118520c", request.addr_text)
        self.assertIn("libunity.so", request.addr_text)

    def test_parse_addr2line_request_ignores_tombstone_mapping_lines(self):
        text = (
            "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release 反解堆栈\n"
            "#00 pc 0000000000123456 /apex/lib64/libbar.so\n"
            "7f8a2c3000-7f8a2c4000 r--p 00000000 fd:00 12345678  /system/lib64/libfoo.so"
        )

        request = parse_addr2line_request(text)

        self.assertTrue(request.triggered)
        self.assertIn("0000000000123456", request.addr_text)
        self.assertIn("libbar.so", request.addr_text)
        self.assertNotIn("12345678", request.addr_text)
        self.assertNotIn("libfoo.so", request.addr_text)

    def test_parse_addr2line_request_does_not_extract_substring_from_long_hex_token(self):
        request = parse_addr2line_request(
            "反解堆栈\n"
            "deadbeefdeadbeef001122334455667788 /system/lib64/liblong.so",
            allow_missing_address=True,
        )

        self.assertTrue(request.triggered)
        self.assertEqual(request.addr_text, "")
        self.assertEqual(request.error, "missing_address")

    def test_parse_addr2line_request_recognizes_stack_analysis_intent_variants(self):
        rom = "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release"
        variants = (
            "反解符合",
            "分解符号表",
            "分解导航符号表",
            "堆栈分析",
            "Unity堆栈",
            "3D堆栈",
            "帮我看下 crash stack",
            "解析 tombstone",
        )

        for phrase in variants:
            with self.subTest(phrase=phrase):
                request = parse_addr2line_request(f"{rom} {phrase}", allow_missing_address=True)

                self.assertTrue(request.triggered)
                self.assertEqual(request.rom_version, rom)
                self.assertEqual(request.error, "missing_address")

    def test_parse_addr2line_request_with_file_intent_does_not_treat_prompt_as_address(self):
        rom = "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release"

        request = parse_addr2line_request(f"{rom} 反解符号表", allow_missing_address=True)

        self.assertTrue(request.triggered)
        self.assertEqual(request.addr_text, "")
        self.assertEqual(request.error, "missing_address")

    def test_parse_addr2line_request_extracts_log_folder_hint(self):
        request = parse_addr2line_request("请用 log2 里的日志反解符号表", allow_missing_address=True)

        self.assertTrue(request.triggered)
        self.assertEqual(request.log_folder, "log2")

    def test_parse_addr2line_request_extracts_fault_time_hint(self):
        request = parse_addr2line_request("5月22日 7:46 反解符号表", allow_missing_address=True)

        self.assertTrue(request.triggered)
        self.assertEqual(request.fault_time, "05-22 07:46")

    def test_parse_addr2line_request_does_not_confuse_non_stack_requests(self):
        for text in (
            "分析3D生命周期",
            "Unity 场景是什么",
            "这个信号符合预期吗",
            "XMARTQGZHE29E5_V6.1.0.8810_20260327200937.9_REV01_USER_Release 查符号表地址",
        ):
            with self.subTest(text=text):
                request = parse_addr2line_request(text, allow_missing_address=True)

                self.assertFalse(request.triggered)

    def test_source_analysis_intent_with_crash_keyword_does_not_trigger_addr2line(self):
        # "源码分析 找crash原因" is an explicit source-code investigation, not a
        # tombstone stack reverse lookup. Even with a log resource present
        # (allow_missing_address=True) and a "crash" keyword, addr2line must
        # defer so the source/direct analysis route can handle the log.
        request = parse_addr2line_request(
            "源码分析 找出最后一次crash的原因", allow_missing_address=True
        )
        self.assertFalse(request.triggered)

    def test_explicit_addr2line_term_still_triggers_even_with_source_keyword(self):
        # A genuine reverse-lookup request must still trigger even if it mentions
        # 源码, because "反解符号表" is an explicit addr2line term.
        request = parse_addr2line_request(
            "基于源码 反解符号表", allow_missing_address=True
        )
        self.assertTrue(request.triggered)

    def test_parse_followup_action_recognizes_generic_retry_terms(self):
        for text in ("重试一次", "再来一次", "重新跑", "再查一次", "重新分析", "重新分析下"):
            with self.subTest(text=text):
                self.assertEqual(parse_followup_action(text), "retry")

    def test_parse_followup_action_recognizes_generic_continue_terms(self):
        for text in ("继续", "继续分析", "接着查"):
            with self.subTest(text=text):
                self.assertEqual(parse_followup_action(text), "continue")

    def test_parse_followup_action_treats_question_as_ask(self):
        self.assertEqual(parse_followup_action("这个结论是什么意思"), "ask")

    def test_help_bug_theme_example_is_a_valid_bug_request(self):
        text = "@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6991604970 分析主题变化"

        request = parse_bug_request(text)

        self.assertTrue(request.triggered)
        self.assertEqual(request.bug_url, "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6991604970")
        self.assertEqual(request.prompt, "分析主题变化")

    def test_signal_slash_command_is_not_a_default_trigger(self):
        request = parse_signal_request("/signal 日志 https://example.com/log.zip")

        self.assertFalse(request.triggered)
        self.assertIsNone(request.signal)

    def test_basic_chat_identity_reply(self):
        reply = build_basic_chat_reply("你好，你是谁？")

        self.assertIsNotNone(reply)
        self.assertIn("Lark Agent Bridge", reply)
        self.assertIn("当前感知数据总结", reply)
        self.assertIn("个人知识库问答", reply)

    def test_basic_chat_help_reply(self):
        reply = build_basic_chat_reply("帮助")

        self.assertIsNotNone(reply)
        self.assertIn("常见触发方式", reply)
        self.assertIn("https://project.feishu.cn/xpfailuremgmt/buglo/detail/6991604970 分析主题变化", reply)
        self.assertIn("知识库 OTA信号如何模拟", reply)
        self.assertIn("查知识 主题信号如何模拟", reply)
        self.assertIn("/kb 仍兼容", reply)
        self.assertIn("需求源码分析", reply)
        self.assertIn("仓库源码分析", reply)
        self.assertIn("结合源码分析是否可行", reply)
        self.assertIn("UnityReady", reply)
        self.assertIn("ROM/导航版本", reply)
        self.assertIn("找下导航版本", reply)
        self.assertNotIn("SIGNAL_OTA_ST 怎么模拟", reply)
        self.assertIn("个人知识库", reply)
        self.assertIn("signal-chain-analyzer", reply)
        self.assertIn("SIGNAL_X3D_LD_NORMAL_OVER_ALL_DATA", reply)
        self.assertNotIn("/perception-summary", reply)
        self.assertNotIn("/skill", reply)
        self.assertNotIn("/claude", reply)
        self.assertIn("分析启动和卡顿 file_xxx", reply)

    def test_basic_chat_capability_question_lists_current_capabilities(self):
        reply = build_basic_chat_reply("你能做什么")

        self.assertIsNotNone(reply)
        self.assertIn("Bug 分析", reply)
        self.assertIn("需求源码分析", reply)
        self.assertIn("个人知识库", reply)
        self.assertIn("普通聊天", reply)

    def test_basic_chat_does_not_match_greeting_inside_url(self):
        # URLs containing the word "vehicle" must NOT trigger greeting fallback
        # because "hi" is a substring of "vehicle".
        reply = build_basic_chat_reply(
            "@朱云龙的飞书 CLI [05-28 15:23:11](http://test.manage.prod.xiaopeng.link/driving/oss/browser?prefix=test_vehicle_data_warehouse/) 调查靠边停车数据"
        )
        self.assertIsNone(reply)

    def test_basic_chat_does_not_match_short_ascii_term_inside_word(self):
        # Defend against false positives for: hi, help, hello, etc. inside other words.
        for noisy in ("vehicle", "achievement", "https://example.com/help", "[link](https://x.com/help)"):
            self.assertIsNone(build_basic_chat_reply(noisy), f"unexpected match for {noisy!r}")

    def test_basic_chat_still_matches_legitimate_greeting(self):
        for greet in ("hi", "Hello", "你好", "在吗", "hi there!"):
            self.assertIsNotNone(build_basic_chat_reply(greet), f"missed match for {greet!r}")
        self.assertIsNotNone(build_basic_chat_reply("help"))
        self.assertIsNotNone(build_basic_chat_reply("who are you"))

    def test_pullover_chain_request_with_url_routes_to_direct_analysis(self):
        # The exact failing scenario: at-mention + Feishu-injected timestamped URL
        # block + Chinese trigger phrase. Must not be hijacked by basic_chat.
        from lark_agent_bridge.parser import looks_like_direct_analysis_prompt
        text = (
            "@朱云龙的飞书 CLI [05-28 15:23:11]"
            "(http://test.manage.prod.xiaopeng.link/driving/oss/browser?prefix=test_vehicle_data_warehouse/) "
            "调查靠边停车数据"
        )
        self.assertIsNone(build_basic_chat_reply(text))
        # With a referenced file resource present, direct analysis should be triggered.
        self.assertTrue(looks_like_direct_analysis_prompt(text, resources_present=True))

    def test_parse_claude_skill_prefix_requires_opt_in(self):
        request = parse_claude_skill_request("@bot /skill 请分析这个日志排查流程")

        self.assertFalse(request.triggered)

    def test_parse_claude_skill_prefix_when_configured(self):
        request = parse_claude_skill_request("@bot /skill 请分析这个日志排查流程", trigger_prefixes=["/skill"])

        self.assertTrue(request.triggered)
        self.assertEqual(request.prompt, "请分析这个日志排查流程")

    def test_parse_claude_skill_prefix_without_slash_requires_opt_in(self):
        request = parse_claude_skill_request("@bot skill 请分析这个日志排查流程")

        self.assertFalse(request.triggered)

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

    def test_parse_bug_request_removes_markdown_link_shell_from_prompt(self):
        request = parse_bug_request(
            "[ [缺陷] 【F01】车机大屏页面卡住-SB174577](https://project.feishu.cn/xpfailuremgmt/buglo/detail/6979499593) 分析3D生命周期"
        )

        self.assertTrue(request.triggered)
        self.assertEqual(
            request.bug_url,
            "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6979499593",
        )
        self.assertEqual(request.prompt, "分析3D生命周期")

    def test_parse_bug_request_stops_before_chinese_punctuation_prompt(self):
        request = parse_bug_request(
            "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6993883118，调查 SIGNAL_CTL_XPILOT_ADAS_LD_STATE 信号链路和现状"
        )

        self.assertTrue(request.triggered)
        self.assertEqual(
            request.bug_url,
            "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6993883118",
        )
        self.assertEqual(request.prompt, "调查 SIGNAL_CTL_XPILOT_ADAS_LD_STATE 信号链路和现状")

    def test_parse_perception_summary_prefix_is_not_a_default_trigger(self):
        request = parse_perception_summary_request("@bot /perception-summary file_abc123")

        self.assertFalse(request.triggered)

    def test_parse_perception_summary_natural_language(self):
        request = parse_perception_summary_request("@bot perception-summary 总结当前感知数据")

        self.assertTrue(request.triggered)
        self.assertEqual(request.prompt, "perception-summary 总结当前感知数据")

    def test_parse_perception_summary_keyword_without_slash(self):
        request = parse_perception_summary_request("@bot perception 总结当前感知数据")

        self.assertTrue(request.triggered)
        self.assertEqual(request.prompt, "perception 总结当前感知数据")

    def test_parse_perception_summary_data_chain_investigation(self):
        request = parse_perception_summary_request("@bot 时间点6月8日 19:17 感知数据链路调查")

        self.assertTrue(request.triggered)
        self.assertEqual(request.prompt, "时间点6月8日 19:17 感知数据链路调查")

    def test_parse_perception_summary_extracts_file_resource(self):
        request = parse_perception_summary_request("@bot 总结当前感知数据 file_abc123")

        self.assertTrue(request.triggered)
        self.assertEqual(request.resources[0].kind, "file")

    def test_parse_perception_summary_keeps_full_lark_file_v3_key(self):
        request = parse_perception_summary_request(
            '@bot 总结当前感知数据 <file key="file_v3_0011s_6d5d723c-ec0b-44f3-9908-a02be496b54g" name="Log.zip"/>'
        )

        self.assertTrue(request.triggered)
        self.assertEqual(len(request.resources), 1)
        self.assertEqual(
            request.resources[0].value,
            "file_v3_0011s_6d5d723c-ec0b-44f3-9908-a02be496b54g",
        )
        self.assertEqual(request.resources[0].display_name, "Log.zip")

    def test_find_resources_extracts_drive_folder_url_as_folder_resource(self):
        resources = find_resources("日志目录 https://example.feishu.cn/drive/folder/fldcnlog123")

        self.assertEqual(len(resources), 1)
        self.assertEqual(resources[0].kind, "folder")
        self.assertEqual(resources[0].value, "fldcnlog123")

    def test_parse_direct_analysis_request(self):
        request = parse_direct_analysis_request("@bot 分析启动和卡顿 file_abc123 11:30")

        self.assertTrue(request.triggered)
        self.assertEqual(request.resources[0].kind, "file")
        self.assertIn("11:30", request.prompt)

    def test_parse_direct_analysis_request_does_not_steal_perception_summary(self):
        request = parse_direct_analysis_request("@bot 总结当前感知数据 file_abc123")

        self.assertFalse(request.triggered)
        self.assertEqual(request.resources[0].kind, "file")

    def test_parse_direct_analysis_request_requires_analysis_intent_for_inline_file(self):
        request = parse_direct_analysis_request("@bot 这个是什么 file_abc123")

        self.assertFalse(request.triggered)
    def test_parse_direct_analysis_request_accepts_timed_diagnostic_question(self):
        request = parse_direct_analysis_request(
            "@bot 时间点15:11左右 为啥退出有图进入了无图？ file_v3_diagnostic_log"
        )

        self.assertTrue(request.triggered)
        self.assertEqual(request.resources[0].value, "file_v3_diagnostic_log")
        self.assertEqual(request.resources[0].kind, "file")

    def test_source_analysis_request_requires_source_intent_and_target(self):
        request = parse_source_analysis_request("@bot 基于源码分析 UnityReady 信号链路如何监听")

        self.assertTrue(request.triggered)
        self.assertEqual(request.target, "UnityReady")
        self.assertEqual(request.source_mode, "repository_only")
        self.assertIn("swimlane", request.diagram_kinds)

    def test_source_analysis_request_does_not_steal_simple_chat(self):
        request = parse_source_analysis_request("@bot UnityReady 是什么")

        self.assertFalse(request.triggered)

    def test_source_analysis_request_does_not_steal_bug_link(self):
        request = parse_source_analysis_request(
            "@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6993883118 基于源码分析 UnityReady"
        )

        self.assertFalse(request.triggered)

    def test_source_analysis_request_does_not_steal_file_analysis(self):
        request = parse_source_analysis_request("@bot 基于源码分析 UnityReady file_abc123")

        self.assertFalse(request.triggered)

    def test_report_followup_request_recognizes_diagram_output(self):
        request = parse_report_followup_request("基于这个回复画出泳道图和时序图")

        self.assertTrue(request.triggered)
        self.assertIn("swimlane", request.diagram_kinds)
        self.assertIn("sequence", request.diagram_kinds)
        self.assertTrue(request.output_html)

    def test_app_server_investigation_auto_trigger_requires_leading_token(self):
        request = parse_app_server_investigation_request(
            "@bot auto https://project.feishu.cn/xpfailuremgmt/buglo/detail/6993883118 调查下 3D 生命周期"
        )

        self.assertTrue(request.triggered)
        self.assertEqual(request.trigger_mode, "auto")
        self.assertEqual(request.trigger_term.lower(), "auto")
        self.assertEqual(request.bug_url, "https://project.feishu.cn/xpfailuremgmt/buglo/detail/6993883118")
        self.assertEqual(request.prompt, "调查下 3D 生命周期")

    def test_app_server_investigation_auto_trigger_is_case_insensitive(self):
        request = parse_app_server_investigation_request(
            "@bot AUTO https://project.feishu.cn/xpfailuremgmt/buglo/detail/6993883118 调查 3D 生命周期"
        )

        self.assertTrue(request.triggered)
        self.assertEqual(request.trigger_mode, "auto")

    def test_app_server_investigation_auto_trigger_does_not_match_nonleading_token(self):
        request = parse_app_server_investigation_request(
            "@bot 请 auto https://project.feishu.cn/xpfailuremgmt/buglo/detail/6993883118 调查 3D 生命周期"
        )

        self.assertFalse(request.triggered)

    def test_app_server_investigation_free_trigger_matches_independent_term(self):
        request = parse_app_server_investigation_request(
            "@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6993883118 全技能分析 调查 3D 生命周期"
        )

        self.assertTrue(request.triggered)
        self.assertEqual(request.trigger_mode, "free")
        self.assertEqual(request.trigger_term, "全技能分析")
        self.assertEqual(request.prompt, "调查 3D 生命周期")

    def test_app_server_investigation_free_trigger_requires_independent_term(self):
        request = parse_app_server_investigation_request(
            "@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6993883118 全技能分析一下 调查 3D 生命周期"
        )

        self.assertFalse(request.triggered)

    def test_story_link_with_requirement_wording_routes_to_requirement_analysis_not_direct(self):
        text = (
            "@bot https://project.feishu.cn/adcvehicleroject/story/detail/6979403058 "
            "这是需求链接，结合源码分析是否可行"
        )

        requirement = parse_requirement_analysis_request(text)
        direct = parse_direct_analysis_request(text)

        self.assertTrue(requirement.triggered)
        self.assertEqual(requirement.workitem.project_key, "adcvehicleroject")
        self.assertEqual(requirement.workitem.work_item_type, "story")
        self.assertEqual(requirement.workitem.work_item_id, "6979403058")
        self.assertFalse(direct.triggered)

    def test_bug_link_with_source_wording_still_routes_to_bug_not_requirement(self):
        text = "@bot https://project.feishu.cn/xpfailuremgmt/buglo/detail/6993883118 基于源码分析 UnityReady"

        requirement = parse_requirement_analysis_request(text)
        bug = parse_bug_request(text)

        self.assertTrue(bug.triggered)
        self.assertFalse(requirement.triggered)

    def test_generic_url_direct_analysis_is_not_blocked_by_requirement_parser(self):
        text = "@bot https://example.com/log.zip 分析这份日志"

        requirement = parse_requirement_analysis_request(text)
        direct = parse_direct_analysis_request(text)

        self.assertFalse(requirement.triggered)
        self.assertTrue(direct.triggered)

    def test_workitem_link_without_analysis_intent_does_not_trigger_requirement_analysis(self):
        text = "@bot https://project.feishu.cn/demo/story/detail/12345"

        requirement = parse_requirement_analysis_request(text)

        self.assertFalse(requirement.triggered)

    def test_omlx_chat_candidate_for_simple_question(self):
        self.assertTrue(should_use_omlx_chat("帮我解释一下什么是 token？"))
        self.assertFalse(should_use_omlx_chat("这条消息当前没有实现对应能力"))
        self.assertFalse(should_use_omlx_chat("/signal 132002 日志 https://example.com/log.zip"))


if __name__ == "__main__":
    unittest.main()
