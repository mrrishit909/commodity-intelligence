"""Public API (modular monolith). Blueprint services map to: market-data-ingestion and fundamentals (the load and advance jobs),
shipping-flow-service, nowcast-service, scenario-engine, signal-explanation-service (the contributions and drivers on every nowcast),
news-event-service (structured events with a source, recorded by a person: no language model) and the research API.
Not built: exchange, EIA/USGS, NOAA or AIS connectors; a feature store; news extraction.

    uvicorn cm.api:app          python -m core.jobs cm.api      # the worker
"""
import datetime
import functools
import uuid
from typing import Literal

import numpy as np
from fastapi import Depends, Header, Query
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from core import audit, db, jobs
from core.app import Ctx, Problem, create_app, run

from . import engine, world

READ = {"market:read", "jobs:read"}
WORK = READ | {"forecasts:run", "scenarios:run", "events:write"}
PERMISSIONS = {"viewer": READ, "analyst": WORK, "head": WORK | {"market:load", "market:advance", "audit:read"}}
app = create_app("commodity-intelligence", PERMISSIONS)
auth = app.state.auth
IdemKey = Header(None, alias="Idempotency-Key")
START = datetime.date(2021, 1, 4)                 # week 0 is the Monday of this week
UNIT = "kt"


def week_date(w):
    return START + datetime.timedelta(weeks=int(w))


def week_ts(w):
    return datetime.datetime.combine(week_date(w), datetime.time(17, 0), tzinfo=datetime.timezone.utc)


def worker_ctx(job):
    return Ctx(job["tenant_id"], uuid.UUID(job["payload"]["actor_id"]), "worker", "system")


def market(c):
    m = c.execute("SELECT * FROM markets").fetchone()
    if not m:
        raise Problem(409, "no_market", "load a market first: POST /v1/market:load")
    shocks = tuple(tuple(s) for s in m["shocks"]) or None
    return m, world.build(m["model_seed"], shocks)


def copper_id(t):
    return uuid.uuid5(t, "commodity-CU")


@functools.lru_cache(maxsize=16)
def state(seed, shocks, now):
    """Signals, fitted equations and the nowcast for a world at a week: computed once per state of knowledge."""
    w = world.build(seed, shocks)
    sig = engine.signals(w, now)
    m = engine.fit(sig)
    return {"sig": sig, "model": m, "nowcast": engine.nowcast(sig, m, now)}


def st(mk, w):
    return state(mk["model_seed"], tuple(tuple(s) for s in mk["shocks"]) or None, mk["now_week"])


# ---------------------------------------------------------------- load and advance (ingestion, simulated)

def rows_for_weeks(t, cid, w, weeks, published_upto):
    """Prices, fundamentals and vessel departures for the given weeks, plus official figures for periods published by `published_upto`."""
    cash, three = uuid.uuid5(t, "inst-cash"), uuid.uuid5(t, "inst-3m")
    prices = []
    for k in weeks:
        p = float(w["price"][k])
        prices.append((cash, week_ts(k), round(p * 0.995, 2), round(p * 1.01, 2), round(p * 0.99, 2), round(p, 2), 12000, t, k))
        prices.append((three, week_ts(k), None, None, None, round(p + float(w["spread"][k]), 2), 9000, t, k))
    fund = []
    for k in weeks:
        fund.append((cid, "exchange", week_date(k), "exchange_stocks", round(float(w["stocks"][k]), 1), UNIT, "exchange", t, k, k))
        fund.append((cid, "world", week_date(k), "macro_index", round(float(w["macro"][k]), 4), "index", "statistics office", t, k, k))
    return prices, fund


def official_rows(t, cid, w, periods):
    fund = []
    for p in periods:
        a, b = p * world.PERIOD, (p + 1) * world.PERIOD
        pub = b + world.LAG
        for name in world.MINES:
            fund.append((cid, name, week_date(a), "mine_production", round(float(w["prod"][name][a:b].mean()), 2), UNIT + "/week", "mining ministry", t, a, pub))
        for name in world.IMPORTERS:
            fund.append((cid, name, week_date(a), "refined_demand", round(float(w["demand"][name][a:b].mean()), 2), UNIT + "/week", "industry association", t, a, pub))
    return fund


