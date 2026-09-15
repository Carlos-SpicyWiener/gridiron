# Gridiron Phase 2 — CBB, NBA, MLB ingest adapters

**Build spec for extending gridiron beyond football**
Status: draft v1 · 2026-09-15
Scope: ingest + rate + slate for men's college basketball, NBA, and MLB.
Sized betting stays locked. Football remains the primary edge loop.

This document does not replace `docs/phase1-spec.md`. Phase 1's §0 principles,
schema rules, fail-closed gates, and §7 unlock criteria still apply. Where this
spec and Phase 1 would conflict, Phase 1 wins.

---

## 0. Design principles (inherited)

From Phase 1 §0, restated so they cannot be "adapted" into something weaker:

1. **League-agnostic from day one.** Every table and function keys on `league`.
   Phase 2 is an ingest adapter plus a config block. The Elo replay loop, the
   pick lock, the Kelly sizer, and the ledger do not grow a sport-specific
   branch.
2. **The tool proposes, the operator disposes.** Still no auto-execution.
3. **Prove edge cheaply before betting real size.** §7 is **per league**. NBA
   games in the database are not NFL evidence. A CBB backfill does not unlock
   CBB betting.
4. **Fail closed.** A missing Kalshi series, a missing Platt fit, or a
   postponed game with no final is a reason not to bet and not to invent a
   score. Never inherit NFL coefficients because a KeyError would be
   inconvenient.
5. **House style.** Stdlib only, singular table names, `schema.sql` append-only
   `IF NOT EXISTS`, `db.now()` timestamps, derived data never hand-patched.

Product-owner addition, 2026-09: **MLB is in Phase 2**, not deferred. The
original Phase 1 §9 scoped CBB+NBA only (~Oct start). The adapter interface
does not care; baseball is another config block. It does add operational
risks (doubleheaders, postponements, a sparse ESPN calendar) that §4
calls out rather than papering over.

Football stays first. `GRIDIRON_LEAGUES` defaults to `nfl,cfb`. A one-off NBA
backfill must not take over `gridiron_cycle.sh`.

---

## 1. What this phase ships

| ships | does not ship |
|---|---|
| `lib/leagues.py` — the config block | a second Elo engine |
| ESPN adapters for `nba`, `mlb`, `cbb` | pip dependencies |
| `gridiron backfill/sync/rate/predict/slate --league nba` (and mlb, cbb) | auto-enabled sized betting |
| moneyline winners only, same `game` / `team` / `prediction` tables | props, spreads, totals |
| provisional K/HFA so ratings exist | a fitted Platt curve for the new sports |
| tests that NFL/CFB ingest and ratings are unchanged | Kalshi poll/scan for the new series |

Honest leftovers, called out so they cannot be mistaken for done:

- Kalshi series are **documented, not wired**. `lib/kalshi.py` `SERIES` stays
  `{nfl, cfb}`. Scan/poll of NBA/MLB/CBB is a no-op with `league_disabled`.
- No calibration coefficients for nba/mlb/cbb. `calibration.calibrate(p, "nba")`
  still raises `KeyError`. That is the Phase 1 test: coefficients are added
  deliberately after a backtest, not inherited.
- §7 unlock is not tripped by ingesting games. `BETTING = ("nfl", "cfb")`.
- Elo K/HFA/revert for the new sports are starting guesses (§5). `backtest`
  on a filled season is what replaces them, between seasons, not mid-stream.

---

## 2. Adapter interface

One ingest function, many configs.

```
lib/leagues.py     INGEST / PARAMS / enabled() / betting_allowed()
lib/espn.py        scoreboard_url, ingest_events, ingest_season, ingest_current
lib/elo.py         PARAMS = leagues.PARAMS   (replay loop unchanged)
```

`ingest_events(conn, league, payload, major_ids=None)` is the seam. It already
wrote `team` and `game` rows keyed on `league`. Phase 2 did not add a parallel
writer. A payload is a payload: two competitors, a status, optional scores,
optional embedded odds.

What differs per league is **how you obtain the payload**:

| league | ESPN sport/path | calendar | major membership |
|---|---|---|---|
| nfl | `football/nfl` | week 1–18 + post 1–5 | all `major` |
| cfb | `football/college-football` | week 1–15 + post 1; `groups=80` | group 80 = FBS |
| nba | `basketball/nba` | day, ESPN calendar | all `major` |
| mlb | `baseball/mlb` | day, filled date window | all `major` |
| cbb | `basketball/mens-college-basketball` | day; `groups=50` | group 50 = D1 |

