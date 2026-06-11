from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from . import _shared


class _SourceEvidenceMixin:
    """源码证据收集：codegraph/grep 检索、术语提取与证据 metadata 写入（与 AgentSummaryMixin 共享 self 状态）。"""

    def _append_source_evidence_metadata(self, metadata_path: Path, source_evidence_path: Path | None) -> None:
        if source_evidence_path is None:
            return
        try:
            evidence = source_evidence_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            evidence = f"读取失败: {exc}"
        try:
            original = metadata_path.read_text(encoding="utf-8")
        except OSError:
            original = ""
        lines = [
            "",
            "## 源码证据",
            "",
            f"- 文件: `{source_evidence_path}`",
            "",
            "```text",
            evidence[:5000],
            "```",
        ]
        metadata_path.write_text(original.rstrip() + "\n" + "\n".join(lines).rstrip() + "\n", encoding="utf-8")
    def _append_evidence_log_metadata(self, metadata_path: Path, evidence_log_bundle: dict[str, object] | None) -> None:
        if not evidence_log_bundle:
            return
        try:
            original = metadata_path.read_text(encoding="utf-8")
        except OSError:
            original = ""
        focus_logs = evidence_log_bundle.get("focus_logs")
        if isinstance(focus_logs, list) and focus_logs:
            focus_text = ", ".join(str(item) for item in focus_logs)
        else:
            focus_text = "未识别"
        lines = [
            "",
            "## 证据日志保留包",
            "",
            f"- 目录: `{evidence_log_bundle.get('bundle_dir') or ''}`",
            f"- 清单: `{evidence_log_bundle.get('manifest_path') or ''}`",
            f"- 命中日志: `{focus_text}`",
            f"- 文件数: `{evidence_log_bundle.get('file_count') or 0}`",
        ]
        metadata_path.write_text(original.rstrip() + "\n" + "\n".join(lines).rstrip() + "\n", encoding="utf-8")
    def _should_collect_source_evidence(self, *texts: str) -> bool:
        source_terms = ("源码", "源代码", "根据源码", "基于源码")
        merged = "\n".join(texts).casefold()
        return any(term.casefold() in merged for term in source_terms) or self._source_analysis_shortcut(*texts)
    def _write_reanalysis_source_evidence(
        self,
        *,
        plans: list["BugAnalysisPlan"],
        request_text: str,
        followup_text: str,
        output_dir: Path,
        enabled: bool,
        extra_texts: tuple[str, ...] = (),
    ) -> Path | None:
        if not enabled:
            return None
        terms = self._source_evidence_terms(
            plans=plans,
            request_text=request_text,
            followup_text=followup_text,
            extra_texts=extra_texts,
        )
        if not terms:
            return None
        evidence_path = output_dir / "bug_source_evidence.md"
        started = time.monotonic()
        budget_seconds = min(
            self._SOURCE_EVIDENCE_TOTAL_BUDGET_SECONDS,
            5.0 * max(1, min(len(terms), 5)),
        )
        deadline = started + budget_seconds

        # Use all configured repo_roots (includes Napa5 when configured), fallback to guideengine_repo
        si_opts = self.config.source_investigation
        repo_roots = [
            p.expanduser()
            for p in (si_opts.repo_roots or [self.config.guideengine_repo])
        ]
        existing_repos = [r for r in repo_roots if r.exists()]

        lines = [
            "# Bug Source Evidence",
            "",
            f"- 源码根目录: `{', '.join(str(r) for r in (existing_repos or repo_roots))}`",
            f"- 检索词: `{', '.join(terms)}`",
            f"- 检索预算: `{budget_seconds:.1f}s`",
            "",
        ]
        if not existing_repos:
            lines.append(f"源码根目录不存在，未执行源码检索: `{', '.join(str(r) for r in repo_roots)}`")
            evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return evidence_path

        all_matches: list[tuple[str, int, str]] = []
        trace_lines: list[str] = []
        for repo in existing_repos:
            if time.monotonic() >= deadline:
                trace_lines.append(f"- `{repo}`: skipped, source evidence budget exhausted")
                break
            repo_started = time.monotonic()
            # Try codegraph first (semantic symbol search)
            cg_matches = self._collect_source_evidence_with_codegraph(repo=repo, terms=terms, deadline=deadline)
            if cg_matches is not None:
                repo_prefix = f"[{repo.name}] " if len(existing_repos) > 1 else ""
                all_matches.extend((repo_prefix + path, ln, text) for path, ln, text in cg_matches)
                trace_lines.append(
                    f"- `{repo}`: codegraph, {len(cg_matches)} matches, {time.monotonic() - repo_started:.2f}s"
                )
                continue
            # Fall back to ripgrep only; avoid pure Python full-repo scans on large trees.
            matches = self._collect_source_evidence(repo=repo, terms=terms, deadline=deadline)
            if len(existing_repos) > 1:
                all_matches.extend((f"[{repo.name}] {path}", ln, text) for path, ln, text in matches)
            else:
                all_matches.extend(matches)
            trace_lines.append(
                f"- `{repo}`: rg fallback, {len(matches)} matches, {time.monotonic() - repo_started:.2f}s"
            )

        if trace_lines:
            elapsed = time.monotonic() - started
            lines.extend(["## 检索路径", "", *trace_lines, f"- 总耗时: `{elapsed:.2f}s`", ""])

        if not all_matches:
            lines.append("未检索到匹配源码。")
        else:
            current_file = ""
            for path, line_no, text in all_matches[:80]:
                if path != current_file:
                    current_file = path
                    lines.extend(["", f"## {path}"])
                lines.append(f"- L{line_no}: `{text}`")
        evidence_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return evidence_path
    def _collect_source_evidence_with_codegraph(
        self, *, repo: Path, terms: list[str], deadline: float | None = None
    ) -> list[tuple[str, int, str]] | None:
        """Try codegraph semantic search; return None to fall through to ripgrep."""
        si_opts = self.config.source_investigation
        if not si_opts.codegraph_enabled:
            return None
        try:
            from ...knowledge.codegraph_client import CodeGraphClient
        except ImportError:
            return None
        cg = CodeGraphClient(
            command=si_opts.codegraph_command,
            timeout=min(si_opts.codegraph_timeout_seconds, self._SOURCE_EVIDENCE_CODEGRAPH_CALL_SECONDS),
        )
        if not cg.is_available():
            return None
        status_timeout = self._source_evidence_call_timeout(deadline, si_opts.codegraph_timeout_seconds)
        if status_timeout <= 0 or not cg.is_indexed(repo, timeout=status_timeout):
            return None
        matches: list[tuple[str, int, str]] = []
        seen: set[tuple[str, int]] = set()
        for term in terms[:5]:
            call_timeout = self._source_evidence_call_timeout(deadline, si_opts.codegraph_timeout_seconds)
            if call_timeout <= 0:
                break
            hits = cg.search_symbol(term, repo, limit=5, timeout=call_timeout)
            for h in hits:
                key = (h.path, h.line)
                if key in seen:
                    continue
                seen.add(key)
                matches.append((h.path, h.line, f"[{h.kind}] {h.qualified_name or h.name}"))
            call_timeout = self._source_evidence_call_timeout(deadline, si_opts.codegraph_timeout_seconds)
            if call_timeout <= 0:
                break
            callers = cg.get_callers(term, repo, limit=5, timeout=call_timeout)
            for c in callers:
                key = (c.path, c.line)
                if key in seen:
                    continue
                seen.add(key)
                matches.append((c.path, c.line, f"[caller→{term}] {c.name}"))
        return matches if matches else None
    def _source_evidence_terms(
        self,
        *,
        plans: list["BugAnalysisPlan"],
        request_text: str,
        followup_text: str,
        extra_texts: tuple[str, ...] = (),
    ) -> list[str]:
        terms: list[str] = []
        title_text = extra_texts[0] if extra_texts else ""
        description_texts = extra_texts[1:] if len(extra_texts) > 1 else ()
        for text in (request_text, followup_text, title_text):
            for term in self._explicit_source_terms_from_text(text):
                self._append_unique(terms, term)
        for plan in plans:
            if plan.kind != "signal" or not plan.signal_code:
                continue
            self._append_unique(terms, plan.signal_code)
            if plan.signal_code.startswith("SIGNAL_"):
                self._append_unique(terms, plan.signal_code.removeprefix("SIGNAL_"))
        for text in (followup_text, request_text):
            signal = self._extract_signal_code_for_reanalysis(text)
            if signal:
                self._append_unique(terms, signal)
                if signal.startswith("SIGNAL_"):
                    self._append_unique(terms, signal.removeprefix("SIGNAL_"))
        for text in (request_text, followup_text, title_text, *description_texts):
            for term in self._business_source_terms_from_text(text):
                self._append_unique(terms, term)
        return terms[:8]
    def _explicit_source_terms_from_text(self, text: str) -> list[str]:
        search_text = re.sub(r"https?://\S+", " ", text or "")
        search_text = re.sub(
            r"(?im)^\s*(?:Version|Build|Serial|ICCID|VIN)\s*[:：].*$",
            " ",
            search_text,
        )
        ignored_tokens = {"Version", "Build", "Serial", "ICCID", "VIN", "CLI"}
        terms: list[str] = []
        for match in re.finditer(
            r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_.-]{1,120}\.(?:kt|java|cpp|cc|c|h|hpp|proto|xml))",
            search_text,
        ):
            filename = match.group(1).strip()
            self._append_unique(terms, filename)
            stem = Path(filename).stem
            if stem:
                self._append_unique(terms, stem)
        for match in re.finditer(r"\b([A-Z][A-Za-z0-9_]{4,})\b", search_text):
            token = match.group(1).strip()
            if token in ignored_tokens:
                continue
            self._append_unique(terms, token)
        return terms
    def _business_source_terms_from_text(self, text: str) -> list[str]:
        suffixes = (
            "模式",
            "功能",
            "场景",
            "页面",
            "流程",
            "链路",
            "策略",
            "状态",
            "异常",
            "失败",
            "开关",
            "服务",
            "模块",
            "信号",
            "电量",
            "电流",
            "电压",
            "百分比",
        )
        generic_prefixes = (
            "结合",
            "根据",
            "找到",
            "分析",
            "重新",
            "通过",
            "查看",
            "确认",
            "排查",
            "调查",
            "主要是",
            "为什么",
            "无法",
            "不能",
            "开启",
        )
        ascii_stopwords = {
            "http",
            "https",
            "project",
            "feishu",
            "meegle",
            "buglo",
            "detail",
            "cli",
            "bug",
            "code",
            "html",
            "report",
            "version",
            "build",
            "serial",
            "iccid",
            "vin",
            "cli",
        }
        search_text = re.sub(r"https?://\S+", " ", text or "")
        search_text = re.sub(
            r"(?im)^\s*(?:Version|Build|Serial|ICCID|VIN)\s*[:：].*$",
            " ",
            search_text,
        )
        terms: list[str] = []
        for match in re.finditer(r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_]{1,16}[\u4e00-\u9fff]{1,8})", search_text):
            term = match.group(1)
            self._append_unique(terms, term)
            ascii_part = re.match(r"[A-Za-z][A-Za-z0-9_]{1,16}", term)
            if ascii_part:
                token = ascii_part.group(0)
                chinese_part = term[len(token) :]
                for suffix in suffixes:
                    suffix_index = chinese_part.find(suffix)
                    if suffix_index >= 0:
                        compact = token + chinese_part[: suffix_index + len(suffix)]
                        if compact != term:
                            self._append_unique(terms, compact)
                if token.casefold() not in ascii_stopwords:
                    self._append_unique(terms, token)
        for match in re.finditer(r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_]{2,31})(?![A-Za-z0-9_])", search_text):
            token = match.group(1)
            if token.casefold() not in ascii_stopwords and not token.isdigit():
                self._append_unique(terms, token)
        for suffix in suffixes:
            pattern = re.compile(rf"[\u4e00-\u9fff]{{2,14}}{re.escape(suffix)}")
            for match in pattern.finditer(search_text):
                term = match.group(0)
                changed = True
                while changed:
                    changed = False
                    for prefix in generic_prefixes:
                        if term.startswith(prefix) and len(term) > len(prefix) + len(suffix):
                            term = term[len(prefix) :]
                            changed = True
                self._append_unique(terms, term)
                compact_len = len(suffix) + 2
                if len(term) > compact_len:
                    self._append_unique(terms, term[-compact_len:])
        return terms
    def _collect_source_evidence(
        self, *, repo: Path, terms: list[str], deadline: float | None = None
    ) -> list[tuple[str, int, str]]:
        if deadline is not None and time.monotonic() >= deadline:
            return []
        target_timeout = 5.0
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            target_timeout = min(target_timeout, remaining)
        explicit_matches = self._collect_source_evidence_by_targets(repo=repo, terms=terms, timeout=target_timeout)
        if deadline is not None and time.monotonic() >= deadline:
            return explicit_matches
        rg_timeout = 20.0
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return explicit_matches
            rg_timeout = min(rg_timeout, remaining)
        rg_matches = self._collect_source_evidence_with_rg(repo=repo, terms=terms, timeout=rg_timeout)
        if rg_matches is not None:
            return self._merge_source_evidence_matches(explicit_matches, rg_matches)
        return explicit_matches
