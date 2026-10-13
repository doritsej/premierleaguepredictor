# Player data refresh

The Scouting tab reads `data/players.json`. A GitHub Action rebuilds that file every Tuesday morning.

## What's in this folder

| Path | What it does |
| --- | --- |
| `index.html` | The site: Match predictor and Scouting tabs. It carries a built-in copy of the player data, and swaps in `data/players.json` when it's served from GitHub Pages. |
| `data/players.json` | Player stats for the current season, one row per player. |
| `scripts/refresh_data.py` | Downloads, checks and writes `data/players.json`. Standard library only, no installs needed. |
| `tests/test_refresh_data.py` | Offline tests for the script. |
| `.github/workflows/refresh-data.yml` | Runs the tests and the script every Tuesday at 09:00 UTC, and commits the new file. |

## Setup

1. Copy these files into the root of your repo and push.
2. In the repo, open **Settings → Actions → General → Workflow permissions** and choose **Read and write permissions**. Without this, the Action can't commit the new data file.
3. Open the **Actions** tab, pick **Refresh player data**, and click **Run workflow** to do the first run now instead of waiting for Tuesday.
4. Check the run's **Fetch and check player data** step. Each league prints `ok: N players` or `FAILED:` with the reason.

## Sources

- **Understat** for all five leagues: minutes, matches, goals, assists, xG, xA, non-penalty xG, shots and key passes.
- **Fantasy Premier League** data, mirrored on GitHub by [FPL-Core-Insights](https://github.com/olbauday/FPL-Core-Insights) and [vaastav/Fantasy-Premier-League](https://github.com/vaastav/Fantasy-Premier-League): ages, positions, starts, tackles, clearances/blocks/interceptions, recoveries, saves, clean sheets and goals conceded for Premier League players. These are matched to Understat by name and club.

If Understat fails for the Premier League, the script falls back to the Fantasy Premier League data alone.

## When a run fails

Each league is checked on its own: enough players, the right number of clubs, no negative stats, no impossible minutes, and minutes that haven't gone down since the last run. A league that fails keeps last week's rows, is marked stale, and the run shows as failed so GitHub emails you. Leagues that passed are still updated.

The two most likely causes:

- **Understat changed its site or blocked the request.** The error says which address failed. The fetch code is in `fetch_understat()`.
- **The FPL mirrors haven't updated yet.** Wait a day and use **Run workflow** again.

## Known gaps

- Ages and defensive stats exist only for Premier League players. Adding ages for the other four leagues needs another source, such as Transfermarkt squad pages.
- Positions for the other four leagues come from Understat's position codes, which are broad (GK, D, M, F).
- Scraping these sites is fine for a personal or portfolio project. A public product, especially a paid one, should use a licensed data provider.

## Run it on your computer

```
python scripts/refresh_data.py
python -m unittest discover tests
```
