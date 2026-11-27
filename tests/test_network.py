import os
import queue
import tempfile
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from doggychat.history import ChatHistory
from doggychat.network import (
    ChatNetwork,
    create_listener,
    encode_message,
)
from doggychat.security import TLSIdentity, TrustStore


class ChatNetworkTests(unittest.TestCase):
    def test_encode_message_validates_size(self):
        with self.assertRaises(ValueError):
            encode_message("Buddy", "x" * 2001)
        with self.assertRaises(ValueError):
            encode_message(" " * 3, "hello")
        self.assertIn(b'"text":"hello"', encode_message(" Buddy ", "hello"))

    def test_users_are_listed_host_first_then_join_order(self):
        network = ChatNetwork(
            port=0,
            bind_host="127.0.0.1",
            nickname="Host",
        )
        for nickname, order in (("Second", 2), ("First", 1)):
            user_id = uuid.uuid4().hex
            member = {
                "id": user_id,
                "nickname": nickname,
                "ip": "127.0.0.1",
                "port": 48652,
                "order": order,
                "revision": order,
            }
            network._add_member(member)
            network._set_user(user_id, nickname, "127.0.0.1", order)

        self.assertEqual(
            [user["nickname"] for user in network.get_users()],
            ["Host", "First", "Second"],
        )
        self.assertEqual(network.get_member_count(), 3)

    def test_network_starts_on_a_reserved_random_port(self):
        listener = create_listener(0, "127.0.0.1")
        port = listener.getsockname()[1]
        network = ChatNetwork(
            port=port,
            bind_host="127.0.0.1",
            nickname="Host",
        )

        try:
            network.start(listener)
            self.assertEqual(network.port, port)
        finally:
            network.close()

    def test_two_instances_exchange_message(self):
        host = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Host")
        guest = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Guest")
        host.start()
        guest.start()
        try:
            guest.connect_to("127.0.0.1", host.port)
            self._wait_for(host.events, "join_request")
            self.assertTrue(host.control_join("127.0.0.1", True)[0])
            self._wait_for(guest.events, "connected")
            self._wait_for(host.events, "connected")

            self.assertEqual(guest.send("Buddy", "Hello!"), 1)
            event, data = self._wait_for(host.events, "message")
            self.assertEqual(event, "message")
            self.assertEqual(data[1]["nickname"], "Buddy")
            self.assertEqual(data[1]["text"], "Hello!")
        finally:
            guest.close()
            host.close()

    def test_tofu_rejects_a_changed_peer_certificate(self):
        with tempfile.TemporaryDirectory() as directory:
            host_id = uuid.uuid4().hex
            guest_id = uuid.uuid4().hex
            old_identity = TLSIdentity(
                host_id,
                f"{directory}/old.crt",
                f"{directory}/old.key",
            )
            replacement_identity = TLSIdentity(
                host_id,
                f"{directory}/new.crt",
                f"{directory}/new.key",
            )
            guest_identity = TLSIdentity(
                guest_id,
                f"{directory}/guest.crt",
                f"{directory}/guest.key",
            )
            trust_store = TrustStore(f"{directory}/trusted.json")
            self.assertEqual(
                trust_store.trust(host_id, old_identity.fingerprint),
                "new",
            )
            host = ChatNetwork(
                port=0,
                bind_host="127.0.0.1",
                identity_id=host_id,
                nickname="Host",
                tls_identity=replacement_identity,
            )
            guest = ChatNetwork(
                port=0,
                bind_host="127.0.0.1",
                identity_id=guest_id,
                nickname="Guest",
                tls_identity=guest_identity,
                trust_store=trust_store,
            )
            host.start()
            guest.start()
            try:
                guest.connect_to("127.0.0.1", host.port)
                event = self._wait_for(guest.events, "connection_failed")
                self.assertIn("fingerprint changed", event[1])
                self.assertEqual(guest.get_member_count(), 1)
            finally:
                guest.close()
                host.close()

    def test_messages_are_relayed_once_across_connected_peers(self):
        host = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Host")
        first_guest = ChatNetwork(
            port=0, bind_host="127.0.0.1", nickname="Lisa"
        )
        second_guest = ChatNetwork(
            port=0, bind_host="127.0.0.1", nickname="Mouchen"
        )
        networks = (host, first_guest, second_guest)
        for network in networks:
            network.start()
        try:
            first_guest.connect_to("127.0.0.1", host.port)
            self._wait_for(host.events, "join_request")
            self.assertTrue(host.control_join("127.0.0.1", True)[0])
            self._wait_for(first_guest.events, "connected")

            second_guest.connect_to("127.0.0.1", host.port)
            self._wait_for(host.events, "join_request")
            self.assertTrue(host.control_join("127.0.0.1", True)[0])
            self._wait_for(second_guest.events, "connected")

            second_guest.connect_to("127.0.0.1", first_guest.port)
            self._wait_for(first_guest.events, "connected")
            self._wait_for(second_guest.events, "connected")

            self.assertEqual(first_guest.send("Buddy", "Group hello!"), 2)
            host_record = self._wait_for(host.events, "message")[1][1]
            self.assertEqual(
                (host_record["nickname"], host_record["text"]),
                ("Buddy", "Group hello!"),
            )
            guest_record = self._wait_for(second_guest.events, "message")[1][1]
            self.assertEqual(
                (guest_record["nickname"], guest_record["text"]),
                ("Buddy", "Group hello!"),
            )
            for network in networks:
                self._wait_for_users(network, 3)
                self.assertEqual(
                    [user["nickname"] for user in network.get_users()],
                    ["Host", "Lisa", "Mouchen"],
                )
                self._wait_for_room_owner(network, host.identity_id)
            with self.assertRaises(queue.Empty):
                self._wait_for(first_guest.events, "message", timeout=0.1)
            with self.assertRaises(queue.Empty):
                self._wait_for(second_guest.events, "message", timeout=0.1)
        finally:
            for network in networks:
                network.close()

    def test_new_peer_receives_saved_history(self):
        with tempfile.TemporaryDirectory() as directory:
            host_history = ChatHistory(f"{directory}/host.json")
            guest_history = ChatHistory(f"{directory}/guest.json")
            old_message = host_history.new_entry(
                uuid.uuid4().hex,
                host_history.identity_id,
                "Lisa",
                "Earlier message",
            )
            host_history.add(old_message)
            host = ChatNetwork(
                port=0,
                bind_host="127.0.0.1",
                identity_id=host_history.identity_id,
                nickname="Lisa",
                history_provider=host_history.entries,
            )
            guest = ChatNetwork(
                port=0,
                bind_host="127.0.0.1",
                identity_id=guest_history.identity_id,
                nickname="Mouchen",
                history_provider=guest_history.entries,
            )
            host.start()
            guest.start()
            try:
                guest.connect_to("127.0.0.1", host.port)
                self._wait_for(host.events, "join_request")
                self.assertTrue(host.control_join("127.0.0.1", True)[0])
                self._wait_for(guest.events, "connected")
                event, received = self._wait_for(guest.events, "history_batch")
                self.assertEqual(event, "history_batch")
                self.assertEqual(received, [old_message])
                guest_history.add_many(received)
                self.assertEqual(guest_history.entries(), [old_message])
            finally:
                guest.close()
                host.close()

    def test_room_rejects_case_insensitive_duplicate_nickname(self):
        host = ChatNetwork(
                port=0,
                bind_host="127.0.0.1",
                nickname="Host",
        )
        guest = ChatNetwork(
                port=0,
                bind_host="127.0.0.1",
                nickname="hOsT",
        )
        host.start()
        guest.start()
        try:
                guest.connect_to("127.0.0.1", host.port)

                rejected = self._wait_for(guest.events, "connection_failed")
                host_notice = self._wait_for(host.events, "duplicate_nickname_rejected")
                self.assertIn("already in use", rejected[1])
                self.assertEqual(host_notice[1][0], "hOsT")
                self.assertEqual(host.get_member_count(), 1)
                self.assertEqual(guest.get_member_count(), 1)
        finally:
                guest.close()
                host.close()

    def test_existing_nickname_conflict_blocks_sending_until_renamed(self):
        network = ChatNetwork(
                port=0,
                bind_host="127.0.0.1",
                nickname="Mouchen",
        )
        network._add_member(
                {
                    "id": uuid.uuid4().hex,
                    "nickname": "mOuChEn",
                    "ip": "127.0.0.1",
                    "port": 48652,
                    "order": 1,
                    "revision": 1,
                }
        )

        self.assertEqual(
                network.events.get_nowait(),
                ("nickname_conflict", "Mouchen"),
        )
        with self.assertRaisesRegex(ValueError, "rename before sending"):
                network.send("Mouchen", "Blocked")

        success, error = network.update_nickname("mouchen")
        self.assertFalse(success)
        self.assertIn("already in use", error)
        self.assertEqual(network.nickname, "Mouchen")

        success, error = network.update_nickname("Buddy")
        self.assertTrue(success)
        self.assertEqual(error, "")
        self.assertEqual(network.nickname, "Buddy")
        self.assertEqual(network.send("Buddy", "Allowed"), 0)

    def test_relayed_join_request_rejects_duplicate_nickname(self):
        host = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Host")
        host._add_member(
            {
                "id": uuid.uuid4().hex,
                "nickname": "Buddy",
                "ip": "127.0.0.1",
                "port": 48652,
                "order": 1,
                "revision": 1,
            }
        )
        peer = SimpleNamespace(admitted=True, user_id=host.identity_id)

        host._handle_join_request(
            {
                "request_id": uuid.uuid4().hex,
                "user_id": uuid.uuid4().hex,
                "nickname": "bUdDy",
                "ip": "127.0.0.2",
                "port": 48652,
            },
            peer,
        )

        self.assertEqual(
            host.events.get_nowait(),
            ("duplicate_nickname_rejected", ("bUdDy", "127.0.0.2")),
        )
        self.assertEqual(host.get_pending_joins(), [])

    def test_denied_guest_is_not_admitted(self):
        host = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Host")
        guest = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Guest")
        original_room = (guest.room_id, guest.room_name)
        host.start()
        guest.start()
        try:
            guest.connect_to("127.0.0.1", host.port)
            self._wait_for(host.events, "join_request")
            self._wait_for(guest.events, "join_pending")
            self.assertEqual((guest.room_id, guest.room_name), original_room)
            self.assertTrue(host.control_join("127.0.0.1", False)[0])
            event, result = self._wait_for(guest.events, "join_result")
            self.assertEqual((event, result[0]), ("join_result", False))
            self.assertEqual((guest.room_id, guest.room_name), original_room)
            self.assertTrue(guest._admitted)
            self.assertEqual(guest.send("Guest", "Blocked"), 0)
            self.assertNotIn(guest.identity_id, {user["id"] for user in host.get_users()})
        finally:
            guest.close()
            host.close()

    def test_approved_guest_adopts_room_snapshot_only_after_approval(self):
        host = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Host")
        guest = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Guest")
        host.set_room_config(room_name="Approved room")
        original_room = (guest.room_id, guest.room_name)
        host.start()
        guest.start()
        try:
            guest.connect_to("127.0.0.1", host.port)
            self._wait_for(host.events, "join_request")
            self._wait_for(guest.events, "join_pending")
            self.assertEqual((guest.room_id, guest.room_name), original_room)

            self.assertTrue(host.control_join("127.0.0.1", True)[0])
            self._wait_for(guest.events, "join_result")
            self.assertEqual(guest.room_id, host.room_id)
            self.assertEqual(guest.room_name, "Approved room")
            self.assertEqual(guest.room_owner_id, host.identity_id)
        finally:
            guest.close()
            host.close()

    def test_join_approval_timeout_restores_the_original_room(self):
        host = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Host")
        guest = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Guest")
        original_room = (guest.room_id, guest.room_name)
        host.start()
        guest.start()
        try:
            with patch("doggychat.network.JOIN_APPROVAL_TIMEOUT", 0.1):
                guest.connect_to("127.0.0.1", host.port)
                self._wait_for(host.events, "join_request")
                self._wait_for(guest.events, "join_pending")
                event, result = self._wait_for(guest.events, "join_result")

            self.assertEqual(event, "join_result")
            self.assertFalse(result[0])
            self.assertIn("timed out", result[2])
            self.assertEqual((guest.room_id, guest.room_name), original_room)
            self.assertTrue(guest._admitted)
        finally:
            guest.close()
            host.close()

    def test_connection_to_own_room_is_rejected(self):
        network = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Host")
        network.start()
        try:
            network.connect_to("127.0.0.1", network.port)
            self.assertEqual(
                self._wait_for(network.events, "error")[1],
                "You cannot connect to your own room.",
            )
            network.room_directory = SimpleNamespace(
                find_room=lambda _room_id: {
                    "room_uuid": network.room_id,
                    "ip": "127.0.0.1",
                    "port": network.port,
                },
                search_rooms=lambda: [],
            )
            network.connect_to_room("1")
            self.assertEqual(
                self._wait_for(network.events, "error")[1],
                "You are already in that room.",
            )
        finally:
            network.close()

    def test_pending_guests_are_queued_and_approved_in_order(self):
        host = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Host")
        first = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="First")
        second = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Second")
        for network in (host, first, second):
            network.start()
        try:
            first.connect_to("127.0.0.1", host.port)
            self._wait_for(host.events, "join_request")
            second.connect_to("127.0.0.1", host.port)
            self._wait_for(host.events, "join_request")

            self.assertEqual(
                [request["nickname"] for request in host.get_pending_joins()],
                ["First", "Second"],
            )
            self.assertTrue(host.control_join("127.0.0.1", True)[0])
            self.assertTrue(host.control_join("127.0.0.1", True)[0])
            self._wait_for(first.events, "join_result")
            self._wait_for(second.events, "join_result")
        finally:
            for network in (host, first, second):
                network.close()

    def test_guest_joining_through_member_requires_room_owner_approval(self):
        host = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Host")
        member = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Member")
        guest = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Guest")
        networks = (host, member, guest)
        for network in networks:
            network.start()
        try:
            member.connect_to("127.0.0.1", host.port)
            self._wait_for(host.events, "join_request")
            self.assertTrue(host.control_join("127.0.0.1", True)[0])
            self._wait_for(member.events, "connected")

            guest.connect_to("127.0.0.1", member.port)
            self._wait_for(host.events, "join_request")
            allowed, error = member.control_join("127.0.0.1", True)
            self.assertFalse(allowed)
            self.assertIn("Only the room host", error)
            self.assertNotIn(
                guest.identity_id,
                {user["id"] for user in host.get_users()},
            )
            self.assertTrue(host.control_join("127.0.0.1", True)[0])
            self._wait_for(guest.events, "join_result")
            self._wait_for(member.events, "connected")
            self._wait_for_users(host, 3)
            self.assertEqual(guest.room_owner_id, host.identity_id)
            self.assertEqual(
                guest.send("Guest", "Relay hello!"),
                1,
            )
            self.assertEqual(
                self._wait_for(host.events, "message")[1][1]["text"],
                "Relay hello!",
            )
        finally:
            for network in networks:
                network.close()

    def test_room_owner_hands_over_to_first_admitted_member(self):
        host = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Host")
        first = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="First")
        second = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Second")
        for network in (host, first, second):
            network.start()
        try:
            for guest in (first, second):
                guest.connect_to("127.0.0.1", host.port)
                self._wait_for(host.events, "join_request")
                self.assertTrue(host.control_join("127.0.0.1", True)[0])
                self._wait_for(guest.events, "connected")
            self._wait_for_users(first, 3)
            self._wait_for_users(second, 3)

            host.close()

            self._wait_for(first.events, "room_owner_changed")
            self._wait_for(second.events, "room_owner_changed")
            self._wait_for(second.events, "connected")
            self.assertEqual(first.room_owner_id, first.identity_id)
            self.assertEqual(second.room_owner_id, first.identity_id)
            self.assertEqual(second.send("Second", "Room continues!"), 1)
            self.assertEqual(
                self._wait_for(first.events, "message")[1][1]["text"],
                "Room continues!",
            )
        finally:
            first.close()
            second.close()

    def test_room_config_changes_reach_members(self):
        host = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Host")
        guest = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Guest")
        host.start()
        guest.start()
        try:
            guest.connect_to("127.0.0.1", host.port)
            self._wait_for(host.events, "join_request")
            self.assertTrue(host.control_join("127.0.0.1", True)[0])
            self._wait_for(guest.events, "join_result")

            self.assertTrue(
                host.set_room_config(
                    room_name="My room name",
                    max_members=20,
                )[0]
            )
            self._wait_for(guest.events, "room_config_changed")
            self.assertEqual(
                guest.get_room_config()["room_name"],
                "My room name",
            )
            self.assertEqual(guest.get_room_config()["max_members"], 20)
        finally:
            guest.close()
            host.close()

    def test_full_room_rejects_guest_and_notifies_host(self):
        host = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Host")
        guest = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Guest")
        self.assertTrue(host.set_room_config(max_members=1)[0])
        host.start()
        guest.start()
        try:
            guest.connect_to("127.0.0.1", host.port)
            host_event = self._wait_for(host.events, "join_capacity")
            guest_event = self._wait_for(guest.events, "connection_failed")
            self.assertEqual(
                host_event[1],
                ("Guest", "127.0.0.1", 1, 1),
            )
            self.assertIn("Room is full (1/1 members)", guest_event[1])
            self.assertEqual(len(host.get_users()), 1)
            self.assertEqual(guest.room_owner_id, guest.identity_id)
        finally:
            guest.close()
            host.close()

    def test_full_room_rejects_guest_connecting_through_a_member(self):
        host = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Host")
        member = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Member")
        guest = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Guest")
        self.assertTrue(host.set_room_config(max_members=2)[0])
        networks = (host, member, guest)
        for network in networks:
            network.start()
        try:
            member.connect_to("127.0.0.1", host.port)
            self._wait_for(host.events, "join_request")
            self.assertTrue(host.control_join("127.0.0.1", True)[0])
            self._wait_for(member.events, "join_result")

            guest.connect_to("127.0.0.1", member.port)
            host_event = self._wait_for(host.events, "join_capacity")
            guest_event = self._wait_for(guest.events, "join_result")
            self.assertEqual(host_event[1][2:], (2, 2))
            self.assertIn("Room is full", guest_event[1][2])
            self.assertEqual(len(host.get_users()), 2)
        finally:
            for network in networks:
                network.close()

    def test_member_must_disconnect_before_connecting_to_another_room(self):
        first_host = ChatNetwork(
            port=0, bind_host="127.0.0.1", nickname="First host"
        )
        second_host = ChatNetwork(
            port=0, bind_host="127.0.0.1", nickname="Second host"
        )
        guest = ChatNetwork(port=0, bind_host="127.0.0.1", nickname="Guest")
        networks = (first_host, second_host, guest)
        for network in networks:
            network.start()
        try:
            guest.connect_to("127.0.0.1", first_host.port)
            self._wait_for(first_host.events, "join_request")
            self.assertTrue(first_host.control_join("127.0.0.1", True)[0])
            self._wait_for(guest.events, "join_result")

            guest.connect_to("127.0.0.1", second_host.port)
            event = self._wait_for(guest.events, "connection_failed")
            self.assertIn("disconnect first", event[1])
            self.assertEqual(second_host.get_pending_joins(), [])

            self.assertTrue(guest.disconnect_from_room()[0])
            guest.connect_to("127.0.0.1", second_host.port)
            self._wait_for(second_host.events, "join_request")
            self.assertTrue(second_host.control_join("127.0.0.1", True)[0])
            self._wait_for(guest.events, "join_result")
            self.assertEqual(guest.room_owner_id, second_host.identity_id)
        finally:
            for network in networks:
                network.close()

    def test_history_moves_to_successor_and_clears_when_last_member_leaves(self):
        with tempfile.TemporaryDirectory() as directory:
            host_history = ChatHistory(f"{directory}/host.json")
            successor_history = ChatHistory(f"{directory}/successor.json")
            old_record = host_history.new_entry(
                uuid.uuid4().hex,
                host_history.identity_id,
                "Host",
                "Room history",
            )
            host_history.add(old_record)
            host = ChatNetwork(
                port=0,
                bind_host="127.0.0.1",
                identity_id=host_history.identity_id,
                nickname="Host",
                history_provider=host_history.entries,
                history_consumer=host_history.replace,
            )
            successor = ChatNetwork(
                port=0,
                bind_host="127.0.0.1",
                identity_id=successor_history.identity_id,
                nickname="Successor",
                history_provider=successor_history.entries,
                history_consumer=successor_history.replace,
            )
            host.start()
            successor.start()
            try:
                successor.connect_to("127.0.0.1", host.port)
                self._wait_for(host.events, "join_request")
                self.assertTrue(host.control_join("127.0.0.1", True)[0])
                self._wait_for(successor.events, "join_result")
                self._wait_for(successor.events, "history_batch")

                host.close()
                self._wait_for(successor.events, "room_owner_changed")
                self.assertEqual(successor_history.entries(), [old_record])
                self.assertEqual(host_history.entries(), [])

                successor.close()
                self.assertEqual(successor_history.entries(), [])
            finally:
                host.close()
                successor.close()

    def test_scp_requires_a_direct_peer(self):
        with tempfile.TemporaryDirectory() as directory:
            source = f"{directory}/source.txt"
            with open(source, "wb") as file_handle:
                file_handle.write(b"content")
            network = ChatNetwork(
                port=0,
                bind_host="127.0.0.1",
                nickname="Guest",
            )

            success, error = network.request_file_transfer("Missing", source)

            self.assertFalse(success)
            self.assertIn("No room member named", error)
            self.assertIsNone(network._scp_transfer)

    def test_scp_requires_acceptance_and_overwrites_only_after_completion(self):
        with tempfile.TemporaryDirectory() as sender_directory, tempfile.TemporaryDirectory() as receiver_directory:
            source = f"{sender_directory}/shared file.bin"
            destination = f"{receiver_directory}/shared file.bin"
            payload = bytes(range(256)) * 100
            with open(source, "wb") as file_handle:
                file_handle.write(payload)
            with open(destination, "wb") as file_handle:
                file_handle.write(b"old contents")

            host = ChatNetwork(
                port=0,
                bind_host="127.0.0.1",
                nickname="Host",
                file_directory=receiver_directory,
            )
            guest = ChatNetwork(
                port=0,
                bind_host="127.0.0.1",
                nickname="Guest",
                file_directory=sender_directory,
            )
            host.start()
            guest.start()
            try:
                guest.connect_to("127.0.0.1", host.port)
                self._wait_for(host.events, "join_request")
                self.assertTrue(host.control_join("127.0.0.1", True)[0])
                self._wait_for(guest.events, "join_result")

                self.assertTrue(
                    guest.request_file_transfer("Host", source)[0]
                )
                self._wait_for(host.events, "scp_request")
                with open(destination, "rb") as file_handle:
                    self.assertEqual(file_handle.read(), b"old contents")

                self.assertTrue(host.accept_file_transfer()[0])
                self._wait_for(host.events, "scp_received")
                self._wait_for(guest.events, "scp_sent")

                with open(destination, "rb") as file_handle:
                    self.assertEqual(file_handle.read(), payload)
                self.assertEqual(
                    [
                        name
                        for name in os.listdir(receiver_directory)
                        if name.endswith(".part")
                    ],
                    [],
                )
            finally:
                guest.close()
                host.close()

    def test_scp_cancellation_releases_both_peers(self):
        with tempfile.TemporaryDirectory() as directory:
            source = f"{directory}/source.txt"
            with open(source, "wb") as file_handle:
                file_handle.write(b"content")
            host = ChatNetwork(
                port=0,
                bind_host="127.0.0.1",
                nickname="Host",
                file_directory=directory,
            )
            guest = ChatNetwork(
                port=0,
                bind_host="127.0.0.1",
                nickname="Guest",
                file_directory=directory,
            )
            host.start()
            guest.start()
            try:
                guest.connect_to("127.0.0.1", host.port)
                self._wait_for(host.events, "join_request")
                self.assertTrue(host.control_join("127.0.0.1", True)[0])
                self._wait_for(guest.events, "join_result")
                self.assertTrue(
                    guest.request_file_transfer("Host", source)[0]
                )
                self._wait_for(host.events, "scp_request")

                self.assertTrue(guest.cancel_file_transfer()[0])
                self._wait_for(host.events, "scp_cancelled")
                self.assertIsNone(guest._scp_transfer)
                self.assertIsNone(host._scp_transfer)
            finally:
                guest.close()
                host.close()

    def test_scp_pending_request_expires_after_five_minutes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = f"{directory}/source.txt"
            with open(source, "wb") as file_handle:
                file_handle.write(b"content")
            host = ChatNetwork(
                port=0,
                bind_host="127.0.0.1",
                nickname="Host",
                file_directory=directory,
            )
            guest = ChatNetwork(
                port=0,
                bind_host="127.0.0.1",
                nickname="Guest",
                file_directory=directory,
            )
            host.start()
            guest.start()
            try:
                guest.connect_to("127.0.0.1", host.port)
                self._wait_for(host.events, "join_request")
                self.assertTrue(host.control_join("127.0.0.1", True)[0])
                self._wait_for(guest.events, "join_result")
                self.assertTrue(
                    guest.request_file_transfer("Host", source)[0]
                )
                self._wait_for(host.events, "scp_request")
                with host._scp_lock:
                    host._scp_transfer["last_activity"] = (
                        time.monotonic() - 5 * 60 - 1
                    )

                success, error = host.accept_file_transfer()

                self.assertFalse(success)
                self.assertIn("expired", error)
                self.assertIsNone(host._scp_transfer)
                self._wait_for(host.events, "scp_timeout")
                self._wait_for(guest.events, "scp_cancelled")
                self.assertIsNone(guest._scp_transfer)
            finally:
                guest.close()
                host.close()

    def test_scp_all_delivers_to_each_member_who_accepts(self):
        with tempfile.TemporaryDirectory() as sender_directory, tempfile.TemporaryDirectory() as host_directory, tempfile.TemporaryDirectory() as other_directory:
            source = f"{sender_directory}/shared.bin"
            payload = b"shared file" * 2000
            with open(source, "wb") as file_handle:
                file_handle.write(payload)
            host = ChatNetwork(
                port=0,
                bind_host="127.0.0.1",
                nickname="Host",
                file_directory=host_directory,
            )
            sender = ChatNetwork(
                port=0,
                bind_host="127.0.0.1",
                nickname="Sender",
                file_directory=sender_directory,
            )
            other = ChatNetwork(
                port=0,
                bind_host="127.0.0.1",
                nickname="Other",
                file_directory=other_directory,
            )
            networks = (host, sender, other)
            for network in networks:
                network.start()
            try:
                for guest in (sender, other):
                    guest.connect_to("127.0.0.1", host.port)
                    self._wait_for(host.events, "join_request")
                    self.assertTrue(host.control_join("127.0.0.1", True)[0])
                    self._wait_for(guest.events, "join_result")
                for network in networks:
                    self._wait_for_users(network, 3)

                self.assertTrue(sender.request_file_transfer("all", source)[0])
                self._wait_for(host.events, "scp_request")
                self._wait_for(other.events, "scp_request")
                self.assertTrue(host.accept_file_transfer()[0])
                self.assertTrue(other.accept_file_transfer()[0])
                self._wait_for(host.events, "scp_received")
                self._wait_for(other.events, "scp_received")
                self._wait_for(sender.events, "scp_sent")
                self._wait_for(sender.events, "scp_sent")

                for directory in (host_directory, other_directory):
                    with open(f"{directory}/shared.bin", "rb") as file_handle:
                        self.assertEqual(file_handle.read(), payload)
            finally:
                for network in networks:
                    network.close()

    @staticmethod
    def _wait_for(events, expected, timeout=2):
        while True:
            event = events.get(timeout=timeout)
            if event[0] == expected:
                return event

    @staticmethod
    def _wait_for_users(network, count, timeout=2):
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(network.get_users()) >= count:
                return
            time.sleep(0.01)
        raise AssertionError(f"Expected {count} users, got {network.get_users()!r}")

    @staticmethod
    def _wait_for_room_owner(network, owner_id, timeout=2):
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if network.room_owner_id == owner_id:
                return
            time.sleep(0.01)
        raise AssertionError(
            f"Expected room owner {owner_id}, got {network.room_owner_id}"
        )


if __name__ == "__main__":
    unittest.main()
