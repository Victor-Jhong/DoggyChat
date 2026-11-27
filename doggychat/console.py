import codecs
import glob
import os
import queue
import select
import shutil
import sys
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from .network import (
    DEFAULT_CONNECT_IP,
    DEFAULT_PORT,
    MAX_HISTORY_ENTRIES,
    MAX_MESSAGE_LENGTH,
    MAX_NICKNAME_LENGTH,
    MAX_ROOM_MEMBERS,
)
from .notifications import DesktopNotifier
from .version import RELEASE_TIME


COMMANDS = ("/control", "/help", "/manager", "/quit", "/scp", "/show", "/who")
CONTROL_ACTIONS = ("connect", "disconnect", "search", "user")
USER_ACTIONS = ("rename",)
MANAGER_ACTIONS = ("approve", "denny", "set")
MANAGER_SETTINGS = ("maxmember", "roomname")
SCP_ACTIONS = ("accept", "exit")
HEADER_SEPARATOR = "=" * 58


def _room_id_number(room_id):
    if isinstance(room_id, str) and room_id.casefold().startswith("id"):
        return room_id[2:]
    return room_id


def _is_room_selector(value):
    if "-" not in value:
        return False
    room_id = value.split("-", 1)[0].casefold()
    if room_id.startswith("id"):
        room_id = room_id[2:]
    return room_id.isdigit()


@dataclass(frozen=True)
class _ChatLine:
    marker: str
    nickname: str
    text: str
    timestamp: str = ""
    color: str = ""


class _SystemMessage(str):
    def __new__(cls, text, color):
        message = super().__new__(cls, text)
        message.color = color
        return message


