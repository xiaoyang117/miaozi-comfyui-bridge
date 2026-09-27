import sqlite3
import tempfile
import unittest
from pathlib import Path

from character_lookup.build_db import COLS, SCHEMA, parse_row
from character_lookup.query import (best_match, direct_candidates,
                                    find_candidates, format_character,
                                    get_character, lookup)


class CharacterQueryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = str(Path(directory.name) / "characters.db")
        with sqlite3.connect(self.db) as conn:
            conn.executescript(SCHEMA)
            for character, copyright_, trigger, count, tags in [
                ("shiroko_(blue_archive)", "blue_archive",
                 "Shiroko, Blue Archive", 50, "white hair, blue eyes"),
                ("shiroko_(other_series)", "other_series",
                 "Shiroko, Other Series", 500, "black hair"),
                ("rain", "weather_series", "Rain, Weather Series",
                 1000, "shiroko"),
            ]:
                row = parse_row({
                    "character": character, "copyright": copyright_,
                    "trigger": trigger, "count": str(count), "core_tags": tags,
                })
                conn.execute(
                    f"INSERT INTO characters ({','.join(COLS)}) "
                    f"VALUES ({','.join('?' * len(COLS))})",
                    [row[column] for column in COLS])

    def test_series_resolves_same_name_despite_popularity(self):
        matches = find_candidates("shiroko, blue_archive", db_path=self.db)
        self.assertEqual([m["character"] for m in matches],
                         ["shiroko_(blue_archive)"])
        self.assertEqual(direct_candidates("shiroko, blue archive", self.db),
                         matches)

    def test_ambiguous_name_does_not_pick_most_popular(self):
        best, alternatives = best_match("shiroko", db_path=self.db)
        self.assertIsNone(best)
        self.assertEqual(len(alternatives), 2)
        self.assertEqual(lookup("shiroko", db_path=self.db), "")

    def test_wrong_series_and_unrelated_tags_do_not_match(self):
        self.assertEqual(find_candidates("shiroko, missing", db_path=self.db), [])
        self.assertEqual(find_candidates("white hair", db_path=self.db), [])
        self.assertEqual(direct_candidates("画一个 Shiroko", self.db), [])

    def test_explicit_choice_only_injects_selected_character(self):
        selected = get_character("shiroko_(blue_archive)", self.db)
        context = format_character(selected)
        self.assertIn("white hair", context)
        self.assertNotIn("Other Series", context)
        self.assertIsNone(get_character("unknown", self.db))


if __name__ == "__main__":
    unittest.main()
