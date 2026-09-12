# Gridiron Phase 1 — Edge Engine, Kelly Sizer & Bet Ledger

**Build spec for extending the gridiron CLI**
Status: draft v4 · 2026-09-12 · supersedes v1, v2 and v3
Scope: decision-support tool. Finds mispriced contracts, sizes the bet, logs and grades it.
Execution stays manual in the Robinhood app.

> **v4 changelog.** Adds §4.0, a **calibration layer**. v3 computed `edge` from the model's
> raw stated probability, but the repo's own backtest documents a league-specific
> calibration bias — and Kelly is far more sensitive to a biased `p` than a pick record is.
> Every probability entering the betting path is now recalibrated first. §2 stores raw and
> calibrated side by side; §5's worked example changes; §7 criterion 2 becomes per-league;
> §8 gains a build step. Appendix C carries the fitted coefficients and the significance
> work behind them.
>
> **v3 changelog.** v2 fixed the Kalshi and math errors but was still written against an
> imagined schema. v3 was written against the real source rather than an assumed schema. Corrections:
> table names are singular; there are no numbered migrations; `odds_snapshot` cannot hold
> Kalshi quotes, so closing capture is *not* free; no alias table exists to extend;
> `team.tier` already encodes FBS/FCS; `rating_current.games` is cumulative and cannot
> answer "this season"; the "book consensus" is one book. Every Kalshi fact was verified
> against the live v2 API the same day.

---

## 0. Design principles

1. **League-agnostic from day one.** Every new table and function keys on `league`.
   Phase 2 (CBB/NBA) must be an ingest adapter + config block, zero engine changes.
2. **The tool proposes, the operator disposes.** No auto-execution in Phase 1. Robinhood has no
   API; auto-trading would require a direct Kalshi account (Phase 1.5, only if edge is
   proven).
3. **Prove edge cheaply before betting real size.** Sizing is gated (§7) until calibration
   and closing-line value are demonstrated on graded data.
4. **Codify the manual workflow we already run:** model number → live price → artifact
   filter → news gate → size → log → grade.
5. **Fail closed.** Every gate that loses its input skips the bet and logs why. A missing
   reference price is a reason not to bet, never a reason to bet unchecked.
6. **Match the house style.** Stdlib only, singular table names, `db.now()` timestamps,
   derived data never hand-patched, and NULL means "not observed" — see §1.

## 1. What exists — verified against source

Read from the deployment checkout on 2026-09-12. **This section replaces v1's §1,
which was wrong in four places.**

```
bin/gridiron            CLI, argparse, flat cmd_* subcommands
bin/gridiron_cycle.sh   sync, rate, grade, odds, predict
lib/db.py               connect/init/now, upsert helpers
lib/espn.py             ESPN ingestion — schedules, results, FBS membership, embedded line
lib/elo.py              the rating model
lib/odds.py             The Odds API client + devig + consensus
lib/picks.py            prediction, grading, report renderers
mcp/gridiron_mcp.py     read-only MCP server, port 8914
migrations/schema.sql   the whole schema, idempotent
```

**Hard constraints to build inside:**

| constraint | consequence for Phase 1 |
|---|---|
| **Stdlib only.** No pip, no virtualenv, "runs anywhere with Python 3.9+" | Kalshi client uses `urllib.request`, not `requests`. `Decimal` and `sqlite3` are stdlib, so both are fine. Host currently runs 3.14.4, but write to the 3.9 floor the README promises. |
| **No numbered migrations.** `migrations/schema.sql` is one file, applied by `db.init()` via `executescript`, every statement `CREATE TABLE IF NOT EXISTS` | New tables are **appended to `schema.sql`**, not a new `00X_betting.sql`. Re-running `init` must stay safe. |
| **Singular table names:** `team`, `game`, `prediction`, `odds_snapshot`, `rating_current`, `rating_history` | Every FK below is `game(id)` / `team(id)`. v1 and v2 both wrote `games`/`teams`. |
| **`db.now()`** — UTC, second precision, ISO8601 Z | Every timestamp written by Phase 1 uses it. No `datetime.now()` calls. |
| **Predictions are immutable once locked** — `predict` refuses to overwrite a kicked-off row | Bets inherit this: once placed, only the grading columns may ever be written. |
| **NULL is honest** — the schema comments say a NULL `market_prob` means "no line observed", not "no line existed" | This is already the house convention; §4's fail-closed rule extends it rather than introducing it. |

**What v1 got wrong about what exists:**

1. **There is no alias table to extend.** v1 §3 said "you already solve this for odds
   ingest; extend it." What exists is *fuzzy name matching* in `lib/odds.py` —
   `_norm()` tokenises a name, `_similarity()` is Jaccard overlap, and `match_event()`
   requires a combined both-sides score ≥ 0.45 within a 30-hour kickoff window. That is
   built for The Odds API's full team names and is actively unsafe for Kalshi's truncated
   ones — see §3.3.
2. **`odds_snapshot` cannot hold a Kalshi quote.** Its columns are `home_price`,
   `away_price` (American moneyline **integers**) and `home_spread`, keyed
   `UNIQUE(game_id, book, fetched_at)`. There is no `source` column — it is `book` — and
   nowhere for a bid, an ask, or open interest. v1's "store into the existing
   odds-snapshot pathway with `source='kalshi'` so closing price capture is free" is wrong
   twice over. Kalshi needs its own table (§2), and closing capture is cheap but not free.
