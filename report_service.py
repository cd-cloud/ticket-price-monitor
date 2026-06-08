from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from analyzer import PriceAnalyzer
from config_manager import AppConfig
from data_storage import PriceRepository


class ReportService:
    """Generates report artifacts and CSV exports from stored price snapshots."""

    def __init__(self, *, config: AppConfig, repository: PriceRepository) -> None:
        self.config = config
        self.repository = repository

    def refresh_config(self, config: AppConfig) -> None:
        self.config = config

    def report(self, route_key: str | None = None, provider: str | None = None) -> dict[str, str]:
        analyzer = PriceAnalyzer(self.config.output_dir)
        snapshots = self.repository.fetch_snapshots()
        artifacts = analyzer.generate_report(snapshots, route_key=route_key, provider=provider)
        logging.info("report csv: %s", artifacts.csv_path)
        logging.info("report markdown: %s", artifacts.markdown_path)
        return self._artifact_paths(artifacts)

    def artifact_paths(self) -> dict[str, str]:
        return {
            "csv_path": str(self.config.output_dir / "price_history.csv"),
            "markdown_path": str(self.config.output_dir / "report.md"),
        }

    def export_csv(self, output_path: Path | None = None) -> Path:
        path = output_path or (self.config.output_dir / "price_history_export.csv")
        final = self.repository.export_csv(path)
        logging.info("csv exported to %s", final)
        return final

    @staticmethod
    def _artifact_paths(artifacts: Any) -> dict[str, str]:
        return {
            "csv_path": str(artifacts.csv_path),
            "markdown_path": str(artifacts.markdown_path),
        }
