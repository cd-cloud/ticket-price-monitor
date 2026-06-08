from __future__ import annotations

import os
import subprocess
import sys
from typing import Any, Callable

from config_manager import AppConfig
from data_storage import PriceRepository
from run_summary import RunSummaryStore


CompletedProcessRunner = Callable[..., subprocess.CompletedProcess]
ProcessSpawner = Callable[..., Any]


class LegacyRunService:
    """Keeps older CLI-based run paths isolated from the main app orchestration."""

    def __init__(
        self,
        *,
        config: AppConfig,
        repository: PriceRepository,
        run_summary: RunSummaryStore,
        process_runner: CompletedProcessRunner | None = None,
        process_spawner: ProcessSpawner | None = None,
        platform_name: str | None = None,
    ) -> None:
        self.config = config
        self.repository = repository
        self.run_summary = run_summary
        self.process_runner = process_runner or subprocess.run
        self.process_spawner = process_spawner or subprocess.Popen
        self.platform_name = platform_name or os.name

    def refresh_config(self, config: AppConfig) -> None:
        self.config = config

    def run_once_via_cli(self, provider_filter: str | None = None) -> dict[str, Any]:
        before = self.repository.count_snapshots()
        command = [sys.executable, "main.py", "run-once"]
        if provider_filter:
            command.extend(["--provider", provider_filter])

        completed = self.process_runner(
            command,
            cwd=self.config.root_dir,
            capture_output=True,
            text=True,
        )
        after = self.repository.count_snapshots()
        saved = max(after - before, 0)
        errors: list[str] = []

        if completed.returncode != 0:
            stderr = (completed.stderr or completed.stdout or "").strip()
            errors.append(stderr or f"run-once exited with code {completed.returncode}")

        return self.run_summary.finished(
            saved=saved,
            errors=errors,
            stdout=(completed.stdout or "").strip(),
        )

    def start_run_cycle_via_cli(self, provider_filter: str | None = None) -> dict[str, Any]:
        self.config.log_file.parent.mkdir(parents=True, exist_ok=True)
        pid: int | None = None

        if self.platform_name == "nt":
            worker_script = self.config.root_dir / "run_cycle_worker.cmd"
            command = ["cmd", "/c", "start", "", "/min", "cmd", "/c", "call", str(worker_script)]
            if provider_filter:
                command.append(provider_filter)

            process = self.process_spawner(
                command,
                cwd=self.config.root_dir,
            )
            pid = process.pid
        else:
            command = [sys.executable, "main.py", "run-cycle"]
            if provider_filter:
                command.extend(["--provider", provider_filter])
            with self.config.log_file.open("a", encoding="utf-8") as log_handle:
                process = self.process_spawner(
                    command,
                    cwd=self.config.root_dir,
                    stdout=log_handle,
                    stderr=log_handle,
                )
            pid = process.pid

        return self.run_summary.running(pid=pid)
