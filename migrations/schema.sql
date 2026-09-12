-- gridiron schema — football results, ratings, and graded predictions.
--
-- Two hard rules are encoded here rather than left to convention:
--   1. Ratings and predictions are DERIVED. `rate` recomputes every rating from
--      the game table on every run, so a bad rating is always a defect in the
--      model, never a row to hand-patch.
--   2. A prediction is immutable once its game kicks off. `predict` refuses to
--      write over a locked row, so the graded record cannot be flattered after
--      the fact.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS team (
  id            INTEGER PRIMARY KEY,
  league        TEXT    NOT NULL,              -- 'nfl' | 'cfb'
  espn_id       TEXT    NOT NULL,
  name          TEXT    NOT NULL,
  abbrev        TEXT,
  conference_id TEXT,
  -- 'major' = NFL team or FBS program; 'other' = an FCS/non-FBS opponent that
  -- only appears as somebody's non-conference game. Tier decides where a team's
  -- rating starts, so a directional-state blowout doesn't read as elite form.
  tier          TEXT    NOT NULL DEFAULT 'major',
  first_seen    TEXT    NOT NULL,
  UNIQUE (league, espn_id)
);

CREATE TABLE IF NOT EXISTS game (
  id           INTEGER PRIMARY KEY,
  league       TEXT    NOT NULL,
  espn_id      TEXT    NOT NULL,
  season       INTEGER NOT NULL,
  season_type  INTEGER NOT NULL,               -- 2 = regular, 3 = post
  week         INTEGER,
  kickoff_utc  TEXT    NOT NULL,               -- ISO8601 Z
  home_team_id INTEGER NOT NULL REFERENCES team(id),
  away_team_id INTEGER NOT NULL REFERENCES team(id),
  neutral      INTEGER NOT NULL DEFAULT 0,
  status       TEXT    NOT NULL,               -- scheduled | in | final
  home_score   INTEGER,
  away_score   INTEGER,
  fetched_at   TEXT    NOT NULL,
  UNIQUE (league, espn_id)
);
CREATE INDEX IF NOT EXISTS ix_game_order  ON game (league, season, season_type, week);
CREATE INDEX IF NOT EXISTS ix_game_kick   ON game (kickoff_utc);
CREATE INDEX IF NOT EXISTS ix_game_status ON game (status);

-- Market lines, kept as append-only snapshots. Never overwritten: line movement
-- between the open and kickoff is itself signal, and an overwrite would destroy
-- the record of what the market said WHEN the pick was made.
CREATE TABLE IF NOT EXISTS odds_snapshot (
  id          INTEGER PRIMARY KEY,
  game_id     INTEGER NOT NULL REFERENCES game(id),
  book        TEXT    NOT NULL,
  fetched_at  TEXT    NOT NULL,
  home_price  INTEGER,                          -- american moneyline
  away_price  INTEGER,
  home_spread REAL,
  UNIQUE (game_id, book, fetched_at)
);
CREATE INDEX IF NOT EXISTS ix_odds_game ON odds_snapshot (game_id, fetched_at DESC);

-- Current rating per team. Truncated and rebuilt by `rate`; never edited.
CREATE TABLE IF NOT EXISTS rating_current (
  team_id      INTEGER PRIMARY KEY REFERENCES team(id),
  league       TEXT    NOT NULL,
  rating       REAL    NOT NULL,
  games        INTEGER NOT NULL,
  last_game_id INTEGER REFERENCES game(id),
  computed_at  TEXT    NOT NULL
);

-- Rating after each game, so form over a season is inspectable.
CREATE TABLE IF NOT EXISTS rating_history (
  team_id      INTEGER NOT NULL REFERENCES team(id),
  game_id      INTEGER NOT NULL REFERENCES game(id),
  season       INTEGER NOT NULL,
  rating_after REAL    NOT NULL,
  PRIMARY KEY (team_id, game_id)
);

CREATE TABLE IF NOT EXISTS prediction (
  id                  INTEGER PRIMARY KEY,
  game_id             INTEGER NOT NULL REFERENCES game(id),
  model               TEXT    NOT NULL,
  made_at             TEXT    NOT NULL,
  pick_team_id        INTEGER NOT NULL REFERENCES team(id),
  win_prob            REAL    NOT NULL,         -- model's prob for pick_team_id
  confidence          TEXT    NOT NULL,         -- lock | lean | coin-flip
  home_rating         REAL,
  away_rating         REAL,
  -- What the market thought at pick time, when an odds feed is configured.
  -- NULL is honest: it means no line was observed, not that there was no line.
  market_pick_team_id INTEGER REFERENCES team(id),
  market_prob         REAL,
  correct             INTEGER,                  -- NULL until graded; 1/0; ties -> 0
  graded_at           TEXT,
  UNIQUE (game_id, model)
);
CREATE INDEX IF NOT EXISTS ix_pred_graded ON prediction (correct);