3. **"De-vigged book consensus" is one book.** `odds_snapshot` currently holds only
   `draftkings` (382 rows) and `draftkings-open` (113), arriving free inside the ESPN
   scoreboard payload. `lib/odds.py`'s multi-book path needs `GRIDIRON_ODDS_KEY`, which is
   not set. `consensus()` takes a median over a single book. Everywhere this spec says
   "market", read "DraftKings".
4. **`rating_current.games` is cumulative since the 2021 backfill**, not this season —
   NFL ranges 85–98, CFB 0–73. It cannot answer "fewer than 3 graded games this season";
   §4 needs a real query.

**The model has a documented, league-specific calibration bias.** The README's "Does it
work?" section and `gridiron backtest` both report it, measured two independent ways —
replayed seasons and live prices — agreeing on the sign. NFL is overconfident at the top of
its range, CFB is underconfident at the top of its. `prediction.win_prob` is the *raw*
stated probability with no correction applied, deliberately: the README declines to tune it
away because fitting a shrinkage constant to the same two seasons used to measure it would
be overfitting.

That reasoning is right for the model and wrong for a sizer. A pick record survives a biased
`p` — it just reads a little optimistically. Kelly does not: `p` enters the numerator of the
stake directly, so a few points of bias is the difference between a bet and an anti-bet.
Phase 1 therefore recalibrates in the betting path only, leaving the model untouched. See
§4.0 and Appendix C.

**Reusable as-is:** `odds.american_to_prob()`, `odds.devig()`, `odds.consensus(conn,
game_id)` (returns the median de-vigged **home** probability, or None),
`db.upsert_*` patterns, and `lib/odds.py`'s `_fetch()` shape — `urllib.request.Request`
with `User-Agent: gridiron/1.0`.

## 2. New data model

Appended to `migrations/schema.sql`, same idempotent style as everything above it.

```sql
-- ---------------------------------------------------------------- betting --
-- Bankroll is an append-only ledger. A singleton row cannot represent a
-- deposit, so it cannot produce a bankroll curve; the balance is SUM(delta).
CREATE TABLE IF NOT EXISTS bankroll_event (
  id     INTEGER PRIMARY KEY,
  ts     TEXT    NOT NULL,
  delta  REAL    NOT NULL,
  reason TEXT    NOT NULL,                  -- deposit | withdrawal | settlement | correction
  bet_id INTEGER REFERENCES bet(id),
  note   TEXT
);
CREATE INDEX IF NOT EXISTS ix_bankroll_ts ON bankroll_event (ts);

-- Kalshi quotes. Its own table because odds_snapshot stores American integers
-- for a home/away pair and has nowhere to put a bid, an ask or open interest.
-- Append-only, same reasoning as odds_snapshot: line movement is the signal.
CREATE TABLE IF NOT EXISTS kalshi_snapshot (
  id            INTEGER PRIMARY KEY,
  game_id       INTEGER NOT NULL REFERENCES game(id),
  team_id       INTEGER NOT NULL REFERENCES team(id),   -- the YES side
  market_ticker TEXT    NOT NULL,
  fetched_at    TEXT    NOT NULL,
  yes_bid       REAL,
  yes_ask       REAL,
  mid           REAL,
  open_interest REAL,
  volume        REAL,
  UNIQUE (market_ticker, fetched_at)
);
CREATE INDEX IF NOT EXISTS ix_kalshi_game ON kalshi_snapshot (game_id, fetched_at DESC);

-- Kalshi's team abbreviation -> our team. Asserted once, then a miss is a bug.
CREATE TABLE IF NOT EXISTS kalshi_alias (
  league        TEXT    NOT NULL,
  kalshi_abbrev TEXT    NOT NULL,
  team_id       INTEGER NOT NULL REFERENCES team(id),
  first_seen    TEXT    NOT NULL,
  PRIMARY KEY (league, kalshi_abbrev)
);

CREATE TABLE IF NOT EXISTS bet (
  id                INTEGER PRIMARY KEY,
  placed_at         TEXT    NOT NULL,
  league            TEXT    NOT NULL,
  game_id           INTEGER NOT NULL REFERENCES game(id),
  side_team_id      INTEGER NOT NULL REFERENCES team(id),
  venue             TEXT    NOT NULL DEFAULT 'robinhood',
  market_ticker     TEXT    NOT NULL,             -- the audit key back to Kalshi
  contracts         INTEGER NOT NULL,
  price             REAL    NOT NULL,             -- per contract, e.g. 0.64
  fee_per_contract  REAL    NOT NULL,             -- realised, from fee(price); no default
  fee_model         TEXT    NOT NULL,             -- 'rh_kalshi_2026' (§3.4)
  cost              REAL    NOT NULL,             -- contracts * (price + fee_per_contract)
  model_prob_raw    REAL    NOT NULL,             -- prediction.win_prob, untouched
  model_prob        REAL    NOT NULL,             -- CALIBRATED (§4.0); what edge and Kelly use
  calib_model       TEXT    NOT NULL,             -- 'platt_backtest_2024_25'
  market_prob       REAL,                         -- DraftKings de-vigged; NULL = not observed
  edge              REAL    NOT NULL,             -- model_prob - price - fee_per_contract
  kelly_full        REAL,
  kelly_used        REAL,
  close_price       REAL,                         -- last PRE-KICKOFF mid (§3.5)
  close_snapshot_at TEXT,
  status            TEXT    NOT NULL DEFAULT 'open',  -- open | won | lost | push | void
  settlement_value  REAL,                         -- 1.0 | 0.0 | 0.5 | NULL while open
  payout            REAL,
  pnl               REAL,
  settled_at        TEXT,
  notes             TEXT    NOT NULL              -- --ack-news context
);
CREATE INDEX IF NOT EXISTS ix_bet_status ON bet (status);
CREATE INDEX IF NOT EXISTS ix_bet_game   ON bet (game_id);

CREATE TABLE IF NOT EXISTS scan_log (
  id            INTEGER PRIMARY KEY,
  scanned_at    TEXT    NOT NULL,
  league        TEXT    NOT NULL,
  game_id       INTEGER NOT NULL REFERENCES game(id),
  market_ticker TEXT,
  model_prob_raw REAL,
  model_prob    REAL,                         -- calibrated (§4.0)
  market_price  REAL,                         -- Kalshi ask at scan time
  market_prob   REAL,                         -- DraftKings de-vigged, nullable
  spread        REAL,
  open_interest REAL,
  edge          REAL,
  gate_result   TEXT    NOT NULL,             -- 'candidate' or a §4 skip reason
  acted         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_scan_game ON scan_log (game_id, scanned_at);
```