def vessel_rows(t, cid, w, weeks):
    ws = set(weeks)
    return [(uuid.uuid5(t, v["vessel_id"]), t, cid, v["vessel_id"], v["origin"], v["destination"], week_ts(v["departed"]), week_ts(v["planned_eta"]), v["qty"], v["region"], v["departed"], v["planned_eta"], v["eta"]) for v in w["vessels"] if v["departed"] in ws]


def published_periods(now, after=-1):
    return [p for p in range(0, world.period_of(now) + 1) if (p + 1) * world.PERIOD + world.LAG <= now and (p + 1) * world.PERIOD + world.LAG > after]


class LoadIn(BaseModel):
    seed: int = Field(17, ge=0, le=10 ** 6)


@app.post("/v1/market:load", status_code=202, tags=["market"], summary="(+) Load the invented copper market as of week 288: six years of weekly prices, exchange stocks, a macro index, official production and demand as published so far, and 5,000+ cargoes; fit the nowcast and back-test it")
def load_market(body: LoadIn, ctx: Ctx = Depends(auth("market:load")), idem: str | None = IdemKey):
    def work(c):
        if c.execute("SELECT 1 FROM markets LIMIT 1").fetchone():
            raise Problem(409, "market_exists")
        return 202, jobs.enqueue(c, ctx, "market.load", body.model_dump())
    return run(ctx, idem, body, work)


@jobs.handler("market.load")
def load_job(c, job):
    t, now = job["tenant_id"], world.NOW
    w = world.build(job["payload"]["seed"])
    mid, cid = uuid.uuid5(t, "market"), copper_id(t)
    c.execute("INSERT INTO markets (id, tenant_id, model_seed, now_week, start_date) VALUES (%s,%s,%s,%s,%s)", [mid, t, w["seed"], now, START])
    c.execute("INSERT INTO commodities (id, tenant_id, symbol, name, unit) VALUES (%s,%s,'CU','Copper',%s)", [cid, t, UNIT])
    db.load(c, "instruments", ["id", "tenant_id", "commodity_id", "venue", "contract_code", "expiry", "currency"], [(uuid.uuid5(t, "inst-cash"), t, cid, "exchange", "CU-CASH", None, "USD"), (uuid.uuid5(t, "inst-3m"), t, cid, "exchange", "CU-3M", None, "USD")])
    weeks = list(range(0, now + 1))
    prices, fund = rows_for_weeks(t, cid, w, weeks, now)
    db.load(c, "prices", ["instrument_id", "ts", "open", "high", "low", "close", "volume", "tenant_id", "week"], prices)
    db.load(c, "fundamentals", ["commodity_id", "region", "period", "metric", "value", "unit", "source", "tenant_id", "week", "published_week"], fund + official_rows(t, cid, w, published_periods(now)))
    db.load(c, "vessel_flows", ["id", "tenant_id", "commodity_id", "vessel_id", "origin", "destination", "departed_at", "eta", "estimated_qty", "origin_region", "departed_week", "planned_eta_week", "arrival_week"], vessel_rows(t, cid, w, weeks))
    s = state(w["seed"], None, now)
    # back-test: the nowcast at the last week of each of the past 32 periods against the balance later published
    err = {"nowcast": [], "last_official": [], "regression": [], "covered": []}
    for p in range(world.period_of(now) - 32, world.period_of(now)):
        nw = p * world.PERIOD + world.PERIOD - 1
        sg = engine.signals(w, nw)
        nc = engine.nowcast(sg, engine.fit(sg), nw)
        truth = float(w["balance"][p * world.PERIOD:(p + 1) * world.PERIOD].mean())
        err["nowcast"].append(nc["value"] - truth)
        err["last_official"].append(nc["last_official"]["value"] - truth)
        err["regression"].append((nc["regression"] if nc["regression"] is not None else nc["last_official"]["value"]) - truth)
        err["covered"].append(nc["lower"] <= truth <= nc["upper"])
    nc = s["nowcast"]
    p = nc["period"]
    out = {"market_id": mid, "commodity_id": cid, "week": now, "date": str(week_date(now)), "prices": len(prices), "fundamentals": len(fund), "official_periods": len(published_periods(now)), "cargoes": len([v for v in w["vessels"] if v["departed"] <= now]),
           "latest": {"price": round(float(w["price"][now])), "spread_3m": round(float(w["spread"][now]), 1), "exchange_stocks": round(float(w["stocks"][now]), 1), "weeks_of_cover": round(float(w["stocks"][now] / nc["demand"]), 2)},
           "mines": {k: {"capacity_kt_week": v["capacity"], "port": v["port"]} for k, v in world.MINES.items()}, "importers": world.IMPORTERS, "official_lag_weeks": world.LAG,
           "model": {"version": engine.VERSION, **s["model"]["report"], "backtest_32_periods": {"mae_nowcast": round(float(np.mean(np.abs(err["nowcast"]))), 2), "mae_last_official": round(float(np.mean(np.abs(err["last_official"]))), 2), "mae_regression": round(float(np.mean(np.abs(err["regression"]))), 2), "inside_80pct_band": round(float(np.mean(err["covered"])), 2)}},
           "simulation_truth": {"what": "the real balance of the current period, which the models never see", "period_balance_kt_week": round(float(w["balance"][p * world.PERIOD:(p + 1) * world.PERIOD].mean()), 2), "past_outages": len(w["outages"])}}
    audit.record(c, worker_ctx(job), "market.loaded", "market", mid, {"week": now, "cargoes": out["cargoes"]})
    return out


