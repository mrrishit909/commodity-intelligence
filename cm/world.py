"""An invented copper market, week by week: three mining regions ship concentrate and cathode through their ports to three
import regions; demand follows a slow macro cycle; the balance of supply over demand moves visible exchange stocks, and stocks move
the price and the futures spread. Official production and demand figures arrive two months late; shipping, stocks and prices are
seen at once. Everything is a pure function of a seed. Nothing here is a real market."""
import functools

import numpy as np

WEEKS = 312                      # six years of history
NOW = 288                        # the demo's "today": week 288, with 24 weeks of future kept for scoring
LAG = 8                          # official production and demand arrive 8 weeks after the 4-week period ends
PERIOD = 4                       # a "month" is four weeks
MINES = {"Chile": {"capacity": 122.0, "port": "Antofagasta"}, "Peru": {"capacity": 56.0, "port": "Callao"}, "DRC": {"capacity": 52.0, "port": "Durban"}}       # kt a week
IMPORTERS = {"China": 0.56, "Europe": 0.26, "US": 0.18}                                       # demand shares
TRANSIT = {("Antofagasta", "China"): 5, ("Antofagasta", "Europe"): 4, ("Antofagasta", "US"): 3, ("Callao", "China"): 5, ("Callao", "Europe"): 4, ("Callao", "US"): 2, ("Durban", "China"): 4, ("Durban", "Europe"): 3, ("Durban", "US"): 4}   # weeks
SHIPPED = 0.85                   # share of production that moves by sea (the rest is rail or smelted locally)
STOCK_SHARE = 0.5                # share of the balance that shows up in exchange warehouses; the rest is invisible
OFF_WARRANT = 0.03               # weekly reversion of exchange stocks towards normal through off-warrant flows
PRICE_SLOPE = 0.09               # log price per week of cover below normal (4 weeks); the structural relation the scenario engine uses
SPREAD_SLOPE = 12.0              # $/t of 3-month contango per week of cover above normal
DEMO = {"outage": {"region": "Chile", "start": NOW + 1, "weeks": 10, "cut": 0.35}, "congestion": {"port": "Callao", "start": NOW + 1, "weeks": 8, "extra_weeks": 2}}


@functools.lru_cache(maxsize=8)
def build(seed=17, shocks=None):
    """-> dict of weekly arrays and the vessel list. `shocks` is a tuple of (kind, region/port, start, weeks, size) overriding the random ones from NOW on."""
    r = np.random.default_rng(seed)
    W = WEEKS
    t = np.arange(W)
    macro = np.zeros(W)
    for w in range(1, W):
        macro[w] = 0.97 * macro[w - 1] + r.normal(0, 0.012)
    base_demand = 200.0 * (1 + 0.004 * t / 52)                                       # kt a week, slow growth
    season = 1 + 0.04 * np.sin(2 * np.pi * (t % 52) / 52 - 0.6)
    demand_total = base_demand * season * (1 + 1.2 * macro) + r.normal(0, 2.0, W)
    demand = {k: demand_total * s * (1 + r.normal(0, 0.01, W)) for k, s in IMPORTERS.items()}
    # mines: utilisation around 0.9 with its own slow wobble, plus random outages (2% a week to start one) before NOW
    prod, util, outages = {}, {}, []
    for name, m in MINES.items():
        u = np.full(W, 0.9)
        for w in range(1, W):
            u[w] = 0.9 + 0.85 * (u[w - 1] - 0.9) + r.normal(0, 0.012)
        cut = np.zeros(W)
        w = 20
        while w < NOW - 12:
            if r.random() < 0.02:
                length, size = int(r.integers(4, 11)), float(r.uniform(0.2, 0.5))
                cut[w:w + length] = size
                outages.append({"region": name, "start": w, "weeks": length, "cut": round(size, 2)})
                w += length + 8
            w += 1
        util[name], prod[name] = u, m["capacity"] * u * (1 - cut) + r.normal(0, 0.8, W)
    congestion = {p: np.zeros(W, dtype=int) for p in {m["port"] for m in MINES.values()}}
    # the future: random outages continue unless shocks are given for the demo
    for kind, where, start, weeks, size in (shocks or ()):
        if kind == "outage":
            prod[where][start:start + weeks] *= (1 - size)
            outages.append({"region": where, "start": start, "weeks": weeks, "cut": size})
        else:
            congestion[where][start:start + weeks] = int(size)
    supply = sum(prod.values())
    balance = supply - demand_total
    stocks = np.zeros(W)
    stocks[0] = 820.0
    for w in range(1, W):
        stocks[w] = max(50.0, stocks[w - 1] + STOCK_SHARE * balance[w] - OFF_WARRANT * (stocks[w - 1] - 820.0) + r.normal(0, 3.0))      # off-warrant metal flows back when warehouses run high
    cover = stocks / demand_total                                                     # weeks of consumption in warehouses
    noise = np.zeros(W)
    for w in range(1, W):
        noise[w] = 0.92 * noise[w - 1] + r.normal(0, 0.02)                            # what the fundamentals do not explain
    price = np.exp(np.log(8600.0) - PRICE_SLOPE * (cover - 4.0) + noise)
    spread = SPREAD_SLOPE * (cover - 4.0) + r.normal(0, 6.0, W)                       # 3-month minus cash, $/t
    # vessels: each week each mine ships its sea share across the importers, in 1-3 cargoes per route
    vessels = []
    for name, m in MINES.items():
        port = m["port"]
        for w in range(W):
            qty_week = prod[name][max(0, w - 1)] * SHIPPED                             # loads what was produced last week
            for imp, share in IMPORTERS.items():
                q = qty_week * share
                n = int(r.integers(1, 4))
                for k in range(n):
                    transit = TRANSIT[(port, imp)] + congestion[port][w] + (1 if r.random() < 0.1 else 0)
                    vessels.append({"vessel_id": f"{port[:3].upper()}-{w:03d}-{imp[:2].upper()}{k}", "origin": port, "region": name, "destination": imp, "departed": w, "planned_eta": w + TRANSIT[(port, imp)], "eta": w + transit, "qty": round(float(q / n * r.uniform(0.85, 1.15)), 2)})
    return {"seed": seed, "macro": macro, "demand": demand, "demand_total": demand_total, "prod": prod, "util": util, "supply": supply, "balance": balance, "stocks": stocks, "cover": cover, "price": price, "spread": spread,
            "vessels": vessels, "outages": outages, "congestion": congestion, "shocks": shocks}


def demo_shocks():
    o, c = DEMO["outage"], DEMO["congestion"]
    return (("outage", o["region"], o["start"], o["weeks"], o["cut"]), ("congestion", c["port"], c["start"], c["weeks"], c["extra_weeks"]))


def period_of(w):
    return w // PERIOD


def official_available(w, now):
    """Is the official figure for week w's period published by `now`?"""
    return (period_of(w) + 1) * PERIOD + LAG <= now


def departures(vessels, upto, origin=None):
    """Weekly tonnage departed, as AIS would show it, up to and including week `upto`. -> array[upto+1]"""
    out = np.zeros(upto + 1)
    for v in vessels:
        if v["departed"] <= upto and (origin is None or v["origin"] == origin):
            out[v["departed"]] += v["qty"]
    return out


def overdue(vessels, upto, origin):
    """Tonnage past its planned arrival and not yet arrived, week by week, as a desk watching AIS would count it. -> array[upto+1]"""
    out = np.zeros(upto + 1)
    for v in vessels:
        if v["origin"] == origin and v["eta"] > v["planned_eta"]:
            out[v["planned_eta"]:min(v["eta"], upto + 1)] += v["qty"]
    return out
