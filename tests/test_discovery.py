import queue
import unittest
import uuid

from doggychat.discovery import RoomDirectory


class FakeNetwork:
    def __init__(self):
        self.events = queue.Queue()
        self.identity_id = uuid.uuid4().hex
        self.room_id = uuid.uuid4().hex
        self.room_owner_id = self.identity_id
        self.room_name = "Test room"

    def get_room_config(self):
        return {"room_name": self.room_name}

    def get_room_endpoint(self):
        return "192.168.1.10", 48652


class RoomDirectoryTests(unittest.TestCase):
    def test_search_hides_the_current_room_but_keeps_other_rooms(self):
        network = FakeNetwork()
        directory = RoomDirectory(network)
        directory._sync_local_room()

        self.assertEqual(directory.search_rooms(), [])
        self.assertIsNone(directory.find_room("1"))

        remote_room_id = "f" * 32
        directory._record_announcement(
            {
                "type": "announce",
                "slot": 2,
                "room_uuid": remote_room_id,
                "owner_id": "1" * 32,
                "room_name": "Remote room",
                "ip": "192.168.1.11",
                "port": 48653,
            },
            "192.168.1.11",
        )

        self.assertEqual(
            [room["room_name"] for room in directory.search_rooms()],
            ["Remote room"],
        )

    def test_local_room_uses_first_free_id(self):
        network = FakeNetwork()
        directory = RoomDirectory(network)

        directory._sync_local_room()

        self.assertEqual(directory.get_local_room_id(), "id1")
        self.assertEqual(directory.get_room_id(network.room_id), "id1")
        self.assertTrue(network.events.empty())

    def test_startup_uses_free_id_without_publishing_a_provisional_id(self):
        network = FakeNetwork()
        network.room_id = "f" * 32
        directory = RoomDirectory(network)
        directory._record_announcement(
            {
                "type": "announce",
                "slot": 1,
                "room_uuid": "0" * 32,
                "owner_id": "1" * 32,
                "room_name": "Existing room",
                "ip": "192.168.1.11",
                "port": 48653,
            },
            "192.168.1.11",
        )

        directory._sync_local_room()

        self.assertEqual(directory.get_local_room_id(), "id2")
        self.assertTrue(network.events.empty())
        self.assertEqual(directory._finish_startup(), "id2")
        self.assertEqual(
            network.events.get_nowait(),
            ("room_directory_id_changed", "id2"),
        )

    def test_same_id_conflict_is_resolved_and_room_limit_is_50(self):
        network = FakeNetwork()
        network.room_id = "f" * 32
        directory = RoomDirectory(network)
        directory._initializing = False
        directory._sync_local_room()

        directory._record_announcement(
            {
                "type": "announce",
                "slot": 1,
                "room_uuid": "0" * 32,
                "owner_id": "1" * 32,
                "room_name": "Earlier room",
                "ip": "192.168.1.11",
                "port": 48653,
            },
            "192.168.1.11",
        )
        directory._sync_local_room()

        self.assertEqual(directory.get_local_room_id(), "id2")
        self.assertEqual(directory.find_room("id1")["room_name"], "Earlier room")
        self.assertEqual(
            directory.find_room("1-Earlier room")["room_name"],
            "Earlier room",
        )
        self.assertIsNone(directory.find_room("2"))
        self.assertEqual(
            network.events.get_nowait(),
            ("room_directory_id_changed", "id1"),
        )
        self.assertEqual(
            network.events.get_nowait(),
            ("room_directory_id_changed", "id2"),
        )

    def test_simultaneous_room_id_conflict_converges_on_distinct_ids(self):
        first_network = FakeNetwork()
        second_network = FakeNetwork()
        first_network.room_id = "0" * 32
        second_network.room_id = "f" * 32
        first = RoomDirectory(first_network)
        second = RoomDirectory(second_network)
        first._sync_local_room()
        second._sync_local_room()
        first._initializing = False
        second._initializing = False

        first_announcement = {
            "type": "announce",
            "slot": 1,
            "room_uuid": first_network.room_id,
            "owner_id": first_network.identity_id,
            "room_name": "First room",
            "ip": "192.168.1.10",
            "port": 48652,
        }
        second_announcement = {
            "type": "announce",
            "slot": 1,
            "room_uuid": second_network.room_id,
            "owner_id": second_network.identity_id,
            "room_name": "Second room",
            "ip": "192.168.1.11",
            "port": 48653,
        }

        second._record_announcement(first_announcement, "192.168.1.10")
        second._sync_local_room()
        first._record_announcement(second_announcement, "192.168.1.11")
        first._record_announcement(
            {**second_announcement, "slot": 2},
            "192.168.1.11",
        )

        self.assertEqual(first.get_local_room_id(), "id1")
        self.assertEqual(second.get_local_room_id(), "id2")
        self.assertEqual(
            {room["room_uuid"] for room in first.search_rooms()},
            {second_network.room_id},
        )
        self.assertEqual(
            {room["room_uuid"] for room in second.search_rooms()},
            {first_network.room_id},
        )

    def test_invalid_room_advertisements_are_ignored(self):
        directory = RoomDirectory(FakeNetwork())

        directory._record_announcement(
            {
                "type": "announce",
                "slot": 51,
                "room_uuid": uuid.uuid4().hex,
                "owner_id": uuid.uuid4().hex,
                "room_name": "Out of range",
                "ip": "192.168.1.12",
                "port": 48652,
            },
            "192.168.1.12",
        )

        self.assertEqual(directory.search_rooms(), [])


if __name__ == "__main__":
    unittest.main()