class Shock(BaseModel):
    kind: Literal["outage", "congestion"]
    region: str | None = Field(None, description="mine region for an outage")
    port: str | None = Field(None, description="port for congestion")
    start: int = Field(1, ge=0, le=52, description="weeks from now")
    weeks: int = Field(ge=1, le=52)
    cut: float | None = Field(None, ge=0.05, le=1.0, description="share of production lost")
    extra_weeks: int | None = Field(None, ge=1, le=8, description="extra transit weeks")

    def check(self):
        if self.kind == "outage" and (self.region not in world.MINES or self.cut is None):
            raise Problem(422, "bad_shock", f"an outage needs a mine region in {list(world.MINES)} and a cut")
        if self.kind == "congestion" and (self.port not in {m['port'] for m in world.MINES.values()} or self.extra_weeks is None):
            raise Problem(422, "bad_shock", "congestion needs a known port and extra_weeks")
        return self


class AdvanceIn(BaseModel):
    weeks: int = Field(ge=1, le=12)
    shocks: list[Shock] = Field(default_factory=list, max_length=4, description="what really happens in the weeks being generated; the desk only sees the data")


@app.post("/v1/market:advance", status_code=202, tags=["market"], summary="(+) The feeds move forward: generate the next weeks of prices, stocks, cargoes and newly published official figures. In the demo the generator is told what happens; the service only sees the data")
def advance(body: AdvanceIn, ctx: Ctx = Depends(auth("market:advance")), idem: str | None = IdemKey):
    def work(c):
        mk, _ = market(c)
        for s in body.shocks:
            s.check()
        if mk["now_week"] + body.weeks >= world.WEEKS:
            raise Problem(422, "end_of_data", f"the simulator holds {world.WEEKS} weeks")
        return 202, jobs.enqueue(c, ctx, "market.advance", body.model_dump())
    return run(ctx, idem, body, work)


@jobs.handler("market.advance")
def advance_job(c, job):
    t = job["tenant_id"]
    mk, _ = market(c)
    now0, now = mk["now_week"], mk["now_week"] + job["payload"]["weeks"]
    shocks = [tuple(s) for s in mk["shocks"]] + [("outage", s["region"], now0 + s["start"], s["weeks"], s["cut"]) if s["kind"] == "outage" else ("congestion", s["port"], now0 + s["start"], s["weeks"], s["extra_weeks"]) for s in job["payload"]["shocks"]]
    w = world.build(mk["model_seed"], tuple(shocks) or None)
    cid = copper_id(t)
    weeks = list(range(now0 + 1, now + 1))
    prices, fund = rows_for_weeks(t, cid, w, weeks, now)
    newly = published_periods(now, after=now0)
    db.load(c, "prices", ["instrument_id", "ts", "open", "high", "low", "close", "volume", "tenant_id", "week"], prices)
    db.load(c, "fundamentals", ["commodity_id", "region", "period", "metric", "value", "unit", "source", "tenant_id", "week", "published_week"], fund + official_rows(t, cid, w, newly))
    db.load(c, "vessel_flows", ["id", "tenant_id", "commodity_id", "vessel_id", "origin", "destination", "departed_at", "eta", "estimated_qty", "origin_region", "departed_week", "planned_eta_week", "arrival_week"], vessel_rows(t, cid, w, weeks))
    c.execute("UPDATE markets SET now_week = %s, shocks = %s WHERE id = %s", [now, Jsonb(shocks), mk["id"]])
    audit.record(c, worker_ctx(job), "market.advanced", "market", mk["id"], {"from_week": now0, "to_week": now, "shocks": len(job["payload"]["shocks"])})
    return {"market_id": mk["id"], "from_week": now0, "week": now, "date": str(week_date(now)), "new_prices": len(prices), "new_cargoes": len([v for v in w["vessels"] if now0 < v["departed"] <= now]), "official_periods_published": [{"period": p, "weeks": [p * world.PERIOD, (p + 1) * world.PERIOD - 1]} for p in newly],
            "simulation_truth": {"what": "what the generator was told happens; the desk sees only the data", "shocks": job["payload"]["shocks"]}}