class _InputState:
    def __init__(self):
        self.text = ""
        self.cursor = 0
        self.commands = deque(maxlen=500)
        self.history_index = None
        self.draft = ""
        self.scroll_offset = 0
        self._scp_path_cycle = None

    def reset(self):
        self.text = ""
        self.cursor = 0
        self.history_index = None
        self.draft = ""
        self.scroll_offset = 0
        self._scp_path_cycle = None

    def remember(self, line):
        self.commands.append(line)
        self.history_index = None
        self.draft = ""

    def previous(self):
        if not self.commands:
            return
        if self.history_index is None:
            self.draft = self.text
            self.history_index = len(self.commands)
        if self.history_index:
            self.history_index -= 1
            self.text = self.commands[self.history_index]
            self.cursor = len(self.text)

    def next(self):
        if self.history_index is None:
            return
        self.history_index += 1
        if self.history_index >= len(self.commands):
            self.text = self.draft
            self.cursor = len(self.text)
            self.history_index = None
            self.draft = ""
        else:
            self.text = self.commands[self.history_index]
            self.cursor = len(self.text)

    def complete(self, emit, network=None):
        prefix = self.text[: self.cursor]
        if not prefix.startswith("/"):
            return
        if " " not in prefix:
            commands = COMMANDS
            if (
                network is not None
                and network.room_owner_id != network.identity_id
            ):
                commands = tuple(
                    command for command in commands if command != "/manager"
                )
            matches = [
                command for command in commands if command.startswith(prefix)
            ]
            if not matches:
                return
            if len(matches) == 1:
                completed = matches[0]
                if completed in ("/control", "/manager", "/scp"):
                    completed += " "
            else:
                completed = os.path.commonprefix(matches)
                if completed == prefix:
                    emit("! Commands: " + " ".join(matches))
                    return
            self._apply_completion(completed)
            if completed == "/scp ":
                self._complete_scp("", completed, emit, network)
            return

        control_prefix = "/control "
        if prefix.startswith(control_prefix):
            remainder = prefix[len(control_prefix) :]
            self._complete_control(remainder, control_prefix, emit, network)
            return

        manager_prefix = "/manager "
        if prefix.startswith(manager_prefix):
            if network is not None and network.room_owner_id != network.identity_id:
                return
            remainder = prefix[len(manager_prefix) :]
            self._complete_manager(remainder, manager_prefix, emit, network)
            return

        scp_prefix = "/scp "
        if prefix.startswith(scp_prefix):
            remainder = prefix[len(scp_prefix) :]
            self._complete_scp(remainder, scp_prefix, emit, network)

    def _complete_scp(self, remainder, base, emit, network):
        choices = list(SCP_ACTIONS) + ["all"]
        recipients = []
        if network is not None:
            recipients = [
                user["nickname"]
                for user in network.get_users()
                if user["id"] != network.identity_id
            ]
            choices.extend(recipients)

        if remainder.casefold().startswith("all "):
            self._complete_scp_path(base + "all ", remainder[4:])
            return

        target_name = next(
            (
                name
                for name in sorted(recipients, key=len, reverse=True)
                if remainder.casefold().startswith(name.casefold() + " ")
            ),
            None,
        )
        if target_name is not None:
            self._complete_scp_path(
                base + target_name + " ",
                remainder[len(target_name) + 1 :],
            )
            return

        self._complete_word(
            base,
            remainder.rstrip(),
            tuple(dict.fromkeys(choices)),
            emit,
            "! SCP recipients/actions: ",
        )

    def _complete_scp_path(self, base, current):
        cycle = self._scp_path_cycle
        if (
            cycle is not None
            and cycle["base"] == base
            and current in (cycle["selected"], cycle["selected"] + " ")
        ):
            matches = cycle["matches"]
            selected_index = (cycle["index"] + 1) % len(matches)
        else:
            expanded = os.path.expanduser(current)
            home = os.path.expanduser("~")
            matches = []
            for path in sorted(glob.glob(expanded + "*")):
                if path == home:
                    display_path = "~"
                elif path.startswith(home + os.sep):
                    display_path = "~" + path[len(home) :]
                else:
                    display_path = path
                if os.path.isdir(path):
                    display_path += os.sep
                matches.append(display_path)
            if not matches:
                self._scp_path_cycle = None
                return
            selected_index = 0

        selected = matches[selected_index]
        self._apply_completion(
            base + selected + ("" if selected.endswith(os.sep) else " ")
        )
        self._scp_path_cycle = {
            "base": base,
            "matches": matches,
            "index": selected_index,
            "selected": selected,
        }

    def _complete_control(self, remainder, base, emit, network):
        if remainder == "" or " " not in remainder:
            current = remainder.rstrip()
            self._complete_word(
                base,
                current,
                CONTROL_ACTIONS,
                emit,
                "! Control actions: ",
            )
            return
        action, _, tail = remainder.partition(" ")
        if action == "connect":
            current = tail.strip()
            self._complete_room_id(
                base + "connect ", current, emit, network
            )
        elif action == "user":
            if tail == "" or " " not in tail:
                self._complete_word(
                    base + "user ",
                    tail.rstrip(),
                    USER_ACTIONS,
                    emit,
                    "! User actions: ",
                )

    def _complete_manager(self, remainder, base, emit, network):
        if remainder == "" or " " not in remainder:
            self._complete_word(
                base,
                remainder.rstrip(),
                MANAGER_ACTIONS,
                emit,
                "! Manager actions: ",
            )
            return
        action, _, tail = remainder.partition(" ")
        if action in ("approve", "denny"):
            self._complete_pending_ip(
                base + action + " ", tail, emit, network
            )
        elif action == "set" and (tail == "" or " " not in tail):
            self._complete_word(
                base + "set ",
                tail.rstrip(),
                MANAGER_SETTINGS,
                emit,
                "! Settings: ",
            )

    def _complete_word(self, base, current, choices, emit, label):
        matches = [choice for choice in choices if choice.startswith(current)]
        if not matches:
            return
        if len(matches) == 1:
            self._apply_completion(base + matches[0] + " ")
            return
        completed = os.path.commonprefix(matches)
        if completed == current:
            emit(label + " ".join(matches))
        else:
            self._apply_completion(base + completed)

    def _complete_pending_ip(self, base, current, emit, network):
        if network is None:
            return
        pending_selectors = [
            request["selector"] for request in network.get_pending_joins()
        ]
        if not pending_selectors:
            emit("! No pending join requests.")
            return
        if current in pending_selectors:
            selected = pending_selectors[
                (pending_selectors.index(current) + 1) % len(pending_selectors)
            ]
        else:
            selected = next(
                (
                    selector
                    for selector in pending_selectors
                    if selector.startswith(current)
                ),
                pending_selectors[0],
            )
        self._apply_completion(base + selected)

    def _complete_room_id(self, base, current, emit, network):
        if network is None:
            return
        rooms = network.search_rooms()
        selectors = [
            f'{_room_id_number(room["id"])}-{room["room_name"]}'
            for room in rooms
        ]
        room_ids = [str(_room_id_number(room["id"])) for room in rooms]
        if not selectors:
            emit("! No available rooms were found.")
            return
        if current.casefold().startswith("id"):
            current = current[2:]
        normalized = current.casefold()
        normalized_selectors = [selector.casefold() for selector in selectors]
        if normalized in normalized_selectors:
            selected = selectors[
                (normalized_selectors.index(normalized) + 1) % len(selectors)
            ]
        elif current in room_ids:
            selected = selectors[room_ids.index(current)]
        else:
            selected = next(
                (
                    selector
                    for selector in selectors
                    if selector.casefold().startswith(normalized)
                ),
                None,
            )
            if selected is None:
                return
        self._apply_completion(base + selected + " ")

    def _apply_completion(self, completed):
        self.text = completed + self.text[self.cursor :]
        self.cursor = len(completed)
        self.history_index = None
        self.draft = ""


def run_console(
    network,
    nickname,
    input_stream=None,
    output_stream=None,
    history_store=None,
    desktop_notifier=None,
):
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    if desktop_notifier is None:
        desktop_notifier = DesktopNotifier()
    try:
        if input_stream.isatty() and output_stream.isatty():
            return _run_interactive(
                network,
                nickname,
                input_stream,
                output_stream,
                history_store,
                desktop_notifier,
            )
        return _run_plain(
            network,
            nickname,
            input_stream,
            output_stream,
            history_store,
            desktop_notifier,
        )
    finally:
        desktop_notifier.close()


