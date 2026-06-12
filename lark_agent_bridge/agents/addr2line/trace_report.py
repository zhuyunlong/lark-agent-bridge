"""addr2line subrealitytrace 线程报告解析与渲染。"""

from __future__ import annotations

import glob
from pathlib import Path
import re
import time
from typing import Any
from .common import (
    _STACK_LINE_RE,
    _ADDR_SO_PAIR_RE,
    _MEMORY_MAP_LINE_RE,
    _CRASH_FILE_NAME_RE,
    _NAVIGATION_CRASH_TERMS,
    _MAX_TEXT_BYTES,
    _THREAD_SUMMARY_SYSTEM_LIBS,
    _SCENE_SO_HINT_TERMS,
    _NAVI_SYMBOL_SO_NAMES,
    _BUILTIN_UNITY_SO_NAMES,
    _NAPA5_SYMBOL_SO_NAMES,
    _TimePoint,
    _CrashStackSelection,
    _PropSelection,
    _PreparedResolveRequest,
    _SymbolSourceSelection,
    _ThreadTraceBlock,
    _FrameSummary,
)


class _TraceReportMixin:
    """addr2line subrealitytrace 线程报告解析与渲染。（与 Addr2LineRunner 共享 self 状态）。"""

    def _build_subrealitytrace_thread_report(
        self,
        *,
        roots: list[Path],
        extract_root: Path,
        source_hint: Path | None,
    ) -> dict[str, Any] | None:
        source_path = self._pick_subrealitytrace_source(roots, extract_root=extract_root, source_hint=source_hint)
        if source_path is None:
            return None
        text = self._read_text_tail(source_path)
        if not text:
            return None
        blocks = self._extract_subrealitytrace_blocks(text)
        if not blocks:
            return None
        selected = self._select_subrealitytrace_categories(blocks)
        if not selected["all"]:
            return None
        scene_source_blocks: list[_ThreadTraceBlock] = []
        seen_scene_blocks: set[tuple[str, str]] = set()
        for item_list in selected["categories"].values():
            for item in item_list:
                key = (item.name, item.tid)
                if key in seen_scene_blocks:
                    continue
                seen_scene_blocks.add(key)
                scene_source_blocks.append(item)
        return self._format_subrealitytrace_report(
            source_path=source_path,
            blocks=selected["all"],
            scene_source_blocks=scene_source_blocks,
            categories=selected["categories"],
            section_time=selected["section_time"],
        )

    def _pick_subrealitytrace_source(
        self,
        roots: list[Path],
        *,
        extract_root: Path,
        source_hint: Path | None,
    ) -> Path | None:
        if source_hint is not None:
            hint_text = self._read_text_tail(source_hint)
            if self._looks_like_subrealitytrace(source_hint, hint_text):
                return source_hint
        for path in self._iter_text_candidates(roots, extract_root=extract_root):
            text = self._read_text_tail(path)
            if self._looks_like_subrealitytrace(path, text):
                return path
        return None

    def _looks_like_subrealitytrace(self, path: Path, text: str) -> bool:
        if "subrealitytrace" in path.name.casefold():
            return True
        if not text:
            return False
        if "----- Waiting Channels:" in text and "sysTid=" in text:
            return True
        if "----- pid " in text and '"UnityMain" sysTid=' in text:
            return True
        return False

    def _extract_subrealitytrace_blocks(self, text: str) -> list[_ThreadTraceBlock]:
        section_re = re.compile(r"^-----\s+pid\s+(?P<pid>\d+)\s+at\s+(?P<time>.+?)\s+-----\s*$")
        header_re = re.compile(r'^\s*"(?P<name>[^"]+)"\s+sysTid=(?P<tid>\d+)\s*$')
        frame_re = re.compile(r"^\s*#\d+\s+pc\s+[0-9a-fA-F]{8,16}\s+\S+.*$")
        lines = text.splitlines()
        blocks: list[_ThreadTraceBlock] = []
        current_pid = ""
        current_time = ""
        index = 0
        while index < len(lines):
            line = lines[index]
            section_match = section_re.match(line)
            if section_match:
                current_pid = section_match.group("pid")
                current_time = section_match.group("time").strip()
                index += 1
                continue
            header_match = header_re.match(line)
            if header_match:
                block = _ThreadTraceBlock(
                    name=header_match.group("name"),
                    tid=header_match.group("tid"),
                    pid=current_pid,
                    section_time=current_time,
                    frames=[],
                )
                index += 1
                while index < len(lines) and frame_re.match(lines[index]):
                    block.frames.append(lines[index].strip())
                    index += 1
                if block.frames:
                    blocks.append(block)
                continue
            index += 1
        return blocks

    def _select_subrealitytrace_categories(self, blocks: list[_ThreadTraceBlock]) -> dict[str, Any]:
        latest_section_time = ""
        for block in reversed(blocks):
            if block.section_time:
                latest_section_time = block.section_time
                break
        scoped = [item for item in blocks if latest_section_time and item.section_time == latest_section_time] or blocks
        categories: dict[str, list[_ThreadTraceBlock]] = {
            "UnityMain": [],
            "XPD_*": [],
            "JniSurfaceTex*": [],
            "主线程": [],
            "渲染相关线程": [],
        }
        for block in scoped:
            name_cf = block.name.casefold()
            frame_text = "\n".join(block.frames).casefold()
            if name_cf == "unitymain":
                categories["UnityMain"].append(block)
            if block.name.startswith("XPD_"):
                categories["XPD_*"].append(block)
            if name_cf.startswith("jnisurfacetex"):
                categories["JniSurfaceTex*"].append(block)
            if name_cf == "main" or (block.pid and block.tid == block.pid) or "android.app.activitythread.main" in frame_text:
                categories["主线程"].append(block)
            if (
                "renderthread" in name_cf
                or "glthread" in name_cf
                or "jnisurfacetex" in name_cf
                or "unitymain" in name_cf
                or "renderthread::threadloop" in frame_text
                or "glsurfaceview$glthread" in frame_text
                or "libhwui.so" in frame_text
                or "renderextend::" in frame_text
            ):
                categories["渲染相关线程"].append(block)

        deduped_categories: dict[str, list[_ThreadTraceBlock]] = {}
        for label, items in categories.items():
            deduped_categories[label] = sorted(
                self._dedupe_trace_blocks(items),
                key=lambda item, category=label: self._trace_block_priority(category, item),
            )

        ordered: list[_ThreadTraceBlock] = []
        seen: set[tuple[str, str]] = set()
        order_limits = {
            "UnityMain": 2,
            "JniSurfaceTex*": 3,
            "渲染相关线程": 4,
            "主线程": 2,
            "XPD_*": 4,
        }
        for label in ("UnityMain", "JniSurfaceTex*", "渲染相关线程", "主线程", "XPD_*"):
            for block in deduped_categories[label][: order_limits[label]]:
                key = (block.name, block.tid)
                if key in seen:
                    continue
                seen.add(key)
                ordered.append(block)
        return {"all": ordered, "categories": deduped_categories, "section_time": latest_section_time}

    def _dedupe_trace_blocks(self, blocks: list[_ThreadTraceBlock]) -> list[_ThreadTraceBlock]:
        seen: set[tuple[str, str]] = set()
        result: list[_ThreadTraceBlock] = []
        for item in blocks:
            key = (item.name, item.tid)
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    def _trace_block_priority(self, category: str, block: _ThreadTraceBlock) -> tuple[int, int, str]:
        name_cf = block.name.casefold()
        frame_text = "\n".join(block.frames).casefold()
        if category == "UnityMain":
            return (0, 0, name_cf)
        if category == "JniSurfaceTex*":
            return (0, 0, name_cf)
        if category == "渲染相关线程":
            interesting_count = sum(
                1
                for line in block.frames[:8]
                if any(term in line.casefold() for term in ("render", "glthread", "libhwui", "libunity", "surface"))
            )
            if "jnisurfacetex" in name_cf:
                return (0, -interesting_count, name_cf)
            if "renderthread" in name_cf:
                return (1, -interesting_count, name_cf)
            if "glthread" in name_cf:
                return (2, -interesting_count, name_cf)
            if "renderextend" in frame_text:
                return (3, -interesting_count, name_cf)
            return (4, -interesting_count, name_cf)
        if category == "主线程":
            if block.pid and block.tid == block.pid:
                return (0, 0, name_cf)
            return (1, 0, name_cf)
        if category == "XPD_*":
            name_rank = 9
            for index, token in enumerate(("jni", "x3d", "ld", "map", "surface", "render")):
                if token in name_cf or token in frame_text:
                    name_rank = index
                    break
            interesting_count = sum(1 for line in block.frames[:6] if any(term in line.casefold() for term in _SCENE_SO_HINT_TERMS))
            return (name_rank, -interesting_count, name_cf)
        return (9, 0, name_cf)

    def _format_subrealitytrace_report(
        self,
        *,
        source_path: Path,
        blocks: list[_ThreadTraceBlock],
        scene_source_blocks: list[_ThreadTraceBlock],
        categories: dict[str, list[_ThreadTraceBlock]],
        section_time: str,
    ) -> dict[str, Any]:
        scene_summaries = self._collect_scene_so_summaries(scene_source_blocks)
        current_time_label = self._subrealitytrace_display_time(source_path, section_time)
        previous_source = self._find_previous_subrealitytrace_source(source_path)
        previous_blocks = self._extract_subrealitytrace_blocks(self._read_text_tail(previous_source)) if previous_source else []
        current_focus = self._select_focus_trace_blocks(blocks)
        previous_focus = self._map_focus_trace_blocks(previous_blocks)
        thread_summaries = [
            self._build_focus_thread_summary(
                block,
                current_time_label=current_time_label,
                previous_block=previous_focus.get(self._focus_thread_key(block)),
                previous_time_label=self._subrealitytrace_display_time(previous_source, previous_blocks[0].section_time if previous_blocks else "") if previous_source else "",
            )
            for block in current_focus
        ]
        lines = [
            "单文件线程解析完成（subrealitytrace）",
            "说明: 当前输入缺少 ROM/Napa/APK 版本；已优先使用 trace 自带符号，并对 libunity.so / libmain.so 使用本地 Unity 符号表做补充反解。",
            f"文件: {source_path}",
        ]
        if section_time:
            lines.append(f"采样时间: {section_time}")
        counts = {label: len(items) for label, items in categories.items()}
        if scene_summaries:
            lines.append("场景/渲染相关 so:")
            for item in scene_summaries[:8]:
                lines.append(f"- {item['so_name']}: {item['summary']}")
        if counts:
            lines.append(
                "线程分类统计: "
                + ", ".join(
                    f"{label}={counts[label]}"
                    for label in ("UnityMain", "JniSurfaceTex*", "主线程", "渲染相关线程", "XPD_*")
                )
            )
        lines.append("结果如下：")
        for item in thread_summaries:
            lines.append("---")
            lines.append(item["title"])
            lines.extend(item["frame_lines"])
            if item["delta"]:
                lines.append("")
                lines.append(item["delta"])
        payload = {
            "source_path": str(source_path),
            "section_time": section_time,
            "current_time_label": current_time_label,
            "previous_source_path": str(previous_source) if previous_source else "",
            "scene_so_summaries": scene_summaries,
            "categories": {
                label: [
                    {
                        "name": block.name,
                        "sys_tid": block.tid,
                        "pid": block.pid,
                        "frames": block.frames[:12],
                    }
                    for block in items
                ]
                for label, items in categories.items()
            },
            "selected_threads": thread_summaries,
        }
        return {
            "message": "\n".join(lines),
            "payload": payload,
            "counts": counts,
            "source_path": str(source_path),
            "section_time": section_time,
        }

    def _select_focus_trace_blocks(self, blocks: list[_ThreadTraceBlock]) -> list[_ThreadTraceBlock]:
        picks: list[_ThreadTraceBlock] = []
        for block in blocks:
            key = self._focus_thread_key(block)
            if key == "unitymain" and not any(self._focus_thread_key(item) == key for item in picks):
                picks.append(block)
        for block in blocks:
            key = self._focus_thread_key(block)
            if key == "jnisurfacetextu" and not any(self._focus_thread_key(item) == key for item in picks):
                picks.append(block)
        if not picks:
            picks.extend(blocks[:2])
        return picks[:2]

    def _map_focus_trace_blocks(self, blocks: list[_ThreadTraceBlock]) -> dict[str, _ThreadTraceBlock]:
        mapped: dict[str, _ThreadTraceBlock] = {}
        for block in blocks:
            key = self._focus_thread_key(block)
            if key and key not in mapped:
                mapped[key] = block
        return mapped

    def _focus_thread_key(self, block: _ThreadTraceBlock) -> str:
        name_cf = block.name.casefold()
        if name_cf == "unitymain":
            return "unitymain"
        if name_cf.startswith("jnisurfacetextu"):
            return "jnisurfacetextu"
        if name_cf.startswith("jnisurfacetex"):
            return "jnisurfacetextu"
        return name_cf

    def _build_focus_thread_summary(
        self,
        block: _ThreadTraceBlock,
        *,
        current_time_label: str,
        previous_block: _ThreadTraceBlock | None,
        previous_time_label: str,
    ) -> dict[str, Any]:
        current_frames = self._resolve_trace_block_frames(block)
        previous_frames = self._resolve_trace_block_frames(previous_block) if previous_block is not None else []
        return {
            "name": block.name,
            "sys_tid": block.tid,
            "pid": block.pid,
            "title": f'{block.name}  sysTid={block.tid}' + (f"  ({current_time_label})" if current_time_label else ""),
            "frame_lines": [self._render_full_frame_line(item) for item in current_frames],
            "frames": [self._frame_to_dict(item) for item in current_frames],
            "delta": self._describe_trace_transition(current_frames, previous_frames, previous_time_label),
        }

    def _resolve_trace_block_frames(self, block: _ThreadTraceBlock | None) -> list[_FrameSummary]:
        if block is None:
            return []
        frames = [self._parse_trace_frame(line) for line in block.frames]
        return self._resolve_unity_frame_symbols(frames)

    def _parse_trace_frame(self, line: str) -> _FrameSummary:
        match = re.match(r"^\s*#(?P<frame_no>\d+)\s+pc\s+(?P<address>[0-9a-fA-F]{8,16})\s+(?P<path>\S+)(?P<rest>.*)$", line)
        if not match:
            return _FrameSummary(frame_no=-1, so_name="", address="", path="", symbol="")
        frame_no = int(match.group("frame_no"))
        address = match.group("address")
        path = match.group("path")
        remainder = match.group("rest")
        groups = self._extract_parenthetical_groups(remainder)
        symbol = ""
        for candidate in groups:
            candidate_text = candidate.strip()
            candidate_cf = candidate_text.casefold()
            if (
                not candidate_text
                or candidate_cf.startswith("buildid:")
                or candidate_cf.startswith("offset ")
                or candidate_cf == "deleted"
            ):
                continue
            symbol = candidate_text
            break
        return _FrameSummary(
            frame_no=frame_no,
            so_name=self._trace_display_so_name(path),
            address=address.lower(),
            path=path,
            symbol=symbol,
        )

    def _trace_display_so_name(self, path: str) -> str:
        if "jit-cache" in path:
            return "jit-cache"
        name = Path(path).name
        return name or path

    def _extract_parenthetical_groups(self, text: str) -> list[str]:
        groups: list[str] = []
        current: list[str] = []
        depth = 0
        for char in text:
            if char == "(":
                if depth > 0:
                    current.append(char)
                depth += 1
                continue
            if char == ")":
                if depth == 0:
                    continue
                depth -= 1
                if depth == 0:
                    groups.append("".join(current))
                    current = []
                else:
                    current.append(char)
                continue
            if depth > 0:
                current.append(char)
        return groups

    def _render_frame_summary(self, item: _FrameSummary) -> str:
        display = self._display_frame_symbol(item)
        return f"{item.so_name}: {display}"

    def _frame_to_dict(self, item: _FrameSummary) -> dict[str, Any]:
        return {
            "frame_no": item.frame_no,
            "so_name": item.so_name,
            "address": item.address,
            "symbol": item.symbol,
            "path": item.path,
            "display_symbol": self._display_frame_symbol(item),
        }

    def _render_full_frame_line(self, item: _FrameSummary) -> str:
        return f"#{item.frame_no:02d}  {self._display_frame_symbol(item)}  {item.so_name}"

    def _display_frame_symbol(self, item: _FrameSummary) -> str:
        if not item.symbol:
            return f"+0x{item.address}"
        symbol = re.sub(r"\+\d+$", "", item.symbol).strip()
        if "." in symbol and "::" not in symbol:
            parts = symbol.split(".")
            if len(parts) >= 2:
                symbol = ".".join(parts[-2:])
        if symbol.startswith("art::"):
            symbol = symbol[5:]
        symbol = re.sub(r"\([^)]*\)", "", symbol).strip()
        symbol = re.sub(r"<[^>]+>", "", symbol).strip()
        return symbol or item.symbol

    def _subrealitytrace_display_time(self, source_path: Path | None, section_time: str) -> str:
        if source_path is not None:
            match = re.search(r"subrealitytrace_(\d{4}-\d{2}-\d{2})-(\d{2})-(\d{2})-(\d{2})$", source_path.name)
            if match:
                return f"{match.group(2)}:{match.group(3)}:{match.group(4)}"
        time_match = re.search(r"(\d{2}:\d{2}:\d{2})", section_time)
        return time_match.group(1) if time_match else ""

    def _find_previous_subrealitytrace_source(self, source_path: Path) -> Path | None:
        match = re.search(r"subrealitytrace_(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})$", source_path.name)
        if not match:
            return None
        current_key = match.group(1)
        siblings = sorted(source_path.parent.glob("subrealitytrace_*"))
        previous: Path | None = None
        for candidate in siblings:
            candidate_match = re.search(r"subrealitytrace_(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})$", candidate.name)
            if not candidate_match:
                continue
            if candidate_match.group(1) < current_key:
                previous = candidate
            elif candidate == source_path:
                break
        return previous

    def _describe_trace_transition(
        self,
        current_frames: list[_FrameSummary],
        previous_frames: list[_FrameSummary],
        previous_time_label: str,
    ) -> str:
        if not current_frames or not previous_frames or not previous_time_label:
            return ""
        current_key = self._top_meaningful_frame(current_frames)
        previous_key = self._top_meaningful_frame(previous_frames)
        if current_key is None or previous_key is None:
            return ""
        current_text = self._display_frame_symbol(current_key)
        previous_text = self._display_frame_symbol(previous_key)
        if current_text == previous_text:
            return ""
        if "WaitVSync" in current_text:
            return f"▎ 与上一帧 {previous_time_label} 不同：当前已进入 {current_text}，说明 UnityMain 在等待上一帧 present / VSync。"
        if "ThreadedStreamBuffer::HandleOutOfBufferToReadFrom" in current_text:
            return f"▎ 与上一帧 {previous_time_label} 不同：当前已从 GPU 提交路径切到 {current_text}，说明渲染线程在等新的命令缓冲。"
        return f"▎ 与上一帧 {previous_time_label} 不同：顶部关键帧从 {previous_text} 切换为 {current_text}。"

    def _top_meaningful_frame(self, frames: list[_FrameSummary]) -> _FrameSummary | None:
        for item in frames:
            display = self._display_frame_symbol(item)
            if not display:
                continue
            if display in {"syscall", "__futex_wait_ex", "pthread_cond_wait", "__pthread_start", "__start_thread", "sem_wait"}:
                continue
            if display.startswith("UnityClassic::Baselib_SystemFutex_Wait") or display.startswith("Semaphore::WaitForSignal"):
                continue
            return item
        return frames[0] if frames else None

    def _collect_scene_so_summaries(self, blocks: list[_ThreadTraceBlock]) -> list[dict[str, str]]:
        buckets: dict[str, list[str]] = {}
        for block in blocks:
            for frame in self._resolve_unity_frame_symbols([self._parse_trace_frame(line) for line in block.frames]):
                so_cf = frame.so_name.casefold()
                if not so_cf or not any(term in so_cf for term in _SCENE_SO_HINT_TERMS):
                    continue
                buckets.setdefault(frame.so_name, [])
                rendered = self._display_frame_symbol(frame)
                if rendered not in buckets[frame.so_name]:
                    buckets[frame.so_name].append(rendered)
        ranked = sorted(
            buckets.items(),
            key=lambda item: (-len(item[1]), item[0].casefold()),
        )
        return [
            {
                "so_name": so_name,
                "summary": "; ".join(entries[:3]),
            }
            for so_name, entries in ranked
        ]
