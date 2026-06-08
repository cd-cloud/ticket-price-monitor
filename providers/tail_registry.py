"""Tail-discovery runner registry."""

from __future__ import annotations

from typing import Any

from providers.ctrip_tail_runner import CtripTailDiscoveryRunner
from providers.feizhu_tail_runner import FeizhuTailDiscoveryRunner


TAIL_RUNNERS = {
    "ctrip": CtripTailDiscoveryRunner,
    "feizhu": FeizhuTailDiscoveryRunner,
}


def create_tail_runner(provider_name: str, automation: Any) -> Any:
    normalized = str(provider_name or "").strip().lower()
    runner_cls = TAIL_RUNNERS.get(normalized)
    if runner_cls is None:
        raise RuntimeError(f"Provider {provider_name!r} does not support tail discovery.")
    return runner_cls(automation)


def supports_tail_discovery(provider_name: str) -> bool:
    return str(provider_name or "").strip().lower() in TAIL_RUNNERS
