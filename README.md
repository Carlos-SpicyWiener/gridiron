# gridiron

Tracks NFL and college football, rates every team from results, and makes
straight-up picks on upcoming matchups — then grades itself against what
actually happened and against the betting market.

Stdlib Python 3 and SQLite. No pip install, no virtualenv, no external
dependencies — it runs anywhere with Python 3.9+ and nothing else.

```
gridiron/
  bin/gridiron            CLI (all commands)
  bin/gridiron_cycle.sh   one weekly cycle, in the correct order
  lib/db.py               connections, upserts, timestamps
  lib/espn.py             ESPN ingestion (schedules, results, FBS membership)
  lib/elo.py              the rating model
  lib/odds.py             The Odds API — market lines as a benchmark
  lib/picks.py            prediction, grading, report renderers
  mcp/gridiron_mcp.py     read-only MCP server (port 8914)
  migrations/schema.sql   schema
  data/gridiron.db        the database (gitignored)
```

## Setup

```bash
./bin/gridiron init
./bin/gridiron backfill --seasons 2021-2025   # ~5 min, ESPN-throttled
./bin/gridiron sync
./bin/gridiron rate
./bin/gridiron predict
./bin/gridiron slate
```

Backfill matters more than it looks. A rating is only a summary of games already
played, so on day one the model knows nothing. Five prior seasons give every team
a starting position earned from results rather than assumed.

### Weekly

```bash
./bin/gridiron_cycle.sh     # sync, rate, grade, odds, predict
```