def _initial_history(history_store, network):
    history = deque(maxlen=2000)
    if history_store is not None:
        for record in history_store.entries():
            history.append(
                _record_to_display(
                    record,
                    network.identity_id,
                    network.room_owner_id,
                )
            )
    return history


def _run_plain(
    network, nickname, input_stream, output_stream, history_store, desktop_notifier
):
    room_id = _room_id_number(network.get_room_directory_id() or "unavailable")
    print(HEADER_SEPARATOR, file=output_stream)
    print(
        f"[{network.room_name}] Room id: {room_id} TCP {network.port}",
        file=output_stream,
    )
    print(f"Released {RELEASE_TIME}", file=output_stream)
    print(HEADER_SEPARATOR, file=output_stream)
    commands = "/control, /scp, /show, /who, /help, /quit"
    if network.room_owner_id == network.identity_id:
        commands = commands.replace("/show", "/manager, /show")
    print(f"Commands: {commands}", file=output_stream)
    for entry in _initial_history(history_store, network):
        _write_plain_line(entry, output_stream)

    running = True
    while running:
        _drain_events(
            network,
            lambda line: _write_plain_line(line, output_stream),
            history_store,
            desktop_notifier,
        )
        output_stream.write(f">> {nickname}: ")
        output_stream.flush()
        line = input_stream.readline()
        if not line:
            break
        nickname, running = _handle_line(
            line.rstrip("\r\n"),
            network,
            nickname,
            lambda message: _write_plain_line(message, output_stream),
        )
    output_stream.write("\n")
    output_stream.flush()


def _write_plain_line(message, output_stream):
    text = _message_text(message)
    if isinstance(message, _SystemMessage) and output_stream.isatty():
        color_code = {"red": "91", "yellow": "93"}.get(message.color)
        if color_code:
            output_stream.write(f"\x1b[{color_code}m{text}\x1b[0m\n")
        else:
            print(text, file=output_stream)
    else:
        print(text, file=output_stream)


def _run_interactive(
    network, nickname, input_stream, output_stream, history_store, desktop_notifier
):
    if os.name == "nt":
        return _run_windows_console(
            network,
            nickname,
            input_stream,
            output_stream,
            history_store,
            desktop_notifier,
        )
    return _run_posix_console(
        network,
        nickname,
        input_stream,
        output_stream,
        history_store,
        desktop_notifier,
    )


def _run_windows_console(
    network, nickname, input_stream, output_stream, history_store, desktop_notifier
):
    import msvcrt

    history = _initial_history(history_store, network)
    editor = _InputState()
    running = True
    ansi_state = _enable_windows_ansi()
    special_keys = {
        "H": "UP",
        "P": "DOWN",
        "K": "LEFT",
        "M": "RIGHT",
        "G": "HOME",
        "O": "END",
        "S": "DELETE",
        "I": "PAGEUP",
        "Q": "PAGEDOWN",
    }
    try:
        _render(network, nickname, editor, history, output_stream)
        while running:
            changed = _drain_events(
                network,
                lambda line: history.append(line),
                history_store,
                desktop_notifier,
                history.clear,
            )
            while msvcrt.kbhit():
                char = msvcrt.getwch()
                if char in ("\x00", "\xe0"):
                    key = special_keys.get(msvcrt.getwch())
                    if key is None:
                        continue
                else:
                    key = char
                nickname, running, key_changed = _handle_key(
                    key, editor, nickname, network, history
                )
                changed = changed or key_changed
                if not running:
                    break
            if changed:
                _render(network, nickname, editor, history, output_stream)
            if running:
                time.sleep(0.05)
    finally:
        _restore_windows_ansi(ansi_state)
        output_stream.write("\n")
        output_stream.flush()


def _enable_windows_ansi():
    if os.name != "nt":
        return None
    try:
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.GetStdHandle(-11)
        original_mode = ctypes.c_uint()
        if not kernel.GetConsoleMode(handle, ctypes.byref(original_mode)):
            return None
        if not kernel.SetConsoleMode(handle, original_mode.value | 0x0004):
            return None
        return kernel, handle, original_mode.value
    except (AttributeError, OSError):
        return None


def _restore_windows_ansi(ansi_state):
    if ansi_state is not None:
        kernel, handle, original_mode = ansi_state
        kernel.SetConsoleMode(handle, original_mode)