-- ---------------------------------------------------------------- betting --
-- Phase 1: find mispriced contracts, size the bet, log and grade it. Execution
-- stays manual, so nothing here places an order; these tables are the record.
--
-- Two more rules encoded rather than left to convention:
--   3. A placed bet is immutable except for grading. Same reasoning as
--      prediction: a ledger that can be edited after the fact is not a ledger.
--   4. Probabilities are stored twice — as the model said them and as the
--      calibration layer corrected them — so the correction can itself be
--      graded rather than trusted.

-- The bankroll is an append-only ledger and the balance is SUM(delta). A
-- singleton row cannot represent a deposit, so it cannot produce a curve.
CREATE TABLE IF NOT EXISTS bankroll_event (
  id     INTEGER PRIMARY KEY,
  ts     TEXT    NOT NULL,
  delta  REAL    NOT NULL,
  reason TEXT    NOT NULL,              -- deposit | withdrawal | settlement | correction
  bet_id INTEGER REFERENCES bet(id),
  note   TEXT
);
CREATE INDEX IF NOT EXISTS ix_bankroll_ts ON bankroll_event (ts);

-- Kalshi quotes get their own table: odds_snapshot stores American integers for
-- a home/away pair and has nowhere to put a bid, an ask or open interest.
-- Append-only for the same reason as odds_snapshot — movement is the signal.
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

-- Kalshi's team abbreviation -> our team. Seeded once and asserted thereafter:
-- an unresolved abbreviation is a stale alias table, which is a bug, not a
-- market condition. Never match Kalshi's display names — they are truncated to
-- 13 characters while a market is open ("New York G") and change format
-- entirely once it settles ("CHI Bears").
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
  market_ticker     TEXT    NOT NULL,          -- the audit key back to Kalshi
  contracts         INTEGER NOT NULL,
  price             REAL    NOT NULL,          -- per contract, e.g. 0.64
  fee_per_contract  REAL    NOT NULL,          -- realised, from fees.fee(price)
  fee_model         TEXT    NOT NULL,          -- 'rh_kalshi_2026'
  cost              REAL    NOT NULL,          -- contracts * (price + fee_per_contract)
  model_prob_raw    REAL    NOT NULL,          -- prediction.win_prob, untouched
  model_prob        REAL    NOT NULL,          -- calibrated; what edge and Kelly used
  calib_model       TEXT    NOT NULL,          -- 'platt_backtest_2022_25'
  -- NULL is honest here too: no line was observed, not no line existed.
  market_prob       REAL,
  edge              REAL    NOT NULL,          -- model_prob - price - fee_per_contract
  kelly_full        REAL,
  kelly_used        REAL,
  -- The last mid strictly BEFORE kickoff. Never fetched at grade time: a
  -- settled market quotes 0.99/0.01 with a 0.00/1.00 book, which would turn CLV
  -- into a restatement of the win/loss record and a midpoint into a fake 0.50.
  close_price       REAL,
  close_snapshot_at TEXT,
  status            TEXT    NOT NULL DEFAULT 'open',  -- open|won|lost|push|void
  settlement_value  REAL,                      -- 1.0 | 0.0 | 0.5 tie | NULL while open
  payout            REAL,
  pnl               REAL,
  settled_at        TEXT,
  notes             TEXT    NOT NULL           -- news context at bet time (--ack-news)
);
CREATE INDEX IF NOT EXISTS ix_bet_status ON bet (status);
CREATE INDEX IF NOT EXISTS ix_bet_game   ON bet (game_id);

-- The bets NOT taken, and why. This is what makes the gates falsifiable: replay
-- it against finals and the skip reasons can be graded like anything else.
-- Written only when gate_result changes for a game, or the last row is 6h old —
-- an unconditional write at polling cadence is ~14k near-identical rows a day.
CREATE TABLE IF NOT EXISTS scan_log (
  id             INTEGER PRIMARY KEY,
  scanned_at     TEXT    NOT NULL,
  league         TEXT    NOT NULL,
  game_id        INTEGER NOT NULL REFERENCES game(id),
  market_ticker  TEXT,
  model_prob_raw REAL,
  model_prob     REAL,                         -- calibrated
  market_price   REAL,                         -- Kalshi ask at scan time
  market_prob    REAL,                         -- DraftKings de-vigged, nullable
  spread         REAL,
  open_interest  REAL,
  edge           REAL,
  gate_result    TEXT    NOT NULL,             -- 'candidate' or a skip reason
  acted          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_scan_game ON scan_log (game_id, scanned_at);