**No `direction_flip` column.** It is already derivable: `prediction.market_pick_team_id`
exists and is nullable, so a flip is `market_pick_team_id IS NOT NULL AND
market_pick_team_id != pick_team_id`. Storing it again would be a second source of truth.

**Current balance** is `SELECT COALESCE(SUM(delta), 0.0) FROM bankroll_event`. A view named
`bankroll` would collide conceptually with nothing, but keep it a helper in `lib/betting.py`
rather than a view — the codebase has no views and `rate` truncating derived tables is the
established pattern for derived state.

**`scan_log` write rule.** Append a row only when `gate_result` changes for that `game_id`,
or when the last row for that game is over 6 hours old. At the §3.6 cadence across ~150 CFB
games an unconditional write is ~14k near-identical rows a day, and every §7 analysis would
have to dedupe before it could say anything.

## 3. Live price feed (Kalshi)

Robinhood routes sports contracts to Kalshi. Kalshi's market-data endpoints are public —
verified, the OpenAPI spec declares `security: []` and unauthenticated calls return 200.

### 3.1 Endpoint

- Base: `https://external-api.kalshi.com/trade-api/v2` (config `kalshi_base_url`). The older
  `https://api.elections.kalshi.com/trade-api/v2` also returns 200; pin one, don't alternate.
- Use `urllib.request` with `User-Agent: gridiron/1.0`, matching `lib/odds.py::_fetch`.
- Rate limits are published only for authenticated token buckets. Polling at §3.6 cadence is
  far below any documented threshold; still back off on 429.

### 3.2 Discovery

`GET /series?category=Sports` returns **~3766 series** — the category filter is loose, so
filter client-side.

| league | series ticker | title |
|---|---|---|
| nfl | `KXNFLGAME` | Professional Football Game |
| cfb | `KXNCAAFGAME` | College Football Game |

Cache in config after verifying each against a known game; re-verify at season start. Beware
the neighbours — `KXNFLGAMESACK`, `KXNFLGAMETD`, `KXNFLGAMEFG`, `KXNCAAFCSGAME` — a substring
match on `NFLGAME` picks up prop series.

### 3.3 Mapping Kalshi markets → `game` / `team`

**Do not reuse `odds.match_event()`.** It scores Jaccard token overlap on full team names.
Kalshi's open-market display names are city-only and truncated at 13 characters, which
breaks it in the worst possible way:

```
_norm("New York G")      -> {new, york, g}
  vs "New York Giants"   -> {new, york, giants}   similarity 0.50
  vs "New York Jets"     -> {new, york, jets}     similarity 0.50   ← tie
```

A 0.50 tie clears the 0.45 floor and `match_event` takes whichever it sees first. That is a
silent wrong-team attribution on a real-money row.

It gets worse at settlement — the same market changes format:

```
open     KXNFLGAME-26SEP13CHICAR   no_sub_title = "Chicago"
settled  KXNFLGAME-26AUG29CHITEN   no_sub_title = "CHI Bears"
```

So any name-keyed mapping stops matching exactly when you grade.
`custom_strike.football_team` is a bare UUID with no public resolution endpoint.

**Key on the event ticker instead:** `{SERIES}-{YYMMMDD}{AWAY}{HOME}`, e.g.
`KXNFLGAME-26SEP13DALNYG`. Do **not** split the abbreviation pair by string surgery —
abbreviations are variable length (`GB`, `TB`, `WAS`, `LAC`, `NYG`), so `GBMIN` has more than
one valid split.

Procedure, reusing `match_event`'s *structure* (kickoff window + both sides must agree) but
not its name scoring:

