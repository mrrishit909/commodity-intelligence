"""The signals available at a date, the fitted observation equations, a two-state Kalman nowcast of supply and demand with the
contribution of each signal, inventory forecasts, change-point detection on shipping, and the structural scenario engine."""
import numpy as np

from . import world

VERSION = "copper-nowcast-1"
S_, D_ = 0, 1
OBS = ("official_supply", "official_demand", "departures", "stock_change", "macro")


def signals(w, now):
    """Everything the desk can see at week `now`: shipping, stocks, price, spread, macro each week; official supply and demand only for
    published periods (nan otherwise). -> dict of arrays[now+1]"""
    n = now + 1
    official = np.array([world.official_available(k, now) for k in range(n)])
    return {"departures": world.departures(w["vessels"], now), "dep_by_origin": {m["port"]: world.departures(w["vessels"], now, m["port"]) for m in world.MINES.values()},
            "stocks": w["stocks"][:n], "price": w["price"][:n], "spread": w["spread"][:n], "macro": w["macro"][:n],
            "official_supply": np.where(official, w["supply"][:n], np.nan), "official_demand": np.where(official, w["demand_total"][:n], np.nan),
            "official_prod": {k: np.where(official, v[:n], np.nan) for k, v in w["prod"].items()}, "official": official}


def _ols(x, y):
    x1 = np.column_stack([np.ones(len(x)), x])
    beta, *_ = np.linalg.lstsq(x1, y, rcond=None)
    res = y - x1 @ beta
    return beta, float(res.std())


def fit(sig):
    """Observation equations and the state transition, from weeks whose official figures are published.
    departures_w = a0 + a1 S_{w-1};  stock_change_w = b0 + b1 (S_w - D_w) + b2 stocks_{w-1};  D_w = c0 + c1 macro_w.  -> dict"""
    ok = sig["official"].copy()
    ok[0] = False
    S, D, X = sig["official_supply"], sig["official_demand"], sig["stocks"]
    idx = np.where(ok)[0]
    idx = idx[idx >= 1]
    dep = sig["departures"]
    a, sa = _ols(S[idx - 1][:, None], dep[idx])
    dx = X[idx] - X[idx - 1]
    b, sb = _ols(np.column_stack([S[idx] - D[idx], X[idx - 1]]), dx)
    c, sc = _ols(sig["macro"][idx][:, None], D[idx])
    phi_s, qs = _ols(S[idx[:-1]][:, None], S[idx[1:]])
    phi_d, qd = _ols(D[idx[:-1]][:, None], D[idx[1:]])
    return {"dep": (a, sa), "dx": (b, sb), "macro": (c, sc), "s_ar": (phi_s, qs), "d_ar": (phi_d, qd), "official_sd": 1.0, "fitted_on": int(len(idx)),
            "report": {"departures_per_kt_supply": round(float(a[1]), 3), "stock_share_of_balance": round(float(b[1]), 3), "demand_per_macro_unit": round(float(c[1]), 1), "weeks_used": int(len(idx))}}


def filter(sig, m, now):
    """Kalman filter over weeks 1..now with the information set of `now`: official figures where published, proxies elsewhere.
    -> dict with filtered means x[now+1, 2], variances P[now+1, 2, 2], and per-week contribution of each signal to (S - D)."""
    n = now + 1
    (a, sa), (b, sb), (c, sc), (ps, qs), (pd, qd) = m["dep"], m["dx"], m["macro"], m["s_ar"], m["d_ar"]
    F = np.diag([ps[1], pd[1]])
    const = np.array([ps[0], pd[0]])
    Q = np.diag([qs ** 2, qd ** 2])
    x = np.zeros((n, 2))
    P = np.zeros((n, 2, 2))
    contrib = np.zeros((n, len(OBS)))
    S0 = np.nanmean(sig["official_supply"])
    D0 = np.nanmean(sig["official_demand"])
    x[0], P[0] = [S0, D0], np.diag([25.0, 25.0])
    for w in range(1, n):
        xp = const + F @ x[w - 1]
        Pp = F @ P[w - 1] @ F.T + Q
        obs = []                                                             # (H row, z, r, obs index)
        if not np.isnan(sig["official_supply"][w]):
            obs.append((np.array([1.0, 0.0]), sig["official_supply"][w], m["official_sd"] ** 2, 0))
            obs.append((np.array([0.0, 1.0]), sig["official_demand"][w], m["official_sd"] ** 2, 1))
        else:
            obs.append((np.array([a[1], 0.0]), sig["departures"][w] - a[0], (sa * 1.3) ** 2, 2))             # last week's supply loaded this week: a lag we accept
            obs.append((np.array([b[1], -b[1]]), sig["stocks"][w] - sig["stocks"][w - 1] - b[0] - b[2] * sig["stocks"][w - 1], sb ** 2, 3))
            obs.append((np.array([0.0, 1.0]), c[0] + c[1] * sig["macro"][w], sc ** 2, 4))
        for H, z, r, k in obs:
            y = z - H @ xp
            s = H @ Pp @ H + r
            K = Pp @ H / s
            xp = xp + K * y
            Pp = (np.eye(2) - np.outer(K, H)) @ Pp
            contrib[w, k] = (K[S_] - K[D_]) * y
        x[w], P[w] = xp, Pp
    return {"x": x, "P": P, "contrib": contrib}


