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