1. Enumerate open markets per series once a day; group by `event_ticker`.
2. Parse `{YYMMMDD}` from the ticker → candidate `game` rows within ±30h, same league.
3. Resolve each of the event's two markets to a team via `kalshi_alias`.
4. Accept the game only if **both** resolved teams match that game's home and away ids.
5. An unresolved abbreviation is a **hard error**, not a skip — it means the alias table is
   stale, which is a bug. Seed `kalshi_alias` once by hand against `team.abbrev`, then assert.

### 3.4 Fees — a function of price, not a constant

Kalshi's published taker fee is `ceil(0.07 × P × (1−P) × 100)/100` per contract per side — a
parabola peaking at 50¢ — and Robinhood adds its own commission (probability-weighted since
2026-06-01, 10% standard / 5% Gold, capped at $0.01/contract).

```python
from decimal import Decimal, ROUND_CEILING

CENT = Decimal("0.01")

def kalshi_fee(price):
    return (Decimal("0.07") * price * (1 - price)).quantize(CENT, ROUND_CEILING)

def rh_commission(price, gold=False):
    rate = Decimal("0.05") if gold else Decimal("0.10")
    return min(rate * price * (1 - price), CENT).quantize(CENT, ROUND_CEILING)

def fee(price, gold=False):                 # fee_model = 'rh_kalshi_2026'
    return kalshi_fee(price) + rh_commission(price, gold)
```

| price | 0.10 | 0.25 | 0.40 | 0.50 | 0.64 | 0.75 | 0.90 |
|---|---|---|---|---|---|---|---|
| **fee** | 0.02 | 0.03 | 0.03 | 0.03 | 0.03 | 0.03 | 0.02 |

A 5¢ gross edge is therefore **40–60% fee**, worst in the middle of the range where the model
has least to say. v1's flat `0.01` understates cost by 2–3×.

Use `Decimal` end to end — the API returns prices as strings and float rounding at cent
granularity is exactly where a 5¢ threshold goes wrong.

**Calibrate against reality.** An order preview showing a cost basis exactly equal to
contracts × price is a *cost basis* display, not the debit. Reconcile against a monthly statement before trusting any P&L; that
is what `fee_model` is for, so a recalibration can be applied retroactively.

### 3.5 Reading prices, and the closing price

Field names on the v2 payload are **`yes_bid_dollars`, `yes_ask_dollars`,
`last_price_dollars`** — dollar-denominated **strings** (`"0.2300"`). The integer-cent
`yes_bid` / `yes_ask` fields v1 named are not on this response.

- **Fair price** = midpoint of bid/ask → `kalshi_snapshot.mid`.
- **Edge price** = the ask. You pay the ask.

**Closing price comes from `kalshi_snapshot`, never from the API at grade time.** Verified —
a settled market reports:

```
KXNFLGAME-26AUG29CHITEN  CHI Bears  last=0.9900  yes_bid=0.0000  yes_ask=1.0000  result=yes
```

Fetching at grade time returns 0.99/0.01, which makes "CLV" a restatement of your W-L record
and quietly destroys §7's fastest edge proof. The degenerate `0.00/1.00` book is worse: a
naive midpoint returns a very plausible **0.50**.

`bet grade` reads the last `kalshi_snapshot` strictly before `game.kickoff_utc`, writing
`close_price` and `close_snapshot_at`. If that snapshot is more than 60 minutes pre-kickoff,
keep it but flag the CLV as low-confidence rather than dropping the row.

Guard everywhere: `bid == 0.00 and ask == 1.00` means **no quote**.

### 3.6 Cadence

`GET /markets?series_ticker=…&status=open&limit=1000`, paginating on `cursor`. Every 15 min
on game days, hourly otherwise, tightening to 5 min in the hour before each kickoff so the
closing snapshot is genuinely close. Write to `kalshi_snapshot`.

Run it from the existing systemd user timers alongside `gridiron-sync.timer`.

## 4. Edge engine

### 4.0 The calibration layer

Every probability entering the betting path is recalibrated first. The model is **not**
modified — `prediction.win_prob` stays exactly as it is, so the pick record and `record`
output remain comparable across the project's whole history. The correction lives in
`lib/calibration.py` and applies only to bets.

```
p_cal = calibrate(prediction.win_prob, league)
fair  = midpoint(yes_bid, yes_ask)
ask   = yes_ask
edge  = p_cal − ask − fee(ask)
```

