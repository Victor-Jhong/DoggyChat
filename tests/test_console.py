import io
import os
import queue
import tempfile
import unittest
import uuid
from unittest.mock import patch

from doggychat.console import (
    _ChatLine,
    _InputState,
    _SystemMessage,
    _drain_events,
    _format_timestamp,
    _format_event,
    _handle_line,
    _record_to_display,
    _render,
    run_console,
)
from doggychat.history import ChatHistory
from doggychat.network import DEFAULT_CONNECT_IP, DEFAULT_PORT
from doggychat.version import RELEASE_TIME, VERSION


class FakeNetwork:
    def __init__(self):
        self.port = 48652
        self.events = queue.Queue()
        self.connections = []
        self.messages = []
        self.nickname = "Guest"
        self.identity_id = uuid.uuid4().hex
        self.room_owner_id = uuid.uuid4().hex
        self.room_id = uuid.uuid4().hex
        self.room_name = f"DoggyChat {VERSION} - Unknow chat room"
        self.max_members = 15
        self.room_endpoint = ("10.10.11.11", 48653)
        self.room_directory_id = "id1"
        self.rooms = [
            {
                "id": "id1",
                "room_name": self.room_name,
                "ip": "10.10.11.11",
                "port": 48653,
            },
            {
                "id": "id2",
                "room_name": "Second room",
                "ip": "10.10.11.12",
                "port": 48654,
            },
        ]
        self.pending_joins = []
        self.control_calls = []
        self.config_calls = []
        self.disconnected = False
        self.file_transfer_calls = []

    def connect_to(self, address, port):
        self.connections.append((address, port))

    def connect_to_room(self, room_id):
        self.connections.append((room_id,))

    def search_rooms(self):
        return list(self.rooms)

    def get_room_directory_id(self):
        return self.room_directory_id

    def disconnect_from_room(self):
        self.disconnected = True
        return True, ""

    def update_nickname(self, nickname):
        self.nickname = nickname
        return True, ""

    def get_users(self):
        return [
            {
                "id": self.identity_id,
                "nickname": self.nickname,
                "ip": "10.10.11.22",
                "order": 2,
            },
            {
                "id": uuid.uuid4().hex,
                "nickname": "Tiger",
                "ip": "10.10.11.16",
                "order": 1,
            },
            {
                "id": self.room_owner_id,
                "nickname": "Lisa",
                "ip": "10.10.11.11",
                "order": 0,
            },
        ]

    def get_member_count(self):
        return 3

    def get_room_endpoint(self):
        return self.room_endpoint

    def get_pending_joins(self):
        return list(self.pending_joins)

    def get_room_config(self):
        return {
            "room_name": self.room_name,
            "max_members": self.max_members,
        }

    def set_room_config(self, room_name=None, max_members=None):
        self.config_calls.append((room_name, max_members))
        if self.room_owner_id != self.identity_id:
            return False, "Only the room host can change room settings."
        if room_name is not None:
            self.room_name = room_name
        if max_members is not None:
            self.max_members = max_members
        return True, ""

    def control_join(self, address, accepted):
        self.control_calls.append((address, accepted))
        if self.room_owner_id != self.identity_id:
            return False, "Only the room host can control join requests."
        return True, ""

    def send(self, nickname, text):
        self.messages.append((nickname, text))
        self.events.put(
            (
                "sent",
                {
                    "id": uuid.uuid4().hex,
                    "sender_id": self.identity_id,
                    "nickname": nickname,
                    "text": text,
                    "timestamp": "2026-11-26T00:00:00+00:00",
                },
            )
        )
        return 1

    def request_file_transfer(self, address, path):
        self.file_transfer_calls.append(("request", address, path))
        return True, ""

    def accept_file_transfer(self):
        self.file_transfer_calls.append(("accept",))
        return True, ""

    def cancel_file_transfer(self):
        self.file_transfer_calls.append(("cancel",))
        return True, ""