def _run_posix_console(
    network, nickname, input_stream, output_stream, history_store, desktop_notifier
):
    import termios
    import tty

    input_fd = input_stream.fileno()
    original_terminal = termios.tcgetattr(input_fd)
    history = _initial_history(history_store, network)
    editor = _InputState()
    decoder = codecs.getincrementaldecoder("utf-8")()
    escape_sequence = ""
    running = True
    try:
        tty.setraw(input_fd)
        raw_terminal = termios.tcgetattr(input_fd)
        raw_terminal[1] |= termios.OPOST
        if hasattr(termios, "ONLCR"):
            raw_terminal[1] |= termios.ONLCR
        termios.tcsetattr(input_fd, termios.TCSADRAIN, raw_terminal)
        _render(network, nickname, editor, history, output_stream)
        while running:
            changed = _drain_events(
                network,
                lambda line: history.append(line),
                history_store,
                desktop_notifier,
                history.clear,
            )
            readable, _, _ = select.select([input_fd], [], [], 0.05)
            if readable:
                data = os.read(input_fd, 64)
                if not data:
                    break
                for byte in data:
                    char = decoder.decode(bytes((byte,)))
                    for character in char:
                        key, escape_sequence = _decode_posix_key(
                            character, escape_sequence
                        )
                        if key is None:
                            continue
                        nickname, running, key_changed = _handle_key(
                            key, editor, nickname, network, history
                        )
                        changed = changed or key_changed
                        if not running:
                            break
                    if not running:
                        break
            if changed:
                _render(network, nickname, editor, history, output_stream)
    finally:
        termios.tcsetattr(input_fd, termios.TCSADRAIN, original_terminal)
        output_stream.write("\n")
        output_stream.flush()


def _decode_posix_key(char, escape_sequence):
    if not escape_sequence:
        if char == "\x1b":
            return None, "\x1b"
        return char, ""
    sequence = escape_sequence + char
    key_map = {
        "\x1b[A": "UP",
        "\x1b[B": "DOWN",
        "\x1b[C": "RIGHT",
        "\x1b[D": "LEFT",
        "\x1b[H": "HOME",
        "\x1b[F": "END",
        "\x1b[1~": "HOME",
        "\x1b[3~": "DELETE",
        "\x1b[4~": "END",
        "\x1b[5~": "PAGEUP",
        "\x1b[6~": "PAGEDOWN",
    }
    if sequence in key_map:
        return key_map[sequence], ""
    if any(item.startswith(sequence) for item in key_map):
        return None, sequence
    return None, ""


def _handle_key(key, editor, nickname, network, history):
    if key in ("\r", "\n"):
        line = editor.text
        if line:
            editor.remember(line)
            nickname, running = _handle_line(
                line,
                network,
                nickname,
                lambda message: history.append(
                    message
                    if isinstance(message, (_ChatLine, _SystemMessage))
                    else _safe_line(message)
                ),
            )
        else:
            running = True
        editor.reset()
        return nickname, running, True
    if key in ("\x03", "\x04"):
        return nickname, False, True
    if key == "\x15":
        editor.reset()
        return nickname, True, True
    if key == "LEFT":
        editor.cursor = max(0, editor.cursor - 1)
        return nickname, True, True
    if key == "RIGHT":
        editor.cursor = min(len(editor.text), editor.cursor + 1)
        return nickname, True, True
    if key == "HOME" or key == "\x01":
        editor.cursor = 0
        return nickname, True, True
    if key == "END" or key == "\x05":
        editor.cursor = len(editor.text)
        return nickname, True, True
    if key == "UP":
        editor.previous()
        return nickname, True, True
    if key == "DOWN":
        editor.next()
        return nickname, True, True
    if key == "PAGEUP":
        editor.scroll_offset += 10
        return nickname, True, True
    if key == "PAGEDOWN":
        editor.scroll_offset = max(0, editor.scroll_offset - 10)
        return nickname, True, True
    if key in ("\x08", "\x7f"):
        if editor.cursor:
            editor.text = editor.text[: editor.cursor - 1] + editor.text[editor.cursor :]
            editor.cursor -= 1
            editor.history_index = None
        return nickname, True, True
    if key == "DELETE":
        if editor.cursor < len(editor.text):
            editor.text = editor.text[: editor.cursor] + editor.text[editor.cursor + 1 :]
            editor.history_index = None
        return nickname, True, True
    if key == "\t":
        editor.complete(
            lambda line: history.append(_safe_line(line)),
            network,
        )
        return nickname, True, True
    if isinstance(key, str) and len(key) == 1 and key.isprintable():
        if len(editor.text) < MAX_MESSAGE_LENGTH:
            editor.text = (
                editor.text[: editor.cursor] + key + editor.text[editor.cursor :]
            )
            editor.cursor += 1
            editor.history_index = None
            editor.draft = ""
        return nickname, True, True
    return nickname, True, False


