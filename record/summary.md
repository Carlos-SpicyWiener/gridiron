# Pick record

Written by `gridiron export`. Every figure below is measured from the database, not derived.

- predictions: 382 (104 graded, 278 awaiting a result)
- teams rated: 307
- ratings computed at: 2026-09-12T23:19:32Z
- last graded at: 2026-09-18T08:17:02Z

Source of truth is `data/gridiron.db`, which is not tracked. These files
exist so the record survives losing it — predictions lock at kickoff and
cannot be regenerated.

## Accuracy

```
Graded picks: 86-18  (82.7%)  model elo-1.0

By league
  CFB   75-12  (86.2%)
  NFL   11-6  (64.7%)

By confidence  (a tier is working if its hit rate tracks its stated band)
  lock       49-8  actual  86.0%   claimed  86.4%
  lean       24-4  actual  85.7%   claimed  68.0%
  coin-flip  13-6  actual  68.4%   claimed  54.8%

Versus the market  (87 games where a line was observed)
  model  69-18  (79.3%)
  market 73-14  (83.9%)
  Beating the market is the real bar; matching it means the model is re-deriving public information.
```
