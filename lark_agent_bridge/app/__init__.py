from __future__ import annotations

from ._shared import *  # noqa: F401,F403
from .handle_event import _HandleEventMixin
from .replay_flow import _ReplayFlowMixin
from .result_bug import _ResultBugMixin
from .log_resources import _LogResourcesMixin
from .context_from import _ContextFromMixin


class BridgeApp(
    _HandleEventMixin,
    _ReplayFlowMixin,
    _ResultBugMixin,
    _LogResourcesMixin,
    _ContextFromMixin,
):
    pass
