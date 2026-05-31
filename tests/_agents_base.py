import os
from datetime import datetime
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from unittest import mock

import lark_agent_bridge.prompt_snapshots as prompt_snapshots_module
from lark_agent_bridge.agents import (
    BugAnalysisPlan,
    BugAnalysisRunner,
    ClaudeSkillRunner,
    IntentAnalysisFailure,
    IntentAnalysisRunner,
    OmlxChatClient,
    PerceptionSummaryRunner,
)
from lark_agent_bridge.agents.bug_summary_policy import (
    SummaryBackendInput,
    choose_summary_backend,
)
from lark_agent_bridge.agents.codex_app_server_runtime import CodexAppServerResult, CompletionState
from lark_agent_bridge.agents.llm_client import LLMClientError, LLMResponse
from lark_agent_bridge.models import (
    AIProviderOptions,
    BridgeConfig,
    BugRequest,
    CodexAppServerOptions,
    DirectAnalysisRequest,
    DownloadedResource,
    DownloadResource,
    InternalNetworkEnvOptions,
    IntentDecision,
    LarkEvent,
    PerceptionSummaryRequest,
    SourceInvestigationOptions,
    TaskResult,
)
from lark_agent_bridge.replay import AnalysisReplayContext, ReplayResourceBundle
from lark_agent_bridge.prompt_snapshots import (
    BugPromptSnapshot,
    SnapshotEvidence,
    SnapshotFact,
)
from lark_agent_bridge.reporting import ReportComposition


class _AgentTestBase(unittest.TestCase):
    def setUp(self):
        self._bug_decision_patcher = mock.patch.object(
            BugAnalysisRunner,
            "_run_bug_decision_agent",
            return_value=(None, ""),
        )
        self._bug_decision_patcher.start()
    def tearDown(self):
        self._bug_decision_patcher.stop()
    def _write_matching_log(self, root: Path, timestamp: str = "2026-05-16 10:01:00") -> Path:
        path = root / "time_anchor.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{timestamp} I TestTag: time anchor\n", encoding="utf-8")
        return path


__all__ = [
    'os',
    'datetime',
    'Path',
    'json',
    'subprocess',
    'sys',
    'tempfile',
    'unittest',
    'urllib',
    'zipfile',
    'mock',
    'prompt_snapshots_module',
    'BugAnalysisPlan',
    'BugAnalysisRunner',
    'ClaudeSkillRunner',
    'IntentAnalysisFailure',
    'IntentAnalysisRunner',
    'OmlxChatClient',
    'PerceptionSummaryRunner',
    'SummaryBackendInput',
    'choose_summary_backend',
    'CodexAppServerResult',
    'CompletionState',
    'LLMClientError',
    'LLMResponse',
    'AIProviderOptions',
    'BridgeConfig',
    'BugRequest',
    'CodexAppServerOptions',
    'DirectAnalysisRequest',
    'DownloadedResource',
    'DownloadResource',
    'InternalNetworkEnvOptions',
    'IntentDecision',
    'LarkEvent',
    'PerceptionSummaryRequest',
    'SourceInvestigationOptions',
    'TaskResult',
    'BugPromptSnapshot',
    'SnapshotEvidence',
    'SnapshotFact',
    'AnalysisReplayContext',
    'ReplayResourceBundle',
    'ReportComposition',
    '_AgentTestBase',
]



if __name__ == "__main__":
    unittest.main()
