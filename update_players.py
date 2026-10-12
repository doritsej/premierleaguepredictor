"""
Builds players_data.js for the Premier League Predictor page from
player_match_stats.csv (the file made by the soccerdata loop).

players_data.js holds everything the page's predictions use:
  TEAMS   - each team's record and attack/defense ratings
  PLAYERS - goalscorer data for the current season
  META    - a few numbers the page shows in its text

Run it in the same folder as the CSV and index.html, either as a notebook
cell or with:  python update_players.py
Re-run it whenever you refresh the CSV, then upload the new players_data.js.
"""
import html
import json

import numpy as np
import pandas as pd

SEASON = 2627          # current season, as stored in the CSV
TOP_N = 8              # players kept per team (the page shows the top 6)

# ---- How much the past counts -------------------------------------------
# Past seasons are combined into one "track record", with each season counting
# SEASON_DECAY times as much as the one after it (last season 1, the one
# before 0.5, then 0.25 ...). That track record is then worth a fixed number
# of matches; every match this season counts in full on top of it.
SEASON_DECAY = 0.5
TEAM_HISTORY_MATCHES = 19   # a team's track record counts as 19 matches
PLAYER_HISTORY_MATCHES = 5  # a player's track record counts as up to 5 full matches

# Recent form: each team's most recent matches this season count extra.
# With these settings the last 5 matches count double (1 + 1.0).
# Set RECENT_BOOST = 0 to count every match this season the same.
RECENT_MATCHES = 5
RECENT_BOOST = 1.0

# Every player also starts with 90 "phantom" minutes at an ordinary 0.15 xG/90,
# which steadies the numbers for players with very little data
PRIOR_XG_PER_90 = 0.15
PRIOR_MINUTES = 90

# Understat team names -> the names used on the page
TEAM_NAMES = {
    "Manchester City": "Man City",
    "Manchester United": "Man United",
    "Newcastle United": "Newcastle",
    "Nottingham Forest": "Nott'm Forest",
    "Wolverhampton Wanderers": "Wolves",
    "Tottenham Hotspur": "Tottenham",
    "Brighton & Hove Albion": "Brighton",
    "Hull City": "Hull",
    "Coventry City": "Coventry",
    "Leeds United": "Leeds",
    "Leicester City": "Leicester",
    "Ipswich Town": "Ipswich",
    "West Ham United": "West Ham",
    "AFC Bournemouth": "Bournemouth",
    "Norwich City": "Norwich",
    "Luton Town": "Luton",
}

# Corrections for player names as Understat lists them -> the name to show.
# Add a line here for any other name you want to fix.
PLAYER_NAMES = {
    "Mathis Cherki": "Rayan Cherki",
}

# The page's previous five-season figures (2021-22 to 2025-26, goals for and
# against per match), used only to check this script reads the data correctly
OLD_PAGE = {
    "Man City": (2.31, 0.91), "Arsenal": (2.00, 0.95), "Liverpool": (2.13, 1.09),
    "Aston Villa": (1.54, 1.37), "Man United": (1.50, 1.38), "Newcastle": (1.67, 1.36),
    "Chelsea": (1.65, 1.25), "Tottenham": (1.71, 1.51), "Brighton": (1.51, 1.39),
    "Brentford": (1.49, 1.45), "West Ham": (1.34, 1.62), "Crystal Palace": (1.26, 1.34),
    "Fulham": (1.39, 1.44), "Everton": (1.08, 1.41), "Wolves": (1.05, 1.59),
    "Bournemouth": (1.36, 1.57), "Nott'm Forest": (1.27, 1.53), "Leicester": (1.28, 1.82),
    "Leeds": (1.22, 1.87), "Southampton": (0.92, 1.98), "Burnley": (0.99, 1.81),
    "Sunderland": (1.11, 1.26), "Watford": (0.89, 2.03), "Luton": (1.37, 2.24),
    "Norwich": (0.61, 2.21), "Ipswich": (0.95, 2.16), "Sheffield United": (0.92, 2.74),
}


def season_label(s):
    s = str(s).zfill(4)
    return f"20{s[:2]}–{s[2:]}"


# ---- Load ------------------------------------------------------------------
df = pd.read_csv("player_match_stats.csv")
df["season"] = df["season"].astype(int)
df = df[df["season"] <= SEASON].copy()
df["team"] = df["team"].replace(TEAM_NAMES)
df["player"] = df["player"].map(html.unescape)  # "O&#039;Shea" -> "O'Shea"
df["player"] = df["player"].replace(PLAYER_NAMES)
if "own_goals" not in df.columns:
    print("Warning: no own_goals column, so own goals are missing from team totals")
    df["own_goals"] = 0
for col in ["goals", "own_goals", "xg", "minutes"]:
    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

