#!/usr/bin/env python3
"""Refresh the data behind the site.

Writes two files the site reads:
  * data/players.json - this season's player stats for the top five European
    leagues (Scouting tab, and goalscorer odds in the Match predictor).
  * data/teams.json - Premier League results for the last five completed
    seasons plus this season so far (Match predictor team ratings).

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
  python scripts/refresh_data.py                  # writes data/players.json and data/teams.json
  python scripts/refresh_data.py --out other.json --teams-out other-teams.json
  python scripts/refresh_data.py --season 2026    # season start year
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import html
import io
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import zlib
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
    "Hull City": "Hull", "Sheffield United": "Sheffield United", "Sheffield Utd": "Sheffield United",
    "Luton Town": "Luton", "Leicester": "Leicester", "Norwich City": "Norwich", "Watford FC": "Watford",
    "AFC Bournemouth": "Bournemouth",
}

FPL_CORE = "https://raw.githubusercontent.com/olbauday/FPL-Core-Insights/main/data/{season}"
FPL_VAASTAV = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data/{season}"
# Transfermarkt player list from github.com/dcaribou/transfermarkt-datasets (birth dates, clubs).
# It stopped updating in July 2026, but birth dates don't change, so it still covers most players.
TM_PLAYERS = "https://pub-e682421888d945d684bcae8890b0ec20.r2.dev/data/players.csv.gz"
TM_COMPETITIONS = {"Premier League": "GB1", "La Liga": "ES1", "Bundesliga": "L1", "Serie A": "IT1", "Ligue 1": "FR1"}
CLUB_FILLER = {"fc", "cf", "ac", "as", "sc", "ss", "us", "ssc", "afc", "club", "de", "di", "calcio",
               "football", "futbol", "the", "and", "real", "rc", "ca", "cd", "ud", "sd", "vfb", "vfl",
               "tsg", "sv", "fsv", "bv", "olympique", "stade", "racing"}
UA = "Mozilla/5.0 (compatible; premier-league-predictor data refresh)"

# Checks
MIN_PLAYERS = 150            # players with minutes in a league
MAX_MINUTES_DROP_SHARE = 0.05  # share of returning players whose minutes went down
MINUTES_TOLERANCE = 5          # smaller drops are treated as corrections, not resets
SQUAD_RANGE = (11, 34)         # players with minutes per club; outside this, something's wrong
MULTI_CLUB_GROUP = 3           # this many players sharing the same list of clubs = a recording error


LEAGUE_NAMES = [name for name, _ in LEAGUES.values()]


class LeagueError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def decode_body(raw: bytes, encoding: str = "") -> str:
    """Decompress gzip/deflate responses (Understat sends gzip even unasked) and decode as UTF-8."""
    encoding = (encoding or "").lower()
    if raw[:2] == b"\x1f\x8b" or encoding == "gzip":
        raw = gzip.decompress(raw)
    elif encoding == "deflate":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8")


def http_get(url: str, headers: dict | None = None, retries: int = 3) -> str:
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip"}
    h.update(headers or {})
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=60) as r:
                return decode_body(r.read(), r.headers.get("Content-Encoding", ""))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 404:  # missing file: retrying won't help
                break
            time.sleep(2 * (attempt + 1))
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
        data = json.loads(body)
        players = data.get("players") if isinstance(data, dict) else None
        if isinstance(players, dict):  # some responses key players by id
            players = list(players.values())
        if players:
            return players
        keys = list(data)[:10] if isinstance(data, dict) else type(data).__name__
        errors.append(f"JSON endpoint returned no players (got: {keys})")
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
        teams = [team_name(t) for t in html.unescape(p.get("team_title", "")).split(",") if t.strip()]
        pos = (p.get("position") or "").split()
        rows.append({
            "name": html.unescape(p.get("player_name", "")).strip(),
            "team": teams[-1] if teams else "",  # settled by resolve_clubs() below
            "_teams": teams,
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
    resolve_clubs(rows)
    return rows


def resolve_clubs(rows: list[dict]) -> list[str]:
    """Pick one club for players Understat lists with several this season.

    A genuine mid-season transfer involves one or two players. When several players share
    the same list of clubs, it's a recording error on Understat's side (a match credited to
    the wrong club), so those players go to whichever listed club has the thinner squad
    otherwise. Returns notes for the run log.
    """
    single: dict[str, int] = {}
    groups: dict[tuple, int] = {}
    for r in rows:
        ts = r["_teams"]
        if len(ts) == 1:
            single[ts[0]] = single.get(ts[0], 0) + 1
        elif len(ts) > 1:
            groups[tuple(ts)] = groups.get(tuple(ts), 0) + 1
    notes = []
    for ts, n in groups.items():
        if n >= MULTI_CLUB_GROUP:
            club = min(ts, key=lambda t: single.get(t, 0))
            notes.append(f"{n} players listed at {' + '.join(ts)}: looks like a recording error, kept at {club}")
        else:
            club = ts[-1]
            notes.append(f"{n} player{'s' if n > 1 else ''} listed at {' + '.join(ts)}: "
                         f"treated as a transfer to {club} (check this)")
        for r in rows:
            if tuple(r["_teams"]) == ts:
                r["team"] = club
    for line in notes:
        print(f"  {line}", file=sys.stderr)
    return notes


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


# ---------------------------------------------------------------- ages (Transfermarkt)

def fetch_transfermarkt(get=http_get) -> list[dict]:
    """Players with birth dates. The file is gzipped CSV; http_get decompresses it."""
    out = []
    for r in csv.DictReader(io.StringIO(get(TM_PLAYERS))):
        dob = (r.get("date_of_birth") or "")[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dob):
            continue
        try:
            last_season = int(float(r.get("last_season") or 0))
        except ValueError:
            last_season = 0
        first, last = r.get("first_name") or "", r.get("last_name") or ""
        out.append({"id": r.get("player_id") or f"{first} {last} {dob}",
                    "name": r.get("name") or f"{first} {last}".strip(),
                    "first": first, "last": last, "dob": dob, "last_season": last_season,
                    "comp": r.get("current_club_domestic_competition_id") or "",
                    "club": r.get("current_club_name") or ""})
    if not out:
        raise LeagueError("Transfermarkt player list had no birth dates (columns may have changed)")
    return out


def age_on(dob: str, today: dt.date) -> int:
    b = dt.date.fromisoformat(dob)
    return today.year - b.year - ((today.month, today.day) < (b.month, b.day))


def club_words(name: str) -> set[str]:
    return {w for w in norm(name).split() if w not in CLUB_FILLER and len(w) > 2}


def pick_player(cands: list[dict], row: dict) -> dict | None:
    """One clear match, using league and then club to break ties; otherwise none."""
    cands = list({c["id"]: c for c in cands}.values())
    if len(cands) <= 1:
        return cands[0] if cands else None
    if len({c["dob"] for c in cands}) == 1:  # same person listed twice
        return cands[0]
    comp = TM_COMPETITIONS.get(row["league"])
    same_league = [c for c in cands if c["comp"] == comp]
    if len(same_league) == 1:
        return same_league[0]
    words = club_words(row["team"])
    same_club = [c for c in (same_league or cands) if words & club_words(c["club"])]
    return same_club[0] if len(same_club) == 1 else None


def add_ages(rows: list[dict], tm: list[dict], season: int, today: dt.date | None = None) -> dict[str, int]:
    """Fill in missing ages. Returns how many were filled per league."""
    today = today or dt.date.today()
    recent = [p for p in tm if p["last_season"] >= season - 3]  # skip retired namesakes
    by_name: dict[str, list] = {}
    by_last: dict[str, list] = {}
    for p in recent:
        for key in {norm(p["name"]), norm(f"{p['first']} {p['last']}")}:
            if key:
                by_name.setdefault(key, []).append(p)
        last = norm(p["last"]).split()
        if last:
            by_last.setdefault(last[-1], []).append(p)
    filled: dict[str, int] = {}
    for r in rows:
        if r.get("age") is not None:
            continue
        un = norm(r["name"])
        hit = pick_player(by_name.get(un, []), r)
        toks = un.split()
        if not hit and len(toks) >= 2:  # e.g. "Vini Jr" style differences: surname + first initial
            hit = pick_player([p for p in by_last.get(toks[-1], []) if norm(p["first"])[:1] == toks[0][:1]], r)
        if hit:
            r["age"] = age_on(hit["dob"], today)
            filled[r["league"]] = filled.get(r["league"], 0) + 1
    return filled


# ---------------------------------------------------------------- checks

def check_league(rows: list[dict], league: str, n_teams: int, previous: list[dict],
                 compare_minutes: bool = True) -> None:
    if len(rows) < MIN_PLAYERS:
        raise LeagueError(f"only {len(rows)} players with minutes (expected at least {MIN_PLAYERS})")
    teams = {r["team"] for r in rows}
    if len(teams) != n_teams:
        raise LeagueError(f"{len(teams)} teams, expected {n_teams}")
    squads: dict[str, int] = {}
    for r in rows:
        squads[r["team"]] = squads.get(r["team"], 0) + 1
    odd = [f"{t} has {n}" for t, n in sorted(squads.items()) if not SQUAD_RANGE[0] <= n <= SQUAD_RANGE[1]]
    if odd:
        raise LeagueError(f"squad sizes look wrong ({', '.join(odd)} players); players may be on the wrong club")
    for r in rows:
        for k in ("minutes", "goals", "assists", "xg", "xa", "apps"):
            if r[k] is not None and r[k] < 0:
                raise LeagueError(f"negative {k} for {r['name']}")
        if r["apps"] and r["minutes"] > r["apps"] * 125:
            raise LeagueError(f"{r['name']} has {r['minutes']} minutes in {r['apps']} matches")
    if not compare_minutes:
        return
    old = {(p["name"], p["team"]): p["minutes"] for p in previous if p["league"] == league}
    shared = [(r, old[(r["name"], r["team"])]) for r in rows if (r["name"], r["team"]) in old]
    # sources sometimes correct a match by a minute or two, so only bigger drops count
    drops = [r["name"] for r, m in shared if r["minutes"] < m - MINUTES_TOLERANCE]
    if shared and len(drops) / len(shared) > MAX_MINUTES_DROP_SHARE:
        raise LeagueError(f"minutes went down for {len(drops)} of {len(shared)} players since the last run "
                          f"(e.g. {', '.join(drops[:3])}); the source may have reset or changed")


# ---------------------------------------------------------------- team results (Match predictor)

WINDOW = 5            # completed seasons in the ratings
CURRENT_WEIGHT = 2    # this season's matches count this many times
PRIOR_MATCHES = 38    # promoted teams start with this many matches of the promoted-team average
CORE_PL_GW = "/By%20Tournament/Premier%20League/GW{gw}/matches.csv"


def as_int(v) -> int:
    return int(float(v))


def results_vaastav(year: int, get=http_get) -> tuple[list[tuple], set[str]]:
    """(home, home goals, away goals, away) for finished matches, plus the season's 20 clubs."""
    base = FPL_VAASTAV.format(season=season_label(year))
    teams = {r["id"]: team_name(r["name"]) for r in csv.DictReader(io.StringIO(get(f"{base}/teams.csv")))}
    out = []
    for r in csv.DictReader(io.StringIO(get(f"{base}/fixtures.csv"))):
        if r.get("finished") != "True" or r.get("team_h_score") in (None, ""):
            continue
        out.append((teams[r["team_h"]], as_int(r["team_h_score"]), as_int(r["team_a_score"]), teams[r["team_a"]]))
    return out, set(teams.values())


