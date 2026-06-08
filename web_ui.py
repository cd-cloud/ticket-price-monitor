import asyncio
import logging
import os
from pathlib import Path
from time import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from starlette.responses import Response

from app_service import FlightPriceApplication
from config_manager import ConfigManager


ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
LOGGER = logging.getLogger(__name__)


def _is_local_origin(value: str | None) -> bool:
    if not value:
        return True
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    return (parsed.hostname or "").lower() in LOCAL_HOSTS


def create_app(config_path: str | None = None) -> FastAPI:
    manager = ConfigManager(config_path=Path(config_path) if config_path else None)
    service = FlightPriceApplication(manager)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(service.start_auto_query_worker)
        try:
            yield
        finally:
            await asyncio.to_thread(service.stop_auto_query_worker)

    app = FastAPI(title="Flight Price Tracker UI", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
    static_version = str(int(time()))
    ui_token = os.getenv("FLIGHT_TRACKER_UI_TOKEN", "").strip()

    @app.middleware("http")
    async def disable_cache(request: Request, call_next):
        response: Response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    @app.middleware("http")
    async def protect_local_admin_api(request: Request, call_next):
        if request.method in MUTATING_METHODS:
            if not _is_local_origin(request.headers.get("origin")):
                return Response("Forbidden origin", status_code=403)
            if ui_token:
                supplied = request.headers.get("x-flight-tracker-token") or request.query_params.get("token")
                if supplied != ui_token:
                    return Response("Missing or invalid UI token", status_code=401)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        payload = service.dashboard_payload()
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "request": request,
                "tasks": payload["tasks"],
                "last_run": payload["last_run"],
                "csv_url": "/download/csv",
                "static_version": static_version,
            },
        )

    @app.get("/api/dashboard")
    async def dashboard() -> dict:
        return service.dashboard_payload()

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True}

    @app.get("/api/browser-session/diagnostics")
    async def browser_session_diagnostics() -> dict:
        return service.browser_session_diagnostics()

    @app.get("/api/browser-profiles")
    async def browser_profiles() -> dict:
        return service.browser_profiles()

    @app.post("/api/browser-profiles/{provider}/open")
    async def open_browser_profile(provider: str, payload: dict = Body(default={})) -> dict:
        try:
            return await asyncio.to_thread(service.open_browser_profile, provider, profile=payload.get("profile"))
        except KeyError:
            raise HTTPException(status_code=404, detail="Provider not found")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/browser-profiles/{provider}/switch")
    async def switch_browser_profile(provider: str, payload: dict = Body(...)) -> dict:
        try:
            return service.switch_browser_profile(provider, profile=str(payload.get("profile") or ""))
        except KeyError:
            raise HTTPException(status_code=404, detail="Provider not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/browser-profiles/{provider}/reset-recovery")
    async def reset_recovery_profile(provider: str) -> dict:
        try:
            return await asyncio.to_thread(service.reset_recovery_profile, provider)
        except KeyError:
            raise HTTPException(status_code=404, detail="Provider not found")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/browser-profiles/{provider}/clean")
    async def clean_browser_profile(provider: str, payload: dict = Body(default={})) -> dict:
        try:
            return await asyncio.to_thread(
                service.clean_browser_profile,
                provider,
                profile=payload.get("profile"),
                switch_to_recovery=bool(payload.get("switch_to_recovery", False)),
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Provider not found")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/run-once")
    async def run_once(provider: str | None = None) -> dict:
        try:
            routes = service.list_routes()
            if provider and not any(provider in (route.get("providers") or []) for route in routes):
                raise HTTPException(status_code=404, detail="Provider not found in routes")

            def run_and_report() -> None:
                try:
                    service.run_once_blocking(provider)
                    service.report()
                except Exception:
                    LOGGER.exception("background run-once query failed: provider=%s", provider)

            asyncio.create_task(asyncio.to_thread(run_and_report))
            return {"running": True, "saved": 0, "errors": [], "provider": provider}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/routes/{route_key:path}/run-once")
    async def run_route_once(route_key: str, provider: str | None = None) -> dict:
        try:
            route = next((item for item in service.list_routes() if item.get("route_key") == route_key), None)
            if route is None:
                raise HTTPException(status_code=404, detail="Route not found")
            if provider and provider not in (route.get("providers") or []):
                raise HTTPException(status_code=404, detail="Provider not found in route")

            def run_and_report() -> None:
                try:
                    service.run_route_once_blocking(route_key, provider)
                    service.report()
                except Exception:
                    LOGGER.exception("background route query failed: route_key=%s provider=%s", route_key, provider)

            asyncio.create_task(asyncio.to_thread(run_and_report))
            return {"running": True, "saved": 0, "errors": [], "route_key": route_key, "provider": provider}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/route-groups/{group_id:path}/run-once")
    async def run_route_group_once(group_id: str, provider: str | None = None) -> dict:
        try:
            routes = [route for route in service.list_routes() if route.get("group_id") == group_id]
            if not routes:
                raise HTTPException(status_code=404, detail="Route group not found")
            if provider and not any(provider in (route.get("providers") or []) for route in routes):
                raise HTTPException(status_code=404, detail="Provider not found in route group")

            def run_and_report() -> None:
                providers = [provider] if provider else _route_group_providers(routes)
                for provider_name in providers:
                    try:
                        service.run_route_group_once_blocking(group_id, provider_name)
                        service.report()
                    except Exception:
                        LOGGER.exception(
                            "background route group query failed: group_id=%s provider=%s",
                            group_id,
                            provider_name,
                        )

            asyncio.create_task(asyncio.to_thread(run_and_report))
            return {
                "running": True,
                "saved": 0,
                "errors": [],
                "group_id": group_id,
                "provider": provider,
                "providers": [provider] if provider else _route_group_providers(routes),
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.get("/api/tail-discovery/candidates")
    async def tail_discovery_candidates() -> dict:
        return service.tail_discovery_candidates()

    @app.get("/api/tail-discovery/results")
    async def tail_discovery_results(limit: int = 100) -> dict:
        return {"results": service.tail_discovery_results(limit=limit)}

    @app.post("/api/tail-discovery/run")
    async def run_tail_discovery(payload: dict = Body(...)) -> dict:
        return await asyncio.to_thread(service.submit_tail_discovery, payload)

    @app.get("/api/tail-discovery/jobs/latest")
    async def latest_tail_discovery_job() -> dict:
        job = service.latest_tail_discovery_job()
        return {"job": job}

    @app.get("/api/tail-discovery/jobs")
    async def tail_discovery_jobs(limit: int = 20) -> dict:
        return {"jobs": service.tail_discovery_jobs(limit=limit)}

    @app.get("/api/tail-discovery/jobs/{job_id}")
    async def tail_discovery_job(job_id: str) -> dict:
        try:
            return service.tail_discovery_job(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Tail discovery job not found")

    @app.get("/api/tail-discovery/attempts")
    async def tail_discovery_attempts(job_id: str | None = None, limit: int = 100) -> dict:
        return {"attempts": service.tail_discovery_attempts(job_id=job_id, limit=limit)}

    @app.post("/api/tail-discovery/jobs/{job_id}/cancel")
    async def cancel_tail_discovery_job(job_id: str) -> dict:
        try:
            return service.cancel_tail_discovery_job(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Tail discovery job not found")

    @app.post("/api/tail-discovery/jobs/{job_id}/continue")
    async def continue_tail_discovery_job(job_id: str) -> dict:
        try:
            return service.continue_tail_discovery_job(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Tail discovery job not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/tail-discovery/jobs/{job_id}/retry-failed")
    async def retry_failed_tail_discovery_job(job_id: str) -> dict:
        try:
            return service.retry_failed_tail_discovery_job(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Tail discovery job not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/report")
    async def generate_report(route_key: str | None = None, provider: str | None = None) -> dict:
        return await asyncio.to_thread(service.report, route_key, provider)

    @app.post("/api/routes")
    async def save_route(payload: dict = Body(...)) -> dict:
        try:
            return service.upsert_route(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/routes/{route_key:path}/auto-query")
    async def update_route_auto_query(route_key: str, payload: dict = Body(...)) -> dict:
        try:
            return service.update_route_auto_query(route_key, enabled=bool(payload.get("enabled", False)))
        except KeyError:
            raise HTTPException(status_code=404, detail="Route not found")

    @app.delete("/api/routes/{route_key:path}")
    async def delete_route(route_key: str) -> dict:
        deleted = service.delete_route(route_key)
        if not deleted:
            raise HTTPException(status_code=404, detail="Route not found")
        return {"deleted": True}

    @app.delete("/api/route-groups/{group_id:path}")
    async def delete_route_group(group_id: str) -> dict:
        deleted = service.delete_route_group(group_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Route group not found")
        return {"deleted": True}

    @app.get("/download/csv")
    async def download_csv() -> FileResponse:
        csv_path = service.export_csv()
        return FileResponse(csv_path, filename=csv_path.name)

    @app.get("/api/log")
    async def recent_log() -> dict:
        lines: list[str] = []
        paths = [
            service.config.log_file,
            service.config.runtime_dir / "web_run_stdout.log",
            service.config.runtime_dir / "web_run_stderr.log",
        ]
        for path in paths:
            if not path.exists():
                continue
            file_lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-40:]
            if file_lines:
                lines.extend([f"[{path.name}] {line}" for line in file_lines])
        lines = lines[-120:]
        return {"lines": lines}

    return app


def _route_group_providers(routes: list[dict]) -> list[str]:
    providers: list[str] = []
    for route in routes:
        for provider in route.get("providers") or []:
            if provider not in providers:
                providers.append(provider)
    return providers
