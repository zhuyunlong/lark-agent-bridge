from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _SignalKotlinSourceMixin:
    """Kotlin 源码解析：仓库信号引用、类/回调定位、目标分支与日志常量提取（与 SignalAndroidMixin 共享 self 状态）。"""

    def _repo_relative_path(self, file_text: str) -> Path | None:
        if not file_text:
            return None
        path = Path(file_text)
        if path.is_absolute():
            return path if path.exists() else None
        candidate = self.config.guideengine_repo / file_text
        return candidate if candidate.exists() else None
    def _read_text_quiet(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    def _signal_repo_signal_references(self, signal_name: str, *, max_refs: int = 80) -> list[dict[str, str]]:
        if not signal_name:
            return []
        repo = Path(self.config.guideengine_repo).expanduser()
        if not repo.exists():
            return []
        try:
            completed = subprocess.run(
                ["rg", "-n", "--fixed-strings", signal_name, str(repo)],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        refs: list[dict[str, str]] = []
        for raw_line in completed.stdout.splitlines():
            if len(refs) >= max_refs:
                break
            match = re.match(r"(.+?):(\d+):(.*)", raw_line)
            if not match:
                continue
            path = Path(match.group(1))
            try:
                file_text = str(path.relative_to(repo))
            except ValueError:
                file_text = str(path)
            refs.append({"file": file_text, "line": match.group(2), "text": match.group(3).strip()})
        return refs
    def _signal_kotlin_class_from_file(self, file_text: str, class_name: str) -> str:
        if not file_text or not class_name:
            return ""
        normalized = file_text.replace("\\", "/")
        marker = "/src/main/java/"
        if marker in normalized:
            normalized = normalized.split(marker, 1)[1]
        elif "src/main/java/" in normalized:
            normalized = normalized.split("src/main/java/", 1)[1]
        else:
            return ""
        normalized = re.sub(r"\.(kt|java)$", "", normalized)
        dotted = normalized.replace("/", ".")
        return dotted if dotted.endswith(f".{class_name}") or dotted == class_name else ""
    def _signal_kotlin_string_constants(self, text: str) -> dict[str, str]:
        constants: dict[str, str] = {}
        for match in re.finditer(r"const\s+val\s+([A-Z0-9_]+)\s*(?::\s*String)?\s*=\s*\"([^\"]+)\"", text):
            constants[match.group(1)] = match.group(2)
        return constants
    def _signal_callback_name_for_event(self, text: str, event_id: str) -> str:
        match = re.search(rf"\b{re.escape(event_id)}\s+to\s+this::([A-Za-z0-9_]+)", text)
        return match.group(1) if match else ""
    def _signal_kotlin_function_block(self, lines: list[str], function_name: str, *, max_lines: int = 140) -> list[tuple[int, str]]:
        start_index = -1
        pattern = re.compile(rf"\bfun\s+{re.escape(function_name)}\b")
        for index, line in enumerate(lines):
            if pattern.search(line):
                start_index = index
                break
        if start_index < 0:
            return []
        block: list[tuple[int, str]] = []
        brace_depth = 0
        opened = False
        for index in range(start_index, min(len(lines), start_index + max_lines)):
            line = lines[index]
            block.append((index + 1, line))
            brace_line = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)
            opens = brace_line.count("{")
            closes = brace_line.count("}")
            if opens:
                opened = True
            if opened:
                brace_depth += opens - closes
                if brace_depth <= 0 and index > start_index:
                    break
        return block
    def _signal_kotlin_target_branch(self, callback_lines: list[tuple[int, str]], signal_name: str) -> list[tuple[int, str]]:
        if not signal_name:
            return callback_lines
        target_indexes = [index for index, (_, line) in enumerate(callback_lines) if signal_name in line]
        if not target_indexes:
            return callback_lines
        result: list[tuple[int, str]] = []
        seen_lines: set[int] = set()
        for target_index in target_indexes:
            start_index = target_index
            for index in range(target_index, -1, -1):
                line = callback_lines[index][1]
                if "->" in line and re.search(r"\b(?:EVENT_KEY|VALUE_KEY)_[A-Z0-9_]+\b|\"[^\"]+\"", line):
                    start_index = index
                    break
            brace_depth = 0
            opened = False
            for index in range(start_index, len(callback_lines)):
                line_no, line = callback_lines[index]
                if line_no not in seen_lines:
                    seen_lines.add(line_no)
                    result.append((line_no, line))
                brace_line = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)
                opens = brace_line.count("{")
                closes = brace_line.count("}")
                if opens:
                    opened = True
                if opened:
                    brace_depth += opens - closes
                    if brace_depth <= 0 and index > start_index:
                        break
                elif index > target_index:
                    break
        return result or callback_lines
    def _signal_consumer_log_terms(self, path: Path, signal_name: str) -> list[str]:
        text = self._read_text_quiet(path)
        if not text:
            return []
        lines = text.splitlines()
        terms: list[str] = []
        for index, line in enumerate(lines):
            if signal_name not in line:
                continue
            nearby_case = lines[max(0, index - 3) : min(len(lines), index + 4)]
            if any("->" in item for item in nearby_case):
                source_lines = [item for _, item in self._signal_kotlin_target_branch([(line_no + 1, value) for line_no, value in enumerate(lines)], signal_name)]
            elif any("getSignalFlow" in item or ".collect" in item for item in lines[index : min(len(lines), index + 16)]):
                source_lines = lines[index : min(len(lines), index + 40)]
            else:
                continue
            for nearby in source_lines:
                literal = self._signal_log_literal_from_source_line(nearby)
                if literal and self._signal_log_literal_matches_signal(literal, signal_name):
                    terms.append(literal)
        return self._unique_nonempty(terms)
    def _signal_log_literal_matches_signal(self, literal: str, signal_name: str) -> bool:
        literal_key = re.sub(r"[^a-z0-9]+", "", literal.casefold())
        if not literal_key:
            return False
        parts = [
            part.casefold()
            for part in re.split(r"[_\W]+", signal_name)
            if len(part) >= 4 and part.casefold() not in {"signal", "powercenter", "change"}
        ]
        return any(part in literal_key for part in parts)
    def _signal_log_literal_from_source_line(self, line: str) -> str:
        match = re.search(r"L\.[idwe]\([^,]+,\s*\"([^\"]+)\"", line)
        if not match:
            return ""
        literal = match.group(1).strip()
        literal = re.split(r"\$\{?|\{", literal, maxsplit=1)[0].strip()
        return literal if len(literal) >= 4 else ""
    def _unique_nonempty(self, values: list[object]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = str(value).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result
    def _signal_stage_label(self, item: dict[str, object]) -> str:
        label = str(item.get("label") or item.get("title") or "")
        text = str(item.get("text") or "")
        lowered = text.casefold()
        if "injectsignalprovider" in lowered:
            return "注入 Provider"
        if "registersignal" in lowered:
            return "注册信号"
        if "getsignalflow" in lowered:
            return "DataCenter 取流"
        generic_label = self._signal_log_semantic_label(text)
        if generic_label:
            return generic_label
        return label or "日志命中"
