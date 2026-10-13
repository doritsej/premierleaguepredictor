#!/usr/bin/env python3
"""Refresh player data for the Scouting tab.

Pulls this season's player stats for the top five European leagues and writes
data/players.json, which the site reads.

Sources
  * Understat (understat.com): minutes, goals, assists, xG, xA, npxG, shots and
    key passes for the Premier League, La Liga, Bundesliga, Serie A and Ligue 1.
  * Fantasy Premier League data mirrored on GitHub: ages, positions, starts and
    defensive stats (tackles, clearances/blocks/interceptions, recoveries,
    saves, clean sheets) for Premier League players only.

Each league is fetched and checked on its own. A league that fails to download
or fails the checks keeps its rows from the previous file and is marked
"stale", and the script exits with status 1 so the GitHub Action run shows as
failed and GitHub emails you. Leagues that passed are still written.

Only the Python standard library is used.

Usage
  python scripts/refresh_data.py                  # writes data/players.json
  python scripts/refresh_data.py --out other.json
  python scripts/refresh_data.py --season 2026    # season start year
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import io
import json
import re
import sys
import time
import unicodedata
import urllib.request
from pathlib import Path

FIELDS = [
    "name", "team", "league", "pos", "age", "apps", "starts", "minutes",
    "goals", "assists", "xg", "xa", "npxg", "shots", "key_passes",
    "tackles", "cbi", "recoveries", "saves", "clean_sheets", "goals_conceded",
]

# Understat league code -> (display name, number of teams)
LEAGUES = {
    "EPL": ("Premier League", 20),
    "La_liga": ("La Liga", 20),
    "Bundesliga": ("Bundesliga", 18),
    "Serie_A": ("Serie A", 20),
    "Ligue_1": ("Ligue 1", 18),
}

# Team names as the predictor shows them. Anything not listed keeps its source name.
TEAM_NAMES = {
    "Manchester City": "Man City", "Manchester United": "Man United", "Man Utd": "Man United",
    "Newcastle United": "Newcastle", "Nottingham Forest": "Nott'm Forest",
    "Tottenham": "Tottenham", "Spurs": "Tottenham", "Tottenham Hotspur": "Tottenham",
    "Wolverhampton Wanderers": "Wolves", "West Ham United": "West Ham",
    "Brighton & Hove Albion": "Brighton", "Leeds United": "Leeds", "Leicester City": "Leicester",
    "Ipswich Town": "Ipswich", "Ipswich": "Ipswich", "Coventry City": "Coventry",
    "Hull City": "Hull", "Sheffield United": "Sheffield United", "Luton Town": "Luton",
    "AFC Bournemouth": "Bournemouth",
}

FPL_CORE = "https://raw.githubusercontent.com/olbauday/FPL-Core-Insights/main/data/{season}"
FPL_VAASTAV = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data/{season}"
UA = "Mozilla/5.0 (compatible; premier-league-predictor data refresh)"

# Checks
MIN_PLAYERS = 150            # players with minutes in a league
MAX_MINUTES_DROP_SHARE = 0.05  # share of returning players whose minutes went down


class LeagueError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def http_get(url: str, headers: dict | None = None, retries: int = 3) -> str:
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8")
        except Exception as e:  # noqa: BLE001 - report whatever went wrong
            last = e
            time.sleep(2 * (attempt + 1))
    raise LeagueError(f"could not download {url}: {last}")


# Letters that Unicode normalization doesn't reduce to plain ASCII
SPECIAL_LETTERS = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "Ae", "ß": "ss", "ł": "l", "Ł": "L",
                                 "đ": "d", "Đ": "D", "ı": "i", "œ": "oe", "Œ": "Oe", "þ": "th", "ð": "d"})


def norm(s: str) -> str:
    """Lowercase ASCII letters only, for name matching."""
    s = (s or "").translate(SPECIAL_LETTERS)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z ]", "", s.lower().replace("-", " ")).strip()


def team_name(raw: str) -> str:
    raw = html.unescape(raw).strip()
    return TEAM_NAMES.get(raw, raw)


def num(v, kind=float):
    if v in (None, ""):
        return None
    try:
        x = float(v)
    except ValueError:
        return None
    return int(round(x)) if kind is int else round(x, 2)


def season_start(today: dt.date) -> int:
    return today.year if today.month >= 7 else today.year - 1


# ---------------------------------------------------------------- Understat

def parse_embedded(page: str, var: str) -> list:
    """Older Understat pages embed data as  var playersData = JSON.parse('...')."""
    m = re.search(var + r"\s*=\s*JSON\.parse\('(.*?)'\)", page, re.S)
    if not m:
        raise LeagueError(f"{var} not found in page")
    raw = re.sub(r"\\x([0-9a-fA-F]{2})", lambda g: chr(int(g.group(1), 16)), m.group(1))
    return json.loads(raw)


def fetch_understat(code: str, season: int, get=http_get) -> list:
    """Return Understat's player list for one league and season.

    Understat has served this two ways: a JSON endpoint the league page calls,
    and (older) JSON embedded in the page. Try both.
    """
    page_url = f"https://understat.com/league/{code}/{season}"
    errors = []
    try:
        body = get(f"https://understat.com/getLeagueData/{code}/{season}",
                   {"X-Requested-With": "XMLHttpRequest", "Referer": page_url})
        players = json.loads(body)["players"]
        if players:
            return players
        errors.append("JSON endpoint returned no players")
    except Exception as e:  # noqa: BLE001
        errors.append(f"JSON endpoint: {e}")
    try:
        return parse_embedded(get(page_url), "playersData")
    except Exception as e:  # noqa: BLE001
        errors.append(f"league page: {e}")
    raise LeagueError("; ".join(errors))


def understat_rows(players: list, league: str) -> list[dict]:
    rows = []
    for p in players:
        minutes = num(p.get("time"), int) or 0
        if minutes <= 0:
            continue
        teams = html.unescape(p.get("team_title", "")).split(",")
        pos = (p.get("position") or "").split()
        rows.append({
            "name": html.unescape(p.get("player_name", "")).strip(),
            "team": team_name(teams[-1]),   # players who moved mid-season: current club
            "league": league,
            "pos": {"GK": "G", "D": "D", "M": "M", "F": "F"}.get(pos[0] if pos else "", None),
            "age": None,
            "apps": num(p.get("games"), int),
            "starts": None,
            "minutes": minutes,
            "goals": num(p.get("goals"), int),
            "assists": num(p.get("assists"), int),
            "xg": num(p.get("xG")),
            "xa": num(p.get("xA")),
            "npxg": num(p.get("npxG")),
            "shots": num(p.get("shots"), int),
            "key_passes": num(p.get("key_passes"), int),
            "tackles": None, "cbi": None, "recoveries": None,
            "saves": None, "clean_sheets": None, "goals_conceded": None,
        })
    return rows


# ---------------------------------------------------------------- Fantasy Premier League

def display_name(first: str, second: str, web: str) -> str:
    """'Bukayo' 'Saka' 'Saka' -> 'Bukayo Saka'; keeps one-name players as they are known."""
    w = re.sub(r"^[A-Z]\.\s?", "", web).strip()
    if norm(w) == norm(first) or not first or norm(w) in norm(first).split():
        return w
    if norm(w) and norm(w) in norm(second):
        return f"{first} {w}"
    return f"{first} {second.split()[0]}" if second else w


def fetch_fpl(season: int, get=http_get) -> list[dict]:
    """Premier League players from the FPL data mirrors (cumulative season totals)."""
    core = FPL_CORE.format(season=f"{season}-{season + 1}")
    stats = list(csv.DictReader(io.StringIO(get(f"{core}/playerstats.csv"))))
    players = {r["player_id"]: r for r in csv.DictReader(io.StringIO(get(f"{core}/players.csv")))}
    teams = {r["code"]: r["name"] for r in csv.DictReader(io.StringIO(get(f"{core}/teams.csv")))}
    births = {}
    try:  # birth dates don't change, so an older snapshot is fine
        raw = get(FPL_VAASTAV.format(season=f"{season}-{str(season + 1)[2:]}") + "/players_raw.csv")
        births = {r["id"]: r.get("birth_date", "") for r in csv.DictReader(io.StringIO(raw))}
    except LeagueError as e:
        print(f"  warning: no birth dates ({e})", file=sys.stderr)

    by_player: dict[str, list] = {}
    for r in stats:
        by_player.setdefault(r["id"], []).append(r)
    today = dt.date.today()
    out = []
    for pid, hist in by_player.items():
        hist.sort(key=lambda r: float(r["gw"]))
        last = hist[-1]
        minutes = num(last["minutes"], int) or 0
        if minutes <= 0 or pid not in players:
            continue
        apps, prev = 0, 0
        for r in hist:
            m = num(r["minutes"], int) or 0
            if m > prev:
                apps += 1
            prev = max(prev, m)
        p = players[pid]
        age = None
        if births.get(pid):
            b = dt.date.fromisoformat(births[pid])
            age = today.year - b.year - ((today.month, today.day) < (b.month, b.day))
        out.append({
            "name": display_name(p["first_name"], p["second_name"], p["web_name"]),
            "team": team_name(teams.get(p["team_code"], "")),
            "league": "Premier League",
            "pos": {"Goalkeeper": "G", "Defender": "D", "Midfielder": "M", "Forward": "F"}.get(p["position"]),
            "age": age, "apps": apps, "starts": num(last["starts"], int), "minutes": minutes,
            "goals": num(last["goals_scored"], int), "assists": num(last["assists"], int),
            "xg": num(last["expected_goals"]), "xa": num(last["expected_assists"]), "npxg": None,
            "shots": None, "key_passes": None,
            "tackles": num(last["tackles"], int), "cbi": num(last["clearances_blocks_interceptions"], int),
            "recoveries": num(last["recoveries"], int), "saves": num(last["saves"], int),
            "clean_sheets": num(last["clean_sheets"], int), "goals_conceded": num(last["goals_conceded"], int),
            "_first": p["first_name"], "_second": p["second_name"], "_web": p["web_name"],
        })
    return out


FPL_EXTRAS = ["age", "starts", "tackles", "cbi", "recoveries", "saves", "clean_sheets", "goals_conceded"]


def merge_fpl(us_rows: list[dict], fpl_rows: list[dict]) -> tuple[list[dict], int]:
    """Add FPL ages, positions and defensive stats to Understat Premier League rows."""
    by_team: dict[str, list] = {}
    for f in fpl_rows:
        by_team.setdefault(norm(f["team"]), []).append(f)
    matched = 0
    for r in us_rows:
        cands = by_team.get(norm(r["team"]), [])
        un = norm(r["name"])
        toks = un.split()
        hit = None
        for test in (
            lambda f: norm(f"{f['_first']} {f['_second']}") == un,
            lambda f: norm(f["name"]) == un,
            lambda f: toks and norm(f["_second"]).split()[-1:] == toks[-1:] and norm(f["_first"])[:1] == un[:1],
            lambda f: norm(f["_web"]) and norm(f["_web"]) in toks,
        ):
            found = [f for f in cands if test(f)]
            if len(found) == 1:
                hit = found[0]
                break
        if hit:
            matched += 1
            for k in FPL_EXTRAS:
                r[k] = hit[k]
            r["pos"] = hit["pos"] or r["pos"]
    return us_rows, matched


# ---------------------------------------------------------------- checks

def check_league(rows: list[dict], league: str, n_teams: int, previous: list[dict]) -> None:
    if len(rows) < MIN_PLAYERS:
        raise LeagueError(f"only {len(rows)} players with minutes (expected at least {MIN_PLAYERS})")
    teams = {r["team"] for r in rows}
    if len(teams) != n_teams:
        raise LeagueError(f"{len(teams)} teams, expected {n_teams}")
    for r in rows:
        for k in ("minutes", "goals", "assists", "xg", "xa", "apps"):
            if r[k] is not None and r[k] < 0:
                raise LeagueError(f"negative {k} for {r['name']}")
        if r["apps"] and r["minutes"] > r["apps"] * 125:
            raise LeagueError(f"{r['name']} has {r['minutes']} minutes in {r['apps']} matches")
    old = {(p["name"], p["team"]): p["minutes"] for p in previous if p["league"] == league}
    shared = [(r, old[(r["name"], r["team"])]) for r in rows if (r["name"], r["team"]) in old]
    drops = [r["name"] for r, m in shared if r["minutes"] < m]
    if shared and len(drops) / len(shared) > MAX_MINUTES_DROP_SHARE:
        raise LeagueError(f"minutes went down for {len(drops)} of {len(shared)} players since the last run "
                          f"(e.g. {', '.join(drops[:3])}); the source may have reset or changed")


# ---------------------------------------------------------------- main

def load_previous(path: Path) -> tuple[list[dict], dict]:
    if not path.exists():
        return [], {}
    d = json.loads(path.read_text("utf-8"))
    return [dict(zip(d["fields"], row)) for row in d["rows"]], d.get("leagues", {})


def build(season: int, previous: list[dict], prev_meta: dict, get=http_get) -> tuple[list[dict], dict, list[str]]:
    today = dt.date.today().isoformat()
    rows, meta, problems = [], {}, []
    fpl = None
    for code, (league, n_teams) in LEAGUES.items():
        print(f"{league}:", file=sys.stderr)
        try:
            source = "understat"
            try:
                lr = understat_rows(fetch_understat(code, season, get), league)
            except LeagueError as e:
                if league != "Premier League":
                    raise
                print(f"  Understat failed ({e}); using Fantasy Premier League data only", file=sys.stderr)
                lr, source = None, "fpl"
            if league == "Premier League":
                try:
                    fpl = fetch_fpl(season, get)
                except LeagueError as e:
                    if lr is None:
                        raise
                    print(f"  warning: no FPL extras ({e})", file=sys.stderr)
                if lr is None:
                    lr = fpl
                elif fpl:
                    lr, matched = merge_fpl(lr, fpl)
                    source = "understat+fpl"
                    print(f"  matched {matched} of {len(lr)} players to FPL", file=sys.stderr)
            for r in lr:
                for k in [k for k in r if k.startswith("_")]:
                    del r[k]
            check_league(lr, league, n_teams, previous)
            rows += lr
            meta[league] = {"status": "ok", "source": source, "updated": today, "players": len(lr),
                            "matches": max((r["apps"] or 0) for r in lr)}
            print(f"  ok: {len(lr)} players", file=sys.stderr)
        except LeagueError as e:
            problems.append(f"{league}: {e}")
            kept = [p for p in previous if p["league"] == league]
            rows += kept
            m = dict(prev_meta.get(league, {}))
            m.update({"status": "stale" if kept else "missing", "players": len(kept), "error": str(e)})
            meta[league] = m
            print(f"  FAILED: {e} (kept {len(kept)} rows from the last good run)", file=sys.stderr)
    return rows, meta, problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="data/players.json")
    ap.add_argument("--season", type=int, default=season_start(dt.date.today()),
                    help="season start year, e.g. 2026 for 2026-27")
    a = ap.parse_args()
    out = Path(a.out)
    previous, prev_meta = load_previous(out)
    rows, meta, problems = build(a.season, previous, prev_meta)
    if not rows:
        print("No data for any league; leaving the existing file alone.", file=sys.stderr)
        return 1
    rows.sort(key=lambda r: (r["league"], r["team"], -r["minutes"]))
    doc = {
        "season": f"{a.season}-{str(a.season + 1)[2:]}",
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "leagues": meta,
        "fields": FIELDS,
        "rows": [[r.get(f) for f in FIELDS] for r in rows],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), "utf-8")
    print(f"Wrote {len(rows)} players to {out}", file=sys.stderr)
    if problems:
        print("\nProblems:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
