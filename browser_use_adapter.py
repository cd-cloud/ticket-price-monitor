"""Optional integration point for the upstream browser-use project.

The current tracker keeps Playwright as the deterministic default because the
price parsers depend on captured network payloads. This adapter gives every
query path one shared place to enable browser-use once the local Python
environment supports it.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from private_files import atomic_write_text, ensure_private_directory, make_file_private


BROWSER_USE_MIN_PYTHON = (3, 11)
LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class BrowserUseStatus:
    available: bool
    python_version: str
    reason: str
    config_dir: str | None = None


@dataclass(slots=True)
class BrowserUseAssistResult:
    attempted: bool
    success: bool
    reason: str
    artifact_path: str | None = None
    page_state: dict[str, Any] | None = None


def classify_page_state(*, text: str = "", url: str = "", title: str = "") -> dict[str, Any]:
    haystack = " ".join([text or "", url or "", title or ""]).lower()
    states: list[str] = []
    signals: list[str] = []

    verification_tokens = ("captcha", "verify", "verification", "robot", "验证码", "安全验证", "滑块", "访问异常")
    city_tokens = ("城市", "机场", "出发地", "目的地", "city", "airport")
    date_tokens = ("日期", "calendar", "datepicker", "出发日期", "返回日期")
    result_tokens = ("航班", "起飞", "到达", "中转", "¥", "￥", "price", "flight")
    empty_tokens = ("无航班", "暂无", "没有找到", "售罄", "no flights", "no results")

    if any(token in haystack for token in verification_tokens):
        states.append("verification")
        signals.append("verification keyword")
    if any(token in haystack for token in city_tokens) and ("dropdown" in haystack or "选择" in haystack or "search" in haystack):
        states.append("city_picker")
        signals.append("city picker keyword")
    if any(token in haystack for token in date_tokens):
        states.append("date_picker")
        signals.append("date keyword")
    if any(token in haystack for token in empty_tokens):
        states.append("empty_results")
        signals.append("empty-results keyword")
    if any(token in haystack for token in result_tokens):
        states.append("results_page")
        signals.append("flight-result keyword")

    primary = states[0] if states else "unknown"
    return {
        "primary": primary,
        "states": states or ["unknown"],
        "signals": signals,
        "text_length": len(text or ""),
    }


def _prepare_browser_use_environment(runtime_dir: Path | None = None) -> Path | None:
    if runtime_dir is None:
        return None
    config_dir = runtime_dir / "browseruse"
    config_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("BROWSER_USE_CONFIG_DIR", str(config_dir))
    os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
    return config_dir


def browser_use_status(runtime_dir: Path | None = None, verify_runtime_import: bool = False) -> BrowserUseStatus:
    config_dir = _prepare_browser_use_environment(runtime_dir)
    python_version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info < BROWSER_USE_MIN_PYTHON:
        return BrowserUseStatus(
            available=False,
            python_version=python_version,
            reason=(
                "browser-use requires Python >= 3.11; "
                f"current interpreter is Python {python_version}."
            ),
            config_dir=str(config_dir) if config_dir else None,
        )

    if importlib.util.find_spec("browser_use") is None:
        return BrowserUseStatus(
            available=False,
            python_version=python_version,
            reason="browser-use is not installed in the active Python environment.",
            config_dir=str(config_dir) if config_dir else None,
        )

    if verify_runtime_import:
        try:
            from browser_use import Agent, Browser  # noqa: F401
        except Exception as exc:
            return BrowserUseStatus(
                available=False,
                python_version=python_version,
                reason=f"browser-use is installed but runtime import failed: {exc}",
                config_dir=str(config_dir) if config_dir else None,
            )

    return BrowserUseStatus(
        available=True,
        python_version=python_version,
        reason="browser-use is installed and the Python version is compatible.",
        config_dir=str(config_dir) if config_dir else None,
    )


class BrowserUseAdapter:
    """Small facade used by BrowserAutomation to keep backend switching central."""

    def __init__(self, runtime_dir: Path | None = None) -> None:
        self.runtime_dir = runtime_dir
        self.status = browser_use_status(runtime_dir, verify_runtime_import=False)
        self._last_page_state: dict[str, Any] | None = None

    def ensure_available(self) -> None:
        self.status = browser_use_status(self.runtime_dir, verify_runtime_import=True)
        if not self.status.available:
            raise RuntimeError(
                "automation_backend=browser_use is configured, but browser-use "
                f"is not available: {self.status.reason}"
            )

    @property
    def backend_note(self) -> str:
        if self.status.available:
            return "browser-use available"
        return f"browser-use unavailable: {self.status.reason}"

    async def assist_page(
        self,
        *,
        page: Any,
        kind: str,
        instruction: str,
        context: dict[str, Any] | None = None,
    ) -> BrowserUseAssistResult:
        """Optionally capture a handoff artifact or run a bounded agent.

        Full-page artifacts require FLIGHT_TRACKER_CAPTURE_DEBUG_PAGES=true.
        Set BROWSER_USE_AGENT_FALLBACK=true after configuring a browser-use LLM
        key if you want to experiment with an isolated browser-use agent.
        """
        capture_enabled = os.getenv(
            "FLIGHT_TRACKER_CAPTURE_DEBUG_PAGES", "false"
        ).strip().lower() in {"1", "true", "yes", "on"}
        artifact_path = await self._write_assist_artifact(
            page=page,
            kind=kind,
            instruction=instruction,
            context=context or {},
        ) if capture_enabled else None
        enabled = os.getenv("BROWSER_USE_AGENT_FALLBACK", "false").lower()[:1] in {"1", "t", "y"}
        if not enabled:
            return BrowserUseAssistResult(
                attempted=False,
                success=False,
                reason=(
                    "browser-use agent fallback is disabled; handoff artifact was recorded."
                    if artifact_path else
                    "browser-use agent fallback and full-page artifact capture are disabled."
                ),
                artifact_path=str(artifact_path) if artifact_path else None,
                page_state=self._last_page_state,
            )

        try:
            self.ensure_available()
            from browser_use import Agent, Browser, ChatBrowserUse

            task = (
                "You are assisting a local flight-price tracker with a Ctrip page. "
                "Do not solve captchas or bypass security checks. "
                "Use only normal visible UI interactions. "
                f"Current URL: {getattr(page, 'url', '')}. "
                f"Task: {instruction}. "
                f"Context: {json.dumps(context or {}, ensure_ascii=False)}"
            )
            browser = Browser(
                headless=False,
                use_cloud=False,
                allowed_domains=["ctrip.com", "*.ctrip.com", "flights.ctrip.com"],
            )
            agent = Agent(
                task=task,
                llm=ChatBrowserUse(),
                browser=browser,
                max_actions_per_step=3,
                use_vision="auto",
                generate_gif=False,
                final_response_after_failure=True,
            )
            history = await agent.run(max_steps=8)
            return BrowserUseAssistResult(
                attempted=True,
                success=True,
                reason=str(history.final_result() if hasattr(history, "final_result") else "browser-use agent finished"),
                artifact_path=str(artifact_path) if artifact_path else None,
                page_state=self._last_page_state,
            )
        except Exception as exc:
            LOGGER.exception("browser-use assist failed for %s", kind)
            return BrowserUseAssistResult(
                attempted=True,
                success=False,
                reason=str(exc),
                artifact_path=str(artifact_path) if artifact_path else None,
                page_state=self._last_page_state,
            )

    async def _write_assist_artifact(
        self,
        *,
        page: Any,
        kind: str,
        instruction: str,
        context: dict[str, Any],
    ) -> Path | None:
        if self.runtime_dir is None:
            return None
        safe_kind = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in kind)[:80]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        assist_dir = ensure_private_directory(self.runtime_dir / "browseruse" / "assist")
        prefix = assist_dir / f"{safe_kind}_{stamp}"
        meta_path = prefix.with_suffix(".json")
        try:
            text = ""
            html = ""
            try:
                screenshot = prefix.with_suffix(".png")
                await page.screenshot(path=str(screenshot), full_page=True)
                make_file_private(screenshot)
            except Exception:
                pass
            try:
                html = await page.content()
                atomic_write_text(prefix.with_suffix(".html"), html)
            except Exception:
                pass
            try:
                text = await page.locator("body").inner_text(timeout=5000)
                atomic_write_text(prefix.with_suffix(".txt"), text)
            except Exception:
                pass
            title = await page.title() if hasattr(page, "title") else ""
            page_state = classify_page_state(text=text, url=getattr(page, "url", ""), title=title)
            self._last_page_state = page_state
            payload = {
                "kind": kind,
                "instruction": instruction,
                "context": context,
                "url": getattr(page, "url", ""),
                "title": title,
                "page_state": page_state,
                "captured_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            }
            return atomic_write_text(
                meta_path, json.dumps(payload, ensure_ascii=False, indent=2)
            )
        except Exception:
            LOGGER.exception("failed to write browser-use assist artifact")
            return None