def nowcast(sig, m, now):
    """The balance (supply - demand) for the current four-week period, from the filter: weeks seen so far plus the state projected
    over the weeks still to come. -> dict with value, band, per-signal contributions, and the baselines."""
    f = filter(sig, m, now)
    p = world.period_of(now)
    weeks = list(range(p * world.PERIOD, (p + 1) * world.PERIOD))
    F = np.diag([m["s_ar"][0][1], m["d_ar"][0][1]])
    const = np.array([m["s_ar"][0][0], m["d_ar"][0][0]])
    Q = np.diag([m["s_ar"][1] ** 2, m["d_ar"][1] ** 2])
    vals, vars_ = [], []
    x, P = f["x"][now], f["P"][now]
    for wk in weeks:
        if wk <= now:
            xx, PP = f["x"][wk], f["P"][wk]
        else:
            x, P = const + F @ x, F @ P @ F.T + Q
            xx, PP = x, P
        vals.append(xx[S_] - xx[D_])
        vars_.append(PP[0, 0] + PP[1, 1] - 2 * PP[0, 1])
    value, sd = float(np.mean(vals)), float(np.sqrt(np.mean(vars_)))
    # baselines: the last published period's balance; a period-level regression of balance on the three proxies
    pub = [k for k in range(now + 1) if sig["official"][k]]
    last_pub_period = world.period_of(pub[-1]) if pub else None
    last_official = float(np.mean((sig["official_supply"] - sig["official_demand"])[last_pub_period * world.PERIOD:(last_pub_period + 1) * world.PERIOD])) if last_pub_period is not None else None
    reg = regression_nowcast(sig, now)
    contrib_now = f["contrib"][weeks[0]:now + 1].sum(axis=0)
    return {"period": p, "weeks": [weeks[0], weeks[-1]], "weeks_seen": sum(wk <= now for wk in weeks), "value": round(value, 2), "sd": round(sd, 2), "lower": round(value - 1.28 * sd, 2), "upper": round(value + 1.28 * sd, 2),
            "supply": round(float(np.mean([f["x"][min(wk, now)][S_] for wk in weeks])), 2), "demand": round(float(np.mean([f["x"][min(wk, now)][D_] for wk in weeks])), 2),
            "contributions": {OBS[k]: round(float(contrib_now[k]), 2) for k in range(len(OBS))}, "last_official": {"period": last_pub_period, "value": round(last_official, 2) if last_official is not None else None, "weeks_old": int(now - ((last_pub_period + 1) * world.PERIOD)) if last_pub_period is not None else None},
            "regression": reg, "filter": f}


def regression_nowcast(sig, now):
    """Baseline: period balance regressed on the period's departures, stock change and mean macro, over published periods. -> float | None"""
    rows, ys = [], []
    for p in range(1, world.period_of(now)):
        a, b = p * world.PERIOD, (p + 1) * world.PERIOD
        if b - 1 <= now and sig["official"][b - 1]:
            rows.append([sig["departures"][a:b].sum(), sig["stocks"][b - 1] - sig["stocks"][a - 1], sig["macro"][a:b].mean()])
            ys.append((sig["official_supply"][a:b] - sig["official_demand"][a:b]).mean())
    if len(rows) < 12:
        return None
    beta, _ = _ols(np.array(rows), np.array(ys))
    p = world.period_of(now)
    a, b = p * world.PERIOD, min(now + 1, (p + 1) * world.PERIOD)
    scale = world.PERIOD / (b - a)                                                     # scale partial-period sums up
    xnow = np.array([sig["departures"][a:b].sum() * scale, (sig["stocks"][b - 1] - sig["stocks"][a - 1]) * scale, sig["macro"][a:b].mean()])
    return round(float(beta[0] + beta[1:] @ xnow), 2)