seasons = sorted(df["season"].unique())
past = [s for s in seasons if s < SEASON]
season_weight = {s: SEASON_DECAY ** i for i, s in enumerate(reversed(past))}

# ---- Match results, rebuilt from the player rows ---------------------------
# A team's goals = its players' goals + own goals scored by the opponent
tm = (df.groupby(["season", "game", "team"])
        .agg(scored=("goals", "sum"), og=("own_goals", "sum"))
        .reset_index())
n_teams = tm.groupby(["season", "game"])["team"].transform("count")
if (n_teams != 2).any():
    print(f"Warning: skipped {tm.loc[n_teams != 2, 'game'].nunique()} matches "
          "that don't have exactly two teams in the data")
    tm = tm[n_teams == 2].copy()
match_og = tm.groupby(["season", "game"])["og"].transform("sum")
tm["gf"] = tm["scored"] + (match_og - tm["og"])
tm["ga"] = tm.groupby(["season", "game"])["gf"].transform("sum") - tm["gf"]

# Check against the page's old five-season numbers
old = tm[tm["season"].between(2122, 2526)].groupby("team")[["gf", "ga"]].mean()
diffs = {t: max(abs(old.loc[t, "gf"] - g), abs(old.loc[t, "ga"] - a))
         for t, (g, a) in OLD_PAGE.items() if t in old.index}
missing = sorted(set(OLD_PAGE) - set(old.index))
if diffs:
    worst = max(diffs, key=diffs.get)
    status = "OK" if diffs[worst] <= 0.02 else "CHECK THIS"
    print(f"Check against the old page: largest difference {diffs[worst]:.2f} "
          f"goals per match ({worst}) - {status}")
if missing:
    print("Not found under the page's team names (add them to TEAM_NAMES):", missing)

# Starting point for teams with no Premier League history in the data:
# the average first season of teams promoted during the data's span
first_season = tm.groupby("team")["season"].min()
promoted = [(t, s) for t, s in first_season.items() if seasons[0] < s < SEASON]
if promoted:
    rows = tm.set_index(["team", "season"]).loc[promoted].groupby(level=0)[["gf", "ga"]].mean()
    PROMO_GF, PROMO_GA = rows["gf"].mean(), rows["ga"].mean()
else:
    PROMO_GF, PROMO_GA = 0.975, 2.107

# ---- Recent-form weights -----------------------------------------------------
# Weight of each current-season match for each team: the last RECENT_MATCHES
# count 1 + RECENT_BOOST, earlier ones 1
cur_tm = tm[tm["season"] == SEASON].sort_values("game")
recent_rank = cur_tm.groupby("team").cumcount(ascending=False)  # 0 = most recent
tm["rw"] = 1.0
tm.loc[cur_tm.index, "rw"] = np.where(recent_rank < RECENT_MATCHES, 1 + RECENT_BOOST, 1.0)
match_weight = {(r.team, r.game): r.rw for r in tm[tm["season"] == SEASON].itertuples()}

# ---- Team ratings ----------------------------------------------------------
teams = []
for team, g in tm.groupby("team"):
    hist, cur = g[g["season"] < SEASON], g[g["season"] == SEASON]
    if len(hist):
        w = hist["season"].map(season_weight)
        h_gf, h_ga = (w * hist["gf"]).sum() / w.sum(), (w * hist["ga"]).sum() / w.sum()
    else:
        h_gf, h_ga = PROMO_GF, PROMO_GA
    n = len(cur)
    cw = cur["rw"].sum()  # this season's matches, recent ones counting extra
    if TEAM_HISTORY_MATCHES + cw > 0:
        gf = (h_gf * TEAM_HISTORY_MATCHES + (cur["rw"] * cur["gf"]).sum()) / (TEAM_HISTORY_MATCHES + cw)
        ga = (h_ga * TEAM_HISTORY_MATCHES + (cur["rw"] * cur["ga"]).sum()) / (TEAM_HISTORY_MATCHES + cw)
    else:  # history switched off and no matches this season: fall back to the past
        gf, ga = h_gf, h_ga
    wins, draws = int((g["gf"] > g["ga"]).sum()), int((g["gf"] == g["ga"]).sum())
    teams.append({
        "name": team, "m": len(g), "w": wins, "d": draws, "l": len(g) - wins - draws,
        "pts": 3 * wins + draws, "gf": gf, "ga": ga,
        "seasons": int(g["season"].nunique()), "current": n > 0, "isNew": len(hist) == 0,
    })

current_teams = [t for t in teams if t["current"]]
AVG = float(np.mean([(t["gf"] + t["ga"]) / 2 for t in current_teams]))
for t in teams:
    t["att"] = round(t["gf"] / AVG, 3)
    t["def"] = round(t["ga"] / AVG, 3)
    t["gf"] = round(t["gf"], 3)
    t["ga"] = round(t["ga"], 3)

