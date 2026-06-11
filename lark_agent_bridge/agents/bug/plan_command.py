from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared


class _BugPlanCommandMixin:
    """Bug 请求分类、分析命令构建与 JSON 子进程执行（与 BugCacheMixin 共享 self 状态）。"""

    def classify_requests(self, *, prompt_text: str, title: str, description: str) -> list["BugAnalysisPlan"]:
        combined = "\n".join(part for part in [prompt_text, title, description] if part).strip()
        # Signal extraction uses only prompt_text to avoid picking up signal names
        # that appear in bug descriptions/logs but are not what the user wants to investigate.
        signal_request = parse_signal_request(
            prompt_text,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
            signal_resolver=self.signal_resolver,
        )
        prompt_lowered = prompt_text.casefold()
        lowered = combined.casefold()
        explicit_signal_enum = "signal_" in prompt_lowered
        explicit_signal_terms = any(term in prompt_lowered for term in SIGNAL_ROUTE_TERMS)
        startup_requested = any(term in lowered for term in STARTUP_ROUTE_TERMS)
        stuck_requested = any(term in lowered for term in STUCK_ROUTE_TERMS)
        startup_blocked = any(term in lowered for term in STARTUP_BLOCK_ROUTE_TERMS)
        candidates: list[tuple[int, str, str | None]] = []

        def add_candidate(score: int, kind: str, signal_code: str | None = None) -> None:
            candidates.append((score, kind, signal_code))

        if any(term in lowered for term in PERCEPTION_ROUTE_TERMS):
            add_candidate(120, "perception")
        if any(term in lowered for term in LD_LANE_LEVEL_ROUTE_TERMS):
            add_candidate(117, "ld_lane_level")
        if any(term in lowered for term in PULLOVER_CHAIN_ROUTE_TERMS):
            add_candidate(116, "pullover_chain")
        if any(term in lowered for term in XTHEME_ROUTE_TERMS):
            add_candidate(115, "xtheme")
        if (
            (looks_like_scene_signal_request(combined) and not (signal_request.signal and (explicit_signal_enum or explicit_signal_terms)))
            or (signal_request.signal and _is_core_scene_signal(signal_request.signal))
            or _has_strong_scene_signal_intent(lowered)
        ):
            add_candidate(110, "scene_signal")
        if (
            signal_request.signal
            and (explicit_signal_enum or explicit_signal_terms)
            and not _is_core_scene_signal(signal_request.signal)
            and not _has_strong_scene_signal_intent(lowered)
        ):
            add_candidate(100, "signal", signal_request.signal)
        if any(term in lowered for term in CRASH_ROUTE_TERMS):
            add_candidate(95, "crash")
        plans: list[BugAnalysisPlan] = []
        if startup_requested or (stuck_requested and startup_blocked):
            plans.append(BugAnalysisPlan(kind="startup"))
        if stuck_requested:
            plans.append(BugAnalysisPlan(kind="stuck"))
        if plans:
            return plans
        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            _score, kind, signal_code = candidates[0]
            return [BugAnalysisPlan(kind=kind, signal_code=signal_code)]
        return [BugAnalysisPlan(kind="general")]
    def classify_request(self, *, prompt_text: str, title: str, description: str) -> "BugAnalysisPlan":
        return self.classify_requests(prompt_text=prompt_text, title=title, description=description)[0]
    def build_command(
        self,
        *,
        plan: "BugAnalysisPlan",
        input_path: Path,
        html_path: Path,
        json_path: Path,
        analysis_dir: Path,
        target_time: str | None = None,
        request_text: str | None = None,
        log_analysis: dict[str, object] | None = None,
    ) -> list[str]:
        if plan.kind == "startup":
            command = [
                sys.executable,
                str(self._startup_script()),
                str(input_path),
                "--output-dir",
                str(analysis_dir),
            ]
            if target_time:
                command.extend(["--target-time", target_time])
            return command
        if plan.kind == "stuck":
            command = [
                sys.executable,
                str(self._stuck_script()),
                str(input_path),
            ]
            if target_time:
                command.extend(["--target-time", target_time])
            return command
        if plan.kind == "perception":
            command = [
                sys.executable,
                str(self._perception_script()),
                str(input_path),
            ]
            if target_time:
                command.extend(["--target-time", target_time])
            return command
        if plan.kind == "xtheme":
            command = [
                sys.executable,
                str(self._xtheme_script()),
                str(input_path),
            ]
            if target_time:
                command.extend(["--target-time", target_time])
            if request_text:
                command.extend(["--request-text", request_text])
            return command
        if plan.kind == "scene_signal":
            command = [
                sys.executable,
                str(self._scene_signal_script()),
                "--log-path",
                str(input_path),
                "--output-dir",
                str(analysis_dir),
            ]
            if target_time:
                command.extend(["--target-time", target_time])
            # 智能日志分析：传入 PID 和日志文件列表
            if log_analysis:
                target_pid = log_analysis.get("target_pid")
                if target_pid:
                    command.extend(["--pid", str(target_pid)])
                log_files = log_analysis.get("log_files")
                if log_files and isinstance(log_files, list):
                    command.extend(["--log-files", ",".join(str(f) for f in log_files)])
            return command
        if plan.kind == "crash":
            command = [
                sys.executable,
                str(self._stuck_script()),
                str(input_path),
            ]
            if target_time:
                command.extend(["--target-time", target_time])
            return command
        if _kind_spec(plan.kind).is_agent_handled:
            return []
        command = [
            sys.executable,
            str(self._signal_script()),
            "--signal-code",
            plan.signal_code or "",
            "--output",
            str(html_path),
            "--json-output",
            str(json_path),
        ]
        if input_path.exists():
            command.extend(["--log-path", str(input_path)])
        command.append("--json-only")
        return command
    def _working_dir(self) -> Path:
        options = self.config.bug_analysis
        return options.working_dir or self.config.workspace_root
    def _plan_requires_log_input(self, plan: "BugAnalysisPlan") -> bool:
        if plan.kind not in PLAN_KIND_REGISTRY:
            return False
        return not _kind_spec(plan.kind).is_source_stage
    def _bug_fetcher_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/feishu-bug-fetcher/scripts/bug-fetcher.sh"
    def _startup_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/unity-startup-lifecycle-check/scripts/analyze_unity_startup.py"
    def _stuck_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/3d-stuck-investigate/scripts/analyze_3d_stuck.py"
    def _signal_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/signal-chain-analyzer/scripts/analyze_signal_chain.py"
    def _scene_signal_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/scene-signal-diagnosis/scripts/extract_scene_signal_events.py"
    def _perception_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/perception-data-summary/scripts/analyze_perception_data_summary.py"
    def _xtheme_script(self) -> Path:
        return self.config.workspace_root / ".ai/skills/xtheme-analyzer/scripts/analyze_xtheme.py"
    def _bug_id(self, url: str) -> str:
        match = re.search(r"/buglo/detail/(\d+)", url)
        return match.group(1) if match else "unknown_bug"
    def _report_name(self, kind: str, suffix: str) -> str:
        return {
            "startup": f"bug_3d_startup_report.{suffix}",
            "stuck": f"bug_3d_stuck_report.{suffix}",
            "crash": f"bug_crash_report.{suffix}",
            "scene_signal": f"bug_scene_signal_report.{suffix}",
            "perception": f"bug_perception_data_summary.{suffix}",
            "signal": f"bug_signal_chain_report.{suffix}",
            "xtheme": f"bug_xtheme_analysis_report.{suffix}",
            "ld_lane_level": f"bug_ld_lane_level_report.{suffix}",
            "pullover_chain": f"bug_pullover_chain_report.{suffix}",
            "general": f"bug_general_analysis_report.{suffix}",
            SOURCE_STAGE_KIND: f"source_stage_report.{suffix}",
            "custom_skill": f"bug_source_code_report.{suffix}",
            SOURCE_CODE_SKILL_KIND: f"bug_source_code_report.{suffix}",
        }[kind]
    def _combined_report_name(self, suffix: str) -> str:
        return f"bug_startup_stuck_report.{suffix}"
    def _analysis_label(self, kind: str) -> str:
        return {
            "startup": "3D启动时序分析",
            "stuck": "3D卡顿分析",
            "crash": "Crash/闪退分析",
            "scene_signal": "3D场景信号分析",
            "perception": "当前感知数据总结",
            "signal": "信号链路分析",
            "xtheme": "XTheme时光主题分析",
            "ld_lane_level": "LD车道级日志分析",
            "pullover_chain": "靠边停车链路分析",
            "general": "通用问题分析",
            SOURCE_STAGE_KIND: "源码分析阶段",
            "custom_skill": "源码分析 (source_code_skill)",
            SOURCE_CODE_SKILL_KIND: "源码分析 (source_code_skill)",
        }[kind]
    def _effective_skill_name_for_plan(self, plan_kind: str, candidate_skill_name: str) -> str:
        normalized = candidate_skill_name.strip()
        default_skill = self._skill_name_for_kind(plan_kind)
        if _kind_spec(plan_kind).is_source_stage:
            if normalized and (
                normalized == "source_analysis"
                or self.skill_manager.custom_skill_executor_for(normalized) == "file_agent"
            ):
                return normalized
            return default_skill
        if plan_kind in _SOURCE_SKILL_KINDS:
            return normalized or default_skill
        return default_skill
    def _subprocess_debug_log_path(
        self,
        directory: Path,
        stem: str,
        *,
        bridge_session_id: str = "",
    ) -> Path:
        safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._") or "subprocess"
        if bridge_session_id:
            safe_session = re.sub(r"[^A-Za-z0-9_.-]+", "_", bridge_session_id).strip("._")
            safe_stem = f"{safe_session}.{safe_stem}"
        return directory / f"{safe_stem}.debug.log"
    def _run_json_command(
        self,
        command: list[str],
        *,
        timeout: int,
        bridge_session_id: str = "",
    ) -> dict[str, object]:
        debug_log_path = self._subprocess_debug_log_path(
            self.config.data_dir / "subprocess_debug",
            "bug-json-command",
            bridge_session_id=bridge_session_id,
        )
        completed = _run_tracked_process(
            command,
            watchdog=self.process_watchdog,
            name="bug-json-command",
            cwd=self._working_dir(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            session_id=bridge_session_id,
            debug_log_path=debug_log_path,
        )
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or "command failed"
            raise RuntimeError(message)
        payload = json.loads(completed.stdout)
        if isinstance(payload, dict) and payload.get("ok") is False:
            raise RuntimeError(str(payload.get("error", "command returned ok=false")))
        return payload
    def _load_option_map(self, project_key: str, *, bridge_session_id: str = "") -> dict[str, str]:
        payload = self._run_json_command(
            [
                "meegle",
                "workitem",
                "meta-fields",
                "--project-key",
                project_key,
                "--work-item-type",
                "buglo",
                "--field-keys",
                "field_24095d",
                "--field-keys",
                "field_45dc84",
                "--page-num",
                "1",
                "--format",
                "json",
            ],
            timeout=120,
            bridge_session_id=bridge_session_id,
        )
        option_map: dict[str, str] = {}
        for field in payload.get("list", []):
            if not isinstance(field, dict):
                continue
            for option in field.get("option", []):
                if not isinstance(option, dict):
                    continue
                option_id = option.get("option_id")
                option_name = option.get("option_name")
                if isinstance(option_id, str) and isinstance(option_name, str):
                    option_map[option_id] = option_name
        return option_map