def inventory_forecast(sig, m, nc, now, horizons=(4, 8, 12), runs=400, seed=0, supply_shift=None, arrival_shift=None):
    """Exchange stocks h weeks ahead: the filtered state projected, the stock equation applied, noise from the fitted residuals.
    `supply_shift[h]` (kt a week) and `arrival_shift[h]` (kt arriving late or catching up) alter the paths for scenarios. -> dict"""
    r = np.random.default_rng(seed)
    f = nc["filter"]
    F = np.diag([m["s_ar"][0][1], m["d_ar"][0][1]])
    const = np.array([m["s_ar"][0][0], m["d_ar"][0][0]])
    Q = np.diag([m["s_ar"][1] ** 2, m["d_ar"][1] ** 2])
    b, sb = m["dx"]
    H = max(horizons)
    x0, P0 = f["x"][now], f["P"][now]
    paths = np.zeros((runs, H + 1))
    paths[:, 0] = sig["stocks"][now]
    for k in range(runs):
        x = r.multivariate_normal(x0, P0)
        for h in range(1, H + 1):
            x = const + F @ x + r.multivariate_normal([0, 0], Q)
            bal = x[S_] - x[D_] + (supply_shift[h - 1] if supply_shift is not None else 0.0) + (arrival_shift[h - 1] if arrival_shift is not None else 0.0)
            paths[k, h] = max(50.0, paths[k, h - 1] + b[0] + b[1] * bal + b[2] * paths[k, h - 1] + r.normal(0, sb))
    q = lambda h, p: round(float(np.quantile(paths[:, h], p)), 1)              # noqa: E731
    return {"now": round(float(sig["stocks"][now]), 1), "horizons": [{"weeks": h, "p10": q(h, 0.1), "p50": q(h, 0.5), "p90": q(h, 0.9), "random_walk": round(float(sig["stocks"][now]), 1)} for h in horizons], "paths": paths}


def cusum(series, k=0.75, h=6.0, train=52, skip=8):
    """Two-sided CUSUM on a series standardised by its trailing window (ending `skip` weeks before each point, so a drift does not
    hide itself). -> list of ongoing/closed runs {start, end, direction, z}"""
    n = len(series)
    s_pos = s_neg = 0.0
    start_pos = start_neg = None
    runs = []
    for t in range(train + skip, n):
        ref = series[t - train - skip:t - skip]
        ref = ref[~np.isnan(ref)]
        if len(ref) < 20 or np.isnan(series[t]):
            continue
        z = (series[t] - ref.mean()) / max(ref.std(), 1e-6)
        s_pos = max(0.0, s_pos + z - k)
        s_neg = max(0.0, s_neg - z - k)
        start_pos = t if s_pos > 0 and start_pos is None else (None if s_pos == 0 else start_pos)
        start_neg = t if s_neg > 0 and start_neg is None else (None if s_neg == 0 else start_neg)
        if s_pos > h:
            runs.append({"start": start_pos, "end": t, "direction": "up", "z": round(float(s_pos), 1)})
            s_pos, start_pos = 0.0, None
        if s_neg > h:
            runs.append({"start": start_neg, "end": t, "direction": "down", "z": round(float(s_neg), 1)})
            s_neg, start_neg = 0.0, None
    # merge consecutive alarms in the same direction into one episode
    out = []
    for a in runs:
        if out and out[-1]["direction"] == a["direction"] and a["start"] - out[-1]["end"] <= 2:
            out[-1]["end"], out[-1]["z"] = a["end"], max(out[-1]["z"], a["z"])
        else:
            out.append(dict(a))
    return out


