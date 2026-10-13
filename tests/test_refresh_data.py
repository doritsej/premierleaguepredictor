"""Offline tests for scripts/refresh_data.py.  Run:  python -m unittest discover tests"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import refresh_data as rd  # noqa: E402


def us_player(i, name, team, minutes=450, games=5, pos="F S"):
    return {"id": str(i), "player_name": name, "games": str(games), "time": str(minutes),
            "goals": "2", "xG": "1.83", "assists": "1", "xA": "0.9", "shots": "11",
            "key_passes": "6", "yellow_cards": "0", "red_cards": "0", "position": pos,
            "team_title": team, "npg": "2", "npxG": "1.83", "xGChain": "2.1", "xGBuildup": "0.4"}


def league(n_teams, per_team=10):
    return [us_player(t * 100 + k, f"Player {t}-{k}", f"Team {t}") for t in range(n_teams) for k in range(per_team)]


class ParseUnderstat(unittest.TestCase):
    def test_json_endpoint(self):
        def get(url, headers=None):
            self.assertIn("getLeagueData", url)
            return json.dumps({"players": [us_player(1, "N&#039;Golo Kanté", "Chelsea")]})
        p = rd.fetch_understat("EPL", 2026, get)
        row = rd.understat_rows(p, "Premier League")[0]
        self.assertEqual(row["name"], "N'Golo Kanté")
        self.assertEqual(row["pos"], "F")
        self.assertEqual(row["minutes"], 450)

    def test_falls_back_to_embedded_page(self):
        data = json.dumps([us_player(1, "Kylian Mbappé", "Real Madrid")])
        escaped = "".join("\\x%02X" % ord(c) if c in "\"[]{}:,'" else c for c in data)
        page = f"<script>var playersData = JSON.parse('{escaped}');</script>"

        def get(url, headers=None):
            if "getLeagueData" in url:
                raise rd.LeagueError("404")
            return page
        p = rd.fetch_understat("La_liga", 2026, get)
        self.assertEqual(p[0]["player_name"], "Kylian Mbappé")

    def test_gzip_and_deflate_bodies(self):
        import gzip
        import zlib
        text = '{"players": [], "name": "Mbappé"}'
        self.assertEqual(rd.decode_body(gzip.compress(text.encode())), text)          # no header
        self.assertEqual(rd.decode_body(gzip.compress(text.encode()), "gzip"), text)
        self.assertEqual(rd.decode_body(zlib.compress(text.encode()), "deflate"), text)
        self.assertEqual(rd.decode_body(text.encode()), text)                          # plain

    def test_players_keyed_by_id(self):
        body = json.dumps({"players": {"1": us_player(1, "A B", "Lens")}})
        p = rd.fetch_understat("Ligue_1", 2026, lambda url, headers=None: body)
        self.assertEqual(p[0]["player_name"], "A B")

    def test_mid_season_move_uses_current_club(self):
        r = rd.understat_rows([us_player(1, "A B", "Fulham,Everton")], "Premier League")[0]
        self.assertEqual(r["team"], "Everton")

    def test_skips_players_without_minutes(self):
        self.assertEqual(rd.understat_rows([us_player(1, "A", "X", minutes=0)], "Serie A"), [])

    def test_team_names_match_predictor(self):
        r = rd.understat_rows([us_player(1, "A", "Manchester United")], "Premier League")[0]
        self.assertEqual(r["team"], "Man United")


class Names(unittest.TestCase):
    def test_display_names(self):
        self.assertEqual(rd.display_name("Bukayo", "Saka", "Saka"), "Bukayo Saka")
        self.assertEqual(rd.display_name("Gabriel", "dos Santos Magalhães", "Gabriel"), "Gabriel")
        self.assertEqual(rd.display_name("Bruno", "Borges Fernandes", "B.Fernandes"), "Bruno Fernandes")
        self.assertEqual(rd.display_name("Bruno", "Guimarães Rodriguez Moura", "Bruno G."), "Bruno Guimarães")
        self.assertEqual(rd.display_name("Francisco Evanilson", "de Lima Barbosa", "Evanilson"), "Evanilson")

    def test_merge_fpl(self):
        us = rd.understat_rows([us_player(1, "Bruno Fernandes", "Manchester United"),
                                us_player(2, "Martin Odegaard", "Arsenal"),
                                us_player(3, "Unknown Person", "Arsenal")], "Premier League")
        fpl = [
            {"name": "Bruno Fernandes", "team": "Man United", "pos": "M", "_first": "Bruno",
             "_second": "Borges Fernandes", "_web": "B.Fernandes", "age": 32, "starts": 6, "tackles": 9,
             "cbi": 3, "recoveries": 20, "saves": 0, "clean_sheets": 1, "goals_conceded": 7},
            {"name": "Martin Ødegaard", "team": "Arsenal", "pos": "M", "_first": "Martin",
             "_second": "Ødegaard", "_web": "Ødegaard", "age": 27, "starts": 5, "tackles": 4,
             "cbi": 1, "recoveries": 15, "saves": 0, "clean_sheets": 3, "goals_conceded": 3},
        ]
        rows, matched = rd.merge_fpl(us, fpl)
        self.assertEqual(matched, 2)
        self.assertEqual(rows[0]["age"], 32)
        self.assertEqual(rows[0]["pos"], "M")      # FPL position wins over Understat's
        self.assertEqual(rows[1]["tackles"], 4)
        self.assertIsNone(rows[2]["age"])


class Checks(unittest.TestCase):
    def rows(self, n_teams=20):
        return rd.understat_rows(league(n_teams), "La Liga")

    def test_good_league_passes(self):
        rd.check_league(self.rows(), "La Liga", 20, [])

    def test_wrong_team_count_fails(self):
        with self.assertRaises(rd.LeagueError):
            rd.check_league(self.rows(19), "La Liga", 20, [])

    def test_too_few_players_fails(self):
        with self.assertRaises(rd.LeagueError):
            rd.check_league(self.rows()[:100], "La Liga", 20, [])

    def test_minutes_going_backwards_fails(self):
        now = self.rows()
        before = [dict(r, minutes=r["minutes"] + 90) for r in now]
        with self.assertRaises(rd.LeagueError):
            rd.check_league(now, "La Liga", 20, before)


class Build(unittest.TestCase):
    def test_failed_league_keeps_previous_rows(self):
        sizes = {"EPL": 20, "La_liga": 20, "Bundesliga": 18, "Serie_A": 20, "Ligue_1": 18}

        def get(url, headers=None):
            if "Serie_A" in url:
                raise rd.LeagueError("blocked")
            if "githubusercontent" in url:
                raise rd.LeagueError("offline")
            code = url.split("/")[-2]
            return json.dumps({"players": league(sizes[code])})

        previous = [{"name": "Old", "team": "Inter", "league": "Serie A", "minutes": 90}]
        rows, meta, problems = rd.build(2026, previous, {"Serie A": {"updated": "2026-10-06"}}, get)
        self.assertEqual(meta["Serie A"]["status"], "stale")
        self.assertEqual(meta["Serie A"]["updated"], "2026-10-06")
        self.assertIn(previous[0], rows)
        self.assertEqual(meta["La Liga"]["status"], "ok")
        self.assertEqual(meta["Premier League"]["source"], "understat")  # FPL extras unavailable
        self.assertEqual(len(problems), 1)


if __name__ == "__main__":
    unittest.main()
