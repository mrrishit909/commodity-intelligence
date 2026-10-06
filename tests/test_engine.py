"""Unit tests for the simulator, the signals, the Kalman nowcast, change-point detection and the scenario engine: no database."""
import numpy as np

from cm import engine, world

W = world.build()
NOW = world.NOW


def test_world_is_deterministic_and_plausible():
    assert world.build()["balance"] is W["balance"] and len(W["vessels"]) > 4000 and len(W["outages"]) >= 5
    assert 3 < W["cover"].mean() < 7 and 3000 < W["price"].min() and W["price"].max() < 20000 and abs(W["balance"].mean()) < 10
    d = world.build(17, world.demo_shocks())
    assert (d["prod"]["Chile"][NOW + 1:NOW + 11] < 0.75 * W["prod"]["Chile"][NOW + 1:NOW + 11]).all() and (d["prod"]["Chile"][:NOW + 1] == W["prod"]["Chile"][:NOW + 1]).all()
    v = [x for x in d["vessels"] if x["origin"] == "Callao" and NOW + 1 <= x["departed"] < NOW + 9]
    assert all(x["eta"] - x["planned_eta"] >= 2 for x in v) and len(v) > 20


def test_signals_respect_the_publication_lag():
    sig = engine.signals(W, NOW)
    assert len(sig["stocks"]) == NOW + 1 and np.isnan(sig["official_supply"][NOW]) and not np.isnan(sig["official_supply"][NOW - 20])
    last_pub = max(k for k in range(NOW + 1) if sig["official"][k])
    assert world.official_available(last_pub, NOW) and not world.official_available(last_pub + 1, NOW) and NOW - last_pub >= world.LAG
    dep = world.departures(W["vessels"], NOW)
    assert dep.shape == (NOW + 1,) and abs(dep[100:200].mean() / W["supply"][99:199].mean() - world.SHIPPED) < 0.05


def test_nowcast_beats_the_last_official_figure_and_the_band_holds():
    err = {"kf": [], "last": [], "reg": [], "cov": []}
    for p in range(52, 72):
        now = p * world.PERIOD + 3
        sig = engine.signals(W, now)
        nc = engine.nowcast(sig, engine.fit(sig), now)
        truth = W["balance"][p * world.PERIOD:(p + 1) * world.PERIOD].mean()
        err["kf"].append(abs(nc["value"] - truth))
        err["last"].append(abs(nc["last_official"]["value"] - truth))
        err["reg"].append(abs(nc["regression"] - truth))
        err["cov"].append(nc["lower"] <= truth <= nc["upper"])
    assert np.mean(err["kf"]) < np.mean(err["reg"]) < np.mean(err["last"]) and 0.6 <= np.mean(err["cov"]) <= 1.0
    sig = engine.signals(W, NOW)
    nc = engine.nowcast(sig, engine.fit(sig), NOW)
    assert nc["lower"] < nc["value"] < nc["upper"] and abs(sum(nc["contributions"].values())) < 50 and nc["weeks_seen"] == 1


def test_cusum_finds_a_step_and_is_quiet_on_noise():
    r = np.random.default_rng(0)
    quiet = r.normal(100, 5, 300)
    assert engine.cusum(quiet) == []
    step = quiet.copy()
    step[200:] -= 30
    runs = engine.cusum(step)
    assert runs and runs[0]["direction"] == "down" and 198 <= runs[0]["start"] <= 203 and runs[0]["end"] >= 230     # the episode runs as long as the shift does


def test_anomalies_name_the_demo_outage_and_congestion():
    d = world.build(17, world.demo_shocks())
    a = engine.anomalies(d, engine.signals(d, NOW + 8), NOW + 8)
    kinds = {(x["kind"], x["origin"], x["direction"]) for x in a}
    assert ("departures", "Antofagasta", "down") in kinds and ("overdue_cargo", "Callao", "up") in kinds
    dep = next(x for x in a if x["kind"] == "departures")
    assert dep["change_kt_week"] < -20 and NOW - 2 <= dep["start_week"] <= NOW + 4


def test_scenario_moves_stocks_the_right_way_and_keeps_the_baseline():
    sig = engine.signals(W, NOW)
    m = engine.fit(sig)
    nc = engine.nowcast(sig, m, NOW)
    out = engine.scenario(W, sig, m, nc, NOW, [{"kind": "outage", "region": "Chile", "start": 1, "weeks": 8, "cut": 0.35}, {"kind": "congestion", "port": "Callao", "start": 1, "weeks": 4, "extra_weeks": 2}], weeks=12, runs=200)
    b, s = out["baseline"], out["scenario"]
    assert s["stocks_p50"][-1] < b["stocks_p50"][-1] - 50 and s["price_p50"][-1] > b["price_p50"][-1] and s["stocks_p10"][-1] < s["stocks_p50"][-1] < s["stocks_p90"][-1]
    assert out["supply_shift"][1] < -25 and out["supply_shift"][0] == 0 and abs(sum(out["arrival_shift"])) < 1e-6      # delayed cargo lands later; nothing is lost
    inv = engine.inventory_forecast(sig, m, nc, NOW, runs=200)
    assert [h["weeks"] for h in inv["horizons"]] == [4, 8, 12] and all(h["p10"] <= h["p50"] <= h["p90"] for h in inv["horizons"])