# ---------------------------------------------------------------- balance, flows, anomalies

def check_commodity(c, commodity_id):
    if not c.execute("SELECT 1 FROM commodities WHERE id = %s", [commodity_id]).fetchone():
        raise Problem(404, "commodity_not_found")


@app.get("/v1/commodities/{commodity_id}/balance", tags=["research"], summary="The supply-demand balance nowcast for the current period with its band, what each signal contributed, the last official figure and its age, stocks, cover, price and spread")
def balance(commodity_id: uuid.UUID, ctx: Ctx = Depends(auth("market:read"))):
    with db.tx(ctx.tenant_id) as c:
        mk, w = market(c)
        check_commodity(c, commodity_id)
    s = st(mk, w)
    nc, sig, now = s["nowcast"], s["sig"], mk["now_week"]
    a1 = s["model"]["dep"][0][1]
    regions = []
    for name, m in world.MINES.items():
        dep = sig["dep_by_origin"][m["port"]]
        off = sig["official_prod"][name]
        last_off = off[~np.isnan(off)]
        regions.append({"region": name, "port": m["port"], "capacity_kt_week": m["capacity"], "implied_from_shipping_kt_week": round(float(dep[now - 3:now + 1].mean() / a1), 1), "last_official_kt_week": round(float(last_off[-world.PERIOD:].mean()), 1) if len(last_off) else None})
    p = nc["period"]
    truth = float(w["balance"][p * world.PERIOD:(p + 1) * world.PERIOD].mean())
    return jsonable_encoder({"commodity_id": commodity_id, "week": now, "date": str(week_date(now)), "period": {"index": p, "weeks": nc["weeks"], "weeks_seen": nc["weeks_seen"]},
                             "balance_kt_week": {"nowcast": nc["value"], "lower": nc["lower"], "upper": nc["upper"], "band": "80%", "supply": nc["supply"], "demand": nc["demand"]}, "contributions_kt_week": nc["contributions"],
                             "last_official": nc["last_official"], "regression_baseline": nc["regression"], "model_version": engine.VERSION,
                             "inventory": {"exchange_stocks_kt": round(float(sig["stocks"][now]), 1), "weeks_of_cover": round(float(sig["stocks"][now] / nc["demand"]), 2), "change_4_weeks_kt": round(float(sig["stocks"][now] - sig["stocks"][now - 4]), 1)},
                             "price": {"cash": round(float(sig["price"][now])), "spread_3m": round(float(sig["spread"][now]), 1), "change_4_weeks_pct": round(float(sig["price"][now] / sig["price"][now - 4] - 1) * 100, 1)}, "regions": regions,
                             "series": {"weeks": list(range(now - 51, now + 1)), "stocks": sig["stocks"][now - 51:now + 1].round(1).tolist(), "price": sig["price"][now - 51:now + 1].round(0).tolist(), "departures": sig["departures"][now - 51:now + 1].round(1).tolist(), "filtered_balance": (nc["filter"]["x"][now - 51:now + 1, 0] - nc["filter"]["x"][now - 51:now + 1, 1]).round(2).tolist()},
                             "simulation_truth": {"what": "the real balance of this period", "value": round(truth, 2), "inside_band": bool(nc["lower"] <= truth <= nc["upper"])}})


