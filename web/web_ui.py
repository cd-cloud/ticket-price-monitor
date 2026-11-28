import asyncio
import hmac
import logging
import os
from pathlib import Path
from time import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, StrictBool, StrictStr
from starlette.requests import Request
from starlette.responses import Response

from services.app_service import FlightPriceApplication
from core.config_manager import ConfigConflictError, ConfigManager
from providers.response_capture import sanitize_sensitive_text
from core.private_files import make_file_private


ROOT = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(ROOT / "templates"))
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
UI_TOKEN_COOKIE = "flight_tracker_ui_token"
PUBLIC_UI_PATHS = {"/", "/api/health", "/api/session"}
LOGGER = logging.getLogger(__name__)


class UiSessionPayload(BaseModel):
    token: str = Field(min_length=1, max_length=512)


class EnabledPayload(BaseModel):
    enabled: StrictBool


class BackupPayload(BaseModel):
    backup: StrictBool = False


class ConfirmPayload(BaseModel):
    confirmed: StrictBool


class ProfilePayload(BaseModel):
    profile: StrictStr | None = Field(default=None, max_length=80)
    switch_to_recovery: StrictBool = False


class SwitchProfilePayload(BaseModel):
    profile: StrictStr = Field(min_length=1, max_length=80)


def _origin_parts(value: str | None) -> tuple[str, str, int] | None:
    if not value:
        return None
    try:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed.scheme.lower(), parsed.hostname.lower().rstrip("."), port
    except (TypeError, ValueError):
        return None


def _request_origin_parts(request: Request) -> tuple[str, str, int] | None:
    scheme = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",", 1)[0].strip().lower()
    host = (request.headers.get("host") or "").strip()
    return _origin_parts(f"{scheme}://{host}") if scheme and host else None


def _is_allowed_origin(value: str | None, request: Request) -> bool:
    if not value:
        return True
    supplied = _origin_parts(value)
    expected = _request_origin_parts(request)
    return supplied is not None and expected is not None and supplied == expected


def _request_is_https(request: Request) -> bool:
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    return (forwarded or request.url.scheme).lower() == "https"


def _valid_ui_token(supplied: str | None, expected: str) -> bool:
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def _request_ui_token(request: Request) -> str:
    return (
        request.headers.get("x-flight-tracker-token")
        or request.cookies.get(UI_TOKEN_COOKIE)
        or ""
    )


LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Flight Price Tracker Login</title><style>body{font-family:system-ui,sans-serif;max-width:32rem;margin:12vh auto;padding:1.5rem;color:#172033}form{display:grid;gap:1rem}input,button{font:inherit;padding:.75rem}button{cursor:pointer}.error{color:#a40000;min-height:1.5rem}</style></head>
<body><main><h1>航班价格监控</h1><p>请输入本机管理 token。token 只通过本次 POST 提交，不会写入 URL 或浏览器存储。</p>
<form id="login"><label>管理 token<input id="token" type="password" autocomplete="current-password" required></label><button type="submit">进入控制台</button></form><p id="error" class="error" role="alert"></p></main>
<script>history.replaceState(null,"",location.pathname);document.getElementById("login").addEventListener("submit",async e=>{e.preventDefault();const out=document.getElementById("error");out.textContent="";try{const r=await fetch("/api/session",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:document.getElementById("token").value})});if(!r.ok)throw new Error("token 无效");location.replace("/")}catch(err){out.textContent=err.message}});</script></body></html>"""


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
            await asyncio.to_thread(service.experimental_queries.close)

    app = FastAPI(title="Flight Price Tracker UI", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
    static_version = str(int(time()))
    ui_token = os.getenv("FLIGHT_TRACKER_UI_TOKEN", "").strip()

    @app.middleware("http")
    async def disable_cache(request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    @app.middleware("http")
    async def protect_local_admin_api(request: Request, call_next):
        path = request.url.path
        if request.method in MUTATING_METHODS:
            sec_fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
            if sec_fetch_site == "cross-site":
                return Response("Forbidden cross-site request", status_code=403)
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            if not _is_allowed_origin(origin, request):
                return Response("Forbidden origin", status_code=403)
            if not origin and referer and not _is_allowed_origin(referer, request):
                return Response("Forbidden referer", status_code=403)

        requires_token = bool(ui_token) and path not in PUBLIC_UI_PATHS and not path.startswith("/static/")
        if requires_token and not _valid_ui_token(_request_ui_token(request), ui_token):
            return Response("Missing or invalid UI token", status_code=401)
        return await call_next(request)

    @app.post("/api/session")
    async def establish_ui_session(payload: UiSessionPayload, request: Request) -> Response:
        if not ui_token:
            return Response('{"authenticated":true}', media_type="application/json")
        if not _valid_ui_token(payload.token, ui_token):
            raise HTTPException(status_code=401, detail="Invalid UI token")
        response = Response('{"authenticated":true}', media_type="application/json")
        response.set_cookie(
            key=UI_TOKEN_COOKIE,
            value=ui_token,
            httponly=True,
            samesite="strict",
            secure=_request_is_https(request),
            path="/",
        )
        return response

    @app.post("/api/session/logout")
    async def clear_ui_session(request: Request) -> Response:
        response = Response('{"authenticated":false}', media_type="application/json")
        response.delete_cookie(
            key=UI_TOKEN_COOKIE,
            path="/",
            httponly=True,
            samesite="strict",
            secure=_request_is_https(request),
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        if ui_token and not _valid_ui_token(_request_ui_token(request), ui_token):
            return HTMLResponse(LOGIN_HTML)
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
                "ui_token_enabled": bool(ui_token),
            },
        )

    @app.get("/api/dashboard")
    async def dashboard() -> dict:
        return service.dashboard_payload()

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True}

    @app.get("/api/experiments/airchina")
    async def airchina_experiments(limit: int = Query(20, ge=1, le=100)) -> dict:
        return {"jobs": await asyncio.to_thread(service.experimental_queries.list, limit),
                "query_policy": await asyncio.to_thread(lambda: service.query_service.airchina_query_gate().status())}

    @app.post("/api/experiments/airchina/policy/resume")
    async def resume_airchina_policy(payload: ConfirmPayload) -> dict:
        try:
            if payload.confirmed is not True:
                raise ValueError()
            return await asyncio.to_thread(
                lambda: service.query_service.airchina_query_gate().resume(confirmed=True))
        except ValueError:
            raise HTTPException(status_code=400, detail="需要明确确认已人工检查访问状态。")
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/api/experiments/airchina", status_code=202)
    async def submit_airchina_experiment(payload: dict = Body(...)) -> dict:
        try:
            return await asyncio.to_thread(service.experimental_queries.submit, payload)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="参数无效：仅支持明确机场、未来单程日期、1成人及经济/公务舱；配置需包含 airchina。")
        except RuntimeError:
            raise HTTPException(status_code=409, detail="已有查询正在运行或服务正在关闭，请稍后再试。")

    @app.get("/api/experiments/airchina/session")
    async def airchina_session_status() -> dict:
        return await asyncio.to_thread(service.experimental_queries.session_status)

    @app.post("/api/experiments/airchina/session/prepare", status_code=202)
    async def prepare_airchina_session() -> dict:
        try:
            return await asyncio.to_thread(service.experimental_queries.prepare_browser_session, restore=False)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/api/experiments/airchina/session/restore", status_code=202)
    async def restore_airchina_session() -> dict:
        try:
            return await asyncio.to_thread(service.experimental_queries.prepare_browser_session, restore=True)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/api/experiments/airchina/session/ready")
    async def confirm_airchina_session(payload: BackupPayload) -> dict:
        try:
            return await asyncio.to_thread(
                service.experimental_queries.browser_session_ready,
                backup=payload.backup,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/api/experiments/airchina/session/cancel-prepare")
    async def cancel_airchina_session_preparation() -> dict:
        try:
            return await asyncio.to_thread(service.experimental_queries.cancel_browser_session_preparation)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.delete("/api/experiments/airchina/session/backup")
    async def delete_airchina_session_backup() -> dict:
        try:
            return await asyncio.to_thread(service.experimental_queries.delete_browser_session_backup)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/api/experiments/airchina/session/close")
    async def close_airchina_session() -> dict:
        try:
            return await asyncio.to_thread(service.experimental_queries.without_browser_session)
        except RuntimeError:
            raise HTTPException(status_code=409, detail="已有查询运行或服务正在关闭，未关闭会话。")
        except Exception:
            raise HTTPException(status_code=500, detail="会话清理未确认，请重启应用；未自动查询。")

    @app.get("/api/experiments/airchina/{job_id}")
    async def airchina_experiment(job_id: str) -> dict:
        try:
            return await asyncio.to_thread(service.experimental_queries.get, job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="实验任务不存在。")

    @app.post("/api/experiments/airchina/{job_id}/{action}")
    async def control_airchina_experiment(job_id: str, action: str) -> dict:
        try:
            return await asyncio.to_thread(service.experimental_queries.control, job_id, action)
        except KeyError:
            raise HTTPException(status_code=404, detail="实验任务不存在。")
        except ValueError:
            raise HTTPException(status_code=400, detail="未知操作。")
        except RuntimeError:
            raise HTTPException(status_code=409, detail="任务当前不可执行此操作，请刷新任务状态。")

    @app.get("/api/browser-session/diagnostics")
    async def browser_session_diagnostics() -> dict:
        return service.browser_session_diagnostics()

    @app.get("/api/browser-profiles")
    async def browser_profiles() -> dict:
        return service.browser_profiles()

    @app.post("/api/browser-profiles/{provider}/open")
    async def open_browser_profile(provider: str, payload: ProfilePayload) -> dict:
        try:
            return await asyncio.to_thread(service.open_browser_profile, provider, profile=payload.profile)
        except KeyError:
            raise HTTPException(status_code=404, detail="Provider not found")
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/browser-profiles/{provider}/switch")
    async def switch_browser_profile(provider: str, payload: SwitchProfilePayload) -> dict:
        try:
            return await asyncio.to_thread(
                service.switch_browser_profile, provider, profile=payload.profile
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Provider not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError:
            raise HTTPException(status_code=409, detail="已有查询运行或会话尚未关闭，未切换档案。")

    @app.post("/api/browser-profiles/{provider}/reset-recovery")
    async def reset_recovery_profile(provider: str) -> dict:
        try:
            return await asyncio.to_thread(service.reset_recovery_profile, provider)
        except KeyError:
            raise HTTPException(status_code=404, detail="Provider not found")
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/browser-profiles/{provider}/clean")
    async def clean_browser_profile(provider: str, payload: ProfilePayload) -> dict:
        try:
            return await asyncio.to_thread(
                service.clean_browser_profile,
                provider,
                profile=payload.profile,
                switch_to_recovery=payload.switch_to_recovery,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Provider not found")
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/run-once")
    async def run_once(provider: str | None = None) -> dict:
        try:
            routes = service.list_routes()
            if provider and not any(provider in (route.get("providers") or []) for route in routes):
                raise HTTPException(status_code=404, detail="Provider not found in routes")
            if service.is_query_busy():
                raise HTTPException(status_code=409, detail="已有查询或任务正在运行，请等待当前任务结束后再试。")

            def run_and_report() -> None:
                try:
                    service.run_once_blocking(provider)
                    service.report()
                except Exception:
                    LOGGER.exception("background run-once query failed: provider=%s", provider)

            asyncio.create_task(asyncio.to_thread(run_and_report))
            return {"running": True, "saved": 0, "errors": [], "provider": provider}
        except HTTPException:
            raise
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
            if service.is_query_busy():
                raise HTTPException(status_code=409, detail="已有查询或任务正在运行，请等待当前任务结束后再试。")

            def run_and_report() -> None:
                try:
                    service.run_route_once_blocking(route_key, provider)
                    service.report()
                except Exception:
                    LOGGER.exception("background route query failed: route_key=%s provider=%s", route_key, provider)

            asyncio.create_task(asyncio.to_thread(run_and_report))
            return {"running": True, "saved": 0, "errors": [], "route_key": route_key, "provider": provider}
        except HTTPException:
            raise
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
            if service.is_query_busy():
                raise HTTPException(status_code=409, detail="已有查询或任务正在运行，请等待当前任务结束后再试。")

            def run_and_report() -> None:
                try:
                    service.run_route_group_once_blocking(group_id, provider)
                    service.report()
                except Exception:
                    LOGGER.exception(
                        "background route group query failed: group_id=%s provider=%s",
                        group_id,
                        provider,
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

    @app.get("/api/tail-discovery/schedules")
    async def tail_discovery_schedules() -> dict:
        return {"schedules": await asyncio.to_thread(service.list_tail_schedules)}

    @app.post("/api/tail-discovery/schedules")
    async def save_tail_discovery_schedule(payload: dict = Body(...)) -> dict:
        try:
            return await asyncio.to_thread(service.save_tail_schedule, payload)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except KeyError:
            raise HTTPException(status_code=404, detail="Tail discovery schedule not found")

    @app.post("/api/tail-discovery/schedules/{schedule_id}/enabled")
    async def set_tail_discovery_schedule_enabled(schedule_id: str, payload: EnabledPayload) -> dict:
        try:
            return await asyncio.to_thread(
                service.set_tail_schedule_enabled, schedule_id, payload.enabled
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except KeyError:
            raise HTTPException(status_code=404, detail="Tail discovery schedule not found")

    @app.delete("/api/tail-discovery/schedules/{schedule_id}")
    async def delete_tail_discovery_schedule(schedule_id: str) -> dict:
        try:
            deleted = await asyncio.to_thread(service.delete_tail_schedule, schedule_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Tail discovery schedule not found")
        if not deleted:
            raise HTTPException(status_code=404, detail="Tail discovery schedule not found")
        return {"deleted": True, "schedule_id": schedule_id}

    @app.get("/api/tail-discovery/results")
    async def tail_discovery_results(limit: int = Query(100, ge=1, le=200),
                                     offset: int = Query(0, ge=0),
                                     job_id: str | None = None) -> dict:
        try:
            rows = service.tail_discovery_results(limit=limit + 1, job_id=job_id, offset=offset)
        except KeyError:
            raise HTTPException(status_code=404, detail="Tail discovery job not found")
        return {"results": rows[:limit], "has_more": len(rows) > limit,
                "next_offset": offset + min(len(rows), limit), "job_id": job_id}

    @app.post("/api/tail-discovery/run")
    async def run_tail_discovery(payload: dict = Body(...)) -> dict:
        try:
            return await asyncio.to_thread(service.submit_tail_discovery, payload)
        except KeyError:
            raise HTTPException(status_code=404, detail="Source job not found")
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/api/tail-discovery/jobs/{job_id}/restart")
    async def restart_tail_discovery(job_id: str, payload: dict = Body(...)) -> dict:
        try:
            return await asyncio.to_thread(service.restart_tail_discovery, job_id, str(payload.get("mode") or "resume"))
        except KeyError:
            raise HTTPException(status_code=404, detail="Source job not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/api/tail-discovery/results/{result_id}/recheck")
    async def recheck_tail_result(result_id: int) -> dict:
        try:
            return await asyncio.to_thread(service.recheck_tail_result, result_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Quote not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

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
            return await asyncio.to_thread(service.upsert_route, payload)
        except ConfigConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/routes/{route_key:path}/history")
    async def route_price_history(
        route_key: str,
        provider: str | None = None,
        limit: int = Query(60, ge=1, le=200),
    ) -> dict:
        return await asyncio.to_thread(
            service.route_price_history,
            route_key,
            provider=provider,
            limit=limit,
        )

    @app.post("/api/routes/{route_key:path}/auto-query")
    async def update_route_auto_query(route_key: str, payload: EnabledPayload) -> dict:
        try:
            return await asyncio.to_thread(
                service.update_route_auto_query, route_key, enabled=payload.enabled
            )
        except ConfigConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except KeyError:
            raise HTTPException(status_code=404, detail="Route not found")

    @app.post("/api/route-groups/{group_id:path}/auto-query")
    async def update_route_group_auto_query(group_id: str, payload: EnabledPayload) -> dict:
        try:
            return await asyncio.to_thread(
                service.update_route_group_auto_query, group_id, enabled=payload.enabled
            )
        except ConfigConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except KeyError:
            raise HTTPException(status_code=404, detail="Route group not found")

    @app.delete("/api/routes/{route_key:path}")
    async def delete_route(route_key: str) -> dict:
        try:
            deleted = await asyncio.to_thread(service.delete_route, route_key)
        except ConfigConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        if not deleted:
            raise HTTPException(status_code=404, detail="Route not found")
        return {"deleted": True}

    @app.delete("/api/route-groups/{group_id:path}")
    async def delete_route_group(group_id: str) -> dict:
        try:
            deleted = await asyncio.to_thread(service.delete_route_group, group_id)
        except ConfigConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        if not deleted:
            raise HTTPException(status_code=404, detail="Route group not found")
        return {"deleted": True}

    @app.get("/download/csv")
    async def download_csv() -> FileResponse:
        csv_path = await asyncio.to_thread(service.export_csv)
        return FileResponse(csv_path, filename=csv_path.name)

    @app.get("/api/log")
    async def recent_log() -> dict:
        lines: list[str] = []
        path = service.config.log_file
        if path.exists():
            make_file_private(path)
            file_lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-120:]
            if file_lines:
                lines.extend([
                    f"[{path.name}] {sanitize_sensitive_text(line)}"
                    for line in file_lines
                ])
        return {"lines": lines[-120:]}

    return app


def _route_group_providers(routes: list[dict]) -> list[str]:
    providers: list[str] = []
    for route in routes:
        for provider in route.get("providers") or []:
            if provider not in providers:
                providers.append(provider)
    return providers
