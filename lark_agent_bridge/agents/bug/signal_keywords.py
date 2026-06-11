from __future__ import annotations

from ._shared import *  # noqa: F401,F403


class _SignalKeywordsMixin:
    """信号关键词：源码事实提取、关键词画像缓存、日志扫描与最佳命中（与 SignalAndroidMixin 共享 self 状态）。"""

    def _signal_android_source_facts(
        self,
        signal_payload: dict[str, object],
        source_evidence_path: Path | None,
    ) -> dict[str, object]:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        signal_name = str(signal.get("name") or "").strip()
        lifecycle = signal_payload.get("lifecycle_report", {}) if isinstance(signal_payload, dict) else {}
        context = lifecycle.get("context", {}) if isinstance(lifecycle, dict) else {}
        helper_file = str(context.get("helper_file") or "") if isinstance(context, dict) else ""
        facts: dict[str, object] = {
            "event_ids": [],
            "producer_terms": [],
            "consumer_terms": [],
            "producer_refs": [],
            "consumer_refs": [],
            "keywords": [],
        }
        event_ids: list[str] = []
        producer_terms: list[str] = []
        producer_refs: list[str] = []
        helper_path = self._repo_relative_path(helper_file)
        if helper_path is not None:
            helper_text = self._read_text_quiet(helper_path)
            helper_lines = helper_text.splitlines()
            consts = self._signal_kotlin_string_constants(helper_text)
            for line_no, line in enumerate(helper_lines, 1):
                if signal_name and signal_name in line:
                    matched_event = False
                    for nearby_no in range(line_no, min(len(helper_lines), line_no + 8) + 1):
                        nearby = helper_lines[nearby_no - 1]
                        match = re.search(r"\b(\d{4,})\b\s*(?:to|,)\s*SignalCode\.([A-Z0-9_]+)", nearby)
                        if match:
                            event_ids.append(match.group(1))
                            producer_refs.append(f"{helper_file}:{nearby_no} {nearby.strip()}")
                            matched_event = True
                            break
                    if not matched_event:
                        match = re.search(r"\b(\d{4,})\b\s*(?:to|,)\s*SignalCode\.([A-Z0-9_]+)", line)
                        if match:
                            event_ids.append(match.group(1))
                    producer_refs.append(f"{helper_file}:{line_no} {line.strip()}")
            for event_id in list(dict.fromkeys(event_ids)):
                callback_name = self._signal_callback_name_for_event(helper_text, event_id)
                if callback_name:
                    callback_lines = self._signal_kotlin_function_block(helper_lines, callback_name)
                    producer_lines = self._signal_kotlin_target_branch(callback_lines, signal_name)
                    for offset, line in producer_lines:
                        if signal_name and signal_name in line:
                            producer_refs.append(f"{helper_file}:{offset} {line.strip()}")
                        for token in re.findall(r"\b(?:EVENT_KEY|VALUE_KEY)_[A-Z0-9_]+\b", line):
                            producer_terms.append(token)
                            if token in consts:
                                producer_terms.append(consts[token])
                        literal = self._signal_log_literal_from_source_line(line)
                        if literal:
                            producer_terms.append(literal)
        refs = []
        refs.extend(signal_payload.get("source_references", []) if isinstance(signal_payload, dict) else [])
        refs.extend(self._parse_source_evidence_entries(source_evidence_path))
        refs.extend(self._signal_repo_signal_references(signal_name))
        consumer_refs: list[str] = []
        consumer_terms: list[str] = []
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            file_text = str(ref.get("file") or "")
            line_text = str(ref.get("text") or "")
            if signal_name and signal_name in line_text and "module_proto" not in file_text and "module_datacenter" not in file_text:
                consumer_refs.append(f"{file_text}:{ref.get('line') or ''} {line_text}".strip())
                path = self._repo_relative_path(file_text)
                if path is not None:
                    consumer_terms.extend(self._signal_consumer_log_terms(path, signal_name))
        keywords = self._unique_nonempty([*event_ids, *producer_terms, *consumer_terms, signal_name])
        facts["event_ids"] = self._unique_nonempty(event_ids)
        facts["producer_terms"] = self._unique_nonempty(producer_terms)
        facts["consumer_terms"] = self._unique_nonempty(consumer_terms)
        facts["producer_refs"] = self._unique_nonempty(producer_refs)
        facts["consumer_refs"] = self._unique_nonempty(consumer_refs)
        facts["keywords"] = keywords
        facts["source_signature"] = self._signal_keyword_source_signature(facts)
        return facts
    def _signal_merge_cached_keywords(
        self,
        signal_payload: dict[str, object],
        source_facts: dict[str, object],
    ) -> dict[str, object]:
        cache_entry = self._signal_load_keyword_profile(signal_payload)
        current_signature = str(source_facts.get("source_signature") or "")
        cache_status = "miss"
        cache_updated_at = ""
        if cache_entry:
            cached_signature = str(cache_entry.get("source_signature") or "")
            cache_updated_at = str(cache_entry.get("updated_at") or "")
            if current_signature and cached_signature == current_signature:
                cache_status = "hit"
                for key in ("event_ids", "producer_terms", "consumer_terms", "producer_refs", "consumer_refs"):
                    cached_values = cache_entry.get(key, [])
                    if isinstance(cached_values, list):
                        source_facts[key] = self._unique_nonempty([*(source_facts.get(key, []) or []), *cached_values])
                cached_keywords = cache_entry.get("keywords", [])
                source_facts["keywords"] = self._unique_nonempty([*(source_facts.get("keywords", []) or []), *(cached_keywords if isinstance(cached_keywords, list) else [])])
            elif not current_signature:
                cache_status = "fallback"
                for key in ("event_ids", "producer_terms", "consumer_terms", "producer_refs", "consumer_refs", "keywords"):
                    cached_values = cache_entry.get(key, [])
                    if isinstance(cached_values, list):
                        source_facts[key] = self._unique_nonempty([*(source_facts.get(key, []) or []), *cached_values])
                source_facts["source_signature"] = cached_signature
            else:
                cache_status = "stale_refresh"
        source_facts["keyword_cache"] = {
            "status": cache_status,
            "updated_at": cache_updated_at,
            "source_signature": str(source_facts.get("source_signature") or ""),
        }
        return source_facts
    def _signal_keyword_source_signature(self, source_facts: dict[str, object]) -> str:
        payload = {
            key: source_facts.get(key, [])
            for key in ("event_ids", "producer_refs", "consumer_refs", "producer_terms", "consumer_terms")
        }
        if not any(isinstance(value, list) and value for value in payload.values()):
            return ""
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    def _signal_keyword_cache_path(self) -> Path:
        return Path(self.config.data_dir).expanduser().resolve() / "signal_keyword_cache.json"
    def _signal_keyword_cache_key(self, signal_payload: dict[str, object]) -> str:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        code = str(signal.get("code") or "").strip()
        name = str(signal.get("name") or "").strip()
        return code or name or "unknown"
    def _signal_load_keyword_profile(self, signal_payload: dict[str, object]) -> dict[str, object] | None:
        path = self._signal_keyword_cache_path()
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        entry = payload.get(self._signal_keyword_cache_key(signal_payload))
        return entry if isinstance(entry, dict) else None
    def _signal_store_keyword_profile(
        self,
        signal_payload: dict[str, object],
        source_facts: dict[str, object],
        *,
        scan_terms: list[str],
        log_hits: dict[str, list[dict[str, object]]],
        selected_input: Path | None,
        source_evidence_path: Path | None,
    ) -> None:
        cache_key = self._signal_keyword_cache_key(signal_payload)
        if not cache_key or cache_key == "unknown":
            return
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        verified_terms = [term for term in self._unique_nonempty(scan_terms) if log_hits.get(term)]
        profile = {
            "signal_code": str(signal.get("code") or ""),
            "signal_name": str(signal.get("name") or ""),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "source_signature": str(source_facts.get("source_signature") or ""),
            "event_ids": source_facts.get("event_ids", []),
            "producer_terms": source_facts.get("producer_terms", []),
            "consumer_terms": source_facts.get("consumer_terms", []),
            "producer_refs": source_facts.get("producer_refs", []),
            "consumer_refs": source_facts.get("consumer_refs", []),
            "keywords": source_facts.get("keywords", []),
            "verified_terms": verified_terms,
            "selected_input": str(selected_input) if selected_input else "",
            "source_evidence_file": str(source_evidence_path) if source_evidence_path else "",
        }
        path = self._signal_keyword_cache_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    payload = {}
            else:
                payload = {}
            payload[cache_key] = profile
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            return
    def _signal_android_scan_terms(self, signal_payload: dict[str, object], source_facts: dict[str, object]) -> list[str]:
        signal = signal_payload.get("signal", {}) if isinstance(signal_payload, dict) else {}
        signal_name = str(signal.get("name") or "").strip()
        lifecycle = signal_payload.get("lifecycle_report", {}) if isinstance(signal_payload, dict) else {}
        context = lifecycle.get("context", {}) if isinstance(lifecycle, dict) else {}
        helper_class = str(context.get("helper_class") or "").strip() if isinstance(context, dict) else ""
        helper_file = str(context.get("helper_file") or "").strip() if isinstance(context, dict) else ""
        helper_full_class = self._signal_kotlin_class_from_file(helper_file, helper_class)
        controller_class = str(context.get("controller_class") or "").strip() if isinstance(context, dict) else ""
        terms: list[str] = [
            signal_name,
            f"getSignalFlow: signalCode={signal_name}" if signal_name else "",
            f"injectSignalProvider[{helper_full_class}" if helper_full_class else "",
            helper_class,
            controller_class,
            "injectSignalProvider[",
        ]
        for event_id in source_facts.get("event_ids", []) if isinstance(source_facts, dict) else []:
            event_id_text = str(event_id)
            terms.extend(
                [
                    f"register {event_id_text} indeed",
                    f"registerRemoteListener: event:{event_id_text}",
                    f"registerEventListener: event:{event_id_text}",
                    f"not support id:{event_id_text}",
                    f"register not support id:{event_id_text}",
                ]
            )
        for key in ("producer_terms", "consumer_terms"):
            terms.extend(str(item) for item in source_facts.get(key, []) if str(item).strip())
        return self._unique_nonempty(terms)
    def _signal_scan_log_terms(
        self,
        signal_payload: dict[str, object],
        selected_input: Path | None,
        terms: list[str],
        *,
        max_hits_per_term: int = 20,
        max_files: int = 80,
        max_lines_per_file: int = 200000,
    ) -> dict[str, list[dict[str, object]]]:
        clean_terms = self._unique_nonempty(terms)
        if not clean_terms:
            return {}
        files: list[Path] = []
        for item in self._signal_evidence_items(signal_payload):
            path = Path(str(item.get("file") or "")).expanduser()
            if path.exists() and path.is_file():
                files.append(path)
        if selected_input is not None and selected_input.exists():
            files.extend(self._iter_log_coverage_files(selected_input)[:max_files])
        unique_files: list[Path] = []
        seen_files: set[str] = set()
        for path in files:
            key = str(path)
            if key in seen_files:
                continue
            seen_files.add(key)
            unique_files.append(path)
            if len(unique_files) >= max_files:
                break
        hits: dict[str, list[dict[str, object]]] = {term: [] for term in clean_terms}
        remaining = set(clean_terms)
        for path in unique_files:
            if not remaining:
                break
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line_no, line in enumerate(handle, 1):
                        if line_no > max_lines_per_file:
                            break
                        for term in list(remaining):
                            if term and term in line:
                                hits[term].append({"term": term, "file": str(path), "line": line_no, "text": line.rstrip()})
                                if len(hits[term]) >= max_hits_per_term:
                                    remaining.discard(term)
                        if not remaining:
                            break
            except OSError:
                continue
        return {term: items for term, items in hits.items() if items}
    def _signal_best_log_hit(
        self,
        hits: dict[str, list[dict[str, object]]],
        terms: list[str],
        *,
        pid: str = "",
        package: str = "",
    ) -> dict[str, object] | None:
        ordered_terms = self._unique_nonempty(terms)
        if pid or package:
            for term in ordered_terms:
                for item in hits.get(term, []) or []:
                    text = str(item.get("text") or "")
                    file_text = str(item.get("file") or "")
                    item_pid = self._signal_pid_from_text(text)
                    if pid and item_pid and item_pid != pid:
                        continue
                    if package and package not in file_text and package not in text:
                        item_package = self._signal_package_from_path(file_text)
                        if item_package and item_package != package:
                            continue
                    return item
        for term in ordered_terms:
            items = hits.get(term)
            if items:
                return items[0]
        return None
    def _signal_log_hit_text(self, hit: dict[str, object] | None) -> str:
        if not isinstance(hit, dict):
            return ""
        file_name = Path(str(hit.get("file") or "")).name
        line = str(hit.get("line") or "")
        text = self._signal_shorten(str(hit.get("text") or ""), 180)
        return f"{file_name}:{line} {text}".strip()
    def _signal_runtime_evidence_text(self, signal_payload: dict[str, object], keyword: str) -> str:
        event = self._signal_find_runtime_event(signal_payload, "", "", keyword)
        if event is None:
            return ""
        return self._signal_log_hit_text(event)
    def _signal_source_fact_text(self, source_facts: dict[str, object], key: str) -> str:
        values = source_facts.get(key, []) if isinstance(source_facts, dict) else []
        if not isinstance(values, list):
            return ""
        return "；".join(str(item) for item in values[:3] if str(item).strip())