def _handle_line(line, network, nickname, emit):
    output = emit

    def emit(message):
        if isinstance(message, str) and message.startswith("!"):
            message = _SystemMessage(message, _system_message_color(message))
        output(message)

    command = line.strip()
    if not command:
        return nickname, True
    if command == "/quit":
        return nickname, False
    if command == "/help":
        emit("! Commands (permissions):")
        emit(
            "  /control connect <room id-room name> — any user; "
            "connect from the LAN room list"
        )
        emit("  /control search — any user; list available LAN rooms")
        emit("  /control connect <ip> <port> — any user; legacy direct connection")
        emit("  /control disconnect — any user; leave the current room")
        emit("  /control user rename <name> — any user; change your nickname")
        emit("  /who — any user; list members (host first, then join order)")
        emit("  /show — any user; show room settings and member count")
        emit("  /scp <user name> <file path> — any user; request a file transfer")
        emit("  /scp all <file path> — any user; offer the file to every member")
        emit("  /scp accept — any user; accept an incoming file")
        emit("  /scp exit — any user; cancel the active transfer")
        if network.room_owner_id == network.identity_id:
            emit("  /manager approve <name-ip> — host only; approve a join request")
            emit("  /manager denny <name-ip> — host only; deny a join request")
            emit("  /manager set maxmember <number> — host only")
            emit("  /manager set roomname <name> — host only")
        emit("  /help — any user; show this command list")
        emit("  /quit — any user; exit DoggyChat")
    elif command == "/who":
        emit("---------------------------")
        users = sorted(
            network.get_users(),
            key=lambda user: (
                user["id"] != network.room_owner_id,
                user.get("order", 0),
            ),
        )
        for user in users:
            emit(
                _ChatLine(
                    "$",
                    user["nickname"],
                    user["ip"],
                    color=(
                        "red" if user["id"] == network.room_owner_id else "green"
                    ),
                )
            )
        emit("---------------------------")
    elif command == "/show":
        config = network.get_room_config()
        room_ip, room_port = network.get_room_endpoint()
        room_id = _room_id_number(
            network.get_room_directory_id() or "unavailable"
        )
        emit("---------------------------")
        emit(f"* Room name: {config['room_name']}")
        emit(f"* Room id: {room_id}")
        emit(f"* Room ip: {room_ip}")
        emit(f"* Room port: {room_port}")
        emit(f"* Members: {network.get_member_count()}")
        emit(f"* Max member: {config['max_members']}")
        emit(f"* Max record: {MAX_HISTORY_ENTRIES}")
        emit("---------------------------")
    elif command == "/control search":
        rooms = network.search_rooms()
        emit("---------------------------")
        if not rooms:
            emit("! No available rooms were found.")
        for room in rooms:
            emit(
                f"* <{_room_id_number(room['id'])}>: {room['room_name']} - "
                f"{room['ip']} - {room['port']}"
            )
        emit("---------------------------")
    elif command == "/scp" or command.startswith("/scp "):
        remainder = command[len("/scp") :].strip()
        if remainder == "accept":
            success, error = network.accept_file_transfer()
            emit("! File transfer accepted." if success else f"! {error}")
        elif remainder == "exit":
            success, error = network.cancel_file_transfer()
            emit("! File transfer cancelled." if success else f"! {error}")
        elif remainder:
            candidates = [
                user["nickname"]
                for user in network.get_users()
                if user["id"] != network.identity_id
            ]
            if remainder.casefold().startswith("all "):
                target_name = "all"
                file_path = remainder[4:].strip()
            else:
                target_name = next(
                    (
                        name
                        for name in sorted(candidates, key=len, reverse=True)
                        if remainder.casefold().startswith(name.casefold() + " ")
                    ),
                    "",
                )
                file_path = (
                    remainder[len(target_name) :].strip()
                    if target_name
                    else ""
                )
            if target_name and file_path:
                success, error = network.request_file_transfer(
                    target_name,
                    file_path,
                )
                if not success:
                    emit(f"! {error}")
            else:
                emit("! Usage: /scp <user name> <file path> | /scp all <file path>")
        else:
            emit(
                "! Usage: /scp <user name> <file path> | /scp all <file path> | "
                "/scp accept | /scp exit"
            )
    elif command == "/control" or command.startswith("/control "):
        parts = command.split(maxsplit=3)
        if len(parts) in (2, 3, 4) and parts[1] == "connect":
            target = command.split(maxsplit=2)[2] if len(parts) > 2 else ""
            if _is_room_selector(target) or (
                len(parts) == 3
                and (target.isdigit() or target.casefold().startswith("id"))
            ):
                network.connect_to_room(target)
                emit(f"! Connecting to room {target}...")
                return nickname, True
            address = parts[2] if len(parts) > 2 else DEFAULT_CONNECT_IP
            remote_port = DEFAULT_PORT
            if len(parts) == 4:
                try:
                    remote_port = int(parts[3])
                except ValueError:
                    emit("! Port must be between 1 and 65535.")
                    return nickname, True
                if not 1 <= remote_port <= 65535:
                    emit("! Port must be between 1 and 65535.")
                    return nickname, True
            network.connect_to(address, remote_port)
            emit(f"! Connecting to {address}:{remote_port}...")
        elif len(parts) == 2 and parts[1] == "search":
            emit("! Usage: /control search")
        elif len(parts) == 2 and parts[1] == "disconnect":
            success, error = network.disconnect_from_room()
            emit("! Disconnected from the current room." if success else f"! {error}")
        elif (
            len(parts) == 4
            and parts[1] == "user"
            and parts[2] == "rename"
        ):
            new_name = parts[3].strip()
            if not new_name or len(new_name) > MAX_NICKNAME_LENGTH:
                emit(
                    f"! Nickname must contain 1 to {MAX_NICKNAME_LENGTH} characters."
                )
                return nickname, True
            success, error = network.update_nickname(new_name)
            if not success:
                emit(f"! {error}")
                return nickname, True
            nickname = new_name.strip()
            emit(f"! Nickname changed to {nickname}.")
        else:
            emit(
                "! Usage: /control connect <room id-room name> | "
                "/control search | "
                "/control disconnect | /control user rename <name>"
            )
    elif command == "/manager" or command.startswith("/manager "):
        if network.room_owner_id != network.identity_id:
            emit("! Only the room host can use /manager.")
            return nickname, True
        parts = command.split(maxsplit=3)
        join_parts = command.split(maxsplit=2)
        if len(join_parts) == 3 and join_parts[1] in ("approve", "denny"):
            accepted = join_parts[1] == "approve"
            selector = join_parts[2]
            success, error = network.control_join(selector, accepted)
            if not success:
                emit(f"! {error}")
            else:
                emit(
                    f"! {'Approved' if accepted else 'Denied'} join request "
                    f"for {selector}."
                )
        elif (
            len(parts) == 4
            and parts[1] == "set"
            and parts[2] == "maxmember"
        ):
            try:
                max_members = int(parts[3])
            except ValueError:
                emit(f"! Max member must be between 1 and {MAX_ROOM_MEMBERS}.")
                return nickname, True
            success, error = network.set_room_config(max_members=max_members)
            emit(
                f"! Max member set to {max_members}."
                if success
                else f"! {error}"
            )
        elif (
            len(parts) == 4
            and parts[1] == "set"
            and parts[2] == "roomname"
        ):
            room_name = parts[3].strip()
            success, error = network.set_room_config(room_name=room_name)
            emit(
                f"! Room name set to {room_name}."
                if success
                else f"! {error}"
            )
        else:
            emit(
                "! Usage: /manager approve <name-ip> | "
                "/manager denny <name-ip> | "
                "/manager set maxmember <number> | /manager set roomname <name>"
            )
    elif (
        command.startswith("/connect")
        or command.startswith("/disconnect")
        or command.startswith("/name")
    ):
        emit("! Use /control connect [ip] [port], /control disconnect, or "
             "/control user rename <name>.")
    elif command.startswith("/"):
        emit("! Unknown command. Type /help for commands.")
    elif len(line) > MAX_MESSAGE_LENGTH:
        emit(f"! Message exceeds the {MAX_MESSAGE_LENGTH}-character limit.")
    else:
        try:
            sent = network.send(nickname, line)
        except ValueError as error:
            emit(f"! Could not send message: {error}")
        else:
            if not sent:
                emit("! Not connected. Use /control connect [ip] [port] first.")
    return nickname, True