def anomalies(w, sig, now, recent=6):
    """Change points in departures by origin and in overdue cargo by origin, reported when the alarm ended within `recent` weeks. -> list"""
    out = []
    for name, m in world.MINES.items():
        port = m["port"]
        dep = sig["dep_by_origin"][port]
        for a in cusum(dep):
            if a["end"] >= now - recent:
                ref = dep[a["start"] - 60:a["start"] - 8].mean()
                out.append({"kind": "departures", "origin": port, "region": name, "direction": a["direction"], "start_week": a["start"], "last_week": a["end"], "score": a["z"], "weekly_level": round(float(dep[a["start"]:a["end"] + 1].mean()), 1), "normal_level": round(float(ref), 1), "change_kt_week": round(float(dep[a["start"]:a["end"] + 1].mean() - ref), 1)})
        od = world.overdue(w["vessels"], now, port)
        for a in cusum(od, k=1.0, h=8.0):
            if a["end"] >= now - recent and a["direction"] == "up":
                ref = od[a["start"] - 60:a["start"] - 8].mean()
                out.append({"kind": "overdue_cargo", "origin": port, "region": name, "direction": "up", "start_week": a["start"], "last_week": a["end"], "score": a["z"], "weekly_level": round(float(od[a["start"]:a["end"] + 1].mean()), 1), "normal_level": round(float(ref), 1), "change_kt_week": round(float(od[a["start"]:a["end"] + 1].mean() - ref), 1)})
    return sorted(out, key=lambda a: -a["score"])


def price_relation(sig):
    """The structural relation the scenario engine uses: log price on weeks of cover, fitted on history. -> (slope, intercept, residual sd)"""
    cover = sig["stocks"] / np.nan_to_num(sig["official_demand"], nan=np.nanmean(sig["official_demand"]))
    beta, sd = _ols(cover[:, None], np.log(sig["price"]))
    return float(beta[1]), float(beta[0]), sd


def scenario(w, sig, m, nc, now, shocks, weeks=12, runs=400, seed=0):
    """Structural propagation of assumed shocks from `now`: production cuts lower the balance for their weeks; port congestion holds
    cargoes at sea, so metal arrives later (a dip, then a catch-up). Inventory paths with bands, cover, and a price band from the
    fitted price-cover relation, which is a structural claim, not a forecast of the market. -> dict"""
    supply_shift, arrival_shift = np.zeros(weeks), np.zeros(weeks)
    for s in shocks:
        if s["kind"] == "outage":
            cap = world.MINES[s["region"]]["capacity"]
            recent = float(sig["dep_by_origin"][world.MINES[s["region"]]["port"]][now - 8:now + 1].mean() / m["dep"][0][1]) if m["dep"][0][1] else cap * 0.9
            level = min(cap, recent)
            supply_shift[s["start"]:s["start"] + s["weeks"]] -= level * s["cut"]
        else:
            port, e, L = s["port"], s["extra_weeks"], s["weeks"]
            flow = float(sig["dep_by_origin"][port][now - 8:now + 1].mean())
            T = int(round(np.mean([v for (p_, _), v in world.TRANSIT.items() if p_ == port])))
            for k in range(s["start"] + T, min(weeks, s["start"] + T + e)):
                arrival_shift[k] -= flow                                                # cargoes that would have landed now are still at sea
            for k in range(s["start"] + L + T, min(weeks, s["start"] + L + T + e)):
                arrival_shift[k] += flow                                                # and land once the port clears
    base = inventory_forecast(sig, m, nc, now, horizons=tuple(range(1, weeks + 1)), runs=runs, seed=seed)
    alt = inventory_forecast(sig, m, nc, now, horizons=tuple(range(1, weeks + 1)), runs=runs, seed=seed, supply_shift=supply_shift, arrival_shift=arrival_shift)
    slope, intercept, sd = price_relation(sig)
    demand = nc["demand"]
    def path(fc):
        p10, p50, p90 = [np.quantile(fc["paths"], q, axis=0) for q in (0.1, 0.5, 0.9)]
        cover = p50 / demand
        lp = np.log(sig["price"][now]) + slope * (cover - cover[0])
        return {"stocks_p10": p10.round(1).tolist(), "stocks_p50": p50.round(1).tolist(), "stocks_p90": p90.round(1).tolist(), "cover_p50": cover.round(2).tolist(),
                "price_p50": np.exp(lp).round(0).tolist(), "price_p10": np.exp(lp - 1.28 * sd).round(0).tolist(), "price_p90": np.exp(lp + 1.28 * sd).round(0).tolist()}
    return {"weeks": weeks, "baseline": path(base), "scenario": path(alt), "supply_shift": supply_shift.round(1).tolist(), "arrival_shift": arrival_shift.round(1).tolist(),
            "price_relation": {"log_price_per_week_of_cover": round(slope, 3), "residual_sd": round(sd, 3), "what": "fitted on this market's history; the band is the residual of that fit, which is wide, and the relation is structural, not a trading call"}}
