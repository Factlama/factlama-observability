"""OBS-11 stage-3 (2026-10-23): `operations.metrics.install_metrics` tested
against a throwaway `FastAPI` app, independent of either real service --
`tests/test_collector_ingress.py`/`tests/test_query_api.py` each add a
thin smoke test proving their own app actually calls this module, not a
second copy of this module's own logic.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from operations.metrics import install_metrics


def _build_app() -> FastAPI:
    app = FastAPI()
    install_metrics(app, build_version="9.9.9")

    @app.get("/widgets/{widget_id}")
    async def get_widget(widget_id: str) -> dict[str, str]:
        return {"widget_id": widget_id}

    return app


def test_metrics_endpoint_exposes_prometheus_text_format() -> None:
    client = TestClient(_build_app())
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


def test_build_info_carries_the_given_version() -> None:
    client = TestClient(_build_app())
    body = client.get("/metrics").text
    assert 'obs_build_info{version="9.9.9"} 1.0' in body


def test_request_count_uses_the_route_template_not_the_literal_path() -> None:
    """Two requests for two different widget IDs must collapse into one
    route label -- a per-ID label would make this metric's cardinality grow
    without bound as real traffic accumulates (ADR-021's "avoid high-
    cardinality ... labels" rule)."""
    client = TestClient(_build_app())
    client.get("/widgets/alpha")
    client.get("/widgets/bravo")

    body = client.get("/metrics").text
    assert 'route="/widgets/{widget_id}"' in body
    assert "widgets/alpha" not in body
    assert "widgets/bravo" not in body


def test_request_count_increments_per_request() -> None:
    client = TestClient(_build_app())
    before = client.get("/metrics").text
    client.get("/widgets/alpha")
    after = client.get("/metrics").text

    def _count_for(body: str, route: str, method: str, status_code: str) -> float:
        # prometheus_client renders labels in sorted-by-name order, not
        # declaration order -- match each label independently rather than
        # assuming a fixed concatenation order.
        needles = [f'route="{route}"', f'method="{method}"', f'status_code="{status_code}"']
        for line in body.splitlines():
            if line.startswith("obs_http_requests_total{") and all(n in line for n in needles):
                return float(line.rsplit(" ", 1)[1])
        return 0.0

    before_count = _count_for(before, "/widgets/{widget_id}", "GET", "200")
    after_count = _count_for(after, "/widgets/{widget_id}", "GET", "200")
    assert after_count == before_count + 1


def test_storage_up_reflects_the_most_recent_recorded_readiness() -> None:
    app = _build_app()
    client = TestClient(app)

    app.state.record_storage_readiness(True)
    assert "obs_storage_up 1.0" in client.get("/metrics").text

    app.state.record_storage_readiness(False)
    assert "obs_storage_up 0.0" in client.get("/metrics").text


def test_two_installations_do_not_share_a_registry() -> None:
    """The real reason this is a function returning a fresh registry, not a
    module-level singleton: two independently-imported app instances (as
    happens within one pytest process importing both real services) must
    not accumulate each other's request counts."""
    client_one = TestClient(_build_app())
    client_two = TestClient(_build_app())

    client_one.get("/widgets/alpha")
    client_one.get("/widgets/alpha")

    body_two = client_two.get("/metrics").text
    assert 'route="/widgets/{widget_id}"' not in body_two