def _drain_events(
    network,
    emit,
    history_store=None,
    desktop_notifier=None,
    clear_display=None,
):
    changed = False
    while True:
        try:
            event, data = network.events.get_nowait()
        except queue.Empty:
            return changed

        if event in ("history_reset", "history_cleared"):
            if clear_display is not None:
                clear_display()
            if event == "history_cleared":
                emit("! Chat history cleared because the room is empty.")
            changed = True
            continue

        if event == "first_run_hint":
            room_id = _room_id_number(data)
            for line in (
                "< HINT >",
                f"You now have your own private room, ID is {room_id}.",
                "* To let others join your room:",
                "  /manager approve <name-ip>",
                "  /manager denny <name-ip>",
                "* To join another room:",
                "  Step 1: Search for ID: /control search",
                "  Step 2: Connect: /control connect <room id-room name>",
                "  Step 3: Wait for the host to approve your request.",
                "  Your private room is destroyed after joining; "
                "you will get a new room ID when you disconnect.",
                "* To disconnect from the room: /control disconnect",
                "* To quit DoggyChat: /quit",
                "Now, have fun!!!",
            ):
                emit(_SystemMessage(_safe_line(line), "default"))
            changed = True
            continue

        if event == "history_batch":
            records = data
            if (
                history_store is not None
                and network.room_owner_id == network.identity_id
            ):
                try:
                    records = history_store.add_many(records)
                except OSError as error:
                    emit(
                        _SystemMessage(
                            _safe_line(f"! Could not save chat history: {error}"),
                            "red",
                        )
                    )
                    records = data
            for record in records:
                emit(
                    _record_to_display(
                        record,
                        network.identity_id,
                        network.room_owner_id,
                    )
                )
            changed = changed or bool(records)
            continue

        if event in ("sent", "message", "history"):
            record = data[1] if event == "message" else data
            if (
                history_store is not None
                and network.room_owner_id == network.identity_id
            ):
                try:
                    added = history_store.add(record)
                except OSError as error:
                    emit(
                        _SystemMessage(
                            _safe_line(f"! Could not save chat history: {error}"),
                            "red",
                        )
                    )
                    added = True
                if not added:
                    continue
                display = _record_to_display(
                    record,
                    network.identity_id,
                    network.room_owner_id,
                )
            else:
                display = _record_to_display(
                    record,
                    network.identity_id,
                    network.room_owner_id,
                )
            emit(display)
            if (
                event == "message"
                and record["sender_id"] != network.identity_id
                and desktop_notifier is not None
            ):
                notification_error = desktop_notifier.notify(
                    f"Message from {record['nickname']}",
                    record["text"],
                )
                if notification_error:
                    emit(
                        _SystemMessage(
                            notification_error,
                            _system_message_color(notification_error),
                        )
                    )
            changed = True
            continue

        line = _format_event(event, data)
        if line:
            emit(
                _SystemMessage(
                    _safe_line(line),
                    _system_message_color(line),
                )
            )
            changed = True