class FakeNotifier:
    def __init__(self):
        self.notifications = []

    def notify(self, title, body):
        self.notifications.append((title, body))
        return None

    def close(self):
        pass


class DoggyChatConsoleTests(unittest.TestCase):
    def test_commands_connect_rename_and_send(self):
        network = FakeNetwork()
        network.events.put(
            (
                "message",
                (
                    ("127.0.0.1", 48652),
                    {
                        "id": uuid.uuid4().hex,
                        "sender_id": uuid.uuid4().hex,
                        "nickname": "Other",
                        "text": "Hi!",
                        "timestamp": "2026-11-26T00:00:00+00:00",
                    },
                ),
            )
        )
        output = io.StringIO()
        notifier = FakeNotifier()
        run_console(
            network,
            "Guest",
            input_stream=io.StringIO(
                "/control connect 192.168.1.12\n"
                "/control user rename Buddy\n"
                "Hello there\n"
                "/quit\n"
            ),
            output_stream=output,
            desktop_notifier=notifier,
        )

        self.assertEqual(network.connections, [("192.168.1.12", 48652)])
        self.assertEqual(network.messages, [("Buddy", "Hello there")])
        self.assertIn("- Other: Hi!", output.getvalue())
        self.assertIn(
            f"{_format_timestamp('2026-11-26T00:00:00+00:00')} - Other: Hi!",
            output.getvalue(),
        )
        self.assertIn("* Buddy: Hello there", output.getvalue())
        self.assertIn(">> Buddy: ", output.getvalue())
        self.assertIn(f"Released {RELEASE_TIME}", output.getvalue())
        self.assertIn(
            f"[{network.room_name}] Room id: 1 TCP {network.port}",
            output.getvalue(),
        )
        self.assertEqual(
            notifier.notifications,
            [("Message from Other", "Hi!")],
        )

    def test_control_connect_defaults_to_default_ip_and_port(self):
        network = FakeNetwork()
        output = io.StringIO()
        run_console(
            network,
            "Guest",
            input_stream=io.StringIO("/control connect\n/quit\n"),
            output_stream=output,
        )

        self.assertEqual(network.connections, [(DEFAULT_CONNECT_IP, DEFAULT_PORT)])
        self.assertIn(
            f"Connecting to {DEFAULT_CONNECT_IP}:{DEFAULT_PORT}",
            output.getvalue(),
        )

    def test_control_connect_accepts_a_custom_port(self):
        network = FakeNetwork()
        lines = []

        _handle_line(
            "/control connect 192.168.1.12 48653",
            network,
            "Guest",
            lines.append,
        )

        self.assertEqual(network.connections, [("192.168.1.12", 48653)])
        self.assertEqual(lines, ["! Connecting to 192.168.1.12:48653..."])

    def test_control_connect_rejects_invalid_custom_port(self):
        network = FakeNetwork()
        lines = []

        _handle_line(
            "/control connect 192.168.1.12 70000",
            network,
            "Guest",
            lines.append,
        )

        self.assertEqual(network.connections, [])
        self.assertEqual(lines, ["! Port must be between 1 and 65535."])

    def test_own_nickname_is_colored_blue(self):
        network = FakeNetwork()
        output = io.StringIO()
        line = _ChatLine(
            "*",
            "Buddy",
            "Hello",
            "2026-11-26T00:00:00+00:00",
            "blue",
        )
        _render(network, "Buddy", _InputState(), [line], output)

        self.assertIn("* \x1b[94mBuddy\x1b[0m: Hello", output.getvalue())

    def test_room_owner_name_is_colored_red(self):
        network = FakeNetwork()
        line = _ChatLine(
            "-",
            "Lisa",
            "Hello",
            "2026-11-26T00:00:00+00:00",
            "red",
        )
        output = io.StringIO()
        _render(network, "Guest", _InputState(), [line], output)

        self.assertIn("- \x1b[91mLisa\x1b[0m: Hello", output.getvalue())

    def test_non_host_name_is_colored_green(self):
        network = FakeNetwork()
        line = _ChatLine(
            "$",
            "Guest",
            "10.10.11.22",
            color="green",
        )
        output = io.StringIO()
        _render(network, "Guest", _InputState(), [line], output)

        self.assertIn("$\x1b[92mGuest\x1b[0m: 10.10.11.22", output.getvalue())

    def test_other_users_chat_names_are_colored_green(self):
        identity_id = uuid.uuid4().hex
        owner_id = uuid.uuid4().hex
        line = _record_to_display(
            {
                "sender_id": uuid.uuid4().hex,
                "nickname": "Other",
                "text": "Hello",
                "timestamp": "2026-11-26T00:00:00+00:00",
            },
            identity_id,
            owner_id,
        )
        output = io.StringIO()
        _render(FakeNetwork(), "Guest", _InputState(), [line], output)

        self.assertIn("- \x1b[92mOther\x1b[0m: Hello", output.getvalue())

    def test_system_status_prompts_are_colored_yellow(self):
        output = io.StringIO()
        _render(
            FakeNetwork(),
            "Guest",
            _InputState(),
            ["! New user joined the room."],
            output,
        )

        self.assertIn(
            "\x1b[93m! New user joined the room.\x1b[0m",
            output.getvalue(),
        )

    def test_error_prompts_are_colored_red(self):
        output = io.StringIO()
        _render(
            FakeNetwork(),
            "Guest",
            _InputState(),
            ["! Connect directly to that peer before sending a file."],
            output,
        )

        self.assertIn(
            "\x1b[91m! Connect directly to that peer before sending a file.\x1b[0m",
            output.getvalue(),
        )

    def test_who_marks_room_owner_name_red(self):
        network = FakeNetwork()
        lines = []
        _handle_line("/who", network, "Guest", lines.append)

        self.assertEqual(lines[0], "---------------------------")
        self.assertEqual(lines[-1], "---------------------------")
        users = lines[1:-1]
        self.assertEqual(
            [line.nickname for line in users],
            ["Lisa", "Tiger", "Guest"],
        )
        self.assertEqual(
            [(line.nickname, line.color) for line in users],
            [("Lisa", "red"), ("Tiger", "green"), ("Guest", "green")],
        )

    def test_show_lists_current_room_configuration(self):
        network = FakeNetwork()
        output = io.StringIO()
        run_console(
            network,
            "Guest",
            input_stream=io.StringIO("/show\n/quit\n"),
            output_stream=output,
        )

        self.assertIn(f"* Room name: DoggyChat {VERSION} - Unknow chat room", output.getvalue())
        self.assertIn("* Members: 3", output.getvalue())
        self.assertIn("* Max member: 15", output.getvalue())
        self.assertIn("* Max record: 500", output.getvalue())
        self.assertIn("* Room ip: 10.10.11.11", output.getvalue())
        self.assertIn("* Room port: 48653", output.getvalue())
        self.assertIn("* Room id: 1", output.getvalue())
        self.assertIn(
            f"[DoggyChat {VERSION} - Unknow chat room] Room id: 1 "
            f"TCP {network.port}",
            output.getvalue(),
        )
        self.assertGreaterEqual(output.getvalue().count("---------------------------"), 2)

    def test_control_search_lists_rooms_and_connect_accepts_room_selector(self):
        network = FakeNetwork()
        lines = []
        _handle_line("/control search", network, "Guest", lines.append)
        self.assertIn(
            f"* <1>: DoggyChat {VERSION} - Unknow chat room - "
            "10.10.11.11 - 48653",
            lines,
        )
        self.assertIn("* <2>: Second room - 10.10.11.12 - 48654", lines)

        _handle_line("/control connect 2", network, "Guest", lines.append)
        _handle_line(
            "/control connect 2-Second room",
            network,
            "Guest",
            lines.append,
        )
        self.assertEqual(
            network.connections,
            [("2",), ("2-Second room",)],
        )

    def test_control_connect_completes_available_room_ids_in_order(self):
        editor = _InputState()
        network = FakeNetwork()
        editor.text = "/control connect "
        editor.cursor = len(editor.text)
        editor.complete(lambda _message: self.fail("unexpected completion list"), network)
        self.assertEqual(
            editor.text,
            f"/control connect 1-DoggyChat {VERSION} - Unknow chat room ",
        )

        editor.complete(lambda _message: self.fail("unexpected completion list"), network)
        self.assertEqual(editor.text, "/control connect 2-Second room ")
        editor.complete(lambda _message: self.fail("unexpected completion list"), network)
        self.assertEqual(
            editor.text,
            f"/control connect 1-DoggyChat {VERSION} - Unknow chat room ",
        )

    def test_control_connect_completes_room_name_with_spaces(self):
        editor = _InputState()
        network = FakeNetwork()
        editor.text = "/control connect 2-"
        editor.cursor = len(editor.text)
        editor.complete(lambda _message: self.fail("unexpected completion list"), network)
        self.assertEqual(editor.text, "/control connect 2-Second room ")

    def test_scp_tab_completes_other_users_and_tilde_paths(self):
        network = FakeNetwork()
        editor = _InputState()
        suggestions = []
        editor.text = "/scp"
        editor.cursor = len(editor.text)
        editor.complete(suggestions.append, network)
        self.assertEqual(editor.text, "/scp ")
        self.assertEqual(
            suggestions,
            ["! SCP recipients/actions: accept exit all Tiger Lisa"],
        )

        editor.text = "/scp Li"
        editor.cursor = len(editor.text)
        editor.complete(lambda _message: self.fail("unexpected recipient list"), network)
        self.assertEqual(editor.text, "/scp Lisa ")

        editor.text = "/scp Gu"
        editor.cursor = len(editor.text)
        editor.complete(lambda _message: self.fail("unexpected recipient list"), network)
        self.assertEqual(editor.text, "/scp Gu")

        with tempfile.TemporaryDirectory() as home:
            os.mkdir(os.path.join(home, "alpha folder"))
            for name in ("beta file.txt",):
                with open(os.path.join(home, name), "w", encoding="utf-8"):
                    pass
            with patch(
                "doggychat.console.os.path.expanduser",
                side_effect=lambda path: (
                    home
                    if path == "~"
                    else os.path.join(home, path[2:])
                    if path.startswith("~/")
                    else path
                ),
            ):
                editor.text = "/scp Lisa ~/"
                editor.cursor = len(editor.text)
                editor.complete(
                    lambda _message: self.fail("unexpected path list"),
                    network,
                )
                self.assertEqual(editor.text, "/scp Lisa ~/alpha folder/")
                editor.complete(
                    lambda _message: self.fail("unexpected path list"),
                    network,
                )
                self.assertEqual(editor.text, "/scp Lisa ~/beta file.txt ")

    def test_scp_commands_request_accept_and_cancel_transfers(self):
        network = FakeNetwork()
        lines = []

        _handle_line(
            "/scp Lisa /tmp/my file.txt",
            network,
            "Guest",
            lines.append,
        )
        _handle_line(
            "/scp all /tmp/shared file.txt",
            network,
            "Guest",
            lines.append,
        )
        _handle_line("/scp accept", network, "Guest", lines.append)
        _handle_line("/scp exit", network, "Guest", lines.append)

        self.assertEqual(
            network.file_transfer_calls,
            [
                ("request", "Lisa", "/tmp/my file.txt"),
                ("request", "all", "/tmp/shared file.txt"),
                ("accept",),
                ("cancel",),
            ],
        )
        self.assertEqual(
            _format_event("scp_sent", ("Lisa", "my file.txt")),
            "! File sent successfully to Lisa: my file.txt",
        )

    def test_room_name_is_bracketed_in_terminal_header(self):
        output = io.StringIO()
        network = FakeNetwork()
        _render(network, "Guest", _InputState(), [], output)

        self.assertIn(
            f"[DoggyChat {VERSION} - Unknow chat room] Room id: 1 "
            f"TCP {network.port}",
            output.getvalue(),
        )
        self.assertIn(f"Released {RELEASE_TIME}", output.getvalue())
        self.assertGreaterEqual(output.getvalue().count("=" * 58), 2)

    def test_failed_rename_does_not_change_console_nickname(self):
        class ConflictingNetwork(FakeNetwork):
            def update_nickname(self, nickname):
                return False, "Nickname is already in use."

        network = ConflictingNetwork()
        lines = []
        nickname, running = _handle_line(
            "/control user rename Lisa",
            network,
            "Guest",
            lines.append,
        )

        self.assertEqual((nickname, running), ("Guest", True))
        self.assertEqual(lines, ["! Nickname is already in use."])

    def test_input_editor_handles_cursor_history_and_completion(self):
        editor = _InputState()
        editor.text = "/con"
        editor.cursor = len(editor.text)
        editor.complete(lambda _message: self.fail("unexpected completion list"))
        self.assertEqual(editor.text, "/control ")
        self.assertEqual(editor.cursor, len("/control "))

        editor.remember("first message")
        editor.remember("/who")
        editor.text = "draft"
        editor.cursor = len(editor.text)
        editor.previous()
        self.assertEqual(editor.text, "/who")
        editor.previous()
        self.assertEqual(editor.text, "first message")
        editor.next()
        self.assertEqual(editor.text, "/who")
        editor.next()
        self.assertEqual(editor.text, "draft")

        editor.text = "abcd"
        editor.cursor = 2
        from doggychat.console import _handle_key

        _handle_key("LEFT", editor, "Guest", FakeNetwork(), [])
        self.assertEqual(editor.cursor, 1)
        _handle_key("RIGHT", editor, "Guest", FakeNetwork(), [])
        self.assertEqual(editor.cursor, 2)

    def test_manager_approval_and_tab_cycle_name_ip_selectors(self):
        network = FakeNetwork()
        network.room_owner_id = network.identity_id
        network.pending_joins = [
            {"selector": "Lisa-10.0.0.2"},
            {"selector": "Tiger-10.0.0.3"},
        ]
        editor = _InputState()
        editor.text = "/manager a"
        editor.cursor = len(editor.text)
        editor.complete(lambda _message: self.fail("unexpected action list"))
        self.assertEqual(editor.text, "/manager approve ")

        editor.complete(lambda _message: self.fail("unexpected selector list"), network)
        self.assertEqual(editor.text, "/manager approve Lisa-10.0.0.2")
        editor.complete(lambda _message: self.fail("unexpected selector list"), network)
        self.assertEqual(editor.text, "/manager approve Tiger-10.0.0.3")

        lines = []
        _handle_line(
            "/manager denny Tiger-10.0.0.3",
            network,
            "Host",
            lines.append,
        )
        self.assertEqual(network.control_calls, [("Tiger-10.0.0.3", False)])
        self.assertTrue(any("Denied join request" in line for line in lines))

    def test_control_command_is_rejected_for_non_host(self):
        network = FakeNetwork()
        lines = []
        _handle_line(
            "/manager approve Tiger-10.0.0.3",
            network,
            "Guest",
            lines.append,
        )
        self.assertEqual(network.control_calls, [])
        self.assertTrue(any("Only the room host" in line for line in lines))

    def test_manager_completion_is_host_only_and_nested_commands_are_ordered(self):
        network = FakeNetwork()
        editor = _InputState()
        editor.text = "/man"
        editor.cursor = len(editor.text)
        editor.complete(lambda _message: self.fail("unexpected completion list"), network)
        self.assertEqual(editor.text, "/man")

        network.room_owner_id = network.identity_id
        editor.text = "/control u"
        editor.cursor = len(editor.text)
        editor.complete(lambda _message: self.fail("unexpected action list"), network)
        self.assertEqual(editor.text, "/control user ")
        editor.text = "/control user r"
        editor.cursor = len(editor.text)
        editor.complete(lambda _message: self.fail("unexpected rename list"), network)
        self.assertEqual(editor.text, "/control user rename ")

        editor.text = "/manager set m"
        editor.cursor = len(editor.text)
        editor.complete(lambda _message: self.fail("unexpected settings list"), network)
        self.assertEqual(editor.text, "/manager set maxmember ")

    def test_control_commands_rename_disconnect_and_manager_set(self):
        network = FakeNetwork()
        network.room_owner_id = network.identity_id
        lines = []

        nickname, _ = _handle_line(
            "/control user rename New Nick",
            network,
            "Guest",
            lines.append,
        )
        self.assertEqual(nickname, "New Nick")
        self.assertEqual(network.nickname, "New Nick")
        _handle_line("/control disconnect", network, nickname, lines.append)
        self.assertTrue(network.disconnected)

        _handle_line(
            "/manager set roomname My room name",
            network,
            nickname,
            lines.append,
        )
        _handle_line(
            "/manager set maxmember 20",
            network,
            nickname,
            lines.append,
        )
        self.assertEqual(network.config_calls, [("My room name", None), (None, 20)])
        self.assertEqual(network.room_name, "My room name")
        self.assertEqual(network.max_members, 20)

    def test_help_shows_permissions_for_current_user(self):
        network = FakeNetwork()
        lines = []
        _handle_line("/help", network, "Guest", lines.append)
        member_help = "\n".join(lines)
        self.assertIn("/control user rename <name> — any user", member_help)
        self.assertIn("/scp all <file path> — any user", member_help)
        self.assertNotIn("/manager", member_help)

        network.room_owner_id = network.identity_id
        lines.clear()
        _handle_line("/help", network, "Guest", lines.append)
        host_help = "\n".join(lines)
        self.assertIn("/manager approve <name-ip> — host only", host_help)
        self.assertIn("/manager set roomname <name> — host only", host_help)

    def test_startup_tutorial_is_default_color(self):
        network = FakeNetwork()
        network.events.put(("first_run_hint", "id3"))
        lines = []
        self.assertTrue(_drain_events(network, lines.append))
        self.assertTrue(lines)
        self.assertTrue(all(isinstance(line, _SystemMessage) for line in lines))
        self.assertTrue(all(line.color == "default" for line in lines))
        self.assertIn("You now have your own private room, ID is 3.", lines)
        self.assertIn("  Step 1: Search for ID: /control search", lines)

    def test_connection_notifications_use_nicknames(self):
        self.assertEqual(
            _format_event("connected", "Lisa"),
            "! Connected to Lisa.",
        )
        self.assertEqual(
            _format_event("disconnected", ("Lisa", "Disconnected")),
            "! Disconnected: Lisa",
        )

    def test_saved_history_is_shown_when_console_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            history = ChatHistory(f"{directory}/history.json")
            record = history.new_entry(
                uuid.uuid4().hex,
                uuid.uuid4().hex,
                "Lisa",
                "Earlier message",
            )
            history.add(record)
            imported = history.new_entry(
                uuid.uuid4().hex,
                uuid.uuid4().hex,
                "Peer",
                "Shared history",
            )
            network = FakeNetwork()
            network.events.put(("history_batch", [imported]))
            output = io.StringIO()
            run_console(
                network,
                "Mouchen",
                input_stream=io.StringIO("/quit\n"),
                output_stream=output,
                history_store=history,
            )

        self.assertIn("- Lisa: Earlier message", output.getvalue())
        self.assertIn("- Peer: Shared history", output.getvalue())
        self.assertEqual(len(history.entries()), 1)


if __name__ == "__main__":
    unittest.main()
