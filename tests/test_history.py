import tempfile
import unittest
import uuid

from doggychat.history import (
    ChatHistory,
    MAX_HISTORY_ENTRIES,
    default_history_path,
    history_path_for_port,
)


class ChatHistoryTests(unittest.TestCase):
    def test_non_default_ports_use_separate_history_files(self):
        default_port = 48652

        self.assertEqual(
            history_path_for_port(default_port, default_port),
            default_history_path(),
        )
        self.assertEqual(
            history_path_for_port(48653, default_port),
            default_history_path().with_name("history-48653.json"),
        )

    def test_history_persists_identity_and_deduplicates_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/history.json"
            history = ChatHistory(path)
            identity_id = history.identity_id
            entry = history.new_entry(
                uuid.uuid4().hex,
                identity_id,
                "Buddy",
                "Hello",
            )

            self.assertTrue(history.add(entry))
            self.assertFalse(history.add(entry))

            loaded = ChatHistory(path)
            self.assertEqual(loaded.identity_id, identity_id)
            self.assertEqual(loaded.entries(), [entry])

    def test_history_keeps_only_recent_five_hundred_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            history = ChatHistory(f"{directory}/history.json")
            for index in range(MAX_HISTORY_ENTRIES + 1):
                history.add(
                    history.new_entry(
                        uuid.uuid4().hex,
                        history.identity_id,
                        "Buddy",
                        f"Message {index}",
                    )
                )

            records = history.entries()
            self.assertEqual(len(records), MAX_HISTORY_ENTRIES)
            self.assertEqual(records[0]["text"], "Message 1")
            self.assertEqual(records[-1]["text"], "Message 500")

    def test_replace_updates_room_history_and_clear_preserves_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/history.json"
            history = ChatHistory(path)
            identity_id = history.identity_id
            old_entry = history.new_entry(
                uuid.uuid4().hex,
                identity_id,
                "Old room",
                "Old message",
            )
            new_entry = history.new_entry(
                uuid.uuid4().hex,
                identity_id,
                "New room",
                "New message",
            )
            history.add(old_entry)

            history.replace([new_entry, new_entry])

            self.assertEqual(history.entries(), [new_entry])
            self.assertEqual(ChatHistory(path).entries(), [new_entry])
            history.clear()
            self.assertEqual(history.entries(), [])
            self.assertEqual(ChatHistory(path).identity_id, identity_id)


if __name__ == "__main__":
    unittest.main()