def _format_event(event, data):
    if event == "room_directory_id_changed":
        if data is None:
            return "! This instance no longer hosts a listed room."
        return f"! This room is listed as {data} on the LAN."
    if event == "tls_fingerprint":
        return f"! TLS 1.2+ enabled; local identity SHA-256: {data}"
    if event == "tls_peer_trusted":
        nickname, fingerprint = data
        return (
            f"! First connection to {nickname}; TOFU fingerprint saved "
            f"(SHA-256): {fingerprint}"
        )
    if event == "tls_identity_rejected":
        nickname, address, reason = data
        return (
            f"! Rejected TLS identity for {nickname} ({address}): {reason}"
        )
    if event == "connected":
        return f"! Connected to {data}."
    if event == "disconnected":
        nickname, reason = data
        return f"! {reason}: {nickname}"
    if event == "join_request":
        nickname, address = data
        selector = f"{nickname}-{address}"
        return (
            f"! Join request from {nickname} ({address}). "
            f"Use /manager approve {selector} or /manager denny {selector}."
        )
    if event == "join_pending":
        return f"! Join request sent. Waiting for {data}'s approval."
    if event == "join_result":
        accepted, owner = data[:2]
        reason = data[2] if len(data) > 2 else ""
        if not accepted and reason:
            return f"! Connection failed: {reason}"
        result = "approved" if accepted else "denied"
        return f"! Room host {owner} {result} your join request."
    if event == "join_decision":
        nickname, address, accepted = data
        result = "Approved" if accepted else "Denied"
        return f"! {result} {nickname} ({address})."
    if event == "room_owner_changed":
        return f"! Room host changed to {data}."
    if event == "member_left":
        return f"! {data} disconnected."
    if event == "join_capacity":
        nickname, address, current_members, max_members = data
        return (
            f"! Join attempt from {nickname} ({address}) rejected: room is full "
            f"({current_members}/{max_members})."
        )
    if event == "duplicate_nickname_rejected":
        nickname, address = data
        return (
            f"! Join attempt from {nickname} ({address}) rejected: "
            "nickname is already in use."
        )
    if event == "nickname_conflict":
        return (
            f"! Your nickname '{data}' conflicts with another room member. "
            "Use /control user rename <name> before sending messages."
        )
    if event == "room_config_changed":
        return (
            f"! Room settings updated: {data['room_name']} "
            f"(max {data['max_members']} members)."
        )
    if event == "connection_failed":
        return f"! Connection failed: {data}"
    if event == "scp_request":
        nickname, address, filename, size = data
        return (
            f"! {nickname} ({address}) wants to send {filename} "
            f"({size} bytes). Use /scp accept within 5 minutes."
        )
    if event == "scp_waiting":
        return f"! Waiting for {data}'s approval; request expires in 5 minutes."
    if event == "scp_accepted":
        return f"! {data} accepted the file transfer."
    if event == "scp_sent":
        nickname, filename = data
        return f"! File sent successfully to {nickname}: {filename}"
    if event == "scp_received":
        return f"! File received successfully: {data}"
    if event == "scp_rejected":
        nickname, reason = data
        return f"! File transfer rejected by {nickname}: {reason}"
    if event == "scp_cancelled":
        if isinstance(data, tuple):
            nickname, reason = data
            return f"! File transfer with {nickname} cancelled: {reason}"
        return f"! File transfer cancelled: {data}"
    if event == "scp_timeout":
        return f"! File transfer to {data} timed out after 5 minutes of inactivity."
    if event == "scp_error":
        return f"! File transfer error: {data}"
    if event == "error":
        return f"! Error: {data}"
    return ""


def _record_to_display(record, identity_id, room_owner_id):
    is_own = record["sender_id"] == identity_id
    color = (
        "red"
        if record["sender_id"] == room_owner_id
        else "blue" if is_own else "green"
    )
    return _ChatLine(
        "*" if is_own else "-",
        record["nickname"],
        record["text"],
        record["timestamp"],
        color,
    )


def _safe_line(line):
    return "".join(
        " " if char in "\r\n\t" else "?" if unicodedata.category(char).startswith("C") else char
        for char in line
    )


def _system_message_color(message):
    lowered = str(message).casefold()
    errors = (
        "error",
        "failed",
        "rejected",
        "invalid",
        "could not",
        "cannot",
        "not connected",
        "must be",
        "must contain",
        "exceeds",
        "unknown command",
        "only the",
        "denied",
        "already in use",
        "conflict",
        "usage:",
        "timed out",
        "timeout",
        "unable",
        "not available",
        "not found",
        "no pending",
        "no room member",
        "no available rooms",
        "wait for the current",
        "connect directly",
        "more than one",
        "another file transfer",
        "there is no",
        "not a regular",
        "unsupported file",
        "before sending",
        "expired",
    )
    return "red" if any(error in lowered for error in errors) else "yellow"