def results_core(year: int, get=http_get) -> tuple[list[tuple], set[str]]:
    """Same, from the per-gameweek Premier League files (kept up to date during the season)."""
    base = FPL_CORE.format(season=f"{year}-{year + 1}")
    teams = {r["code"]: team_name(r["name"]) for r in csv.DictReader(io.StringIO(get(f"{base}/teams.csv")))}
    found: dict[tuple, tuple] = {}
    for gw in range(1, 39):
        try:
            text = get(base + CORE_PL_GW.format(gw=gw))
        except LeagueError:
            continue
        for r in csv.DictReader(io.StringIO(text)):
            if r.get("finished") != "True" or r.get("tournament", "prem") != "prem":
                continue
            h, a = teams[str(as_int(r["home_team"]))], teams[str(as_int(r["away_team"]))]
            found[(h, a)] = (h, as_int(r["home_score"]), as_int(r["away_score"]), a)  # each pairing once a season
    return list(found.values()), set(teams.values())


def season_results(year: int, complete: bool, get=http_get) -> tuple[list[tuple], set[str]]:
    """Use whichever source has more finished matches. Completed seasons try the archive first."""
    order = (results_vaastav, results_core) if complete else (results_core, results_vaastav)
    best: tuple[list, set] = ([], set())
    errors = []
    for fetch in order:
        try:
            res = fetch(year, get)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{fetch.__name__}: {e}")
            continue
        if len(res[0]) > len(best[0]) or not best[1]:
            best = res
        if complete and len(best[0]) == 380:
            break
    if not best[1]:
        raise LeagueError(f"{season_label(year)} results unavailable ({'; '.join(errors)})")
    return best