**Method: per-league Platt scaling**, `logit(p_cal) = a + b·logit(p)`, fitted n-weighted over
the backtest's calibration buckets. Coefficients as of 2026-09-12 (`calib_model =
'platt_backtest_2024_25'`, full derivation in Appendix C):

| league | a | b | shape |
|---|---|---|---|
| nfl | +0.13 | **0.74** | `b < 1` — compress toward 0.5 (overconfident at the extremes) |
| cfb | −0.13 | **1.24** | `b > 1` — expand away from 0.5 (underconfident at the extremes) |

The two leagues having opposite-signed slopes is the backtest's qualitative finding —
NFL overconfident, college underconfident — expressed as one number each.

**Why a fitted curve rather than subtracting each bucket's gap.** Tested against its own
standard error, **not one NFL bucket clears 2σ**: n is 24–105 per bucket, SE ≈ 5 points, and
the alarming −12.5 at 85–90% has n = 24 (1.4σ). Subtracting per-bucket gaps would be fitting
noise. A two-parameter monotone fit pools all buckets and cannot invent a wiggle the data
doesn't support. CFB has three genuinely significant buckets (57.5% at 2.2σ, 87.5% at 2.6σ,
92.5% at 3.8σ) and the fit reproduces their direction.

**What it actually does — note the sign flip.** NFL is *not* uniformly overconfident. Below
about 0.62 the fit **raises** the NFL probability:

| stated | 0.50 | 0.55 | 0.60 | 0.65 | 0.75 | 0.85 |
|---|---|---|---|---|---|---|
| **nfl → ** | 0.532 | 0.569 | 0.606 | 0.643 | 0.720 | 0.804 |
| | +3.2 | +1.9 | +0.6 | −0.7 | −3.0 | −4.6 |

Any blanket "subtract N points from NFL" rule is wrong in *direction* at the low end, which
matters because the min-edge band puts plenty of candidates there.

**This layer is itself provisional and must be graded.** It is fitted on two backtested
seasons and corroborated — not validated — by the live-market comparison. Store
`model_prob_raw` alongside `model_prob` on every bet and scan row so `report calibration`
can score the correction against outcomes, and `calib_model` so a refit can be applied
retroactively. Refit at the end of each season; never mid-season, or you are fitting the
same games you are betting.

### 4.1 Gates

A game is a **candidate** only if it passes every gate. Skip reasons are the `gate_result`
vocabulary. **Throughout this table `model_prob` means the calibrated `p_cal` from §4.0**,
never the raw stated probability — including inside the two `suspect_*` comparisons, so that
a disagreement is measured between the market and the model's *corrected* opinion.

| gate | rule (config default) | `gate_result` | implementation note |
|---|---|---|---|
| `min_edge` | `edge ≥ 0.05` | `thin_edge` | 5¢ **net** — the fee is already charged above |
| `liquidity_spread` | `ask − bid ≤ 0.04` | `wide_spread` | wide spread = phantom price |
| `liquidity_depth` | `open_interest ≥ 1000` | `thin_book` | a tight quote with nothing behind it is still phantom |
| `league_enabled` | `league in {nfl, cfb}` | `league_disabled` | Phase 2 flips this |
| `market_reference` | `market_prob IS NOT NULL` | `no_market_reference` | **fails closed.** 42% of open predictions have NULL `market_prob` today (77 of 185) |
| `fbs_vs_fcs` | skip if either side has `team.tier = 'other'` | `fbs_vs_fcs` | **use the existing column** — 109 CFB teams are `other`, 0 NFL teams are. A no-op for NFL, which is correct |
| `stale_rating` | skip if `rating_current.computed_at` older than 14 days | `stale_rating` | currently recomputed daily, so this is a guard, not a filter |
| `thin_history` | fewer than 3 **this-season** graded games → use the carryover rating, flag it | `carryover_rating` (advisory, **not** a skip) | **cannot** use `rating_current.games` — that is cumulative since 2021 (NFL min 85). Needs a real count, below |
| `suspect_same_side` | model and book pick the same winner: skip if `\|model_prob − market_prob\| > 0.20` | `suspect_rating` | a big same-side gap is usually the model being wrong |
| `suspect_flip` | model and book pick different winners: skip if `\|model_prob − market_prob\| > 0.10` | `suspect_direction` | flip test is `prediction.market_pick_team_id != pick_team_id` |
| `news` | pending until `--ack-news` | `pending_news` | the check that killed the Miami and OU-Michigan traps |

This-season graded games, since `rating_current.games` can't answer it:

```sql
SELECT COUNT(*) FROM game
WHERE season = :season AND status = 'final'
  AND (home_team_id = :team_id OR away_team_id = :team_id);
