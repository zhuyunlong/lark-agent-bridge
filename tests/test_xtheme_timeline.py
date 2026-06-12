from pathlib import Path
import importlib.util
import tempfile
import unittest


def _load_xtheme_analyzer():
    script = (
        Path("/Users/zhuyl/Documents/workspace")
        / ".ai/skills/xtheme-analyzer/scripts/analyze_xtheme.py"
    )
    spec = importlib.util.spec_from_file_location("xtheme_analyzer_under_test", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load xtheme analyzer: {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class XThemeTimelineTests(unittest.TestCase):
    def test_complete_timeline_is_sorted_across_event_categories(self):
        analyzer = _load_xtheme_analyzer()
        report_data = {
            "inits": [
                {
                    "ts": "05-18 11:19:55.755",
                    "uiMode": 1,
                    "themeMode": 0,
                    "timeInfo": "TIME_DAY",
                    "xtheme": "1,0,[Basic,,,],[,,,]",
                    "file": "main.log",
                    "line": 30,
                }
            ],
            "ui_changes": [
                {
                    "ts": "05-18 11:13:36.146",
                    "uiMode": 1,
                    "themeMode": 0,
                    "file": "main.log",
                    "line": 20,
                }
            ],
            "theme_switches": [],
            "theme_msgs": [
                {
                    "ts": "05-18 11:10:57.607",
                    "xtheme": "1,0,[Basic,,,],[,,,]",
                    "file": "main.log",
                    "line": 10,
                }
            ],
            "twilight_fails": [],
            "timer_gaps": [],
            "calc_times": [],
            "strategies": [],
        }

        html = analyzer._render_timeline(report_data)

        self.assertLess(html.index("05-18 11:10:57.607"), html.index("05-18 11:13:36.146"))
        self.assertLess(html.index("05-18 11:13:36.146"), html.index("05-18 11:19:55.755"))

    def test_report_defaults_to_problem_time_snapshot_not_multiday_or_morning(self):
        analyzer = _load_xtheme_analyzer()
        result = {
            "inits": [],
            "calc_times": [
                {
                    "ts": "05-18 11:18:30.517",
                    "md": "05-18",
                    "cur": 678,
                    "sunrise": 360,
                    "sunset": 1200,
                    "calTime": "TIME_DAY",
                    "finalTime": "TIME_DAY",
                    "themeMode": 0,
                    "file": "main.log",
                    "line": 5,
                    "raw": "calculateTimeInfo",
                }
            ],
            "time_checks": [],
            "ui_changes": [
                {"ts": "05-18 11:18:29.360", "md": "05-18", "uiMode": 1, "themeMode": 0, "file": "main.log", "line": 4, "raw": "ui mode change"}
            ],
            "theme_changes": [],
            "theme_msgs": [],
            "sunrise_sets": [
                {"ts": "05-18 11:18:29.343", "md": "05-18", "sunriseMin": 360, "sunsetMin": 1200, "file": "main.log", "line": 3}
            ],
            "twilights": [],
            "twilight_fails": [],
            "calc_warns": [],
            "theme_elems": [],
            "strategies": [
                {"ts": "05-18 11:18:31.132", "md": "05-18", "code": 1076, "themeMode": 0, "timePeriod": 1, "detail": "SrThemeSkin:Basic", "file": "main.log", "line": 6}
            ],
            "get_msgs": [],
            "langs": [],
            "cultures": [],
            "theme_type_parses": [
                {"ts": "05-18 11:18:28.908", "md": "05-18", "uiMode": 19, "autoMode": 1, "file": "main.log", "line": 1, "raw": "parseTheme"}
            ],
            "theme_helper_configs": [
                {"ts": "05-18 11:18:28.918", "md": "05-18", "oldTheme": "THEME_DAY_NORMAL", "newTheme": "THEME_DAY_NORMAL", "appUiMode": 19, "file": "main.log", "line": 2, "raw": "onConfigurationChanged"}
            ],
            "theme_collects": [],
            "theme_switches": [],
            "timer_gaps": [],
        }

        data = analyzer.build_report_data(
            result,
            target_time="2026-05-18 11:19",
            request_text="调查主题变化",
        )
        html = analyzer.render_html(Path("/tmp/input"), Path("/tmp/input"), result, data)

        self.assertFalse(data["show_time_period_special"])
        self.assertIn("问题时刻前 XTheme 计算输入快照", html)
        self.assertIn("ThemeType.parseTheme", html)
        self.assertNotIn("<h2>多日分析</h2>", html)
        self.assertNotIn("核心结论: 晨曦时光仅在 Auto 模式下出现", html)

    def test_time_period_special_only_when_request_mentions_dawn_or_evening(self):
        analyzer = _load_xtheme_analyzer()
        result = {
            "inits": [],
            "calc_times": [],
            "time_checks": [],
            "ui_changes": [],
            "theme_changes": [],
            "theme_msgs": [],
            "sunrise_sets": [],
            "twilights": [],
            "twilight_fails": [],
            "calc_warns": [],
            "theme_elems": [],
            "strategies": [],
            "get_msgs": [],
            "langs": [],
            "cultures": [],
            "theme_type_parses": [],
            "theme_helper_configs": [],
            "theme_collects": [],
            "theme_switches": [],
            "timer_gaps": [],
        }

        data = analyzer.build_report_data(result, request_text="为什么没有晨曦/傍晚")
        html = analyzer.render_html(Path("/tmp/input"), Path("/tmp/input"), result, data)

        self.assertTrue(data["show_time_period_special"])
        self.assertIn("早晚时段专项", html)

    def test_analyze_logs_parses_themehelper_source_chain_events(self):
        analyzer = _load_xtheme_analyzer()

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "main.log"
            log.write_text(
                "\n".join(
                    [
                        "05-18 11:18:28.908 15140 15275 511644 I NAV_ThemeType: parseTheme uiMode: 19, autoMode: 1",
                        "05-18 11:18:28.918 15140 15140 511654 I NAV_ThemeHelper: onConfigurationChanged : THEME_DAY_NORMAL newTheme:THEME_NIGHT_NORMAL appCtx.uimode=19 resources=android.content.res.Resources@c9f26d6",
                        "05-18 11:18:29.419 15140 13782 438155 I NAV_XuiConditionHelper: collect themeState: THEME_NIGHT_NORMAL, dayNightType: THEME_NIGHT_NORMAL",
                    ]
                ),
                encoding="utf-8",
            )

            result = analyzer.analyze_logs([log])

        self.assertEqual(result["theme_type_parses"][0]["autoMode"], 1)
        self.assertEqual(result["theme_helper_configs"][0]["newTheme"], "THEME_NIGHT_NORMAL")
        self.assertEqual(result["theme_collects"][0]["dayNightType"], "THEME_NIGHT_NORMAL")

    def test_timer_gap_does_not_join_different_log_dates(self):
        analyzer = _load_xtheme_analyzer()

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "main.log"
            log.write_text(
                "\n".join(
                    [
                        "01-01 08:00:51.626 15140 15308 1 I NAV_XuiConditionHelper: calculateTimeInfo, cur:[480], sunrise_utc:[360], sunset_utc:[1200], CalTimeInfo:[TIME_DAY], FinalTimeInfo:[TIME_DAY], themeMode:[0]",
                        "05-18 11:10:41.373 15140 15308 2 I NAV_XuiConditionHelper: calculateTimeInfo, cur:[670], sunrise_utc:[360], sunset_utc:[1200], CalTimeInfo:[TIME_DAY], FinalTimeInfo:[TIME_DAY], themeMode:[0]",
                    ]
                ),
                encoding="utf-8",
            )

            result = analyzer.analyze_logs([log])

        self.assertEqual(result["timer_gaps"], [])

    def test_report_ignores_events_after_target_time_for_verdict(self):
        analyzer = _load_xtheme_analyzer()
        result = {
            "inits": [],
            "calc_times": [],
            "time_checks": [],
            "ui_changes": [],
            "theme_changes": [],
            "theme_msgs": [],
            "sunrise_sets": [],
            "twilights": [],
            "twilight_fails": [],
            "calc_warns": [
                {
                    "ts": "05-18 14:02:53.330",
                    "md": "05-18",
                    "timePeriod": "TIME_DAY",
                    "file": "main.log",
                    "line": 100,
                    "raw": "after target",
                }
            ],
            "theme_elems": [],
            "strategies": [],
            "get_msgs": [],
            "langs": [],
            "cultures": [],
            "theme_type_parses": [],
            "theme_helper_configs": [],
            "theme_collects": [],
            "theme_switches": [],
            "timer_gaps": [],
        }

        data = analyzer.build_report_data(result, target_time="2026-05-18 11:19", request_text="调查主题变化")
        html = analyzer.render_html(Path("/tmp/input"), Path("/tmp/input"), result, data)

        self.assertEqual(data["verdict"]["sev"], "green")
        self.assertEqual(data["issues"], [])
        self.assertNotIn("05-18 14:02:53.330", html)

    def test_report_verdict_prioritizes_target_focus_over_background_timer_gaps(self):
        analyzer = _load_xtheme_analyzer()
        result = {
            "inits": [],
            "calc_times": [
                {
                    "ts": "06-09 05:50:18.557",
                    "md": "06-09",
                    "cur": 350,
                    "sunrise": 350,
                    "sunset": 1193,
                    "calTime": "TIME_DAY",
                    "finalTime": "TIME_DAY",
                    "themeMode": 0,
                    "file": "main.log",
                    "line": 10,
                    "raw": "early timer",
                },
                {
                    "ts": "06-09 21:44:54.074",
                    "md": "06-09",
                    "cur": 1304,
                    "sunrise": 350,
                    "sunset": 1193,
                    "calTime": "TIME_NIGHT",
                    "finalTime": "TIME_DAY",
                    "themeMode": 0,
                    "file": "main.log",
                    "line": 118197,
                    "raw": "target calc",
                },
            ],
            "time_checks": [],
            "ui_changes": [],
            "theme_changes": [
                {
                    "ts": "06-09 21:44:57.959",
                    "md": "06-09",
                    "themeMode": 1,
                    "newThemeMode": 1,
                    "file": "main.log",
                    "line": 118307,
                    "raw": "setThemeMode",
                }
            ],
            "theme_msgs": [],
            "sunrise_sets": [
                {"ts": "06-09 21:25:45.429", "md": "06-09", "sunriseMin": 350, "sunsetMin": 1193, "file": "main.log", "line": 91694}
            ],
            "twilights": [],
            "twilight_fails": [],
            "calc_warns": [],
            "theme_elems": [],
            "strategies": [
                {
                    "ts": "06-09 21:44:57.959",
                    "md": "06-09",
                    "code": 1076,
                    "themeMode": 1,
                    "timePeriod": 3,
                    "detail": "SrThemeSkin:Basic",
                    "file": "main.log",
                    "line": 118312,
                }
            ],
            "get_msgs": [],
            "langs": [],
            "cultures": [],
            "theme_type_parses": [],
            "theme_helper_configs": [],
            "theme_collects": [],
            "theme_switches": [],
            "timer_gaps": [
                {
                    "from": "06-09 05:50:18.557",
                    "to": "06-09 19:54:03.269",
                    "seconds": 50624,
                    "sev": "red",
                }
            ],
            "condition_mgrs": [],
            "theme_managers": [],
            "theme_elem_sr": [],
            "theme_elem_car": [],
            "locales": [],
            "init_callbacks": [],
        }

        data = analyzer.build_report_data(result, target_time="2026-06-09 21:44", request_text="黑夜白天")

        self.assertIn("Cal/Final 不一致", data["verdict"]["msg"])
        self.assertIn("TIME_NIGHT", data["verdict"]["msg"])
        self.assertIn("TIME_DAY", data["verdict"]["msg"])
        self.assertNotIn("定时器长时间中断", data["verdict"]["msg"])

    def test_twilight_init_failure_is_not_error_when_sunrise_input_exists(self):
        analyzer = _load_xtheme_analyzer()
        result = {
            "inits": [],
            "calc_times": [],
            "time_checks": [],
            "ui_changes": [],
            "theme_changes": [],
            "theme_msgs": [],
            "sunrise_sets": [
                {"ts": "05-18 11:18:29.343", "md": "05-18", "sunriseMin": 360, "sunsetMin": 1200, "file": "main.log", "line": 3}
            ],
            "twilights": [],
            "twilight_fails": [
                {"ts": "05-18 11:18:28.343", "md": "05-18", "reason": "NullPointerException", "file": "main.log", "line": 2, "raw": "fail"}
            ],
            "calc_warns": [],
            "theme_elems": [],
            "strategies": [],
            "get_msgs": [],
            "langs": [],
            "cultures": [],
            "theme_type_parses": [],
            "theme_helper_configs": [],
            "theme_collects": [],
            "theme_switches": [],
            "timer_gaps": [],
        }

        data = analyzer.build_report_data(result, target_time="2026-05-18 11:19", request_text="调查主题变化")

        self.assertEqual(data["issues"], [])
        self.assertTrue(any("日出日落输入" in item["detail"] for item in data["observations"]))

    def test_timeline_collapses_repeated_theme_type_parse_events(self):
        analyzer = _load_xtheme_analyzer()
        result = {
            "inits": [],
            "calc_times": [],
            "ui_changes": [],
            "theme_switches": [],
            "theme_msgs": [],
            "twilight_fails": [],
            "timer_gaps": [],
            "theme_helper_configs": [],
            "theme_collects": [],
            "theme_type_parses": [
                {"ts": "05-18 11:10:44.675", "uiMode": 19, "autoMode": 1, "file": "main.log", "line": 10},
                {"ts": "05-18 11:10:44.740", "uiMode": 19, "autoMode": 1, "file": "main.log", "line": 11},
                {"ts": "05-18 11:10:57.431", "uiMode": 19, "autoMode": 1, "file": "main.log", "line": 12},
                {"ts": "05-18 14:02:53.695", "uiMode": 35, "autoMode": 2, "file": "main.log", "line": 20},
            ],
        }

        html = analyzer._render_timeline(result)

        self.assertEqual(html.count("ThemeType.parseTheme"), 2)
        self.assertIn("连续 3 次", html)
        self.assertIn("05-18 11:10:44.675 ~ 05-18 11:10:57.431", html)

    def test_timeline_does_not_collapse_theme_type_across_dates(self):
        analyzer = _load_xtheme_analyzer()
        result = {
            "inits": [],
            "calc_times": [],
            "ui_changes": [],
            "theme_switches": [],
            "theme_msgs": [],
            "twilight_fails": [],
            "timer_gaps": [],
            "theme_helper_configs": [],
            "theme_collects": [],
            "theme_type_parses": [
                {"ts": "01-01 08:00:48.388", "uiMode": 19, "autoMode": 1, "file": "boot.log", "line": 10},
                {"ts": "05-18 11:10:44.675", "uiMode": 19, "autoMode": 1, "file": "main.log", "line": 20},
            ],
        }

        html = analyzer._render_timeline(result)

        self.assertEqual(html.count("ThemeType.parseTheme"), 2)
        self.assertNotIn("01-01 08:00:48.388 ~ 05-18 11:10:44.675", html)

    def test_timeline_collapses_repeated_no_change_events(self):
        analyzer = _load_xtheme_analyzer()
        result = {
            "theme_type_parses": [],
            "theme_helper_configs": [
                {"ts": "05-18 11:10:44.675", "oldTheme": "THEME_DAY_NORMAL", "newTheme": "THEME_DAY_NORMAL", "appUiMode": 19, "file": "main.log", "line": 10},
                {"ts": "05-18 11:10:44.740", "oldTheme": "THEME_DAY_NORMAL", "newTheme": "THEME_DAY_NORMAL", "appUiMode": 19, "file": "main.log", "line": 11},
            ],
            "theme_collects": [
                {"ts": "05-18 11:10:45.178", "themeState": "THEME_DAY_NORMAL", "dayNightType": "THEME_DAY_NORMAL", "file": "main.log", "line": 20},
                {"ts": "05-18 11:10:45.242", "themeState": "THEME_DAY_NORMAL", "dayNightType": "THEME_DAY_NORMAL", "file": "main.log", "line": 21},
            ],
            "inits": [
                {"ts": "05-18 11:10:57.607", "uiMode": 1, "themeMode": 0, "timeInfo": "TIME_DAY", "xtheme": "1,0,[Basic,,,],[,,,]", "file": "main.log", "line": 30},
                {"ts": "05-18 11:18:29.360", "uiMode": 1, "themeMode": 0, "timeInfo": "TIME_DAY", "xtheme": "1,0,[Basic,,,],[,,,]", "file": "main.log", "line": 31},
            ],
            "ui_changes": [
                {"ts": "05-18 11:13:36.146", "uiMode": 1, "themeMode": 0, "file": "main.log", "line": 40},
                {"ts": "05-18 11:13:36.200", "uiMode": 1, "themeMode": 0, "file": "main.log", "line": 41},
            ],
            "theme_switches": [],
            "theme_msgs": [
                {"ts": "05-18 11:19:55.755", "xtheme": "1,0,[Basic,,,],[,,,]", "file": "main.log", "line": 50},
                {"ts": "05-18 11:19:58.462", "xtheme": "1,0,[Basic,,,],[,,,]", "file": "main.log", "line": 51},
            ],
            "strategies": [
                {"ts": "05-18 11:19:01.206", "code": 1076, "themeMode": 0, "timePeriod": 1, "detail": "SrThemeSkin:1080102 /data/path  CarThemeSkin:", "file": "main.log", "line": 60},
                {"ts": "05-18 11:19:01.300", "code": 1076, "themeMode": 0, "timePeriod": 1, "detail": "SrThemeSkin:1080102 /data/path  CarThemeSkin:", "file": "main.log", "line": 61},
            ],
            "twilight_fails": [],
            "timer_gaps": [],
            "calc_times": [],
        }

        html = analyzer._render_timeline(result)

        self.assertEqual(html.count("ThemeHelper.onConfigurationChanged"), 1)
        self.assertEqual(html.count("XuiConditionHelper collect themeState"), 1)
        self.assertEqual(html.count("XuiConditionHelper init mCurrentUiMode"), 1)
        self.assertEqual(html.count("XuiConditionHelper ui mode change"), 1)
        self.assertEqual(html.count("XuiConditionHelper update mXThemeMsg"), 1)
        self.assertEqual(html.count('<div class="tl-title">XThemeStrategy code:1076</div>'), 1)
        self.assertGreaterEqual(html.count("连续 2 次"), 5)

    def test_timeline_uses_actual_log_labels_not_source_function_inference(self):
        analyzer = _load_xtheme_analyzer()
        result = {
            "theme_type_parses": [],
            "theme_helper_configs": [],
            "theme_collects": [],
            "inits": [
                {"ts": "05-18 11:19:55.755", "uiMode": 1, "themeMode": 0, "timeInfo": "TIME_DAY", "xtheme": "1,0,[Basic,,,],[,,,]", "file": "main.log", "line": 30},
            ],
            "ui_changes": [],
            "theme_switches": [],
            "theme_msgs": [],
            "strategies": [
                {"ts": "05-18 11:19:01.206", "code": 1076, "themeMode": 0, "timePeriod": 1, "detail": "SrThemeSkin:1080102 /data/operation/resource/theme/abc/u3d/sr/  CarThemeSkin:", "file": "main.log", "line": 60},
            ],
            "twilight_fails": [],
            "timer_gaps": [],
            "calc_times": [],
        }

        html = analyzer._render_timeline(result)

        self.assertNotIn("初始化 (initHelper)", html)
        self.assertIn("XuiConditionHelper init mCurrentUiMode", html)
        self.assertIn("XThemeStrategy code:1076", html)
        self.assertIn("ThemeMode:0 TimePeriod:1", html)

    def test_timeline_collapses_strategy_events_per_code_when_interleaved(self):
        analyzer = _load_xtheme_analyzer()
        result = {
            "theme_type_parses": [],
            "theme_helper_configs": [],
            "theme_collects": [],
            "inits": [],
            "ui_changes": [],
            "theme_switches": [],
            "theme_msgs": [],
            "strategies": [
                {"ts": "05-18 11:10:41.373", "code": 1076, "themeMode": 0, "timePeriod": 1, "detail": "SrThemeSkin:1080102 path  CarThemeSkin:", "file": "main.log", "line": 10},
                {"ts": "05-18 11:10:46.023", "code": 1115, "themeMode": 0, "timePeriod": 1, "detail": "SrThemeSkin:Basic   CarThemeSkin:", "file": "main.log", "line": 11},
                {"ts": "05-18 11:10:58.779", "code": 1076, "themeMode": 0, "timePeriod": 1, "detail": "SrThemeSkin:1080102 path  CarThemeSkin:", "file": "main.log", "line": 12},
                {"ts": "05-18 11:11:02.730", "code": 1115, "themeMode": 0, "timePeriod": 1, "detail": "SrThemeSkin:Basic   CarThemeSkin:", "file": "main.log", "line": 13},
            ],
            "twilight_fails": [],
            "timer_gaps": [],
            "calc_times": [],
        }

        html = analyzer._render_timeline(result)

        self.assertEqual(html.count('<div class="tl-title">XThemeStrategy code:1076</div>'), 1)
        self.assertEqual(html.count('<div class="tl-title">XThemeStrategy code:1115</div>'), 1)
        self.assertIn("连续 2 次", html)


if __name__ == "__main__":
    unittest.main()
