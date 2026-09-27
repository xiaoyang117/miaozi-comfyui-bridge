import tempfile
import unittest
from pathlib import Path

from store import Store


class StoreContextTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "sessions.db")
        self.session = self.store.create_session()["id"]

    def test_history_uses_latest_pairs_after_500_messages(self):
        for index in range(251):
            self.store.add_message(self.session, "user", content=f"user-{index}")
            self.store.add_message(self.session, "assistant", content=f"prompt-{index}")

        messages = self.store.list_messages(self.session)
        self.assertEqual(len(messages), 500)
        self.assertEqual(messages[0]["content"], "user-1")
        self.assertEqual(messages[-1]["content"], "prompt-250")
        self.assertEqual(self.store.context_pairs(self.session, 2), [
            ("user-249", "prompt-249"), ("user-250", "prompt-250"),
        ])

    def test_only_last_assistant_character_is_reused(self):
        self.store.add_message(self.session, "assistant",
                               character="角色: shiroko_(blue_archive)\n作品: blue_archive")
        self.assertEqual(self.store.last_assistant_character_id(self.session),
                         "shiroko_(blue_archive)")
        self.store.add_message(self.session, "assistant",
                               meta={"character_id": "hatsune_miku"})
        self.assertEqual(self.store.last_assistant_character_id(self.session),
                         "hatsune_miku")
        self.store.add_message(self.session, "assistant",
                               meta={"character_ids": ["shiroko", "hatsune_miku"],
                                     "character_id": "shiroko"})
        self.assertEqual(self.store.last_assistant_character_ids(self.session),
                         ["shiroko", "hatsune_miku"])
        self.store.add_message(self.session, "assistant")
        self.assertEqual(self.store.last_assistant_character_id(self.session), "")
        self.assertEqual(self.store.last_assistant_character_ids(self.session), [])


if __name__ == "__main__":
    unittest.main()
