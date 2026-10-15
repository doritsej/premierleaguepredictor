# Data refresh

The site reads two files, and a GitHub Action rebuilds both every Tuesday morning:

- `data/players.json`: this season's player stats for the top five leagues. Used by the Scouting tab and by the goalscorer odds in the Match predictor.
- `data/teams.json`: Premier League results for the last five completed seasons plus this season so far. Used for the Match predictor's team ratings.

## What's in this folder

| Path | What it does |
| --- | --- |
| `index.html` | The site: Match predictor and Scouting tabs. It carries a built-in copy of the data, and swaps in the files in `data/` when it's served from GitHub Pages. |
| `data/players.json` | Player stats for the current season, one row per player. |
| `data/teams.json` | Each club's wins, draws, losses and goals: five past seasons combined, and this season separately. |
| `scripts/refresh_data.py` | Downloads, checks and writes both data files. Standard library only, no installs needed. |
| `tests/test_refresh_data.py` | Offline tests for the script. |
| `.github/workflows/refresh-data.yml` | Runs the tests and the script every Tuesday at 09:00 UTC, and commits the new files. |

## Setup

1. Copy these files into the root of your repo and push.
2. In the repo, open **Settings → Actions → General → Workflow permissions** and choose **Read and write permissions**. Without this, the Action can't commit the new data file.
3. Open the **Actions** tab, pick **Refresh data**, and click **Run workflow** to do the first run now instead of waiting for Tuesday.
4. Check the run's **Fetch and check data** step. Each league prints `ok: N players` or `FAILED:` with the reason, and **Team results** prints `ok` with this season's match count.

## Sources

- **Understat** for all five leagues: minutes, matches, goals, assists, xG, xA, non-penalty xG, shots and key passes.
- **Fantasy Premier League** data, mirrored on GitHub by [FPL-Core-Insights](https://github.com/olbauday/FPL-Core-Insights) and [vaastav/Fantasy-Premier-League](https://github.com/vaastav/Fantasy-Premier-League): ages, positions, starts, tackles, clearances/blocks/interceptions, recoveries, saves, clean sheets and goals conceded for Premier League players. These are matched to Understat by name and club.
- **Transfermarkt** player list from [transfermarkt-datasets](https://github.com/dcaribou/transfermarkt-datasets): birth dates for players in all five leagues, used for anyone still missing an age. Matched by name, with league and club breaking ties; unclear matches are left blank. This dataset stopped updating in July 2026, so players new to these leagues may have no age. If it can't be downloaded, the run carries on without it.

- **Premier League results**: past seasons from [vaastav/Fantasy-Premier-League](https://github.com/vaastav/Fantasy-Premier-League), this season from the per-gameweek files in [FPL-Core-Insights](https://github.com/olbauday/FPL-Core-Insights). For each season the script uses whichever source has more finished matches.

If Understat fails for the Premier League, the script falls back to the Fantasy Premier League data alone.

## How the team ratings work

- The window is the last five completed seasons plus this season so far. It moves forward by a season every summer.
- This season's matches count double (`CURRENT_WEIGHT` in the script), so current form matters more as the season goes on.
- Teams with no Premier League season in the window start from the average of recently promoted sides, counted as 38 matches (`PRIOR_MATCHES`), and their own results take over as the season goes on.

## When a run fails

Team results are checked too: every past season must have all 380 matches and 20 clubs, and this season can't have fewer matches than last week. If they fail, last week's `teams.json` stays.

Each league is checked on its own: enough players, the right number of clubs, every club with a normal squad size (11 to 34 players), no negative stats, no impossible minutes, and minutes that haven't gone down since the last run. A league that fails keeps last week's rows, is marked stale, and the run shows as failed so GitHub emails you. Leagues that passed are still updated.

The two most likely causes:

- **Understat changed its site or blocked the request.** The error says which address failed. The fetch code is in `fetch_understat()`.
- **The FPL mirrors haven't updated yet.** Wait a day and use **Run workflow** again.

## Players listed at two clubs

Understat lists every club a player has played for this season. Sometimes that's a real transfer; sometimes it's a match recorded against both clubs for everyone who played in it (this happened with PSG and Rennes in October 2026). Each such player is placed by, in order:

1. **Fantasy Premier League**, for Premier League players: it always has the current club.
2. **The player's own match history on Understat**: the club in his latest match that involves only one of the listed clubs.
3. **Transfermarkt's club**, when several players share the same pair of clubs.
4. **Squad size** (for a group) or **the last club listed** (for one or two players), as a last resort. These are marked "check this" in the run log.

The run log prints a line for every player or group, saying how they were placed.

## Known gaps

- Defensive stats exist only for Premier League players.
- Ages for players who joined these leagues after July 2026 may be missing until a fresher birth-date source is added, such as Transfermarkt squad pages.
- Positions for the other four leagues come from Understat's position codes, which are broad (GK, D, M, F).
- Scraping these sites is fine for a personal or portfolio project. A public product, especially a paid one, should use a licensed data provider.

## Run it on your computer

```
python scripts/refresh_data.py
python -m unittest discover tests
```