```

**Why the disagreement cap splits in two.** v1 used a single `|Δ| > 0.20`, which treats these
identically:

| game | model | DraftKings | Δ | kind |
|---|---|---|---|---|
| CIN @ HOU | HOU 75% | HOU 57% | +18 | same side — a stronger opinion |
| DEN @ KC | **DEN 60%** | **KC 56%** | +16 | **direction flip** — disagreement about who wins |

The second is the ODU-VT / Montana St failure mode; the first is not. Under one rule both
pass, and one of the two candidates the engine fires this week is a flip. The split (0.20
same-side, 0.10 on a flip) is the smallest change that separates them. Both numbers are
config and both are **provisional guesses** — `report gates` (§6) is what settles them.

**These gates define a band, not a floor.** `min_edge` plus the suspect caps means the tool
bets disagreements of roughly 5 to 20 points and nothing else. Say it out loud: the ceiling
does as much work as the floor, and on this week's board the same-side cap caught the Miami
trap at +21, by a single point.

**Why `thin_history` is advisory.** As a hard skip it is every NFL team in week 1 and every
CFB team in week 2 — the tool would ship and do nothing until roughly week 4, which collides
with "prove edge cheaply" and with §7's n ≥ 50. Carryover ratings already exist: the backfill
gives every team a position earned from prior results, which is the whole argument in the
README for backfilling at all.

Also note v1's **"cross-division matchup" clause is deleted.** In CFB it meant FBS/FCS, now
handled by `tier`. In the NFL it reads as AFC-vs-NFC — about a third of the routine schedule,
skipped for no reason.

## 5. Kelly sizing

Contract at ask `c` with per-contract fee `f = fee(c)`, and `p` the **calibrated**
probability from §4.0 — never the raw stated one. Kelly puts `p` straight into the
numerator of the stake, which is exactly why the calibration layer exists. The fee is part
of what you pay, so it belongs in the denominator:

```
all_in     = c + f
kelly_full = (p − all_in) / (1 − all_in)
stake      = bankroll × kelly_full × kelly_multiplier
stake      = min(stake, bankroll × max_stake_pct)
contracts  = floor(stake / all_in)
```

Config for the proving period:

- `kelly_multiplier = 0.25` — quarter-Kelly, model probs are unproven
- `max_stake_pct = 0.05` — hard cap per bet
- `max_open_exposure_pct = 0.20` — across all open bets

**When the exposure cap binds:** fill **highest `edge` first**, skip the rest, log them
`exposure_capped`. Five candidates at the 5% cap want 25% against a 20%
ceiling, so this binds routinely at any bankroll. Any other rule punishes you for scanning early in the week.

**Worked example — a first NFL bet** (stated p = 0.73, c = 0.64). Stakes are given as a
fraction of bankroll, which is what the sizer actually computes:

```
p_cal      = sigmoid(0.13 + 0.74·logit(0.73))  = 0.704    ← §4.0, −2.6 pts
f          = fee(0.64)                          = 0.03
all_in     = 0.67
edge       = 0.704 − 0.64 − 0.03                = 0.0340   ← BELOW min_edge 0.05
```

**No bet.** Uncalibrated it looked like a +0.060 edge worth 4.55% of bankroll; corrected for
the model's own documented NFL overconfidence it is +0.034 and fails the gate. Any bet placed
manually before the tool existed should still be backfilled at its actual size — it is the
record of what was done, and `report clv` should be allowed to judge it — but the sizer would
not have taken this one.

This is the calibration layer earning its place on the very first row: the difference between
a 4.55% stake and no bet came entirely from a correction the model already knew about and
wasn't applying.

The other two live candidates move in *opposite* directions, which is the §4.0 sign flip in
practice:

| bet | stated | calibrated | raw edge | cal. edge | stake, raw → cal. |
|---|---|---|---|---|---|
| first NFL bet | 0.730 | 0.704 | +0.060 | +0.034 | 4.55% → **no bet** |
| DEN @ KC | 0.600 | 0.606 | +0.120 | +0.126 | 5.00% *(capped)* |
| DAL @ NYG | 0.500 | **0.532** | +0.070 | **+0.102** | 3.07% → **4.49%** |

At the 5¢ edge floor quarter-Kelly stays under the 5% cap for every price below **c = 0.73**,
so on a marginal edge Kelly binds and the cap only catches heavy favourites. The cap bites at
lower prices as the edge grows.

Test fixtures for the sizer — pure function, no db. `p` here is an **already-calibrated**
input, so these exercise the Kelly arithmetic in isolation; §4.0's fit has its own fixtures:

`stake` is a fraction of bankroll; the last column is contracts at an **illustrative**
$1,000 bankroll, purely to check the `floor()`.

| case | p | c | fee | kelly_full | stake (%BR) | contracts /$1k |
|---|---|---|---|---|---|---|
| uncalibrated 0.73 | 0.730 | 0.64 | 0.03 | 0.1818 | 4.55% | 67 |
| calibrated 0.73 | 0.704 | 0.64 | 0.03 | 0.1028 | 2.57% | 38 |
| DEN @ KC | 0.606 | 0.45 | 0.03 | 0.2421 | 5.00% *(capped)* | 104 |
| DAL @ NYG | 0.532 | 0.40 | 0.03 | 0.1797 | 4.49% | 104 |
| small stake | 0.560 | 0.50 | 0.03 | 0.0638 | 1.60% | 30 |
| no edge | 0.500 | 0.50 | 0.03 | negative | 0 | 0 |

## 6. CLI surface

The existing CLI is flat one-word subcommands (`init`, `sync`, `rate`, `odds`, `predict`,
`slate`, `grade`, `record`, `ratings`, `matchup`, `backtest`, `export`, `status`), dispatched
by `sub.add_parser(...).set_defaults(fn=cmd_*)`. Phase 1 adds a nested group for `bet`, which
argparse supports cleanly and keeps thirteen top-level commands from becoming twenty.

```
gridiron scan [--league nfl|cfb|all] [--days N]      # candidates + skip log
gridiron bet place <game_id> <team> --contracts N --price 0.64 \
                   --ack-news "Darnold active, no late scratches"