def tally(results: list[tuple]) -> dict[str, list[int]]:
    """team -> [matches, wins, draws, losses, goals for, goals against]"""
    t: dict[str, list[int]] = {}
    for h, hg, ag, a in results:
        for team, gf, ga in ((h, hg, ag), (a, ag, hg)):
            x = t.setdefault(team, [0] * 6)
            x[0] += 1
            x[1 if gf > ga else 2 if gf == ga else 3] += 1
            x[4] += gf
            x[5] += ga
    return t


def build_teams(season: int, previous: dict | None, get=http_get) -> dict:
    window = list(range(season - WINDOW, season))
    per_season: dict[int, dict] = {}
    clubs: dict[int, set] = {}
    for y in [window[0] - 1] + window:  # one extra season to spot who was promoted
        res, teams = season_results(y, True, get)
        if len(res) != 380 or len(teams) != 20:
            raise LeagueError(f"{season_label(y)}: {len(res)} of 380 matches, {len(teams)} clubs")
        per_season[y], clubs[y] = tally(res), teams

    cur_res, cur_clubs = season_results(season, False, get)
    now = tally(cur_res)
    if len(cur_clubs) != 20:
        raise LeagueError(f"{season_label(season)}: {len(cur_clubs)} clubs, expected 20")
    for team, x in now.items():
        if team not in cur_clubs or x[0] > 38 or min(x) < 0:
            raise LeagueError(f"{season_label(season)}: odd results for {team} ({x})")
    if previous and previous.get("season") == season_label(season):
        before = sum(t["now"][0] for t in previous["teams"]) // 2
        if len(cur_res) < before:
            raise LeagueError(f"{season_label(season)}: {len(cur_res)} matches, fewer than the {before} last run")

    # promoted teams' first season back, averaged
    promoted = [per_season[y][t] for y in window for t in clubs[y] - clubs[y - 1]]
    pm = sum(x[0] for x in promoted)
    prior = {"gf": round(sum(x[4] for x in promoted) / pm, 3), "ga": round(sum(x[5] for x in promoted) / pm, 3),
             "teams": len(promoted), "matches": PRIOR_MATCHES}

    names = set(cur_clubs).union(*(clubs[y] for y in window))
    teams = []
    for name in sorted(names):
        past = [0] * 6
        n_seasons = 0
        for y in window:
            if name in per_season[y]:
                n_seasons += 1
                past = [a + b for a, b in zip(past, per_season[y][name])]
        teams.append({"name": name, "past": past, "seasons": n_seasons,
                      "now": now.get(name, [0] * 6), "in_league": name in cur_clubs})
    return {
        "season": season_label(season),
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "window": [season_label(y) for y in window],
        "current_weight": CURRENT_WEIGHT,
        "promoted": prior,
        "fields": ["matches", "wins", "draws", "losses", "goals_for", "goals_against"],
        "matches_this_season": len(cur_res),
        "teams": teams,
    }


