# Pick record

Written by `gridiron export`. Every figure below is measured from the database, not derived.

- predictions: 458 (278 graded, 180 awaiting a result)
- teams rated: 308
- ratings computed at: 2026-09-26T08:17:32Z
- last graded at: 2026-09-28T00:15:47Z

Source of truth is `data/gridiron.db`, which is not tracked. These files
exist so the record survives losing it — predictions lock at kickoff and
cannot be regenerated.

## Accuracy

```
Graded picks: 221-57  (79.5%)  model elo-1.0

By league
  CFB   191-41  (82.3%)
  NFL   30-16  (65.2%)

By confidence  (a tier is working if its hit rate tracks its stated band)
  lock       114-14  actual  89.1%   claimed  86.5%
  lean       74-22  actual  77.1%   claimed  68.2%
  coin-flip  33-21  actual  61.1%   claimed  55.2%

Versus the market  (236 games where a line was observed)
  model  180-56  (76.3%)
  market 193-43  (81.8%)
  Beating the market is the real bar; matching it means the model is re-deriving public information.
```
