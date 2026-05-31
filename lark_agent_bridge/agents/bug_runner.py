from __future__ import annotations

from .bug._shared import *  # noqa: F401,F403
from .bug.resolve_source import _ResolveSourceMixin
from .bug.run_bug import _RunBugMixin
from .bug.run_bug_2 import _RunBug2Mixin
from .bug.bug_cache import _BugCacheMixin
from .bug.archive_extract import _ArchiveExtractMixin
from .bug.general_summary import _GeneralSummaryMixin
from .bug.signal_android import _SignalAndroidMixin
from .bug.custom_skill import _CustomSkillMixin
from .bug.ld_executor import _LdExecutorMixin
from .bug.bug_prompt import _BugPromptMixin
from .bug.direct_api import _DirectApiMixin
from .bug.run_bug_3 import _RunBug3Mixin
from .bug.render_bug import _RenderBugMixin


class BugAnalysisRunner(
    _ResolveSourceMixin,
    _RunBugMixin,
    _RunBug2Mixin,
    _BugCacheMixin,
    _ArchiveExtractMixin,
    _GeneralSummaryMixin,
    _SignalAndroidMixin,
    _CustomSkillMixin,
    _LdExecutorMixin,
    _BugPromptMixin,
    _DirectApiMixin,
    _RunBug3Mixin,
    _RenderBugMixin,
):
    _SOURCE_EVIDENCE_WAIT_SECONDS = 30
    _SOURCE_EVIDENCE_TOTAL_BUDGET_SECONDS = 30.0
    _SOURCE_EVIDENCE_CODEGRAPH_CALL_SECONDS = 5.0