Install the timer to run it for you:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/gridiron-*.service systemd/gridiron-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now gridiron-sync.timer gridiron-mcp
loginctl enable-linger "$USER"
```

### Market lines

Nothing to configure. ESPN embeds a book's line (DraftKings) inside the same
scoreboard payload `sync` already downloads, so market data arrives with no key,
no signup and **no additional HTTP request**. Coverage in practice:

| | games with both moneylines |
|---|---|
| NFL | essentially all |
| CFB | ~two thirds — books often don't price an FBS/FCS mismatch |

Both the opening and closing price are published, so line movement is stored too.
Opening lines are kept under a separate `…-open` pseudo-book and excluded from the
consensus: an opening number is a historical artefact, not a competing quote, and
averaging it with the current line would report a price nobody is offering.

*Optionally*, `GRIDIRON_ODDS_KEY` from <https://the-odds-api.com> (free, 500
requests/month) adds a genuine multi-book consensus on top, which is a better
benchmark than one book. `gridiron odds` fetches it. Nothing depends on it.

A line that was never observed stays NULL, which reads as *no line observed* —
not *there was no line*.

## Commands

| command | what it does |
|---|---|
| `init` | create the database |
| `backfill --seasons 2021-2025` | pull historical seasons |
| `sync` | refresh the current week's schedule and results |
| `rate` | recompute every rating from scratch |
| `odds` | pull the optional multi-book consensus |
| `predict [--days 10]` | write picks for upcoming games |
| `slate` | the current board of picks |
| `grade` | score finished games |
| `record` | accuracy: overall, by tier, versus the market |
| `ratings --league cfb --limit 25` | power ratings |
| `matchup "Georgia" "Alabama" [--neutral]` | any two teams, real fixture or not |
| `backtest --seasons 2024,2025` | walk-forward test on past seasons |
| `export [--commit] [--push]` | write the pick record to tracked CSV |
| `status` | what the database holds and how fresh it is |

## The model

Elo, which is the right starting point here for an unglamorous reason: it needs
only the final score, and it is fully recomputable. Every rating is a pure
function of the `game` table, so `rate` rebuilds all of them from nothing on
every run. A wrong rating is therefore always a defect in `lib/elo.py` — never a
row to correct by hand.

Three football-specific departures from textbook Elo:

- **Home-field advantage** is added to the home rating before the expectation,
  and zeroed at neutral sites. 48 Elo points for the NFL, 65 for college — college
  crowds are worth more.
- **Margin of victory** scales the update, damped by the rating gap, so a
  favourite thrashing a minnow gains very little. Without the damping term,
  blowouts feed on themselves and the top of the table runs away.
- **Season regression** pulls every rating back toward its mean between seasons,
  because rosters and staffs turn over. College regresses harder (35%) than the
  NFL (25%).

Non-FBS teams get their own ratings starting at 1200 rather than 1500, so a
September buy game against an FCS opponent doesn't read as evidence of an elite
offense.

**NFL and college ratings are not comparable.** They are separate populations
that never play each other, so the two 1500s do not mean the same thing.
`matchup` refuses a cross-league comparison rather than printing a number that
looks meaningful and isn't.

### Confidence tiers

`lock` ≥ 75%, `lean` ≥ 60%, `coin-flip` below that. These are labels for reading
convenience, and `record` checks them honestly: it prints each tier's *claimed*
average probability next to its *actual* hit rate. If locks come in at 62%, the
tier is lying and the output says so.

## Does it work?

`gridiron backtest` replays past seasons in order, making each pick from ratings
built only from games that had already finished at that kickoff. Measured on
2024-2025, warmed up on 2021-2023:

| | model | always pick home | Brier |
|---|---|---|---|
| **NFL** | 373-196, **65.6%** | 308-261, 54.1% | 0.218 |
| **CFB** | 1357-494, **73.3%** | 1181-670, 63.8% | 0.173 |

Both beat their baseline by roughly ten points, which is the result to care about
— college's higher raw accuracy is mostly just that college has more mismatches,
which is why the home-team baseline is higher too.

**Known weakness: the NFL tiers are overconfident at the top.** Backtested NFL
"locks" claimed 81.5% and delivered 74.6%, and the 85-90% bucket came in at 75%.
College is close to honest by comparison (locks claimed 86.0%, delivered 88.8%).
So an NFL lock should be read as "strong favourite", not as the number printed
next to it. `backtest` prints this table every run precisely so the flaw stays
visible instead of being buried; it has not been tuned away, because fitting a
shrinkage constant to the same two seasons used to measure it would mostly be
overfitting.

## Two rules the code enforces

**Predictions lock at kickoff.** `predict` will overwrite a pick for a game that
hasn't started, and refuses to touch one that has. `gridiron_cycle.sh` grades
before it predicts for the same reason. Without this, every re-run would quietly
improve the historical record and the win-loss line would become a fiction.

**Odds snapshots are append-only.** Line movement between the open and kickoff is
itself information, and overwriting would destroy the record of what the market
said *at the moment the pick was made*.

## What this system cannot do

Worth stating plainly, because a picks system is unusually easy to fool yourself
with:

- **Elo knows only the final score.** Not injuries, not weather, not a quarterback
  ruled out on Friday, not a team resting starters in week 18, not a coaching
  change. When you know something it doesn't, you are better informed than it is.
- **Beating the market is the real bar.** Picking straight-up winners at 67% sounds
  strong and is roughly what favourites win at anyway. `record` prints the model
  and the market side by side for exactly this reason. Matching the market means
  the model is re-deriving public information more slowly. Early evidence is not
  flattering: on the opening slate the model is consistently *more* confident than
  the book on the same side — 73% against 60%, 78% against 66% — which is the
  overconfidence the backtest already flagged, now visible live.
- **College week 1 is close to guesswork.** Ratings carry over from last season
  through a heavy regression, and a roster can turn over almost entirely.
- **No line is not the same as an even line.** Ungraded and unobserved stay NULL
  everywhere rather than defaulting to something convenient.
- **Backtest accuracy is an optimistic ceiling.** It replays completed seasons
  where every team's schedule is known and nothing was postponed. Live weeks are
  messier.

## MCP server

Read-only, stdlib, JSON-RPC over Streamable HTTP on `127.0.0.1:8914`, same shape
the CLI. It opens the database read-only *and* sets
`PRAGMA query_only`, executes no client-supplied SQL, and holds no API key.

It cannot predict or grade — those write, and writing belongs to the CLI. The
server can only report what has already been computed.

Tools: `slate`, `record`, `ratings`, `matchup`, `team`, `status`.

Register it in `~/.claude.json` under `mcpServers`:

```json
"gridiron": {
  "type": "http",
  "url": "http://127.0.0.1:8914/",
  "headers": { "Authorization": "Bearer <GRIDIRON_MCP_TOKEN>" }
}
```

## The record is exported, the database is not

`data/gridiron.db` is untracked — it is a rebuildable cache of ESPN's data, and a
binary makes a poor diff. Anyone can reconstruct it with `init`, `backfill`, `rate`.

One thing in it cannot be reconstructed: **what was predicted, and when.**
Predictions lock at kickoff by design, so re-running anything after the fact
produces different picks from ratings that have since seen the results. A lost
database would silently reset the record to 0-0.

So `gridiron export` writes it to `record/`, which *is* tracked:

| file | what it holds |
|---|---|
| `record/predictions.csv` | every pick, with the timestamp it was made, the ratings behind it, the market's view, and how it graded |
| `record/ratings.csv` | current power ratings with their computed-at stamp |
| `record/summary.md` | human-readable accuracy report |

The cycle runs `export --push`, so the record commits and publishes on its own.
Only `record/` is ever staged, so a cycle cannot commit the database, the logs,
or the token file even if one appeared in the tree. A rejected push leaves the
commit in place rather than retrying blind — that wants a human, not a cron job.

Ungraded picks export as an empty `result`, not a loss.

Everything in this path is idempotent. `predict` leaves a pick untouched unless
it actually changes, so `made_at` means *when the pick took its current form*
rather than *when predict last ran* — and a cycle that finds no new games
produces no commit at all. Running the cycle ten times in a row changes nothing
ten times.

## Reaching it from a phone

The MCP server binds loopback only. To query it from elsewhere, put a reverse
proxy in front — on this host that is Tailscale:

```bash
tailscale funnel --bg --yes --set-path=/gridiron http://127.0.0.1:8914
```

Then add it as a custom connector wherever you use Claude, with the token as the
last path segment:

```
https://<your-host>.ts.net/gridiron/<GRIDIRON_MCP_TOKEN>
```

The server accepts the token as a `Bearer` header *or* as a path segment. The
header is the better mechanism, but connector UIs generally take only a URL, so
the path form exists for them. Matching on a path segment rather than a prefix
means it works whether or not the proxy strips its mount path.

That URL is reachable from the public internet, and the token is the only thing
protecting it — treat it as a password. `tailscale serve` instead of `funnel`
keeps it inside your tailnet, but then Claude's servers cannot reach it either,
so a connector will not work; that trade is the whole decision.

The record is also readable with no infrastructure at all: `record/summary.md`
renders on the repository page in any phone browser.

## Configuration

| variable | default | what it does |
|---|---|---|
| `GRIDIRON_DB` | `data/gridiron.db` | database location |
| `GRIDIRON_TZ` | `America/Chicago` | zone kickoff times display in |
| `GRIDIRON_ODDS_KEY` | unset | optional; adds a multi-book consensus over the free ESPN line |
| `GRIDIRON_MCP_PORT` | `8914` | port the MCP server listens on (loopback only) |
| `GRIDIRON_MCP_TOKEN` | unset | bearer token; auth is off when unset |

Set them in `~/.config/gridiron/env` (mode 0600), which both systemd units read.

## Possible next steps

1. **Swap the college source to CollegeFootballData.com.** ESPN's endpoints are
   undocumented and can change silently; CFBD is a documented REST API with a free
   key, and carries returning-production and recruiting data — the inputs that
   would actually fix week-1 college being close to guesswork. `lib/espn.py` is
   isolated behind `ingest_events`, so a CFBD adapter is additive, not a rewrite.
2. **Fix NFL overconfidence** with a shrinkage term fitted on 2021-2023 and
   validated on 2024-2025 — fitting and measuring on the same seasons would just
   be overfitting.
3. **Move from Elo to EPA-based ratings.** Elo sees only the final score. Points
   scored are noisy; drive efficiency is much less so.
4. **Injuries and starting quarterbacks**, the single largest thing the model is
   blind to.