Football URLs and the week loop are the Phase 1 path, moved behind
`calendar: "week"`. Day-based sports iterate `YYYYMMDD` (optionally chunked
`YYYYMMDD-YYYYMMDD` to stay under ESPN's ~100-event cap).

**Season types ingested:** 2 (regular) and 3 (post). Preseason / spring
training (type 1) and All-Star (`type.abbreviation == ALLSTAR`) are skipped.
Exhibitions are not evidence; the same rule that dropped the Pro Bowl drops
the MLB All-Star Game and the NBA All-Star Game.

**Tier.** Unchanged column, new meaning where it matters:

- NFL/NBA/MLB: every team is `major`.
- CFB: FBS = `major`, anyone else = `other` (starts at 1200).
- CBB: D1 = `major`, a non-D1 opponent that shows up on a D1 scoreboard =
  `other` (starts at 1200). Same reason as FCS: a November buy game is not
  evidence of an elite offense.

`major_team_ids(league, season)` generalises `fbs_team_ids`. The CFB wrapper
remains.

**Scores.** Partial scores are never stored. `status != 'final'` zeroes
`home_score` / `away_score`. ESPN's scheduled games often carry `"0"`; that
is not a result.

**Odds.** `ingest_odds` is unchanged. Basketball and baseball scoreboard
payloads often omit the DraftKings block. NULL means not observed.

**Optional multi-book.** `lib/odds.py` `SPORT_KEY` gains
`basketball_nba`, `basketball_ncaab`, `baseball_mlb`. Still requires
`GRIDIRON_ODDS_KEY`. Still never an input to the model.

### 2.1 Enablement

```
GRIDIRON_LEAGUES=nfl,cfb            # default; football cycle unchanged
GRIDIRON_LEAGUES=nfl,cfb,nba,mlb,cbb
```

`--league nba` on backfill/sync/rate/predict/slate always addresses that
league, even if it is not in the env var. `all` follows `enabled()`.
Unknown tokens in the env var are ignored; an empty parse falls back to
football (fail closed, not "ingest nothing" and not "ingest everything").

`BETTING` is a separate tuple. Putting `nba` in `GRIDIRON_LEAGUES` does not
put it in `BETTING`.

---

## 3. CLI after ingest

Football, unchanged:

```bash
./bin/gridiron init
./bin/gridiron backfill --seasons 2021-2025          # nfl+cfb
./bin/gridiron sync && ./bin/gridiron rate
./bin/gridiron predict && ./bin/gridiron slate
```

A new league:

```bash
./bin/gridiron backfill --league nba --seasons 2023-2025
./bin/gridiron sync    --league nba
./bin/gridiron rate                              # rebuilds every league from game
./bin/gridiron predict --league nba --days 10
./bin/gridiron slate   --league nba
./bin/gridiron ratings --league nba
./bin/gridiron backtest --league nba --seasons 2025 --warmup-from 2023
```

Same commands for `mlb` and `cbb`.

ESPN season years: football `2024` is the 2024 season. NBA/CBB `2025` is the
2024–25 season (the year the title is decided). MLB `2025` is the 2025
campaign. `--seasons 2023-2025` for NBA is three seasons, not two.

`gridiron_cycle.sh` calls `sync` with no `--league`, so it follows
`GRIDIRON_LEAGUES`. Leave that at football until the new ratings have a
backtest you would actually read.

`scan`, `poll`, and `bet place` refuse nba/mlb/cbb. The skip reason is
`league_disabled`. There is no Kalshi client change that would silently
poll `KXNBAGAME`.

---

## 4. Per-league ingest notes

### 4.1 NBA

Build first. 30 teams, a dense night-by-night calendar ESPN actually
populates, moneyline market that will eventually map onto `KXNBAGAME`.

- Calendar from `dates=YYYY` is ~230 game days, October through June.
- Chunk 3 days per request. A week of NBA is well under ESPN's 100-event cap.
- Current sync: yesterday through +10 days, so `slate --days 10` is not empty.
- Neutral sites exist (in-season tournament, some finals). `neutralSite` is
  already a column; HFA zeroes there, same as football.
- Back-to-backs are just two `game` rows. Elo has no rest term. That is a
  known blindness, same family as "Elo knows only the final score."

### 4.2 MLB

Included in Phase 2 by product decision. Highest operational risk of the
three.

**Doubleheaders.** Two events, two ESPN ids, often the same `shortName`.
`UNIQUE (league, espn_id)` is the right key; do not key on (league, date,
home, away). Verified live: 2024-07-13 CHC @ STL is `401569896` (Game 1)
and `401673997` (Game 2, a May 24 makeup). Both ingest as separate games.
Kalshi's `{SERIES}-{YYMMMDD}{AWAY}{HOME}` ticker **cannot disambiguate a
doubleheader** — both games share the date and the abbreviation pair. That
is a Phase 2.5 mapping problem and a reason betting stays locked, not a
reason to skip ingest.

**Postponements.** `STATUS_POSTPONED` and `STATUS_CANCELED` map to
`canceled`. Scores stay NULL. A makeup usually keeps the ESPN id and moves
`kickoff_utc`; `upsert_game` updates in place. A makeup that is a new event
(the Game 2 above) is a new row. Elo skips anything that is not `final`
with both scores present, so a canceled row is not evidence.

**Suspended games.** `wasSuspended` appears on some competitions. If the
game later completes, ESPN marks `STATUS_FINAL` and we store the final
score. While it is not complete it is not a result.

**Ties.** Modern MLB extra innings make them rare. `prediction.correct`
still scores a tie as a miss (Phase 1 Appendix A). `bet grade` would score
0.5 if betting were ever unlocked. Do not special-case a 0–0 as a skip.

**ESPN calendar is sparse.** A dated MLB scoreboard's `leagues[0].calendar`
is spring training, the All-Star break, and October — not the 162-game
grid. Backfill therefore **fills every day** from March 20 through
November 15 of the season year, in 3-day chunks, and relies on
`season_types (2, 3)` to drop spring training that falls inside the window.
Do not "trust the calendar" here.

**K is small (4).** A 162-game season with high per-game variance. Using
football's K=20 would let a hot week run away with the table. This is a
starting point from the same 538-style reasoning the football constants
came from, not a fitted value. See §5.

### 4.3 CBB

Largest universe, thinnest early-season schedule (this spec is dated
mid-September; tip-off is early November).

- `groups=50` restricts the scoreboard to D1, the way `groups=80` restricts
  CFB to FBS. Non-D1 opponents still appear inside those games.
- A Saturday can be 80+ games. `chunk_days: 1` and `limit=400`. Do not
  request a week as a range; ESPN's 100-event cap would silently truncate.
- Neutral sites are common (early tournaments, conference tournaments,
  March Madness). `neutralSite` already handles HFA.
- D1 membership walks the core API group tree, same shape as FBS. ~31
  conferences, ~360 teams. A program's first appearance as somebody's
  non-D1 opponent starts `other` and may later upgrade to `major`, same
  as `db.upsert_team`'s tier rule.
- Early-season games exist on the board before a long backfill is realistic.
  Scaffolding is the point: `sync --league cbb` should write whatever ESPN
  currently lists, and `rate` should not crash on an empty CBB table.

---

## 5. Elo config — provisional, not a rewrite

The replay in `lib/elo.py` is unchanged: home-field, margin-of-victory
multiplier, between-season revert. New sports add a `PARAMS` row.

| league | K | HFA | revert | other_base | why this starting point |
|---|---|---|---|---|---|
| nfl | 20 | 48 | 0.25 | 1500 | production; do not touch |
| cfb | 38 | 65 | 0.35 | 1200 | production; do not touch |
| nba | 20 | 60 | 0.25 | 1500 | ~82 games, 538-style K; home court is real but smaller than college |
| mlb | 4 | 24 | 0.25 | 1500 | 162 noisy games; football K would overfit a weekend |
| cbb | 32 | 70 | 0.40 | 1200 | ~30 games, college turnover; D1/non-D1 split like FBS/FCS |

The MOV multiplier was built for football margins. NBA margins of 10–20
and MLB margins of 1–3 go through the same `log(margin+1)` term. K is
what absorbs the scale. If a backtest says the NBA top end runs away, K
drops; if CBB locks are underconfident the way CFB's were, that is a
calibration problem (§7 / Phase 1 §4.0), not an excuse to fork the update.

**Do not invent scores** to warm ratings. Backfill real finals. A rating
with 0 games is the tier mean, which is what `starting_rating` is for.

`gridiron backtest --league nba --seasons 2024,2025 --warmup-from 2021` is
the measurement that replaces the guesses. Refit K/HFA between seasons.
Never mid-season, and never by copying NFL's Platt `(a, b)`.

---

## 6. Kalshi series (notes only)

Verified against public naming, **not** against a live open-market sample
on 2026-09-15 (NBA/CBB are preseason / out of season; MLB is in season but
betting is not unlocked). Re-verify at each season start, the same way
Phase 1 §3.2 required for football.

| league | series ticker | title (expected) |
|---|---|---|
| nfl | `KXNFLGAME` | Professional Football Game (wired) |
| cfb | `KXNCAAFGAME` | College Football Game (wired) |
| nba | `KXNBAGAME` | NBA Game |
| mlb | `KXMLBGAME` | MLB Game |
| cbb | `KXNCAAMBGAME` | Men's College Basketball Game |

Beware neighbours — props, series, totals, first-basket. A substring match
on `NBAGAME` is as dangerous as Phase 1's `NFLGAME` vs `NFLGAMESACK`.

**MLB doubleheaders vs the ticker.** Event tickers are
`{SERIES}-{YYMMMDD}{AWAY}{HOME}`. Two games the same day with the same
abbreviation pair are indistinguishable by ticker parse. Resolution that
requires a unique (date, away, home) match will refuse (fail closed) or
need a new disambiguator (start time, game number). Do not guess. This is
why MLB betting is not a config flip.

**Mapping still must not reuse `odds.match_event()`.** Phase 1 §3.3 stands.
City-only truncated display names will tie Yankees/Mets and Lakers/Clippers
the same way they tied Giants/Jets.

Wiring a series is a later PR: add it to `kalshi.SERIES`, seed
`kalshi_alias`, test an open market **and** a settled market, then — only
after §7 — add the league to `BETTING`.

---

## 7. Calibration and §7 gates, per league

Phase 1 §4.0 and §7 are per league. Repeating the part people will try to
weaken:

1. **n ≥ 50 graded model picks** in the league being bet.
2. **Calibration in tolerance, on `p_cal`**, per league: lock / lean /
   coin-flip within ±10 pts of claimed, and Brier beating the market on
   games where a line was observed. A pooled NFL+NBA test would let one
   cancel the other.
3. **CLV ≥ 0 by at least one standard error, n ≥ 20 sized bets**, in that
   league. Football CLV is not baseball CLV.

Until then:

- `kelly_multiplier` stays 0.25 on football, and the sizer is not consulted
  for nba/mlb/cbb at all.
- `calibration.PLATT` has no nba/mlb/cbb keys. Adding identity `(0, 1)`
  "so it runs" would be inventing a fit. The existing unit test
  (`test_rejects_an_unknown_league`) guards this.
- `report movement` skips a league it cannot calibrate rather than crashing
  the football report.

The football coefficients stay `platt_backtest_2022_25`. They are not a
prior for basketball.

---

## 8. Build order (this PR)

1. Config block: `lib/leagues.py`.
2. Generalise `lib/espn.py` behind `ingest_events`; keep the football week
   loop as the `calendar: "week"` path.
3. NBA adapter (day calendar) — first live ingest.
4. MLB adapter (filled date window, doubleheader / postponement handling).
5. CBB adapter (D1 group 50, daily chunks).
6. CLI `--league` choices, `GRIDIRON_LEAGUES`, `rate` / `predict` / `slate`
   filtered to enabled leagues when `all`.
7. Betting fail-closed: `league_disabled`, no SERIES entry, no Platt fit.
8. Stdlib unittest: payload parsing, enablement, NFL/CFB ratings isolation.
9. README: how to turn a league on, and what is still locked.

Out of this PR, on purpose:

- Kalshi snapshot ingest for the new series
- Platt fits
- §7 unlock / raising `kelly_multiplier`
- Auto-bet, props, NHL, WNBA, women's college basketball
- Rest / bullpen / starting-pitcher / injury terms (Elo still sees only
  the final score)

---

## 9. Explicitly out of scope (still)

Everything in Phase 1 §9, plus:

- Treating a successful NBA backfill as evidence that the football sizer
  should loosen
- Sharing ratings across leagues (`matchup` already refuses)
- Using The Odds API as a model input
- Inventing finals for postponed games so the backtest has a denser sample