def refresh_teams(season: int, out: Path, get=http_get) -> str | None:
    """Write data/teams.json. Returns a problem description, or None if all went well."""
    print("Team results:", file=sys.stderr)
    previous = json.loads(out.read_text("utf-8")) if out.exists() else None
    try:
        doc = build_teams(season, previous, get)
    except Exception as e:  # noqa: BLE001
        print(f"  FAILED: {e} (kept the last good file)", file=sys.stderr)
        return f"Team results: {e}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), "utf-8")
    print(f"  ok: {doc['matches_this_season']} matches this season, {len(doc['teams'])} teams "
          f"({doc['window'][0]} to {doc['window'][-1]} plus {doc['season']})", file=sys.stderr)
    return None


# ---------------------------------------------------------------- main

def season_label(season: int) -> str:
    return f"{season}-{str(season + 1)[2:]}"


def load_previous(path: Path, season: int) -> tuple[list[dict], dict]:
    """Last run's rows, used to catch bad data. Ignored once a new season starts."""
    if not path.exists():
        return [], {}
    d = json.loads(path.read_text("utf-8"))
    if d.get("season") != season_label(season):
        print(f"New season {season_label(season)} (last file was {d.get('season')}); "
              "starting fresh instead of comparing with last season.", file=sys.stderr)
        return [], {}
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
            if not lr and dt.date.today().month in (6, 7, 8):
                # summer: the new season's league hasn't kicked off yet
                meta[league] = {"status": "not started", "source": source, "updated": today,
                                "players": 0, "matches": 0}
                print("  no matches played yet this season", file=sys.stderr)
                continue
            prev_source = prev_meta.get(league, {}).get("source")
            same_source = prev_source in (None, source)
            if not same_source:
                print(f"  source changed ({prev_source} -> {source}); skipping the minutes comparison this run",
                      file=sys.stderr)
            check_league(lr, league, n_teams, previous, compare_minutes=same_source)
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
    missing = [r for r in rows if r.get("age") is None]
    if missing:
        print("Ages:", file=sys.stderr)
        try:
            filled = add_ages(rows, fetch_transfermarkt(get), season)
            for league in LEAGUE_NAMES:
                n_missing = sum(1 for r in missing if r["league"] == league)
                if n_missing:
                    print(f"  {league}: added {filled.get(league, 0)} of {n_missing} missing ages", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - ages are nice to have; never fail the run over them
            print(f"  warning: could not add ages ({e})", file=sys.stderr)
    return rows, meta, problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="data/players.json")
    ap.add_argument("--teams-out", default="data/teams.json")
    ap.add_argument("--season", type=int, default=season_start(dt.date.today()),
                    help="season start year, e.g. 2026 for 2026-27")
    a = ap.parse_args()
    out = Path(a.out)
    previous, prev_meta = load_previous(out, a.season)
    rows, meta, problems = build(a.season, previous, prev_meta)
    if rows:
        rows.sort(key=lambda r: (r["league"], r["team"], -r["minutes"]))
        doc = {
            "season": season_label(a.season),
            "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
            "leagues": meta,
            "fields": FIELDS,
            "rows": [[r.get(f) for f in FIELDS] for r in rows],
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), "utf-8")
        print(f"Wrote {len(rows)} players to {out}", file=sys.stderr)
    else:
        print("No player data for any league; leaving the existing file alone.", file=sys.stderr)
        problems.append("Players: no data for any league")

    team_problem = refresh_teams(a.season, Path(a.teams_out))
    if team_problem:
        problems.append(team_problem)

    if problems:
        print("\nProblems:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
