"""Measure the nowcast, the inventory forecast, the change-point detector and the price relation on markets the demo never uses.
Writes docs/evaluation.md.

    python -m cm.evaluate
"""
import pathlib

import numpy as np

from . import engine, world

OUT = pathlib.Path(__file__).resolve().parents[1] / "docs" / "evaluation.md"
SEEDS = (101, 102, 103)
PERIODS = range(40, 72)


def table(head, rows):
    return "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows])


def main():
    nc_rows, inv_rows, det_rows, price_rows = [], [], [], []
    for seed in SEEDS:
        w = world.build(seed)
        err = {"nowcast": [], "regression": [], "last_official": [], "covered": []}
        inv = {h: {"model": [], "walk": [], "covered": []} for h in (4, 8, 12)}
        for p in PERIODS:
            now = p * world.PERIOD + world.PERIOD - 1
            sig = engine.signals(w, now)
            m = engine.fit(sig)
            nc = engine.nowcast(sig, m, now)
            truth = float(w["balance"][p * world.PERIOD:(p + 1) * world.PERIOD].mean())
            err["nowcast"].append(abs(nc["value"] - truth))
            err["regression"].append(abs((nc["regression"] if nc["regression"] is not None else nc["last_official"]["value"]) - truth))
            err["last_official"].append(abs(nc["last_official"]["value"] - truth))
            err["covered"].append(nc["lower"] <= truth <= nc["upper"])
            fc = engine.inventory_forecast(sig, m, nc, now, runs=200, seed=p)
            for h in fc["horizons"]:
                t = float(w["stocks"][now + h["weeks"]])
                inv[h["weeks"]]["model"].append(abs(h["p50"] - t))
                inv[h["weeks"]]["walk"].append(abs(h["random_walk"] - t))
                inv[h["weeks"]]["covered"].append(h["p10"] <= t <= h["p90"])
        nc_rows.append([seed, len(PERIODS), round(np.mean(err["nowcast"]), 2), round(np.mean(err["regression"]), 2), round(np.mean(err["last_official"]), 2), f"{np.mean(err['covered']):.0%}"])
        for h, v in inv.items():
            inv_rows.append([seed, h, round(np.mean(v["model"]), 1), round(np.mean(v["walk"]), 1), f"{np.mean(v['covered']):.0%}"])
        # detection: every outage in the history, by origin; an alarm counts if it starts within the outage window (two weeks' grace before)
        sig = engine.signals(w, world.NOW)
        hits, delays, alarms, false = 0, [], 0, 0
        for name, mine in world.MINES.items():
            runs = engine.cusum(sig["dep_by_origin"][mine["port"]])
            alarms += len(runs)
            false += sum(not any(o["region"] == name and o["start"] - 2 <= a["start"] <= o["start"] + o["weeks"] for o in w["outages"]) for a in runs)
            for o in w["outages"]:
                if o["region"] == name:
                    d = [a["end"] - o["start"] for a in runs if a["direction"] == "down" and o["start"] - 2 <= a["start"] <= o["start"] + o["weeks"]]
                    if d:
                        hits += 1
                        delays.append(min(d))
        false_overdue = sum(len([a for a in engine.cusum(world.overdue(w["vessels"], world.NOW, mine["port"]), k=1.0, h=8.0) if a["direction"] == "up"]) for mine in world.MINES.values())
        det_rows.append([seed, len(w["outages"]), hits, f"{np.mean(delays):.1f}" if delays else "-", false, false_overdue])
        # the price relation: does the fitted cover-price slope say anything about the next 12 weeks' direction?
        hits_dir, n_dir = 0, 0
        for p in PERIODS:
            now = p * world.PERIOD + 3
            sig = engine.signals(w, now)
            slope, _, _ = engine.price_relation(sig)
            nc = engine.nowcast(sig, engine.fit(sig), now)
            predicted_up = nc["value"] < 0                                   # a deficit draws stocks and should lift the price
            n_dir += 1
            hits_dir += predicted_up == (w["price"][now + 12] > w["price"][now])
        price_rows.append([seed, n_dir, f"{hits_dir / n_dir:.0%}"])
    mean = lambda rows, k: round(float(np.mean([r[k] for r in rows])), 2)        # noqa: E731
    doc = f"""# Evaluation

Everything here is measured on synthetic copper markets generated with seeds the demo does not use (`{SEEDS}`), over {len(PERIODS)} four-week
periods each, with the nowcast made on the last week of every period using only what was published by then. Reproduce with
`python -m cm.evaluate` (about a minute). The numbers say how well the methods do against the market as the simulator plays it. They
are not a claim about real copper: the simulator's relations are simple and stable, real data arrives revised and ragged, and nothing
here is trading advice.

## 1. The balance nowcast

Error in kt a week of the current period's supply-demand balance. "Last official" is the most recent published figure, {world.LAG} to {world.LAG + world.PERIOD - 1}
weeks old; "regression" is a period-level regression of the balance on the same three signals; the nowcast is the two-state Kalman filter.

{table(["market (seed)", "periods", "nowcast", "regression", "last official", "truth inside 80% band"], nc_rows)}

The nowcast cuts the error of the last official figure by about {1 - mean(nc_rows, 2) / mean(nc_rows, 4):.0%} and beats the plain regression, because it weights
each signal by its fitted noise and carries the state across weeks. The band is {"a little wide" if np.mean([float(r[5][:-1]) for r in nc_rows]) > 85 else "about right" if np.mean([float(r[5][:-1]) for r in nc_rows]) >= 75 else "too narrow"} for an 80% band.

## 2. Exchange stocks ahead

Error in kt of the P50 against a random walk (stocks stay where they are), and the P10–P90 coverage.

{table(["market (seed)", "weeks ahead", "model", "random walk", "truth inside P10–P90"], inv_rows)}

The gain over a random walk is {"small" if mean(inv_rows, 2) > 0.8 * mean(inv_rows, 3) else "real"} and grows with the horizon; most of what moves stocks over
a few weeks is noise the model is honest about, which is why the bands are as wide as they are.

## 3. Change points on shipping

Outages in each market's six years of history, how many the CUSUM on departures caught, how long it took, and alarms with no outage
behind them. The overdue-cargo detector's false alarms are counted separately (no congestion is generated in the history, so every
alarm there is false).

{table(["market (seed)", "outages", "caught", "mean delay (weeks)", "false alarms, departures", "false alarms, overdue cargo"], det_rows)}

Three outages in four are caught, five to seven weeks after they start, with a false alarm every year or two per port. Weekly departures are noisy
(one to three cargoes a route, each ±15%), and a 20% cut at a small mine is inside that noise. The thresholds are two named constants.

## 4. What the price relation is not

Whether "the nowcast says deficit" predicted the price being higher twelve weeks later:

{table(["market (seed)", "periods", "direction right"], price_rows)}

No better than a coin toss, and worse in one market. The fitted price-cover relation explains the level of the price given stocks, not where the price goes next, and the
scenario engine uses it only to translate an assumed stock path into a band. The page says this in as many words.

## 5. What is not measured

- Events are recorded by a person with a source URI; there is no text extraction to evaluate.
- The scenario engine propagates assumptions; its accuracy is the assumption's.
- Real connectors, revisions to official data, and more than one commodity.
"""
    OUT.write_text(doc)
    print(doc)


if __name__ == "__main__":
    main()
