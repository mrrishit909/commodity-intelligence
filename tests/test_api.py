"""Integration, end-to-end and security tests against a real Postgres."""
import uuid

import pytest

from core import db, jobs, scenario
from cm import world

from .conftest import bearer

HEAD, ANALYST, VIEWER, OTHER = bearer("desk", "head"), bearer("desk", "analyst"), bearer("desk", "viewer"), bearer("other", "head")


def test_nothing_works_before_a_market_is_loaded(client):
    assert client.get("/v1/signals/anomalies", headers=VIEWER).json()["code"] == "no_market" and client.get(f"/v1/flows?commodity={uuid.uuid4()}", headers=VIEWER).json()["code"] == "no_market"


@pytest.fixture(scope="module")
def demo(client):
    results, _ = scenario.run(client, drain=jobs.drain)
    return results


def one(sql, params=(), tenant=None):
    with db.tx(tenant) as c:
        return c.execute(sql, params).fetchone()


def test_demo_journey(demo):
    load = demo["load"]["result"]
    bt = load["model"]["backtest_32_periods"]
    assert load["week"] == world.NOW and load["cargoes"] == one("SELECT count(*) AS n FROM vessel_flows WHERE departed_week <= %s", [world.NOW])["n"] and bt["mae_nowcast"] < bt["mae_regression"] < bt["mae_last_official"]
    assert one("SELECT count(*) AS n FROM fundamentals f, markets m WHERE f.metric = 'mine_production' AND f.published_week > m.now_week")["n"] == 0        # nothing published from the future
    f = demo["flows"]
    assert f["at_sea_kt"] > 300 and len(f["routes"]) == 9 and set(f["shipping_by_origin_kt_week"]) == set(world.MINES) and all(r["destination"] == "China" for r in demo["flows_cn"]["routes"])
    b0, n0 = demo["bal0"], demo["nc0"]
    assert b0["balance_kt_week"]["nowcast"] == n0["balance"]["value"] and b0["last_official"]["weeks_old"] >= world.LAG and n0["revision"] is None and n0["stored"] == 4 and len(n0["inventory"]["horizons"]) == 3
    adv = demo["adv"]["result"]
    assert adv["week"] == world.NOW + 8 and adv["new_prices"] == 16 and [p["period"] for p in adv["official_periods_published"]] == [70, 71] and one("SELECT now_week FROM markets")["now_week"] == world.NOW + 8
    kinds = {(a["kind"], a["origin"], a["direction"]) for a in demo["anom"]["items"]}
    assert ("departures", "Antofagasta", "down") in kinds and ("overdue_cargo", "Callao", "up") in kinds
    assert demo["ev1"]["event_type"] == "mine_outage" and one("SELECT count(*) AS n FROM events")["n"] == 2
    n1 = demo["nc1"]
    r = n1["revision"]
    assert n1["balance"]["value"] < n0["balance"]["value"] - 10 and r["previous"]["asof_week"] == world.NOW and abs(sum(r["drivers_kt_week"].values()) - r["delta_kt_week"]) < 0.05 and r["drivers_kt_week"]["departures"] < 0 and r["official_published_since"] == [70, 71]
    assert n1["simulation_truth"]["inside_band"] and demo["bal1"]["regions"][0]["implied_from_shipping_kt_week"] < 0.8 * demo["bal1"]["regions"][0]["last_official_kt_week"]
    s = demo["scen"]
    assert s["summary"]["stocks_difference"] < -40 and s["summary"]["price_end_scenario"]["p10"] < s["summary"]["price_end_scenario"]["p50"] < s["summary"]["price_end_scenario"]["p90"] and len(s["baseline"]["stocks_p50"]) == 13 and demo["badscen"]["code"] == "bad_shock"
    t = demo["timeline"]
    assert [i["kind"] for i in t["items"]].count("nowcast") == 2 and [i["kind"] for i in t["items"]].count("event") == 2 and t["items"] == sorted(t["items"], key=lambda i: str(i["week"]))
    assert demo["audit"]["chain_valid"] and {"market.loaded", "market.advanced", "event.recorded", "nowcast.run", "scenario.run"} <= {e["action"] for e in demo["audit"]["events"]}