@app.get("/v1/flows", tags=["research"], summary="Mine-to-port-to-importer flows: cargoes at sea, tonnage arriving in the next weeks by route, overdue cargo, and what each origin is shipping against its official production")
def flows(commodity: uuid.UUID, region: str | None = Query(None, description="an importer or a mine region"), ctx: Ctx = Depends(auth("market:read"))):
    with db.tx(ctx.tenant_id) as c:
        mk, w = market(c)
        check_commodity(c, commodity)
        now = mk["now_week"]
        at_sea = c.execute("SELECT origin, origin_region, destination, count(*) AS cargoes, sum(estimated_qty) AS qty, sum(CASE WHEN planned_eta_week <= %s THEN estimated_qty ELSE 0 END) AS overdue_qty, avg(planned_eta_week - departed_week) AS planned_transit FROM vessel_flows WHERE departed_week <= %s AND arrival_week > %s GROUP BY 1,2,3 ORDER BY 1,3", [now, now, now]).fetchall()
        arriving = c.execute("SELECT destination, planned_eta_week - %s AS weeks_ahead, sum(estimated_qty) AS qty FROM vessel_flows WHERE arrival_week > %s AND planned_eta_week BETWEEN %s AND %s GROUP BY 1,2 ORDER BY 1,2", [now, now, now + 1, now + 4]).fetchall()
        recent = c.execute("SELECT origin_region, sum(estimated_qty) / 4 AS kt_week FROM vessel_flows WHERE departed_week BETWEEN %s AND %s GROUP BY 1", [now - 3, now]).fetchall()
    if region and region not in world.IMPORTERS and region not in world.MINES:
        raise Problem(404, "region_not_found", f"regions: {list(world.IMPORTERS) + list(world.MINES)}")
    routes = [dict(r) for r in at_sea if not region or region in (r["destination"], r["origin_region"])]
    return jsonable_encoder({"commodity_id": commodity, "week": now, "region": region, "routes": routes, "at_sea_kt": round(float(sum(r["qty"] for r in routes)), 1), "overdue_kt": round(float(sum(r["overdue_qty"] for r in routes)), 1),
                             "arriving_next_4_weeks": [dict(a) for a in arriving if not region or a["destination"] == region], "shipping_by_origin_kt_week": {r["origin_region"]: round(float(r["kt_week"]), 1) for r in recent},
                             "graph": {"nodes": [*({"id": k, "kind": "mine"} for k in world.MINES), *({"id": v["port"], "kind": "port"} for v in world.MINES.values()), *({"id": k, "kind": "importer"} for k in world.IMPORTERS)], "edges": [*({"from": k, "to": v["port"]} for k, v in world.MINES.items()), *({"from": r["origin"], "to": r["destination"], "kt": round(float(r["qty"]), 1)} for r in at_sea)]}})


@app.get("/v1/signals/anomalies", tags=["research"], summary="Change points on shipping: departures by origin and overdue cargo by port, with the normal level, the new level and when the shift started")
def signal_anomalies(ctx: Ctx = Depends(auth("market:read"))):
    with db.tx(ctx.tenant_id) as c:
        mk, w = market(c)
    s = st(mk, w)
    items = engine.anomalies(w, s["sig"], mk["now_week"])
    return jsonable_encoder({"week": mk["now_week"], "items": [{**a, "start_date": str(week_date(a["start_week"]))} for a in items], "method": "two-sided CUSUM on each series standardised by its trailing year, the last eight weeks excluded so a drift cannot hide itself"})


# ---------------------------------------------------------------- events (news-event-service without the NLP)

class EventIn(BaseModel):
    commodity_id: uuid.UUID
    event_type: Literal["mine_outage", "port_congestion", "policy", "strike", "weather", "other"]
    region: str = Field(max_length=40)
    occurred_at: datetime.datetime
    severity: float = Field(ge=0, le=1)
    source_uri: str = Field(min_length=8, max_length=300)
    detail: dict = Field(default_factory=dict)


@app.post("/v1/events", status_code=201, tags=["research"], summary="(+) Record a market event with its source: an analyst or a feed, structured, no text extraction")
def post_event(body: EventIn, ctx: Ctx = Depends(auth("events:write")), idem: str | None = IdemKey):
    def work(c):
        market(c)
        check_commodity(c, body.commodity_id)
        if body.occurred_at.tzinfo is None:
            raise Problem(422, "timezone_required")
        eid = uuid.uuid7()
        c.execute("INSERT INTO events (id, tenant_id, commodity_id, occurred_at, event_type, region, severity, source_uri, detail, recorded_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", [eid, ctx.tenant_id, body.commodity_id, body.occurred_at, body.event_type, body.region, body.severity, body.source_uri, Jsonb(body.detail), ctx.actor_id])
        audit.record(c, ctx, "event.recorded", "event", eid, {"type": body.event_type, "region": body.region, "source": body.source_uri})
        return 201, {"event_id": eid, **body.model_dump(mode="json")}
    return run(ctx, idem, body, work)


