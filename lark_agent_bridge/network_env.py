"""Helpers for subprocess environments used by internal-network skills."""

from __future__ import annotations

import os
from typing import Mapping

from .models import InternalNetworkEnvOptions


def build_internal_network_env(
    options: InternalNetworkEnvOptions,
    *,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = dict(base or os.environ)
    inherit_env = [key for key in options.inherit_env if key]
    if inherit_env:
        env = {key: source[key] for key in inherit_env if key in source}
    else:
        env = dict(source)
    for key in options.unset_env:
        if key:
            env.pop(key, None)
    return env
