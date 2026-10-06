# Evaluation

Everything here is measured on synthetic copper markets generated with seeds the demo does not use (`(101, 102, 103)`), over 32 four-week
periods each, with the nowcast made on the last week of every period using only what was published by then. Reproduce with
`python -m cm.evaluate` (about a minute). The numbers say how well the methods do against the market as the simulator plays it. They
are not a claim about real copper: the simulator's relations are simple and stable, real data arrives revised and ragged, and nothing
here is trading advice.

## 1. The balance nowcast

Error in kt a week of the current period's supply-demand balance. "Last official" is the most recent published figure, 8 to 11
weeks old; "regression" is a period-level regression of the balance on the same three signals; the nowcast is the two-state Kalman filter.

| market (seed) | periods | nowcast | regression | last official | truth inside 80% band |
|---|---|---|---|---|---|
| 101 | 32 | 2.3 | 2.67 | 11.79 | 91% |
| 102 | 32 | 2.93 | 4.58 | 28.64 | 91% |
| 103 | 32 | 2.88 | 4.32 | 16.15 | 84% |

The nowcast cuts the error of the last official figure by about 86% and beats the plain regression, because it weights
each signal by its fitted noise and carries the state across weeks. The band is a little wide for an 80% band.

## 2. Exchange stocks ahead

Error in kt of the P50 against a random walk (stocks stay where they are), and the P10–P90 coverage.

| market (seed) | weeks ahead | model | random walk | truth inside P10–P90 |
|---|---|---|---|---|
| 101 | 4 | 15.1 | 17.7 | 81% |
| 101 | 8 | 28.5 | 33.5 | 81% |
| 101 | 12 | 38.9 | 44.8 | 84% |
| 102 | 4 | 21.0 | 33.1 | 75% |
| 102 | 8 | 47.1 | 60.9 | 75% |
| 102 | 12 | 70.6 | 82.9 | 62% |
| 103 | 4 | 18.8 | 23.9 | 88% |
| 103 | 8 | 35.7 | 41.5 | 81% |
| 103 | 12 | 47.3 | 52.5 | 84% |

The gain over a random walk is small and grows with the horizon; most of what moves stocks over
a few weeks is noise the model is honest about, which is why the bands are as wide as they are.

## 3. Change points on shipping

Outages in each market's six years of history, how many the CUSUM on departures caught, how long it took, and alarms with no outage
behind them. The overdue-cargo detector's false alarms are counted separately (no congestion is generated in the history, so every
alarm there is false).

| market (seed) | outages | caught | mean delay (weeks) | false alarms, departures | false alarms, overdue cargo |
|---|---|---|---|---|---|
| 101 | 9 | 5 | 5.0 | 3 | 1 |
| 102 | 11 | 9 | 6.6 | 2 | 0 |
| 103 | 12 | 10 | 6.9 | 0 | 3 |

Three outages in four are caught, five to seven weeks after they start, with a false alarm every year or two per port. Weekly departures are noisy
(one to three cargoes a route, each ±15%), and a 20% cut at a small mine is inside that noise. The thresholds are two named constants.

## 4. What the price relation is not

Whether "the nowcast says deficit" predicted the price being higher twelve weeks later:

| market (seed) | periods | direction right |
|---|---|---|
| 101 | 32 | 47% |
| 102 | 32 | 34% |
| 103 | 32 | 53% |

No better than a coin toss, and worse in one market. The fitted price-cover relation explains the level of the price given stocks, not where the price goes next, and the
scenario engine uses it only to translate an assumed stock path into a band. The page says this in as many words.

## 5. What is not measured

- Events are recorded by a person with a source URI; there is no text extraction to evaluate.
- The scenario engine propagates assumptions; its accuracy is the assumption's.
- Real connectors, revisions to official data, and more than one commodity.