def _message_text(message):
    if isinstance(message, _ChatLine):
        timestamp = _format_timestamp(message.timestamp)
        prefix = f"{timestamp} " if timestamp else ""
        marker = message.marker if message.marker == "$" else f"{message.marker} "
        return _safe_line(
            f"{prefix}{marker}{message.nickname}: {message.text}"
        )
    return _safe_line(message)


def _format_timestamp(timestamp):
    if not timestamp:
        return ""
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return timestamp
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone().strftime("%Y/%m/%d %H:%M:%S")


def _char_width(char):
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def _display_width(text):
    return sum(_char_width(char) for char in text)


def _wrap_line(line, width):
    if width < 1:
        return []
    wrapped = []
    current = ""
    current_width = 0
    for char in line:
        char_width = _char_width(char)
        if current and current_width + char_width > width:
            wrapped.append(current)
            current = ""
            current_width = 0
        current += char
        current_width += char_width
    wrapped.append(current)
    return wrapped


def _wrap_chat_line(message, width):
    timestamp = _format_timestamp(message.timestamp)
    prefix = f"{timestamp} " if timestamp else ""
    marker = message.marker if message.marker == "$" else f"{message.marker} "
    segments = (
        (f"{prefix}{marker}", False),
        (_safe_line(message.nickname), message.color),
        (": " + _safe_line(message.text), False),
    )
    lines = []
    current = []
    current_width = 0
    for text, blue in segments:
        for char in text:
            char_width = _char_width(char)
            if current and current_width + char_width > width:
                lines.append(current)
                current = []
                current_width = 0
            if current and current[-1][1] == blue:
                current[-1] = (current[-1][0] + char, blue)
            else:
                current.append((char, blue))
            current_width += char_width
    lines.append(current)
    return lines


def _fit_line(line, width):
    fitted = ""
    fitted_width = 0
    for char in line:
        char_width = _char_width(char)
        if fitted_width + char_width > width:
            break
        fitted += char
        fitted_width += char_width
    return fitted


def _fit_prompt(nickname, text, cursor, width):
    prefix = f">> {nickname}: "
    prefix = _fit_line(prefix, width)
    prefix_width = _display_width(prefix)
    available = max(0, width - prefix_width)
    start = max(0, cursor - available)
    while start < cursor and _display_width(text[start:cursor]) > max(0, available - 1):
        start += 1
    left_marker = "…" if start else ""
    available_text = max(0, available - _display_width(left_marker))
    visible = ""
    visible_width = 0
    for char in text[start:]:
        char_width = _char_width(char)
        if visible_width + char_width > available_text:
            break
        visible += char
        visible_width += char_width
    prompt = prefix + left_marker + visible
    cursor_width = prefix_width + _display_width(left_marker + text[start:cursor])
    cursor_column = min(width, cursor_width + 1)
    return prompt, cursor_column


def _render(network, nickname, editor, history, output_stream):
    size = shutil.get_terminal_size((80, 24))
    width = max(1, size.columns or 80)
    height = max(5, size.lines or 24)
    history_rows = max(0, height - 5)
    wrapped_history = []
    for line in history:
        if isinstance(line, _ChatLine):
            wrapped_history.extend(_wrap_chat_line(line, width))
        else:
            safe_line = _safe_line(line)
            color = (
                line.color
                if isinstance(line, _SystemMessage)
                else _system_message_color(safe_line)
                if safe_line.startswith("!")
                else False
            )
            wrapped_history.extend(
                [[(wrapped_line, color)] for wrapped_line in _wrap_line(safe_line, width)]
            )
    history_end = max(0, len(wrapped_history) - editor.scroll_offset)
    history_start = max(0, history_end - history_rows)
    visible_history = wrapped_history[history_start:history_end]

    room_id = _room_id_number(network.get_room_directory_id())
    room_id = room_id or "unavailable"
    separator = _fit_line(HEADER_SEPARATOR, width)
    header = _fit_line(
        f"[{network.room_name}] Room id: {room_id} TCP {network.port}",
        width,
    )
    release = _fit_line(f"Released {RELEASE_TIME}", width)
    output_stream.write("\x1b[2J\x1b[H")
    output_stream.write(
        "\n".join((separator, header, release, separator))
    )
    for row in range(history_rows):
        output_stream.write("\n")
        if row < len(visible_history):
            for segment, blue in visible_history[row]:
                if blue == "blue":
                    output_stream.write(f"\x1b[94m{segment}\x1b[0m")
                elif blue == "red":
                    output_stream.write(f"\x1b[91m{segment}\x1b[0m")
                elif blue == "green":
                    output_stream.write(f"\x1b[92m{segment}\x1b[0m")
                elif blue == "yellow":
                    output_stream.write(f"\x1b[93m{segment}\x1b[0m")
                else:
                    output_stream.write(segment)
    output_stream.write(f"\x1b[{height};1H\x1b[2K")
    prompt, cursor_column = _fit_prompt(
        nickname, editor.text, editor.cursor, width
    )
    output_stream.write(prompt)
    output_stream.write(f"\x1b[{height};{cursor_column}H")
    output_stream.flush()