gridiron bet grade                                    # settle vs finals; close from snapshots
gridiron bet ledger [--open|--all]                    # W-L, pnl, roi, bankroll curve
gridiron bankroll set 1000.00 | adjust +25.00 | history
gridiron report calibration                           # reliability + Brier, picks AND bets
gridiron report clv                                   # avg (close_price − bet_price)
gridiron report gates                                 # were the skips right?
```

- `--ack-news` is required and stored in `notes`.
- `bankroll set` writes a `correction` event for the difference; it never overwrites history.
- `report gates` replays `scan_log` against finals and reports, per `gate_result`, what the
  skipped bets would have returned. This is what makes §4's thresholds falsifiable rather than
  folklore — especially `suspect_direction`, which is new and unvalidated.
- `gridiron_cycle.sh` gains the price poll; `scan` stays manual.
- Bets stay out of the MCP server for now — it is read-only by design, and exposing a betting
  ledger to chat is a Phase 1.5 decision.

## 7. The gate (sizing unlock criteria)

Quarter-Kelly and the 5% cap stay locked until **all three**:

1. **n ≥ 50 graded model picks** in the league being bet. CFB will hit this in weeks; NFL
   takes half a season — that's fine.
2. **Calibration in tolerance, per league and measured on `p_cal`:** each confidence bucket
   (`lock` / `lean` / `coin-flip`) within ±10 pts of claimed **after** §4.0's correction, and
   Brier beating the market's Brier **on the games where a line was observed** — currently 58%
   of open predictions. Two things to state plainly:
   - **Per league, not pooled.** §0.1 already demands it, and the two leagues have
     opposite-signed calibration slopes, so a pooled test would let one cancel the other.
   - **The subset is biased** toward games DraftKings prices most carefully, which makes it the
     harder and more honest test.

   This criterion now also grades §4.0 itself: if `p_cal` is *not* better calibrated than
   `model_prob_raw` on the same games, the correction is not working and should be refit or
   dropped, not trusted.
3. **CLV ≥ 0 with a sample:** **n ≥ 20 placed bets**, mean `(close_price − bet_price)` above
   zero **by at least one standard error**. A bare "CLV ≥ 0" over the ~10 bets quarter-Kelly
   produces before NFL reaches n = 50 is noise, not evidence.

Then raise `kelly_multiplier` to 0.5. Full Kelly never — drawdown variance at full Kelly is
brutal even with a real edge.

## 8. Build order

1. **Schema:** append §2's tables to `migrations/schema.sql`; confirm `init` is still
   idempotent on the live db.
2. **`lib/kalshi.py` — team resolution first, prices second.** Series discovery,
   `kalshi_alias` seeding, event→game matching (§3.3). Test against **both an open and a
   settled market**; this is the piece most likely to silently corrupt everything downstream.
3. **Snapshot ingest** → `kalshi_snapshot`, parsing `*_dollars` strings to `Decimal`.
4. **`lib/fees.py`** — pure function, §3.4's table as its test.
5. **`lib/calibration.py`** — Platt fit + `calibrate(p, league)`. Pure function; Appendix C's
   table as its test. Include the refit routine that regenerates the coefficients from
   `backtest`, so the numbers are reproducible rather than transcribed. Build it before the
   gates: every downstream number depends on it.
6. **`lib/betting.py` gates** → `gridiron scan`, writing `scan_log` under §2's write rule.
7. **Kelly sizer** — pure function, §5's fixture table as its test.
8. **Close-price capture** from `kalshi_snapshot` — grading depends on it, so it precedes the
   bet commands.
9. **Bet commands:** place / grade / ledger. Backfill any pre-tool manual bet as row 1 at its
   actual size, and let `report clv` judge it.
10. **Reports:** calibration, CLV, gates.
11. **Timer:** add the price poll to `gridiron_cycle.sh` / a systemd timer; auto-grade after
    the finals sync.

Steps 1–9 make it usable; 10–11 make it trustworthy. Est. ~3 focused sessions.

## 9. Explicitly out of scope (Phase 1)

- Auto-execution — needs a direct Kalshi account; revisit only after §7 unlocks
- New sports — Phase 2 is CBB + NBA adapters, start ~Oct so ratings warm by Nov
- Player props / spreads / totals — moneyline winners only, matching the model
- Boxing, F1, UFC — fail the volume/density test permanently
- Exposing bets through the MCP server

## Appendix A — settlement edge cases

Kalshi's rules text on these markets: *"If the game ends in a tie, the market will resolve to
$0.50 for each team"*, and a game postponed and not started within 48 hours *"will resolve to
a fair price."*

Note the existing `prediction.correct` column resolves ties to 0 — a defensible choice for a
pick record, but wrong for money, where a tie returns half the stake.

| outcome | status | settlement_value | payout |
|---|---|---|---|
| pick wins | `won` | 1.0 | `contracts × 1.0` |
| pick loses | `lost` | 0.0 | 0 |
| tie | `push` | 0.5 | `contracts × 0.5` |
| postponed / voided | `void` | as settled | per Kalshi's resolution |

NFL ties run ~0.3% of games and CFB has none, so this is rare — but `bet grade` must **fail
loudly** on an unrecognised resolution rather than coerce it to a loss. One wrong P&L row
corrupts §7 for the rest of the season.

## Appendix B — measured baseline, 2026-09-12

Recorded so drift is visible later.

**Kalshi liquidity**

- **NFL** (`KXNFLGAME`, 60 open markets): median spread **1¢**, mean 1.3¢, max 5¢; 98% ≤ 4¢;
  no market with zero open interest. The spread gate is close to a no-op here.
- **CFB** (`KXNCAAFGAME`, 488 open markets): median 2¢ but mean **7.3¢**, max 86¢; **37% fail**
  the 4¢ test; 46% carry OI < 1000. **50 markets pass the spread gate on OI < 1000** — which
  is exactly why `liquidity_depth` exists.

**Gates against live asks, NFL week 1:** 2 candidates from 30 games — DEN @ KC at edge +0.12
and DAL @ NYG at +0.07. One of the two is a direction flip and would now be caught by
`suspect_direction`. Under v1's hard `thin_history` skip: zero.

**Database state**

| | |
|---|---|
| NFL | 1457 games (1426 final), 2021–2026; 32 ratings; 32 picks, 30 ungraded |
| CFB | 4816 games (4650 final), 2021–2026; 245 ratings; 161 picks, 155 ungraded |
| odds_snapshot | 495 rows, `draftkings` 382 + `draftkings-open` 113 |
| market_prob | NULL on **77 of 185** open predictions (42%) |
| team.tier | CFB 138 `major` / 109 `other`; NFL 32 `major` / 0 `other` |
| rating_current.games | NFL 85–98, CFB 0–73 — cumulative, not this season |

## Appendix C — the calibration fit

Source: `gridiron backtest --league all --seasons 2024,2025 --warmup-from 2021`, run
2026-09-12. Regenerate rather than transcribe; `lib/calibration.py` should carry the refit.

### C.1 Backtest headline

| | model | always pick home | Brier |
|---|---|---|---|
| **NFL** | 373-196, 65.6% | 308-261, 54.1% | 0.2176 |
| **CFB** | 1357-494, 73.3% | 1181-670, 63.8% | 0.1728 |

### C.2 Per-bucket gaps, tested against their own standard error

`gap = actual − stated_midpoint`. `σ` is that bucket's binomial standard error.

**NFL** — total n = 558

| stated | n | actual | gap | SE | σ | verdict |
|---|---|---|---|---|---|---|
| 50–55% | 97 | 46.4% | −6.1 | 5.1 | 1.2 | noise |
| 55–60% | 105 | 65.7% | +8.2 | 4.6 | 1.8 | weak |
| 60–65% | 95 | 61.1% | −1.4 | 5.0 | 0.3 | noise |
| 65–70% | 67 | 71.6% | +4.1 | 5.5 | 0.7 | noise |
| 70–75% | 67 | 74.6% | +2.1 | 5.3 | 0.4 | noise |
| 75–80% | 65 | 67.7% | −9.8 | 5.8 | 1.7 | weak |
| 80–85% | 38 | 81.6% | −0.9 | 6.3 | 0.1 | noise |
| 85–90% | 24 | 75.0% | −12.5 | 8.8 | 1.4 | noise |

**Not one NFL bucket clears 2σ.** The −12.5 that reads as alarming has n = 24. Any scheme
that subtracts these gaps directly is fitting noise.

**CFB** — total n = 1851

| stated | n | actual | gap | SE | σ | verdict |
|---|---|---|---|---|---|---|
| 50–55% | 225 | 56.4% | +3.9 | 3.3 | 1.2 | noise |
| 55–60% | 224 | 50.0% | −7.5 | 3.3 | **2.2** | **real** |
| 60–65% | 201 | 64.7% | +2.2 | 3.4 | 0.7 | noise |
| 65–70% | 212 | 70.3% | +2.8 | 3.1 | 0.9 | noise |
| 70–75% | 209 | 69.9% | −2.6 | 3.2 | 0.8 | noise |
| 75–80% | 186 | 74.7% | −2.8 | 3.2 | 0.9 | noise |
| 80–85% | 162 | 87.0% | +4.5 | 2.6 | 1.7 | weak |
| 85–90% | 175 | 92.6% | +5.1 | 2.0 | **2.6** | **real** |
| 90–95% | 181 | 97.2% | +4.7 | 1.2 | **3.8** | **real** |
| 95–100% | 76 | 98.7% | +1.2 | 1.3 | 0.9 | noise |

CFB's top-end underconfidence is the strongest signal in the table: four consecutive positive
buckets over n = 594, two of them past 2.5σ.

### C.3 The fit

Two-parameter Platt scaling per league, `logit(p_cal) = a + b·logit(p)`, maximising the
n-weighted binomial log-likelihood over the buckets above.

| league | a | b |
|---|---|---|
| nfl | +0.13 | 0.74 |
| cfb | −0.13 | 1.24 |

Test fixtures for `lib/calibration.py`:

| stated | nfl → | cfb → |
|---|---|---|
| 0.50 | 0.532 | 0.468 |
| 0.55 | 0.569 | 0.530 |
| 0.60 | 0.606 | 0.592 |
| 0.65 | 0.643 | 0.654 |
| 0.70 | 0.681 | 0.715 |
| 0.73 | 0.704 | 0.751 |
| 0.80 | 0.761 | 0.830 |
| 0.86 | 0.814 | 0.893 |
| 0.90 | 0.853 | 0.931 |

`b < 1` compresses toward 0.5, `b > 1` expands away from it. The crossover — where the
correction changes sign — is near 0.62 for NFL and 0.63 for CFB.

### C.4 Caveats

- Fitted on **two** backtested seasons. Corroborated by the live-market comparison in the
  README (NFL more confident than the book, CFB less), but that is agreement on *sign*, not
  an out-of-sample validation.
- Fitted on **bucket summaries**, not raw prediction pairs, because the backtest does not
  persist its replayed picks. Refitting from raw pairs would be strictly better; persisting
  them is a small change to `lib/backtest.py` and worth doing before the first refit.
- **Refit only between seasons.** Refitting mid-season fits the same games being bet.
- If §7 criterion 2 shows `p_cal` is not better calibrated than `model_prob_raw`, this whole
  layer is wrong and should be dropped rather than tuned — `calib_model` exists so that
  decision is reversible on already-placed bets.
