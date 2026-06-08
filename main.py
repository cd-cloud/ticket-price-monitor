import argparse
import asyncio
import os
from pathlib import Path

import uvicorn

from app_service import FlightPriceApplication
from backends import known_backend_names
from config_manager import ConfigManager
from internal_smoke_test import run_internal_smoke_test
from local_smoke_test import run_local_smoke_test
from providers.registry import CLI_PROVIDER_CHOICES
from web_ui import create_app


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _is_local_host(host: str) -> bool:
    return str(host or "").strip().lower() in LOCAL_HOSTS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local flight price tracker with Playwright + SQLite")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parent / "config.json"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    provider_choices = CLI_PROVIDER_CHOICES
    backend_choices = known_backend_names()

    init_cmd = subparsers.add_parser("init-config", help="Create a local config.json from config.example.json")
    init_cmd.add_argument("--overwrite", action="store_true", help="Replace an existing config file")

    save_cmd = subparsers.add_parser("save-credentials", help="Encrypt and store provider credentials locally")
    save_cmd.add_argument("--provider", required=True, choices=provider_choices)
    save_cmd.add_argument("--username", required=True)
    save_cmd.add_argument("--password", required=True)

    bootstrap_cmd = subparsers.add_parser("bootstrap-session", help="Save a local logged-in Playwright session")
    bootstrap_cmd.add_argument("--provider", required=True, choices=provider_choices)
    bootstrap_cmd.add_argument("--manual", action="store_true", help="Open login page and let you sign in manually")
    bootstrap_cmd.add_argument("--backend", choices=backend_choices, help="Temporarily force one browser backend")

    run_cmd = subparsers.add_parser("run-once", help="Run all configured queries once")
    run_cmd.add_argument("--provider", choices=provider_choices)
    run_cmd.add_argument("--backend", choices=backend_choices, help="Temporarily force one browser backend")

    cycle_cmd = subparsers.add_parser("run-cycle", help="Run one query cycle and refresh report artifacts")
    cycle_cmd.add_argument("--provider", choices=provider_choices)
    cycle_cmd.add_argument("--backend", choices=backend_choices, help="Temporarily force one browser backend")

    inspect_cmd = subparsers.add_parser("inspect-page", help="Open a live query page and dump DOM hints locally")
    inspect_cmd.add_argument("--provider", required=True, choices=provider_choices)
    inspect_cmd.add_argument("--route-index", type=int, default=0)
    inspect_cmd.add_argument("--no-pause", action="store_true")
    inspect_cmd.add_argument("--backend", choices=backend_choices, help="Temporarily force one browser backend")

    schedule_cmd = subparsers.add_parser("schedule", help="Run recurring queries on a local schedule")
    schedule_cmd.add_argument("--provider", choices=provider_choices)
    schedule_cmd.add_argument("--interval-minutes", type=int)

    subparsers.add_parser("report", help="Generate CSV and markdown report from SQLite")

    export_cmd = subparsers.add_parser("export-csv", help="Export snapshots to CSV")
    export_cmd.add_argument("--output", help="Custom output path")

    serve_cmd = subparsers.add_parser("serve-ui", help="Run the local web dashboard")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8765)

    smoke_cmd = subparsers.add_parser("internal-smoke-test", help="Run an authorized internal smoke test")
    smoke_cmd.add_argument("--url", required=True)
    smoke_cmd.add_argument("--ready-selector", default="")
    smoke_cmd.add_argument("--output-dir", default="./runtime/internal_smoke_tests")
    smoke_cmd.add_argument("--headless", action="store_true")
    smoke_cmd.add_argument("--timeout-ms", type=int, default=45000)
    smoke_cmd.add_argument("--locale", default="zh-CN")
    smoke_cmd.add_argument("--timezone", default="Asia/Shanghai")
    smoke_cmd.add_argument("--viewport", default="1365x900")
    smoke_cmd.add_argument("--access-service-token-env", default="CF_ACCESS_CLIENT_ID,CF_ACCESS_CLIENT_SECRET")

    local_smoke_cmd = subparsers.add_parser("local-smoke-test", help="Run deterministic local smoke checks")
    local_smoke_cmd.add_argument("--output-dir", default="./runtime/local_smoke_tests")
    local_smoke_cmd.add_argument("--timeout-seconds", type=int, default=30)
    local_smoke_cmd.add_argument("--keep-temp", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    config_manager = ConfigManager(config_path=Path(args.config))
    if args.command == "init-config":
        path = config_manager.init_config(overwrite=args.overwrite)
        print(f"local config initialized: {path}")
        return 0

    app = FlightPriceApplication(config_manager)

    if args.command == "save-credentials":
        app.save_credentials(args.provider, args.username, args.password)
        return 0
    if args.command == "bootstrap-session":
        asyncio.run(app.bootstrap_session(args.provider, manual=args.manual, backend_override=args.backend))
        return 0
    if args.command == "run-once":
        asyncio.run(app.run_once(provider_filter=args.provider, backend_override=args.backend))
        return 0
    if args.command == "run-cycle":
        asyncio.run(app.run_once(provider_filter=args.provider, backend_override=args.backend))
        app.report()
        return 0
    if args.command == "inspect-page":
        asyncio.run(
            app.inspect_page(
                args.provider,
                route_index=args.route_index,
                pause=not args.no_pause,
                backend_override=args.backend,
            )
        )
        return 0
    if args.command == "schedule":
        app.schedule(provider_filter=args.provider, interval_minutes=args.interval_minutes)
        return 0
    if args.command == "report":
        app.report()
        return 0
    if args.command == "export-csv":
        app.export_csv(Path(args.output) if args.output else None)
        return 0
    if args.command == "serve-ui":
        if not _is_local_host(args.host) and not os.getenv("FLIGHT_TRACKER_UI_TOKEN"):
            raise RuntimeError(
                "Refusing to bind the admin UI to a non-local host without FLIGHT_TRACKER_UI_TOKEN. "
                "Set a strong token or use --host 127.0.0.1."
            )
        uvicorn.run(create_app(args.config), host=args.host, port=args.port)
        return 0
    if args.command == "internal-smoke-test":
        return asyncio.run(run_internal_smoke_test(args))
    if args.command == "local-smoke-test":
        return run_local_smoke_test(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
