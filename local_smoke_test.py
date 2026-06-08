from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from config_manager import ConfigManager
from data_storage import PriceRepository
from query_service import QueryService
from report_service import ReportService


ROOT = Path(__file__).resolve().parent


class FakeAutomation:
    def __init__(self, *_args, **_kwargs) -> None:
        self.backend_override: str | None = None

    async def __aenter__(self) -> FakeAutomation:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def query_provider(self, provider_name: str, route) -> Any:
        return SimpleNamespace(
            provider=provider_name,
            route=route,
            price=880.0,
            currency="CNY",
            observed_at="2099-01-01T00:00:00Z",
            notes=["local smoke"],
            raw_payload={
                "parser": "local_smoke",
                "parser_confidence": "high",
                "detail_quality": "complete",
                "detail_source": "local_smoke",
                "best_offer": {"price": 880.0},
            },
        )


def run_local_smoke_test(args) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    result: dict[str, Any] = {
        "started_at": timestamp,
        "project_root": str(ROOT),
        "checks": [],
    }
    failures = 0

    with tempfile.TemporaryDirectory(prefix="local_smoke_", dir=str(output_dir)) as tmp_dir:
        tmp_path = Path(tmp_dir)
        config_path = _write_smoke_config(tmp_path)

        cli_result = _run_cli_help_check(timeout_seconds=args.timeout_seconds)
        result["checks"].append(cli_result)
        failures += 0 if cli_result["ok"] else 1

        ui_result = _run_ui_health_check(
            config_path=config_path,
            run_dir=tmp_path,
            timeout_seconds=args.timeout_seconds,
        )
        result["checks"].append(ui_result)
        failures += 0 if ui_result["ok"] else 1

        query_result = _run_query_and_report_check(config_path=config_path)
        result["checks"].append(query_result)
        failures += 0 if query_result["ok"] else 1

        if args.keep_temp:
            result["temp_dir"] = str(tmp_path)
        elif failures == 0:
            result["temp_dir"] = "(cleaned)"

        result["finished_at"] = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        result["ok"] = failures == 0
        json_path = output_dir / f"local_smoke_{timestamp}.json"
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if failures == 0 else 1


def _run_cli_help_check(*, timeout_seconds: int) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--help"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
    )
    stdout = completed.stdout or ""
    ok = completed.returncode == 0 and "serve-ui" in stdout and "run-once" in stdout and "local-smoke-test" in stdout
    return {
        "name": "cli_help",
        "ok": ok,
        "returncode": completed.returncode,
        "stdout_preview": stdout[:500],
        "stderr_preview": (completed.stderr or "")[:500],
    }


def _run_ui_health_check(
    *,
    config_path: Path,
    run_dir: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    port = _find_free_port()
    url = f"http://127.0.0.1:{port}/api/health"
    stdout_path = run_dir / "serve_ui.stdout.log"
    stderr_path = run_dir / "serve_ui.stderr.log"
    stdout_file = stdout_path.open("w", encoding="utf-8")
    stderr_file = stderr_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "main.py"),
            "--config",
            str(config_path),
            "serve-ui",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(ROOT),
        stdout=stdout_file,
        stderr=stderr_file,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    payload: dict[str, Any] | None = None
    error = ""
    try:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if process.poll() is not None:
                error = f"serve-ui exited early with code {process.returncode}"
                break
            try:
                with urllib.request.urlopen(url, timeout=1.5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                time.sleep(0.5)
        else:
            error = f"timed out waiting for {url}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        stdout_file.close()
        stderr_file.close()

    ok = payload == {"ok": True}
    return {
        "name": "serve_ui_health",
        "ok": ok,
        "url": url,
        "payload": payload,
        "error": error,
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
    }


def _run_query_and_report_check(*, config_path: Path) -> dict[str, Any]:
    manager = ConfigManager(config_path=config_path)
    config = manager.load()
    repository = PriceRepository(config.database_path)
    repository.initialize()

    service = QueryService(
        config_manager=manager,
        repository=repository,
        run_lock=__import__("threading").Lock(),
        get_config=lambda: config,
        refresh_config=lambda: None,
        automation_factory=lambda app_config, credentials: FakeAutomation(app_config, credentials),
    )
    query_summary = service.run_once_blocking(provider_filter="ctrip")
    snapshots = repository.fetch_snapshots()
    report_service = ReportService(config=config, repository=repository)
    report_paths = report_service.report()
    export_path = report_service.export_csv()
    ok = (
        query_summary.get("saved") == 1
        and not query_summary.get("errors")
        and len(snapshots) == 1
        and Path(report_paths["csv_path"]).exists()
        and Path(report_paths["markdown_path"]).exists()
        and export_path.exists()
    )
    return {
        "name": "query_and_report",
        "ok": ok,
        "saved": query_summary.get("saved"),
        "errors": query_summary.get("errors"),
        "snapshot_count": len(snapshots),
        "report_paths": report_paths,
        "export_csv_path": str(export_path),
    }


def _write_smoke_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "storage": {
                    "runtime_dir": str((tmp_path / "runtime").resolve()),
                    "output_dir": str((tmp_path / "output").resolve()),
                    "database_path": str((tmp_path / "runtime" / "smoke.db").resolve()),
                    "encrypted_credentials_path": str((tmp_path / "runtime" / "credentials.enc.json").resolve()),
                    "salt_file": str((tmp_path / "runtime" / "master.salt").resolve()),
                    "log_file": str((tmp_path / "runtime" / "smoke.log").resolve()),
                },
                "defaults": {
                    "locale": "zh-CN",
                    "currency": "CNY",
                    "browser_backend": "playwright",
                    "browser_backend_fallbacks": [],
                },
                "scheduler": {"timezone": "Asia/Shanghai"},
                "providers": {
                    "ctrip": {
                        "enabled": True,
                        "login_url": "https://example.test/login",
                        "search_url_template": "https://example.test/{origin}-{destination}",
                        "storage_state_file": str((tmp_path / "runtime" / "ctrip_state.json").resolve()),
                        "timeout_ms": 1000,
                        "min_delay_seconds": 0,
                        "max_delay_seconds": 0,
                    }
                },
                "routes": [
                    {
                        "origin": "SHA",
                        "destination": "PEK",
                        "departure_date": "2099-01-01",
                        "cabin": "economy",
                        "passengers": 1,
                        "providers": ["ctrip"],
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return config_path


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
