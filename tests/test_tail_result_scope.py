"""Task isolation is applied before pagination, including resumed checkpoints."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from data_storage import PriceRepository
from web_ui import create_app
from test_tail_improvements import quote, DATE
from test_web_ui_validation import write_config


@pytest.fixture
def store(tmp_path):
    config = tmp_path / "config.json"
    write_config(config)
    app = create_app(str(config))
    repo = PriceRepository(tmp_path / "runtime/test.db")
    repo.initialize()
    for job in ("a", "b", "legacy"):
        repo.upsert_tail_discovery_job({"job_id": job, "status": "finished", "payload": {}})
    repo.upsert_tail_discovery_job({"job_id": "resumed", "status": "running",
                                    "payload": {"_checkpoint_job_id": "a"}, "progress": {}})
    for index, job in enumerate(("a", "a", "b")):
        item = quote(price=700 + index, provider="ctrip")
        item["raw_payload"]["validation"] = {"status": "valid"}
        repo.checkpoint_tail_candidate(
            root_job_id=job, fingerprint="scope",
            attempt={"job_id": job, "queue_key": "scope", "provider": "ctrip",
                     "origin": "BJS", "transfer": "SHA", "destination": f"C{index}",
                     "departure_date": DATE, "cabin": "economy", "status": "matched"},
            results=[item],
        )
    with TestClient(app) as client:
        yield repo, client


def test_scope_is_not_inferred_from_same_route_date_or_last_200_rows(store):
    repo, client = store
    old = quote(price=300, provider="ctrip")
    for _ in range(205):
        repo.insert_tail_discovery_result(deepcopy(old))
    data = client.get("/api/tail-discovery/results?job_id=a&limit=200").json()
    assert sorted(row["price"] for row in data["results"]) == [700, 701]
    assert not data["has_more"]
    assert all("raw_payload" not in row for row in data["results"])


def test_resumed_job_includes_only_its_root_checkpoints(store):
    _, client = store
    original = client.get("/api/tail-discovery/results?job_id=a").json()["results"]
    resumed = client.get("/api/tail-discovery/results?job_id=resumed").json()["results"]
    assert [r["id"] for r in resumed] == [r["id"] for r in original]
    assert len(resumed) == 2


def test_legacy_or_missing_jobs_never_fall_back_to_global_history(store):
    _, client = store
    assert client.get("/api/tail-discovery/results?job_id=legacy").json()["results"] == []
    assert client.get("/api/tail-discovery/results?job_id=missing").status_code == 404
    assert client.get("/api/tail-discovery/results?job_id=a%27%20OR%201=1--").status_code == 404


def test_history_and_task_pagination_are_stable_for_equal_timestamps(store):
    _, client = store
    for scope, expected in (("", 3), ("&job_id=a", 2)):
        ids, offset = [], 0
        while True:
            data = client.get(f"/api/tail-discovery/results?limit=1&offset={offset}{scope}").json()
            ids.extend(row["id"] for row in data["results"])
            offset = data["next_offset"]
            if not data["has_more"]:
                break
        assert len(set(ids)) == len(ids) == expected


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "offset=-1", "limit=oops"])
def test_pagination_validation(store, query):
    _, client = store
    assert client.get("/api/tail-discovery/results?" + query).status_code == 422
