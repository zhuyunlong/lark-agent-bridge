"""配置子包：装配主干（loader）、分节构建器（sections）、预设（presets）、
路径推导（paths）、值转换（coercion）。"""

from .loader import DEFAULT_SIGNAL_ALIASES, load_config, with_cli_overrides

__all__ = ["DEFAULT_SIGNAL_ALIASES", "load_config", "with_cli_overrides"]