def test_validation(client, demo):
    cid = demo["load"]["result"]["commodity_id"]
    post = lambda path, body, h=ANALYST: client.post(path, headers=h, json=body)     # noqa: E731
    assert client.get(f"/v1/commodities/{uuid.uuid4()}/balance", headers=VIEWER).json()["code"] == "commodity_not_found" and client.get(f"/v1/flows?commodity={cid}&region=Atlantis", headers=VIEWER).json()["code"] == "region_not_found"
    assert post("/v1/scenarios", {"commodity_id": cid, "shocks": [{"kind": "congestion", "port": "Rotterdam", "weeks": 4, "extra_weeks": 1}]}).json()["code"] == "bad_shock"
    assert post("/v1/scenarios", {"commodity_id": cid, "shocks": []}).status_code == 422
    assert post("/v1/events", {"commodity_id": cid, "event_type": "strike", "region": "Peru", "occurred_at": "2026-09-01T09:00:00", "severity": 0.5, "source_uri": "https://example.invalid/x"}).json()["code"] == "timezone_required"
    assert post("/v1/events", {"commodity_id": cid, "event_type": "earthquake", "region": "Peru", "occurred_at": "2026-09-01T09:00:00Z", "severity": 0.5, "source_uri": "https://example.invalid/x"}).status_code == 422
    assert post("/v1/market:advance", {"weeks": 30}, HEAD).status_code == 422 and post("/v1/market:advance", {"weeks": 1, "shocks": [{"kind": "outage", "region": "Chile", "weeks": 2}]}, HEAD).json()["code"] == "bad_shock"


def test_roles(client, demo):
    cid = demo["load"]["result"]["commodity_id"]
    assert client.get(f"/v1/commodities/{cid}/balance").status_code == 401 and client.get(f"/v1/commodities/{cid}/balance", headers=VIEWER).status_code == 200
    for path in ("/v1/forecasts/nowcast", "/v1/scenarios", "/v1/events"):
        assert client.post(path, headers=VIEWER, json={}).status_code == 403, path
    assert client.post("/v1/market:advance", headers=ANALYST, json={"weeks": 1}).status_code == 403 and client.post("/v1/market:load", headers=ANALYST, json={}).status_code == 403
    assert client.get("/v1/audit", headers=ANALYST).status_code == 403 and client.get("/v1/audit", headers=HEAD).status_code == 200


def test_another_desk_sees_nothing(client, demo, seeded):
    assert client.get("/v1/signals/anomalies", headers=OTHER).json()["code"] == "no_market"
    for table in ("markets", "commodities", "instruments", "prices", "fundamentals", "vessel_flows", "events", "forecasts"):
        assert one(f"SELECT count(*) AS n FROM {table}", tenant=seeded["other"])["n"] == 0 < one(f"SELECT count(*) AS n FROM {table}", tenant=seeded["desk"])["n"], table
    with pytest.raises(Exception, match="row-level security"), db.tx(seeded["other"]) as c:
        c.execute("INSERT INTO markets (id, tenant_id, model_seed, now_week, start_date) VALUES (%s,%s,1,0,'2021-01-04')", [uuid.uuid4(), seeded["desk"]])


def test_idempotent_writes(client, demo):
    cid = demo["load"]["result"]["commodity_id"]
    again = client.post("/v1/forecasts/nowcast", headers={**ANALYST, "Idempotency-Key": "demo-1:nc1"}, json={"commodity_id": cid})
    assert again.status_code == 201 and again.headers["Idempotent-Replay"] == "true" and again.json()["run_id"] == demo["nc1"]["run_id"] and one("SELECT count(*) AS n FROM forecasts")["n"] == 8
    assert client.post("/v1/forecasts/nowcast", headers={**ANALYST, "Idempotency-Key": "demo-1:nc1"}, json={"commodity_id": str(uuid.uuid4())}).json()["code"] == "idempotency_key_reused"
    assert client.post("/v1/market:load", headers=HEAD, json={"seed": 17}).json()["code"] == "market_exists"