# ---------------------------------------------------------------- nowcast, scenarios, timeline

class NowcastIn(BaseModel):
    commodity_id: uuid.UUID


@app.post("/v1/forecasts/nowcast", status_code=201, tags=["forecasts"], summary="Run the nowcast and the inventory forecast, store them, and explain the revision since the previous run signal by signal")
def run_nowcast(body: NowcastIn, ctx: Ctx = Depends(auth("forecasts:run")), idem: str | None = IdemKey):
    def work(c):
        mk, w = market(c)
        check_commodity(c, body.commodity_id)
        s = st(mk, w)
        nc, sig, m, now = s["nowcast"], s["sig"], s["model"], mk["now_week"]
        prev = c.execute("SELECT value, lower, upper, asof_week, evidence FROM forecasts WHERE target = 'balance' ORDER BY asof_week DESC, asof DESC LIMIT 1").fetchone()
        inv = engine.inventory_forecast(sig, m, nc, now)
        run_id = uuid.uuid7()
        rows = [(uuid.uuid7(), ctx.tenant_id, body.commodity_id, "balance", f"period {nc['period']}", nc["value"], nc["lower"], nc["upper"], engine.VERSION, week_ts(now), now, Jsonb({"contributions": nc["contributions"], "supply": nc["supply"], "demand": nc["demand"], "last_official": nc["last_official"], "regression": nc["regression"]}), run_id)]
        rows += [(uuid.uuid7(), ctx.tenant_id, body.commodity_id, "exchange_stocks", f"{h['weeks']} weeks", h["p50"], h["p10"], h["p90"], engine.VERSION, week_ts(now), now, Jsonb({"random_walk": h["random_walk"]}), run_id) for h in inv["horizons"]]
        db.load(c, "forecasts", ["id", "tenant_id", "commodity_id", "target", "horizon", "value", "lower", "upper", "model_version", "asof", "asof_week", "evidence", "run_id"], rows)
        revision = None
        if prev:
            w0 = int(prev["asof_week"])
            f = nc["filter"]
            new_weeks = f["contrib"][w0 + 1:now + 1].sum(axis=0)
            drivers = {engine.OBS[k]: round(float(new_weeks[k]), 2) for k in range(len(engine.OBS))}
            delta = round(nc["value"] - float(prev["value"]), 2)
            drivers["re_anchoring_and_drift"] = round(delta - sum(drivers.values()), 2)
            revision = {"previous": {"asof_week": w0, "value": float(prev["value"]), "lower": float(prev["lower"]), "upper": float(prev["upper"])}, "delta_kt_week": delta, "drivers_kt_week": drivers, "new_weeks": [w0 + 1, now],
                        "official_published_since": published_periods(now, after=w0), "how": "each signal's share is the sum of its filter innovations over the new weeks; re-anchoring is what newly published official figures and the state's own drift did to the weeks before"}
        audit.record(c, ctx, "nowcast.run", "forecast", run_id, {"week": now, "balance": nc["value"], "delta": revision and revision["delta_kt_week"]})
        p = nc["period"]
        truth = float(w["balance"][p * world.PERIOD:(p + 1) * world.PERIOD].mean())
        return 201, {"run_id": run_id, "week": now, "date": str(week_date(now)), "balance": {"period": p, "value": nc["value"], "lower": nc["lower"], "upper": nc["upper"], "supply": nc["supply"], "demand": nc["demand"], "contributions": nc["contributions"], "last_official": nc["last_official"], "regression_baseline": nc["regression"]},
                     "inventory": {"now": inv["now"], "horizons": inv["horizons"]}, "revision": revision, "model_version": engine.VERSION, "stored": len(rows),
                     "simulation_truth": {"period_balance": round(truth, 2), "inside_band": bool(nc["lower"] <= truth <= nc["upper"]), "stocks_in_4_8_12_weeks": [round(float(w["stocks"][min(now + h, world.WEEKS - 1)]), 1) for h in (4, 8, 12)]}}
    return run(ctx, idem, body, work)


