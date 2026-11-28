from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from core.private_files import atomic_write_text, ensure_private_directory, make_file_private


@dataclass(slots=True)
class ReportArtifacts:
    csv_path: Path
    markdown_path: Path


class PriceAnalyzer:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        ensure_private_directory(self.output_dir)

    def to_dataframe(self, snapshots: list[dict[str, Any]]) -> pd.DataFrame:
        frame = pd.DataFrame(snapshots)
        if frame.empty:
            return frame
        frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True, format="mixed")
        frame["price"] = pd.to_numeric(frame["price"])
        return frame

    def summarize(self, frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty:
            return pd.DataFrame()

        ordered = frame.sort_values("observed_at")
        summary = (
            ordered.groupby(["provider", "route_key", "currency"], dropna=False)
            .agg(
                min_price=("price", "min"),
                max_price=("price", "max"),
                avg_price=("price", "mean"),
                latest_price=("price", "last"),
                sample_count=("price", "count"),
            )
            .reset_index()
        )
        summary["avg_price"] = summary["avg_price"].round(2)
        return summary

    def export_csv(self, frame: pd.DataFrame, filename: str = "price_history.csv") -> Path:
        path = self.output_dir / filename
        frame.to_csv(path, index=False, encoding="utf-8-sig")
        return make_file_private(path)

    def build_markdown_report(
        self,
        frame: pd.DataFrame,
        summary: pd.DataFrame,
        filename: str = "report.md",
        title: str = "Flight Price Report",
    ) -> Path:
        path = self.output_dir / filename
        if frame.empty:
            return atomic_write_text(path, f"# {title}\n\nNo snapshots available.\n")

        priced = frame.dropna(subset=["price"])
        if priced.empty:
            best_rows = frame.iloc[0:0].copy()
        else:
            indices = priced.groupby(
                ["route_key", "provider", "currency"], dropna=False
            )["price"].idxmin()
            best_rows = frame.loc[indices].sort_values(
                ["route_key", "provider", "currency", "price", "observed_at"]
            )
        lines = [
            f"# {title}",
            "",
            "## Summary",
            "",
            summary.to_markdown(index=False),
            "",
            "## Lowest Price By Provider",
            "",
            best_rows[["provider", "route_key", "price", "currency", "observed_at"]].to_markdown(index=False),
            "",
        ]
        return atomic_write_text(path, "\n".join(lines))

    def generate_report(
        self,
        snapshots: list[dict[str, Any]],
        route_key: str | None = None,
        provider: str | None = None,
    ) -> ReportArtifacts:
        frame = self.to_dataframe(snapshots)
        if route_key:
            frame = frame[frame["route_key"] == route_key] if not frame.empty else frame
        if provider:
            frame = frame[frame["provider"] == provider] if not frame.empty else frame
        summary = self.summarize(frame)
        suffix_parts = [part for part in [provider, route_key] if part]
        suffix = "_" + "_".join(part.replace("|", "_").replace(":", "_").replace("/", "_") for part in suffix_parts) if suffix_parts else ""
        csv_path = self.export_csv(frame, filename=f"price_history{suffix}.csv")
        report_title = "Flight Price Report" if not suffix_parts else f"Flight Price Report - {' | '.join(suffix_parts)}"
        markdown_path = self.build_markdown_report(
            frame,
            summary,
            filename=f"report{suffix}.md",
            title=report_title,
        )
        return ReportArtifacts(csv_path=csv_path, markdown_path=markdown_path)