# ---- Players ---------------------------------------------------------------
key = "player_id" if "player_id" in df.columns else "player"
cur_df = df[df["season"] == SEASON].copy()

games = cur_df.groupby("team")["game"].nunique()
team_games = {t: sorted(g.unique()) for t, g in cur_df.groupby("team")["game"]}

# Players who appeared for more than one club this season are counted only for
# their newest club, using just their matches there
cur_df = cur_df.sort_values("game")
latest = cur_df.groupby(key)["team"].last()
clubs = cur_df.groupby(key)["team"].nunique()
movers = set(clubs[clubs > 1].index)
for pid in movers:
    rows = cur_df[cur_df[key] == pid]
    print(f"{rows['player'].iloc[-1]}: counted for {latest[pid]} only "
          f"(also played for {', '.join(t for t in rows['team'].unique() if t != latest[pid])})")
cur_df = cur_df[cur_df["team"] == cur_df[key].map(latest)]
cur_df["rw"] = [match_weight.get((t, g), 1.0) for t, g in zip(cur_df["team"], cur_df["game"])]
cur_df["wxg"] = cur_df["rw"] * cur_df["xg"]
cur_df["wmin"] = cur_df["rw"] * cur_df["minutes"]

season = (
    cur_df.groupby(["team", key])
    .agg(**({"player": ("player", "last")} if key != "player" else {}),
         goals=("goals", "sum"), xg=("xg", "sum"), wxg=("wxg", "sum"), wmin=("wmin", "sum"),
         minutes=("minutes", "sum"), apps=("game", "nunique"),
         first_game=("game", "min"))
    .reset_index()
)

# Team matches each player was available for: all of them, except for players
# who joined from another club this season (counted from their first match)
# (each match weighted the same way as above, so recent matches count extra)
def matches_available(r):
    tg = team_games[r["team"]]
    if r[key] in movers:
        tg = [g for g in tg if g >= r["first_game"]]
    return sum(match_weight.get((r["team"], g), 1.0) for g in tg)

season["avail"] = season.apply(matches_available, axis=1)

# Each player's track record from past seasons (any club), recent seasons first
past_df = df[df["season"] < SEASON]
pw = past_df["season"].map(season_weight)
hist = (pd.DataFrame({key: past_df[key], "hx": pw * past_df["xg"], "hm": pw * past_df["minutes"]})
          .groupby(key).sum())
season = season.join(hist, on=key)
season[["hx", "hm"]] = season[["hx", "hm"]].fillna(0)
hist_rate = np.where(season["hm"] > 0, season["hx"] / season["hm"].where(season["hm"] > 0, 1), 0)
hist_minutes = np.minimum(season["hm"], PLAYER_HISTORY_MATCHES * 90)

# xG per minute: this season in full + track record + a small ordinary baseline
rate = ((season["wxg"] + hist_rate * hist_minutes + PRIOR_XG_PER_90 * PRIOR_MINUTES / 90)
        / (season["wmin"] + hist_minutes + PRIOR_MINUTES))
# Expected xG per team match = xG per minute x average minutes per match available
# (both lean toward recent matches, so a player who recently won a starting
# place counts more than his early-season minutes suggest)
season["contrib"] = rate * season["wmin"] / season["avail"]

players = {}
for team, g in season.groupby("team"):
    g = g.sort_values("contrib", ascending=False)
    players[team] = {
        "games": int(games[team]),
        "sum": round(float(g["contrib"].sum()), 4),
        "p": [
            [r.player, round(float(r.contrib), 4), int(r.goals),
             round(float(r.xg), 2), int(r.minutes), int(r.apps)]
            for r in g.head(TOP_N).itertuples()
        ],
    }

# ---- Write -------------------------------------------------------------------
meta = {
    "first": season_label(seasons[0]), "current": season_label(SEASON),
    "matches": int(tm.groupby(["season", "game"]).ngroups), "avg": round(AVG, 3),
    "teamHistory": TEAM_HISTORY_MATCHES, "playerHistory": PLAYER_HISTORY_MATCHES,
    "recentMatches": RECENT_MATCHES, "recentBoost": RECENT_BOOST,
}
with open("players_data.js", "w", encoding="utf-8") as f:
    f.write("// Generated by update_players.py - re-run it to refresh, don't edit by hand.\n")
    f.write("var META = " + json.dumps(meta, ensure_ascii=False) + ";\n")
    f.write("var TEAMS = " + json.dumps(teams, ensure_ascii=False) + ";\n")
    f.write("// Per team: [player, expected per-match xG contribution, goals, xG, minutes, apps]\n")
    f.write("var PLAYERS = " + json.dumps(players, ensure_ascii=False) + ";\n")

print(f"Wrote players_data.js: {len(teams)} teams ({len(current_teams)} this season), "
      f"{meta['matches']} matches, {min(games)}-{max(games)} played this season")
