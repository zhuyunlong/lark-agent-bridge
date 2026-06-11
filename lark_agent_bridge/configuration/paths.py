"""工作区/仓库路径推导与误配置告警。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .logging import get_logger

if TYPE_CHECKING:
    from ..models import BridgeConfig

_logger = get_logger("config")

TEMP_PATH_MARKERS = ("/var/folders/", "/tmp/", "/private/var/folders/", "/private/tmp/")


def _is_under_tool_dir(path: Path) -> bool:
    """True when ``path`` is (or lives inside) the lark-agent-bridge tool itself."""
    try:
        resolved = path.resolve()
    except OSError:
        resolved = Path(path).expanduser()
    return resolved.name == "lark-agent-bridge" and resolved.parent.name == "tools"


def _is_temp_path(path: Path) -> bool:
    try:
        text = str(path.resolve())
    except OSError:
        text = str(path)
    return any(marker in text for marker in TEMP_PATH_MARKERS)


def _default_workspace_root(base_dir: Path) -> Path:
    if base_dir.name == "lark-agent-bridge" and base_dir.parent.name == "tools":
        return base_dir.parent.parent
    return base_dir


def _default_repo_roots(guideengine: Path, napa5: Path) -> list[str]:
    """Build default repo_roots list, including only paths that exist."""
    roots = [str(guideengine)]
    if napa5.exists():
        roots.append(str(napa5))
    return roots


def _warn_suspicious_paths(config: "BridgeConfig") -> None:
    """Emit warnings when workspace_root / guideengine_repo look misconfigured.

    Targets the failure mode where subprocess prompts receive either a sandbox
    tempdir as workspace_root or the tool's own directory as guideengine_repo.
    """
    workspace = config.workspace_root
    repo = config.guideengine_repo

    if _is_temp_path(workspace):
        _logger.warning(
            "workspace_root=%s is a sandbox/temp path; subprocess skill scripts will not be found. "
            "Set LARK_AGENT_BRIDGE_WORKSPACE_ROOT or workspace_root in config.toml.",
            workspace,
        )
    elif _is_under_tool_dir(workspace):
        _logger.warning(
            "workspace_root=%s points at the lark-agent-bridge tool itself; set an explicit "
            "LARK_AGENT_BRIDGE_WORKSPACE_ROOT or workspace_root in config.toml.",
            workspace,
        )

    if _is_under_tool_dir(repo):
        _logger.warning(
            "guideengine_repo=%s points at the lark-agent-bridge tool itself; "
            "set LARK_AGENT_BRIDGE_GUIDEENGINE_REPO or guideengine_repo in config.toml.",
            repo,
        )
    elif _is_temp_path(repo):
        _logger.warning(
            "guideengine_repo=%s is a sandbox/temp path; source analysis will see no code. "
            "Set LARK_AGENT_BRIDGE_GUIDEENGINE_REPO or guideengine_repo in config.toml.",
            repo,
        )
    elif not repo.exists():
        _logger.warning(
            "guideengine_repo=%s does not exist; source investigation will fail. "
            "Set LARK_AGENT_BRIDGE_GUIDEENGINE_REPO or guideengine_repo in config.toml.",
            repo,
        )
    elif repo == workspace:
        _logger.warning(
            "guideengine_repo=%s equals workspace_root; source analysis needs an explicit code repo.",
            repo,
        )