class ScenarioIn(BaseModel):
    commodity_id: uuid.UUID
    shocks: list[Shock] = Field(min_length=1, max_length=4)
    weeks: int = Field(12, ge=4, le=26)
    runs: int = Field(400, ge=100, le=2000)


@app.post("/v1/scenarios", status_code=201, tags=["forecasts"], summary="Baseline against a disruption: assumed production cuts and port delays propagated through the stock equation to inventory, cover and a price band from the fitted price-cover relation. Structural, with the uncertainty shown; not a price call")
def scenario(body: ScenarioIn, ctx: Ctx = Depends(auth("scenarios:run")), idem: str | None = IdemKey):
    def work(c):
        mk, w = market(c)
        check_commodity(c, body.commodity_id)
        shocks = [s.check().model_dump(exclude_none=True) for s in body.shocks]
        s = st(mk, w)
        now = mk["now_week"]
        out = engine.scenario(w, s["sig"], s["model"], s["nowcast"], now, shocks, weeks=body.weeks, runs=body.runs)
        end = body.weeks
        audit.record(c, ctx, "scenario.run", "market", mk["id"], {"shocks": [x["kind"] for x in shocks], "weeks": body.weeks})
        sim_end = min(now + end, world.WEEKS - 1)
        return 201, {"week": now, "weeks": body.weeks, "shocks": shocks, "baseline": out["baseline"], "scenario": out["scenario"], "supply_shift_kt_week": out["supply_shift"], "arrival_shift_kt_week": out["arrival_shift"], "price_relation": out["price_relation"],
                     "summary": {"stocks_end_baseline_p50": out["baseline"]["stocks_p50"][-1], "stocks_end_scenario_p50": out["scenario"]["stocks_p50"][-1], "stocks_difference": round(out["scenario"]["stocks_p50"][-1] - out["baseline"]["stocks_p50"][-1], 1), "cover_end_scenario": out["scenario"]["cover_p50"][-1],
                                 "price_end_scenario": {"p10": out["scenario"]["price_p10"][-1], "p50": out["scenario"]["price_p50"][-1], "p90": out["scenario"]["price_p90"][-1]}, "price_now": round(float(s["sig"]["price"][now]))},
                     "simulation_truth": {"what": "what the generator's own future holds for the stocks and price at the horizon, given the shocks it has been told so far; the scenario is an assumption, so these agree only if the assumption matches", "stocks_at_horizon": round(float(w["stocks"][sim_end]), 1), "price_at_horizon": round(float(w["price"][sim_end]))}}
    return run(ctx, idem, body, work)


@app.get("/v1/research/timeline", tags=["research"], summary="Events with their sources, nowcast runs with their bands, anomalies and the market's own clock, in time order")
def timeline(ctx: Ctx = Depends(auth("market:read"))):
    with db.tx(ctx.tenant_id) as c:
        mk, w = market(c)
        ev = c.execute("SELECT id, occurred_at, event_type, region, severity, source_uri, detail, recorded_at FROM events ORDER BY occurred_at").fetchall()
        fc = c.execute("SELECT asof_week, target, horizon, value, lower, upper, model_version, run_id FROM forecasts WHERE target = 'balance' ORDER BY asof_week, asof").fetchall()
        adv = c.execute("SELECT detail, at FROM audit_events WHERE action IN ('market.loaded', 'market.advanced') ORDER BY id").fetchall()
    s = st(mk, w)
    items = [{"week": e["occurred_at"], "kind": "event", **{k: e[k] for k in ("id", "event_type", "region", "severity", "source_uri", "detail", "recorded_at")}} for e in ev]
    items += [{"week": week_ts(f["asof_week"]), "kind": "nowcast", **{k: f[k] for k in ("horizon", "value", "lower", "upper", "model_version", "run_id")}} for f in fc]
    items += [{"week": week_ts(a["start_week"]), "kind": "anomaly", "signal": a["kind"], **{k: a[k] for k in ("origin", "region", "direction", "score", "start_week", "last_week")}} for a in engine.anomalies(w, s["sig"], mk["now_week"], recent=12)]
    items += [{"week": week_ts(a["detail"].get("to_week", a["detail"].get("week"))), "kind": "feed", **a["detail"]} for a in adv]
    return jsonable_encoder({"week": mk["now_week"], "items": sorted(items, key=lambda i: str(i["week"]))})
