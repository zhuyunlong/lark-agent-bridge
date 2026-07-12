from __future__ import annotations

import json
from pathlib import Path
import re

from ._shared import *  # noqa: F401,F403


class _RequestBuildMixin:
    """各业务域请求对象构建（与 LogResourcesMixin 共享 self 状态）。"""

    def _build_signal_request(self, route_content: str, referenced_resources: list[DownloadResource]) -> SignalRequest:
        request = parse_signal_request(
            route_content,
            signal_aliases=self.config.signal_aliases,
            command_prefixes=self.config.command_prefixes,
            signal_resolver=self.signal_resolver,
        )
        if not referenced_resources:
            return request
        return SignalRequest(
            signal=request.signal,
            resources=self._merge_resources(request.resources, referenced_resources),
            since=request.since,
            raw_text=request.raw_text,
            triggered=request.triggered,
            error=request.error,
        )
    def _build_addr2line_request(
        self,
        route_content: str,
        event: LarkEvent,
        referenced_resources: list[DownloadResource] | None = None,
    ) -> Addr2LineRequest:
        resources = referenced_resources or []
        if not resources:
            probe = parse_addr2line_request(route_content, allow_missing_address=True)
            if probe.triggered:
                resources = self._reference_chain_log_resources(event)
        request = parse_addr2line_request(route_content, allow_missing_address=bool(resources))
        if request.triggered and resources and not request.resources:
            request = Addr2LineRequest(
                addr_text=request.addr_text,
                resources=resources,
                raw_text=request.raw_text,
                rom_version=request.rom_version,
                napa_version=request.napa_version,
                apk_version=request.apk_version,
                symbol_table_url=request.symbol_table_url,
                napa5_download_url=request.napa5_download_url,
                log_folder=request.log_folder,
                fault_time=request.fault_time,
                target=request.target,
                prompt=request.prompt,
                triggered=request.triggered,
                error=request.error,
            )
        if not request.triggered:
            return request
        if request.rom_version and not request.apk_version and not request.napa_version:
            recent_required = self._recent_required_outputs_for_chat(event, request.rom_version)
            recent_apk = self._navigation_version_from_required_outputs(recent_required)
            if recent_apk:
                request = Addr2LineRequest(
                    addr_text=request.addr_text,
                    resources=request.resources,
                    raw_text=request.raw_text,
                    rom_version=request.rom_version,
                    napa_version=request.napa_version,
                    apk_version=recent_apk,
                    symbol_table_url=request.symbol_table_url or str(recent_required.get("symbol_table_url") or "").strip(),
                    napa5_download_url=request.napa5_download_url or str(recent_required.get("napa5_download_url") or "").strip(),
                    log_folder=request.log_folder,
                    fault_time=request.fault_time,
                    target=request.target,
                    prompt=request.prompt,
                    triggered=request.triggered,
                    error=None if request.error == "missing_symbol_version" else request.error,
                )
        if request.rom_version or request.napa_version or request.apk_version:
            return request
        inherited_rom = self._recent_rom_version_for_chat(event)
        if not inherited_rom:
            return request
        inherited_required = self._recent_required_outputs_for_chat(event, inherited_rom)
        inherited_apk = self._navigation_version_from_required_outputs(inherited_required)
        return Addr2LineRequest(
            addr_text=request.addr_text,
            resources=request.resources,
            raw_text=request.raw_text,
            rom_version=inherited_rom,
            napa_version=request.napa_version,
            apk_version=inherited_apk or request.apk_version,
            symbol_table_url=request.symbol_table_url or str(inherited_required.get("symbol_table_url") or "").strip(),
            napa5_download_url=request.napa5_download_url or str(inherited_required.get("napa5_download_url") or "").strip(),
            log_folder=request.log_folder,
            fault_time=request.fault_time,
            target=request.target,
            prompt=request.prompt,
            triggered=True,
            error=None if request.error == "missing_symbol_version" else request.error,
        )
    def _followup_addr2line_request(self, ctx: _RouteContext) -> Addr2LineRequest | None:
        followup_context = ctx.followup_context
        if followup_context is None or str(getattr(followup_context, "mode", "") or "") != "addr2line_resolve":
            return None
        action = ctx.conversation_input.followup_action
        if action != "retry":
            return None
        previous_request = parse_addr2line_request(
            str(getattr(followup_context, "request_text", "") or ""),
            allow_missing_address=True,
        )
        resources = self._reference_chain_log_resources(ctx.event) or self._log_resources_from_context(followup_context)
        session = self.activity_store.get_session(followup_context.root_message_id) or {}
        session_details = session.get("details") if isinstance(session.get("details"), dict) else {}
        session_rom = str(session_details.get("rom_version") or "")
        session_symbol = str(session_details.get("symbol_version") or "")
        session_symbol_kind = str(session_details.get("symbol_version_kind") or "")
        rom_version = previous_request.rom_version or session_rom or self._recent_rom_version_for_chat(ctx.event)
        apk_version = previous_request.apk_version
        napa_version = previous_request.napa_version
        if session_symbol:
            if session_symbol_kind == "apk" and not apk_version:
                apk_version = session_symbol
            elif session_symbol_kind == "napa" and not napa_version:
                napa_version = session_symbol
            elif session_symbol_kind == "rom" and not rom_version:
                rom_version = session_symbol
        if not apk_version and rom_version:
            apk_version = self._recent_navigation_version_for_chat(ctx.event, rom_version) or apk_version
        recent_required = self._recent_required_outputs_for_chat(ctx.event, rom_version) if rom_version else {}
        return Addr2LineRequest(
            addr_text=previous_request.addr_text,
            resources=resources,
            raw_text=ctx.route_content,
            rom_version=rom_version,
            napa_version=napa_version,
            apk_version=apk_version,
            symbol_table_url=previous_request.symbol_table_url or str(recent_required.get("symbol_table_url") or "").strip(),
            napa5_download_url=previous_request.napa5_download_url or str(recent_required.get("napa5_download_url") or "").strip(),
            log_folder=previous_request.log_folder,
            fault_time=previous_request.fault_time,
            target=previous_request.target or "auto",
            prompt=ctx.route_content.strip(),
            triggered=True,
        )
    def _build_perception_summary_request(self, route_content: str, referenced_resources: list[DownloadResource]):
        request = parse_perception_summary_request(route_content)
        merged_resources = self._merge_resources(request.resources, referenced_resources)
        if request.triggered:
            return request.__class__(
                prompt=request.prompt,
                resources=merged_resources,
                raw_text=request.raw_text,
                triggered=True,
                error=request.error,
            )
        if not referenced_resources:
            return request
        hinted = f"{route_content.strip()} {' '.join(item.value for item in referenced_resources)}".strip()
        hinted_request = parse_perception_summary_request(hinted)
        if not hinted_request.triggered:
            return request
        return request.__class__(
            prompt=route_content.strip(),
            resources=merged_resources,
            raw_text=route_content,
            triggered=True,
            error=None if route_content.strip() else "missing_prompt",
        )
    def _build_direct_analysis_request(
        self,
        route_content: str,
        referenced_resources: list[DownloadResource],
        *,
        event: LarkEvent | None = None,
    ):
        request = parse_direct_analysis_request(route_content)
        local_resources = self._authorized_local_download_resources(event, route_content)
        merged_resources = self._merge_resources(request.resources, [*referenced_resources, *local_resources])
        if request.triggered:
            return request.__class__(
                prompt=request.prompt,
                resources=merged_resources,
                raw_text=request.raw_text,
                triggered=True,
                error=request.error,
            )
        if local_resources and self._looks_like_direct_analysis_prompt(route_content):
            return request.__class__(
                prompt=route_content.strip(),
                resources=merged_resources,
                raw_text=route_content,
                triggered=True,
                error=None if route_content.strip() else "missing_prompt",
            )
        if not referenced_resources:
            return request
        hinted = f"{route_content.strip()} {' '.join(item.value for item in referenced_resources)}".strip()
        hinted_request = parse_direct_analysis_request(hinted)
        if not hinted_request.triggered:
            prompt = route_content.strip()
            if not prompt or not self._looks_like_direct_analysis_prompt(route_content):
                return request
            return request.__class__(
                prompt=prompt,
                resources=merged_resources,
                raw_text=route_content,
                triggered=True,
                error=None,
            )
        return request.__class__(
            prompt=route_content.strip(),
            resources=merged_resources,
            raw_text=route_content,
            triggered=True,
            error=None if route_content.strip() else "missing_prompt",
        )
    def _build_app_server_investigation_request(
        self,
        route_content: str,
        referenced_resources: list[DownloadResource],
        *,
        event: LarkEvent | None = None,
    ):
        request = parse_app_server_investigation_request(
            route_content,
            bug_url_re=self.bug_url_re,
            auto_terms=self.config.bug_analysis.app_server_investigation.auto_terms,
            free_terms=self.config.bug_analysis.app_server_investigation.free_terms,
        )
        if not request.triggered:
            return request
        local_resources = self._authorized_local_download_resources(event, route_content)
        merged_resources = self._merge_resources(request.resources, [*referenced_resources, *local_resources])
        return request.__class__(
            prompt=request.prompt,
            bug_url=request.bug_url,
            resources=merged_resources,
            raw_text=request.raw_text,
            triggered=True,
            error=request.error,
            trigger_mode=request.trigger_mode,
            trigger_term=request.trigger_term,
        )
