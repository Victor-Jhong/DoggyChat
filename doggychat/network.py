import base64
import hashlib
import ipaddress
import json
import os
import queue
import secrets
import socket
import ssl
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from .security import TLSIdentity, TrustStore, verify_hello_identity
from .version import VERSION


DEFAULT_CONNECT_IP = "10.10.13.114"
DEFAULT_PORT = 48652
MAX_MESSAGE_LENGTH = 2000
MAX_FRAME_BYTES = 16384
MAX_NICKNAME_LENGTH = 32
MAX_MESSAGE_HOPS = 16
MAX_SEEN_MESSAGES = 4096
MAX_HISTORY_ENTRIES = 500
MAX_ROOM_MEMBERS = 100
DEFAULT_ROOM_MAX_MEMBERS = 15
DEFAULT_ROOM_NAME = f"DoggyChat {VERSION} - Unknow chat room"
MAX_ROOM_NAME_LENGTH = 80
PRESENCE_INTERVAL = 20
PRESENCE_TIMEOUT = 75
SCP_TIMEOUT = 5 * 60
SCP_CHUNK_BYTES = 8192
JOIN_APPROVAL_TIMEOUT = 60


def _valid_room_key(key):
    return (
        isinstance(key, str)
        and len(key) == 4
        and key.isascii()
        and key.isdigit()
    )


def _new_room_key():
    return f"{secrets.randbelow(10000):04d}"


def create_listener(port, bind_host="0.0.0.0"):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((bind_host, port))
    except OSError:
        listener.close()
        raise
    return listener


def _application_directory():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _valid_file_name(name):
    reserved_names = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
    return (
        isinstance(name, str)
        and name not in ("", ".", "..")
        and len(name) <= 255
        and "/" not in name
        and "\\" not in name
        and ":" not in name
        and "\0" not in name
        and not any(character in '<>:"|?*' for character in name)
        and not name.endswith((" ", "."))
        and not any(ord(character) < 32 for character in name)
        and name.split(".", 1)[0].rstrip(" .").upper() not in reserved_names
    )


def encode_message(sender, text, sender_id=None):
    if not isinstance(sender, str) or not sender.strip():
        raise ValueError("Nickname cannot be empty")
    if len(sender) > MAX_NICKNAME_LENGTH:
        raise ValueError(f"Nickname must be {MAX_NICKNAME_LENGTH} characters or fewer")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Message cannot be empty")
    if len(text) > MAX_MESSAGE_LENGTH:
        raise ValueError(f"Message must be {MAX_MESSAGE_LENGTH} characters or fewer")
    return _encode_frame(
        {
            "type": "message",
            "id": uuid.uuid4().hex,
            "sender_id": sender_id or uuid.uuid4().hex,
            "nickname": sender.strip(),
            "text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "hops": MAX_MESSAGE_HOPS,
        }
    )


def _encode_frame(frame):
    encoded = json.dumps(
        frame,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(encoded) > MAX_FRAME_BYTES:
        raise ValueError("Message is too large to send")
    return encoded


def _valid_uuid(value):
    if not isinstance(value, str) or len(value) != 32:
        return False
    try:
        return uuid.UUID(hex=value).hex == value
    except ValueError:
        return False


def _validate_record(record):
    if not isinstance(record, dict):
        raise ValueError("Invalid history record")
    if (
        not _valid_uuid(record.get("id"))
        or not _valid_uuid(record.get("sender_id"))
        or not isinstance(record.get("nickname"), str)
        or not record["nickname"].strip()
        or len(record["nickname"]) > MAX_NICKNAME_LENGTH
        or not isinstance(record.get("text"), str)
        or not record["text"].strip()
        or len(record["text"]) > MAX_MESSAGE_LENGTH
        or not isinstance(record.get("timestamp"), str)
        or len(record["timestamp"]) > 40
    ):
        raise ValueError("Invalid history record content")
    return {
        "id": record["id"],
        "sender_id": record["sender_id"],
        "nickname": record["nickname"],
        "text": record["text"],
        "timestamp": record["timestamp"],
    }


def _validate_members(items):
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ROOM_MEMBERS:
        raise ValueError("Invalid room member list")
    members = {}
    for member in items:
        if not isinstance(member, dict):
            raise ValueError("Invalid room member")
        user_id = member.get("id")
        nickname = member.get("nickname")
        address = member.get("ip", "")
        order = member.get("order")
        port = member.get("port")
        revision = member.get("revision")
        if (
            not _valid_uuid(user_id)
            or not isinstance(nickname, str)
            or not nickname.strip()
            or len(nickname) > MAX_NICKNAME_LENGTH
            or not isinstance(address, str)
            or not isinstance(order, int)
            or isinstance(order, bool)
            or order < 0
            or not isinstance(port, int)
            or isinstance(port, bool)
            or not 1 <= port <= 65535
            or not isinstance(revision, int)
            or isinstance(revision, bool)
        ):
            raise ValueError("Invalid room member content")
        if address and address != "unknown":
            ipaddress.IPv4Address(address)
        if user_id in members:
            raise ValueError("Duplicate room member")
        members[user_id] = {
            "id": user_id,
            "nickname": nickname.strip(),
            "ip": address,
            "port": port,
            "order": order,
            "revision": revision,
        }
    return dict(sorted(members.items(), key=lambda item: item[1]["order"]))


def _validate_room_config(frame):
    room_id = frame.get("room_id")
    room_name = frame.get("room_name")
    max_members = frame.get("max_members")
    revision = frame.get("config_revision")
    if (
        not _valid_uuid(room_id)
        or not isinstance(room_name, str)
        or not room_name.strip()
        or len(room_name) > MAX_ROOM_NAME_LENGTH
        or not isinstance(max_members, int)
        or isinstance(max_members, bool)
        or not 1 <= max_members <= MAX_ROOM_MEMBERS
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 0
    ):
        raise ValueError("Invalid room configuration")
    return {
        "room_id": room_id,
        "room_name": room_name.strip(),
        "max_members": max_members,
        "config_revision": revision,
    }


class _Peer:
    def __init__(
        self,
        connection,
        address,
        initiated=False,
        tls_server_fingerprint=None,
    ):
        self.connection = connection
        self.address = address
        self.initiated = initiated
        self.tls_server_fingerprint = tls_server_fingerprint
        self.send_lock = threading.Lock()
        self.user_id = None
        self.nickname = None
        self.request_id = None
        self.remote_request_id = None
        self.admitted = False
        self.ready = False
        self.departed = False
        self.connect_attempt = initiated
        self.pending_room_snapshot = None
        self.join_deadline = None
        self.member_revision = 0


class ChatNetwork:
    def __init__(
        self,
        port=DEFAULT_PORT,
        bind_host="0.0.0.0",
        identity_id=None,
        nickname="Guest",
        history_provider=None,
        history_consumer=None,
        tls_identity=None,
        trust_store=None,
        file_directory=None,
    ):
        self.events = queue.Queue()
        self.port = port
        self.bind_host = bind_host
        self.identity_id = identity_id or uuid.uuid4().hex
        if not _valid_uuid(self.identity_id):
            raise ValueError("Invalid local user ID")
        self.room_owner_id = self.identity_id
        self.room_id = self.identity_id
        self.nickname = nickname
        self.history_provider = history_provider
        self.history_consumer = history_consumer
        self.room_directory = None
        self._tls_identity = tls_identity or TLSIdentity(self.identity_id)
        self._trust_store = trust_store or TrustStore()
        self.file_directory = Path(
            file_directory if file_directory is not None else _application_directory()
        )
        self._scp_lock = threading.RLock()
        self._scp_transfer = None
        self._seen_scp_frames = set()
        self._seen_scp_frame_order = deque()
        self._server_tls_context = self._tls_identity.server_context()
        self._client_tls_context = TLSIdentity.client_context()
        self._stop = threading.Event()
        self._listener = None
        self._connect_lock = threading.Lock()
        self._connect_in_progress = False
        self._peers = {}
        self._peers_lock = threading.Lock()
        self._seen_messages = set()
        self._seen_message_order = deque()
        self._seen_lock = threading.Lock()
        self._revision = time.time_ns()
        self._users = {}
        self._users_lock = threading.Lock()
        self._local_ip = _discover_local_ip()
        self._members_lock = threading.RLock()
        self._nickname_conflict_lock = threading.Lock()
        self._nickname_conflict_active = False
        self._members = {
            self.identity_id: {
                "id": self.identity_id,
                "nickname": self.nickname,
                "ip": self._local_ip,
                "port": self.port,
                "order": 0,
                "revision": self._revision,
            }
        }
        self._next_member_order = 1
        self._admitted = True
        self._config_lock = threading.RLock()
        self.room_name = DEFAULT_ROOM_NAME
        self.max_members = DEFAULT_ROOM_MAX_MEMBERS
        self._config_revision = time.time_ns()
        self._room_key_lock = threading.Lock()
        self._room_key = _new_room_key()
        self._pending_lock = threading.Lock()
        self._join_decision_lock = threading.Lock()
        self._pending_joins = {}
        self._pending_routes = {}
        self._seen_join_requests = set()
        self._seen_join_decisions = set()
        self._seen_leaves = set()
        self._seen_leaves_lock = threading.Lock()
        self._history_transfer_lock = threading.Lock()
        self._seen_history_chunks = set()
        self._seen_history_order = deque()
        self._history_transfers = {}
        self._set_user(
            self.identity_id,
            self.nickname,
            self._local_ip,
            self._revision,
        )

    def start(self, listener=None):
        listener = listener or create_listener(self.port, self.bind_host)
        try:
            listener.listen()
            listener.settimeout(0.5)
        except OSError:
            listener.close()
            raise
        self._listener = listener
        self.port = listener.getsockname()[1]
        with self._members_lock:
            self._members[self.identity_id]["port"] = self.port
        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._presence_loop, daemon=True).start()
        threading.Thread(target=self._join_timeout_loop, daemon=True).start()
        threading.Thread(target=self._scp_timeout_loop, daemon=True).start()
        self.events.put(("listening", self.port))
        self.events.put(("tls_fingerprint", self._tls_identity.fingerprint))

    def connect_to(self, address, port=None, key=None):
        try:
            ip = ipaddress.IPv4Address(address.strip())
        except ValueError:
            self.events.put(("error", "Enter a valid IPv4 address."))
            return
        if key is not None and not _valid_room_key(key):
            self.events.put(("error", "Room key must be exactly 4 digits."))
            return
        remote_port = DEFAULT_PORT if port is None else port
        if not 1 <= remote_port <= 65535:
            self.events.put(("error", "Port must be between 1 and 65535."))
            return
        if remote_port == self.port and (
            str(ip) == self._local_ip
            or str(ip).startswith("127.")
            or str(ip) == self.bind_host
        ):
            self.events.put(("error", "You cannot connect to your own room."))
            return
        with self._connect_lock:
            if self._connect_in_progress:
                self.events.put(("error", "A connection attempt is already in progress."))
                return
            if not self._admitted:
                self.events.put(
                    ("error", "Wait for the current room's join decision first.")
                )
                return
            self._connect_in_progress = True

        threading.Thread(
            target=self._connect_worker,
            args=(str(ip), remote_port, key),
            daemon=True,
        ).start()

    def get_room_endpoint(self):
        with self._members_lock:
            owner = self._members.get(self.room_owner_id)
            if owner is None:
                owner_ip = self._local_ip
            else:
                owner_ip = owner["ip"]
            if self.room_owner_id == self.identity_id and self.bind_host not in (
                "",
                "0.0.0.0",
            ):
                owner_ip = self.bind_host
            return owner_ip, owner["port"] if owner else self.port

    def attach_room_directory(self, room_directory):
        self.room_directory = room_directory

    def search_rooms(self):
        if self.room_directory is None:
            return []
        return self.room_directory.search_rooms()

    def get_room_directory_id(self):
        if self.room_directory is None:
            return None
        return self.room_directory.get_room_id(self.room_id)

    def connect_to_room(self, room_id, key=None):
        if self.room_directory is None:
            self.events.put(("error", "Room discovery is not available."))
            return
        if key is not None and not _valid_room_key(key):
            self.events.put(("error", "Room key must be exactly 4 digits."))
            return
        room = self.room_directory.find_room(room_id)
        if room is None:
            self.room_directory.search_rooms()
            room = self.room_directory.find_room(room_id)
            if room is None:
                self.events.put(("error", f"Room ID '{room_id}' was not found."))
                return
        if room["room_uuid"] == self.room_id:
            self.events.put(("error", "You are already in that room."))
            return
        self.connect_to(room["ip"], room["port"], key=key)

    def send(self, sender, text):
        if not self._admitted:
            return 0
        if self._member_has_nickname_conflict(self.identity_id):
            self._refresh_local_nickname_conflict()
            raise ValueError(
                "Your nickname conflicts with another room member; "
                "rename before sending messages."
            )
        frame = json.loads(encode_message(sender, text, self.identity_id))
        record = _validate_record(frame)
        self._remember_message(record["id"])
        sent = self._broadcast(_encode_frame(frame))
        if sent:
            self.events.put(("sent", record))
        return sent

    def request_file_transfer(self, target_name, source_path):
        if not self._admitted:
            return False, "Join a room before sending files."
        try:
            path = Path(source_path).expanduser().resolve(strict=True)
            if not path.is_file():
                return False, "The selected path is not a regular file."
            size = path.stat().st_size
        except (OSError, RuntimeError, ValueError) as error:
            return False, f"Could not read the selected file: {error}"
        if not _valid_file_name(path.name):
            return False, "The selected file has an unsupported file name."
        with self._members_lock:
            members = list(self._members.values())
        if target_name.strip().casefold() == "all":
            targets = [
                member
                for member in members
                if member["id"] != self.identity_id
            ]
        else:
            target = next(
                (
                    member
                    for member in members
                    if member["nickname"].casefold()
                    == target_name.strip().casefold()
                ),
                None,
            )
            if target is not None and target["id"] == self.identity_id:
                return False, "You cannot send a file to yourself."
            targets = [target] if target is not None else []
        if not targets:
            return False, f"No room member named '{target_name}' was found."
        group_id = uuid.uuid4().hex
        now = time.monotonic()
        state = {
            "id": group_id,
            "role": "sender",
            "filename": path.name,
            "size": size,
            "source_path": path,
            "cancelled": threading.Event(),
            "targets": {},
        }
        for member in targets:
            transfer_id = uuid.uuid4().hex
            state["targets"][transfer_id] = {
                "id": transfer_id,
                "user_id": member["id"],
                "nickname": member["nickname"],
                "phase": "pending",
                "last_activity": now,
                "cancelled": threading.Event(),
            }
        with self._scp_lock:
            if self._scp_transfer is not None:
                return False, "Another file transfer is already active."
            self._scp_transfer = state
        failed_targets = []
        for transfer_id, target in state["targets"].items():
            sent = self._send_scp_frame(
                {
                    "type": "scp_request",
                    "transfer_id": transfer_id,
                    "sender_id": self.identity_id,
                    "target_id": target["user_id"],
                    "filename": path.name,
                    "size": size,
                }
            )
            if not sent:
                failed_targets.append(transfer_id)
        if len(failed_targets) == len(state["targets"]):
            self._finish_scp_transfer(group_id)
            return False, "Could not send the file request."
        for transfer_id in failed_targets:
            state["targets"].pop(transfer_id, None)
        recipient = (
            "all room members"
            if target_name.strip().casefold() == "all"
            else target_name
        )
        self.events.put(("scp_waiting", recipient))
        return True, ""

    def accept_file_transfer(self):
        with self._scp_lock:
            state = self._scp_transfer
            if (
                state is None
                or state["role"] != "receiver"
                or state["phase"] != "pending"
            ):
                return False, "There is no file transfer waiting for acceptance."
            if time.monotonic() - state["last_activity"] >= SCP_TIMEOUT:
                transfer_id = state["id"]
            else:
                transfer_id = None
                descriptor = None
                temporary_path = None
                try:
                    if not self.file_directory.is_dir():
                        raise OSError(
                            f"Destination directory does not exist: "
                            f"{self.file_directory}"
                        )
                    descriptor, temporary_path = tempfile.mkstemp(
                        dir=str(self.file_directory),
                        prefix=".doggychat-",
                        suffix=".part",
                    )
                    file_handle = os.fdopen(descriptor, "wb")
                except OSError as error:
                    if descriptor is not None:
                        try:
                            os.close(descriptor)
                        except OSError:
                            pass
                    if temporary_path is not None:
                        try:
                            Path(temporary_path).unlink()
                        except FileNotFoundError:
                            pass
                        except OSError as cleanup_error:
                            return (
                                False,
                                f"Could not prepare the destination file: {error}; "
                                f"could not remove temporary file: {cleanup_error}",
                            )
                    return False, f"Could not prepare the destination file: {error}"
                state.update(
                    {
                        "phase": "receiving",
                        "file_handle": file_handle,
                        "temporary_path": Path(temporary_path),
                        "destination_path": self.file_directory / state["filename"],
                        "received": 0,
                        "next_chunk": 0,
                        "last_activity": time.monotonic(),
                    }
                )
                sender_id = state["sender_id"]
        if transfer_id is not None:
            self._expire_scp_transfer(transfer_id)
            return False, "The file transfer request has expired."
        accepted = self._send_scp_frame(
            {
                "type": "scp_accept",
                "transfer_id": state["id"],
                "sender_id": sender_id,
                "target_id": self.identity_id,
            }
        )
        if not accepted:
            self._finish_scp_transfer(state["id"])
            return False, "Could not notify the sender that the transfer was accepted."
        self.events.put(("scp_accepted", state["sender_nickname"]))
        return True, ""

    def cancel_file_transfer(self):
        with self._scp_lock:
            state = self._scp_transfer
        if state is None:
            return False, "There is no active file transfer."
        if state["role"] == "sender":
            for transfer_id, target in list(state["targets"].items()):
                self._send_scp_frame(
                    {
                        "type": "scp_cancel",
                        "transfer_id": transfer_id,
                        "sender_id": self.identity_id,
                        "target_id": target["user_id"],
                        "from_id": self.identity_id,
                        "reason": "Cancelled by user",
                    }
                )
            self._finish_scp_transfer(state["id"])
        else:
            self._cancel_scp_transfer(
                state["id"],
                "Cancelled by user",
                send_cancel=True,
                event="scp_cancelled",
            )
        return True, ""

    def update_nickname(self, nickname):
        if not isinstance(nickname, str) or not nickname.strip():
            raise ValueError("Nickname cannot be empty")
        if len(nickname) > MAX_NICKNAME_LENGTH:
            raise ValueError(f"Nickname must be {MAX_NICKNAME_LENGTH} characters or fewer")
        nickname = nickname.strip()
        with self._members_lock:
            reason = self._nickname_conflict_reason_locked(
                nickname,
                excluding_user_id=self.identity_id,
            )
            if reason:
                self._refresh_local_nickname_conflict()
                return False, reason
            self.nickname = nickname
            member = self._members.get(self.identity_id)
            if member is not None:
                member["nickname"] = nickname
        self._publish_local_presence()
        return True, ""

    def set_room_config(self, room_name=None, max_members=None):
        if self.room_owner_id != self.identity_id:
            return False, "Only the room host can change room settings."
        current_members = len(self._member_ids())
        with self._config_lock:
            updated_name = self.room_name
            updated_max = self.max_members
            if room_name is not None:
                if (
                    not isinstance(room_name, str)
                    or not room_name.strip()
                    or len(room_name.strip()) > MAX_ROOM_NAME_LENGTH
                ):
                    return False, (
                        f"Room name must contain 1 to "
                        f"{MAX_ROOM_NAME_LENGTH} characters."
                    )
                updated_name = room_name.strip()
            if max_members is not None:
                if (
                    not isinstance(max_members, int)
                    or isinstance(max_members, bool)
                    or not 1 <= max_members <= MAX_ROOM_MEMBERS
                ):
                    return False, (
                        f"Max member must be between 1 and {MAX_ROOM_MEMBERS}."
                    )
                if max_members < current_members:
                    return False, (
                        f"Max member cannot be below the current room size "
                        f"({current_members})."
                    )
                updated_max = max_members
            if updated_name == self.room_name and updated_max == self.max_members:
                return True, ""
            self.room_name = updated_name
            self.max_members = updated_max
            self._config_revision = max(self._config_revision + 1, time.time_ns())
            config = self._room_config_snapshot()
        frame = {"type": "room_config", **config}
        self._broadcast(_encode_frame(frame))
        self.events.put(("room_config_changed", config))
        return True, ""

    def get_room_config(self):
        return self._room_config_snapshot()

    def get_room_key(self):
        if self.room_owner_id != self.identity_id:
            return False, "Only the room host can view the room key."
        with self._room_key_lock:
            return True, self._room_key

    def set_room_key(self, key):
        if self.room_owner_id != self.identity_id:
            return False, "Only the room host can change the room key."
        if not _valid_room_key(key):
            return False, "Room key must be exactly 4 digits."
        with self._room_key_lock:
            self._room_key = key
        return True, ""

    def disconnect_from_room(self):
        should_clear_history = self._admitted
        if (
            self.room_owner_id == self.identity_id
            and len(self._member_ids()) == 1
            and not self.get_pending_joins()
        ):
            return False, "You are not connected to another room."
        with self._scp_lock:
            transfer_id = (
                self._scp_transfer["id"]
                if self._scp_transfer is not None
                else None
            )
        if transfer_id is not None:
            self._cancel_scp_transfer(
                transfer_id,
                "Room disconnected",
                send_cancel=True,
                event="scp_cancelled",
            )
        self._handover_room_owner()
        if self._admitted:
            self._broadcast(
                _encode_frame(
                    {
                        "type": "leave",
                        "id": self.identity_id,
                        "nickname": self.nickname,
                    }
                )
            )
        with self._peers_lock:
            peers = list(self._peers.values())
        for peer in peers:
            peer.departed = True
            self._remove_peer(peer, "Disconnected from room")
        self._reset_local_room(clear_history=should_clear_history)
        return True, ""

    def get_users(self):
        now = time.monotonic()
        with self._members_lock:
            member_orders = {
                user_id: member["order"]
                for user_id, member in self._members.items()
            }
        with self._users_lock:
            users = [
                {
                    "id": user_id,
                    "nickname": user["nickname"],
                    "ip": user["ip"],
                    "order": member_orders.get(user_id, MAX_ROOM_MEMBERS + 1),
                }
                for user_id, user in self._users.items()
                if user_id == self.identity_id
                or now - user["last_seen"] <= PRESENCE_TIMEOUT
            ]
        return sorted(
            users,
            key=lambda user: (
                user["id"] != self.room_owner_id,
                user["order"],
            ),
        )

    def get_member_count(self):
        return len(self._member_ids())

    def get_pending_joins(self):
        with self._pending_lock:
            return [
                {
                    "request_id": request["request_id"],
                    "user_id": request["user_id"],
                    "nickname": request["nickname"],
                    "ip": request["ip"],
                    "selector": f'{request["nickname"]}-{request["ip"]}',
                    "port": request["port"],
                }
                for request in self._pending_joins.values()
            ]

    def control_join(self, selector, accepted):
        if self.room_owner_id != self.identity_id:
            return False, "Only the room host can control join requests."
        selector = str(selector).strip()
        if not selector:
            return False, "Enter a pending request selector (name-ip)."
        try:
            address = str(ipaddress.IPv4Address(selector))
        except ipaddress.AddressValueError:
            address = None
        if accepted and len(self._member_ids()) >= self.max_members:
            return False, "The room has reached its member limit."
        with self._pending_lock:
            request_id = next(
                (
                    request_id
                    for request_id, request in self._pending_joins.items()
                    if request["nickname"] + "-" + request["ip"] == selector
                    or (address is not None and request["ip"] == address)
                ),
                None,
            )
            if request_id is None:
                return False, f"No pending join request matching {selector}."
        return self._decide_join_request(request_id, accepted)

    def _decide_join_request(self, request_id, accepted):
        if self.room_owner_id != self.identity_id:
            return False, "Only the room host can control join requests."
        member = None
        duplicate_reason = ""
        capacity_reason = ""
        with self._join_decision_lock:
            with self._pending_lock:
                request = self._pending_joins.pop(request_id, None)
                if request is None:
                    return False, "The join request is no longer pending."
                self._pending_routes.pop(request_id, None)

            if accepted:
                with self._members_lock:
                    if len(self._members) >= self.max_members:
                        capacity_reason = (
                            f"Room is full ({len(self._members)}/"
                            f"{self.max_members} members)."
                        )
                    else:
                        duplicate_reason = self._nickname_conflict_reason_locked(
                            request["nickname"]
                        )
                    if not duplicate_reason and not capacity_reason:
                        member = {
                            "id": request["user_id"],
                            "nickname": request["nickname"],
                            "ip": request["ip"],
                            "port": request["port"],
                            "order": self._next_member_order,
                            "revision": time.time_ns(),
                        }
                        self._next_member_order += 1
                        self._add_member(member)
                        self._set_user(
                            member["id"],
                            member["nickname"],
                            member["ip"],
                            member["revision"],
                        )
        decision_reason = duplicate_reason or capacity_reason
        decision = {
            "type": "join_decision",
            "request_id": request_id,
            "accepted": accepted and not decision_reason,
            "member": member,
            "reason": decision_reason,
        }
        self._seen_join_decisions.add(request_id)
        encoded = _encode_frame(decision)
        self._broadcast(encoded)
        if member is not None and not duplicate_reason:
            self._broadcast_presence(member["id"])
        pending_peer = request.get("peer")
        if pending_peer is not None:
            self._send_peer(pending_peer, encoded)
            if accepted and not decision_reason:
                self._activate_peer(pending_peer)
            else:
                self._remove_peer(
                    pending_peer,
                    "Nickname conflict"
                    if duplicate_reason
                    else "Join request denied",
                )
        elif accepted and not decision_reason:
            self._send_history_to_member(member["id"])
        if capacity_reason:
            self.events.put(
                (
                    "join_capacity",
                    (
                        request["nickname"],
                        request["ip"],
                        self.get_member_count(),
                        self.max_members,
                    ),
                )
            )
        self.events.put(
            (
                "join_decision",
                (
                    request["nickname"],
                    request["ip"],
                    accepted and not decision_reason,
                ),
            )
        )
        if decision_reason:
            return False, decision_reason
        return True, ""

    def close(self):
        with self._scp_lock:
            transfer_id = (
                self._scp_transfer["id"]
                if self._scp_transfer is not None
                else None
            )
        if transfer_id is not None:
            self._cancel_scp_transfer(
                transfer_id,
                "Application closed",
                send_cancel=True,
                event="scp_cancelled",
            )
        was_room_owner = self.room_owner_id == self.identity_id
        self._handover_room_owner()
        if was_room_owner:
            self._replace_local_history([])
        if self._admitted:
            self._broadcast(
                _encode_frame(
                    {
                        "type": "leave",
                        "id": self.identity_id,
                        "nickname": self.nickname,
                    }
                )
            )
        self._stop.set()
        if self._listener is not None:
            self._listener.close()
        with self._peers_lock:
            peers = list(self._peers.values())
        for peer in peers:
            try:
                peer.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            peer.connection.close()
        self._tls_identity.cleanup()

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                connection, address = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if not self._stop.is_set():
                    self.events.put(("error", "The chat listener stopped unexpectedly."))
                return
            threading.Thread(
                target=self._accept_tls_peer,
                args=(connection, address),
                daemon=True,
            ).start()

    def _accept_tls_peer(self, connection, address):
        try:
            connection.settimeout(5)
            tls_connection = self._server_tls_context.wrap_socket(
                connection,
                server_side=True,
            )
            tls_connection.settimeout(None)
        except (OSError, ssl.SSLError):
            connection.close()
            return
        self._register_peer(tls_connection, address)

    def _connect_worker(self, address, port, key=None):
        try:
            raw_connection = socket.create_connection((address, port), timeout=5)
            connection = self._client_tls_context.wrap_socket(
                raw_connection,
                server_hostname=address,
            )
            connection.settimeout(None)
        except (OSError, ssl.SSLError) as error:
            self._finish_connect_attempt()
            self.events.put(("error", f"Could not connect to {address}: {error}"))
            return
        if self._stop.is_set():
            connection.close()
            self._finish_connect_attempt()
            return
        self._register_peer(
            connection,
            (address, port),
            initiated=True,
            join_key=key,
        )

    def _register_peer(self, connection, address, initiated=False, join_key=None):
        server_certificate = (
            connection.getpeercert(binary_form=True)
            if initiated
            else None
        )
        peer = _Peer(
            connection,
            address,
            initiated,
            hashlib.sha256(server_certificate).hexdigest()
            if server_certificate
            else None,
        )
        with self._peers_lock:
            self._peers[connection] = peer
        peer.request_id = uuid.uuid4().hex
        local_ip = connection.getsockname()[0]
        if local_ip and local_ip != self._local_ip:
            self._local_ip = local_ip
            self._publish_local_presence()
        hello = {
            "type": "hello",
            "id": self.identity_id,
            "nickname": self.nickname,
            "revision": self._revision,
            "room_owner_id": self.room_owner_id,
            "request_id": peer.request_id,
            "port": self.port,
            "members": self._room_snapshot()["members"],
            **self._room_config_snapshot(),
            **self._tls_identity.sign_hello(
                self.identity_id,
                peer.request_id,
                self.port,
            ),
        }
        if join_key is not None:
            hello["join_key"] = join_key
        if self._send_peer(peer, _encode_frame(hello)):
            threading.Thread(
                target=self._read_peer,
                args=(peer,),
                daemon=True,
            ).start()

    def _read_peer(self, peer):
        buffered = bytearray()
        try:
            while not self._stop.is_set():
                chunk = peer.connection.recv(4096)
                if not chunk:
                    break
                buffered.extend(chunk)
                while b"\n" in buffered:
                    line, _, remainder = buffered.partition(b"\n")
                    buffered = bytearray(remainder)
                    if len(line) > MAX_FRAME_BYTES:
                        raise ValueError("Incoming message frame is too large")
                    self._handle_frame(line, peer)
                if len(buffered) > MAX_FRAME_BYTES:
                    raise ValueError("Incoming message frame is too large")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            pass
        finally:
            self._remove_peer(peer, "Disconnected")

    def _handle_frame(self, line, peer):
        frame = json.loads(line.decode("utf-8"))
        if not isinstance(frame, dict):
            raise ValueError("Invalid frame")
        frame_type = frame.get("type")
        if frame_type == "message":
            self._handle_message(frame, peer)
        elif frame_type == "hello":
            self._handle_hello(frame, peer)
        elif frame_type == "join_request":
            self._handle_join_request(frame, peer)
        elif frame_type == "join_pending":
            self.events.put(("join_pending", frame.get("nickname", "room host")))
        elif frame_type == "join_decision":
            self._handle_join_decision(frame, peer)
        elif frame_type == "room_snapshot":
            self._handle_room_snapshot(frame, peer)
        elif frame_type == "leave":
            self._handle_leave(frame, peer)
        elif frame_type == "presence":
            self._handle_presence(frame, peer)
        elif frame_type == "history":
            self._handle_history(frame, peer)
        elif frame_type == "history_transfer":
            self._handle_history_transfer(frame, peer)
        elif frame_type == "room_owner":
            self._handle_room_owner(frame, peer)
        elif frame_type == "room_config":
            self._handle_room_config(frame, peer)
        elif frame_type == "connection_rejected":
            self._handle_connection_rejected(frame, peer)
        elif frame_type == "scp_request":
            self._handle_scp_request(frame, peer)
        elif frame_type == "scp_accept":
            self._handle_scp_accept(frame, peer)
        elif frame_type == "scp_reject":
            self._handle_scp_reject(frame, peer)
        elif frame_type == "scp_chunk":
            self._handle_scp_chunk(frame, peer)
        elif frame_type == "scp_complete":
            self._handle_scp_complete(frame, peer)
        elif frame_type == "scp_complete_ack":
            self._handle_scp_complete_ack(frame, peer)
        elif frame_type == "scp_cancel":
            self._handle_scp_cancel(frame, peer)
        else:
            raise ValueError("Unsupported frame type")

    def _handle_message(self, frame, peer):
        if (
            not self._admitted
            or not peer.admitted
            or peer.user_id not in self._member_ids()
        ):
            return
        record = _validate_record(frame)
        hops = frame.get("hops")
        if not isinstance(hops, int) or isinstance(hops, bool) or not 0 <= hops <= MAX_MESSAGE_HOPS:
            raise ValueError("Invalid message hop count")
        if not self._remember_message(record["id"]):
            return
        if record["sender_id"] not in self._member_ids():
            return
        if self._member_has_nickname_conflict(record["sender_id"]):
            return

        self.events.put(("message", (peer.address, record)))
        if hops:
            frame["hops"] = hops - 1
            self._broadcast(_encode_frame(frame), exclude=peer.connection)

    def _handle_hello(self, frame, peer):
        user_id = frame.get("id")
        nickname = frame.get("nickname")
        revision = frame.get("revision")
        room_owner_id = frame.get("room_owner_id")
        request_id = frame.get("request_id")
        listen_port = frame.get("port")
        join_key = frame.get("join_key")
        members = _validate_members(frame.get("members"))
        room_config = _validate_room_config(frame)
        if (
            not _valid_uuid(user_id)
            or not isinstance(nickname, str)
            or not nickname.strip()
            or len(nickname) > MAX_NICKNAME_LENGTH
            or not isinstance(revision, int)
            or not _valid_uuid(room_owner_id)
            or not _valid_uuid(request_id)
            or not isinstance(listen_port, int)
            or isinstance(listen_port, bool)
            or not 1 <= listen_port <= 65535
            or room_owner_id not in members
            or user_id not in members
        ):
            raise ValueError("Invalid hello frame")
        try:
            fingerprint = verify_hello_identity(frame)
        except ValueError as error:
            self._reject_tls_identity(peer, nickname, str(error))
            return
        if (
            peer.initiated
            and peer.tls_server_fingerprint != fingerprint
        ):
            self._reject_tls_identity(
                peer,
                nickname,
                "TLS certificate does not match the advertised identity.",
            )
            return
        try:
            trust = self._trust_store.trust(
                frame["tls_identity_id"],
                fingerprint,
            )
        except OSError as error:
            self.events.put(("error", f"Could not save TLS trust data: {error}"))
            self._reject_connection(peer, "Could not save TLS trust data.")
            return
        if trust == "changed":
            self._reject_tls_identity(
                peer,
                nickname,
                "This user's TLS fingerprint changed; connection rejected.",
            )
            return
        if trust == "new":
            self.events.put(
                ("tls_peer_trusted", (nickname.strip(), fingerprint))
            )
        if join_key is not None and not _valid_room_key(join_key):
            self._reject_connection(peer, "Room key must be exactly 4 digits.")
            return
        local_members = self._member_ids()
        if peer.initiated and len(local_members) > 1:
            if room_config["room_id"] != self.room_id:
                self._reject_connection(
                    peer,
                    "Already connected to a different room; disconnect first.",
                )
                return
        elif not peer.initiated and len(members) > 1:
            if room_config["room_id"] != self.room_id:
                self._reject_connection(
                    peer,
                    "Cannot merge two existing rooms; disconnect first.",
                )
                return
        if (
            user_id in local_members
            and room_config["room_id"] != self.room_id
        ):
            self._reject_connection(peer, "This user belongs to a different room.")
            return
        self._finish_connect_attempt(peer)
        peer.user_id = user_id
        peer.nickname = nickname.strip()
        peer.remote_request_id = request_id
        peer.listen_port = listen_port
        if peer.initiated and self.identity_id not in members:
            peer.pending_room_snapshot = (room_owner_id, members, room_config)
            peer.join_deadline = time.monotonic() + JOIN_APPROVAL_TIMEOUT
            peer.member_revision = revision
            self._admitted = False
            return
        if peer.initiated:
            peer.join_deadline = None
        if user_id in self._member_ids():
            self._set_user(user_id, peer.nickname, peer.address[0], revision)
            self._update_member(
                user_id,
                nickname=peer.nickname,
                ip=peer.address[0],
                port=listen_port,
                revision=revision,
            )
            peer.admitted = True
            self._activate_peer(peer)
            return

        if self.room_owner_id == self.identity_id:
            if join_key is not None:
                with self._room_key_lock:
                    key_matches = secrets.compare_digest(
                        join_key,
                        self._room_key,
                    )
                if not key_matches:
                    self._reject_connection(peer, "Invalid room key.")
                    return
            duplicate_reason = self._nickname_conflict_reason(peer.nickname)
            if duplicate_reason:
                self.events.put(
                    (
                        "duplicate_nickname_rejected",
                        (peer.nickname, peer.address[0]),
                    )
                )
                self._reject_connection(peer, duplicate_reason)
                return

        with self._pending_lock:
            self._pending_routes[request_id] = peer
        if self.room_owner_id == self.identity_id:
            if len(self._member_ids()) >= self.max_members:
                self._reject_full_room(peer)
                return
            self._queue_join_request(
                request_id,
                user_id,
                peer.nickname,
                peer.address[0],
                peer.listen_port,
                peer,
                notify=join_key is None,
            )
            if join_key is not None:
                self._decide_join_request(request_id, True)
        else:
            self._send_peer(
                peer,
                _encode_frame(
                    {"type": "join_pending", "nickname": self._owner_name()}
                ),
            )
            self._forward_join_request(
                request_id,
                user_id,
                peer.nickname,
                peer.address[0],
                peer.listen_port,
                exclude=peer.connection,
                join_key=join_key,
            )

    def _handle_room_owner(self, frame, peer):
        if not self._admitted or not peer.admitted:
            return
        previous_owner_id = frame.get("from_id")
        room_owner_id = frame.get("id")
        room_config = _validate_room_config(frame)
        try:
            members = _validate_members(frame.get("members"))
        except ValueError:
            raise
        if (
            not _valid_uuid(previous_owner_id)
            or not _valid_uuid(room_owner_id)
            or previous_owner_id != self.room_owner_id
            or room_owner_id not in members
            or room_config["room_id"] != self.room_id
        ):
            return
        self._replace_room_members(members)
        self.room_owner_id = room_owner_id
        if room_owner_id == self.identity_id:
            with self._room_key_lock:
                self._room_key = _new_room_key()
        self._apply_room_config(room_config)
        nickname = members[room_owner_id]["nickname"]
        self.events.put(("room_owner_changed", nickname))
        self._broadcast(
            _encode_frame(frame),
            exclude=peer.connection,
        )
        if room_owner_id != self.identity_id and not self._has_peer(room_owner_id):
            successor = members[room_owner_id]
            if successor["ip"] and successor["ip"] != "unknown":
                self.connect_to(successor["ip"], successor["port"])

    def _handle_join_request(self, frame, peer):
        if (
            not peer.admitted
            or peer.user_id not in self._member_ids()
        ):
            return
        request_id = frame.get("request_id")
        user_id = frame.get("user_id")
        nickname = frame.get("nickname")
        address = frame.get("ip")
        listen_port = frame.get("port")
        join_key = frame.get("join_key")
        if (
            not _valid_uuid(request_id)
            or not _valid_uuid(user_id)
            or not isinstance(nickname, str)
            or not nickname.strip()
            or len(nickname) > MAX_NICKNAME_LENGTH
            or not isinstance(address, str)
            or not isinstance(listen_port, int)
            or isinstance(listen_port, bool)
            or not 1 <= listen_port <= 65535
        ):
            raise ValueError("Invalid join request")
        address = str(ipaddress.IPv4Address(address))
        if user_id in self._member_ids():
            return
        if join_key is not None and not _valid_room_key(join_key):
            self._reject_join_request(
                request_id,
                "Room key must be exactly 4 digits.",
            )
            return
        if self.room_owner_id == self.identity_id:
            if join_key is not None:
                with self._room_key_lock:
                    key_matches = secrets.compare_digest(
                        join_key,
                        self._room_key,
                    )
                if not key_matches:
                    self._reject_join_request(
                        request_id,
                        "Invalid room key.",
                    )
                    return
            duplicate_reason = self._nickname_conflict_reason(nickname)
            if duplicate_reason:
                self._reject_duplicate_join_request(
                    request_id,
                    nickname,
                    address,
                    duplicate_reason,
                )
                return
            if len(self._member_ids()) >= self.max_members:
                self._reject_full_join_request(request_id, nickname, address)
                return
            self._queue_join_request(
                request_id,
                user_id,
                nickname,
                address,
                listen_port,
                None,
                notify=join_key is None,
            )
            if join_key is not None:
                self._decide_join_request(request_id, True)
            return
        self._forward_join_request(
            request_id,
            user_id,
            nickname,
            address,
            listen_port,
            exclude=peer.connection,
            join_key=join_key,
        )

    def _handle_join_decision(self, frame, peer):
        request_id = frame.get("request_id")
        accepted = frame.get("accepted")
        member = frame.get("member")
        reason = frame.get("reason", "")
        if (
            not _valid_uuid(request_id)
            or not isinstance(accepted, bool)
            or not isinstance(reason, str)
            or len(reason) > MAX_MESSAGE_LENGTH
            or (accepted and reason)
        ):
            raise ValueError("Invalid join decision")
        if accepted:
            if not isinstance(member, dict):
                raise ValueError("Accepted join decision requires a member")
            member = _validate_members([member])[member["id"]]
            if member["id"] not in self._member_ids():
                self._add_member(member)
            if member["id"] == self.identity_id:
                self._admitted = True
                self._replace_local_history([])
                self.events.put(("history_reset", None))
        elif member is not None:
            raise ValueError("Denied join decision must not include a member")

        member_ids = self._member_ids()
        with self._pending_lock:
            if request_id in self._seen_join_decisions:
                return
            pending_snapshot = peer.pending_room_snapshot
            if pending_snapshot is not None:
                if request_id != peer.request_id:
                    return
                if accepted and member["id"] != self.identity_id:
                    raise ValueError("Join decision must admit the local member")
                peer.pending_room_snapshot = None
                peer.join_deadline = None
            elif not peer.admitted or peer.user_id not in member_ids:
                return
            self._seen_join_decisions.add(request_id)
            pending_peer = (
                None
                if pending_snapshot is not None
                else self._pending_routes.pop(request_id, None)
            )

        if pending_snapshot is not None:
            if accepted:
                room_owner_id, members, room_config = pending_snapshot
                self._adopt_room_snapshot(room_owner_id, members, room_config)
                self._add_member(member)
                self._set_user(
                    member["id"],
                    member["nickname"],
                    member["ip"],
                    member["revision"],
                )
                self._admitted = True
                self._replace_local_history([])
                self.events.put(("history_reset", None))
                self.events.put(("join_result", (True, self._owner_name())))
                self._activate_peer(peer)
            else:
                self._admitted = True
                self.events.put(
                    ("join_result", (False, self._owner_name(), reason))
                )
                peer.departed = True
                self._remove_peer(peer, "Join request denied")
            return

        if pending_peer is not None:
            self._send_peer(pending_peer, _encode_frame(frame))
            if accepted and pending_peer.user_id == member["id"]:
                pending_peer.admitted = True
                self._activate_peer(pending_peer)
            elif not accepted:
                pending_peer.departed = True
                self._remove_peer(pending_peer, "Join request denied")

        if accepted:
            if member["id"] == self.identity_id:
                self.events.put(("join_result", (True, self._owner_name())))
            changed = self._set_user(
                member["id"],
                member["nickname"],
                member["ip"],
                member["revision"],
            )
            if changed:
                self._broadcast_presence(member["id"])
            self._activate_approved_peers(member["id"])
            if member["id"] == self.identity_id:
                self._activate_all_peers()
        else:
            with self._peers_lock:
                rejected_peers = [
                    peer
                    for peer in self._peers.values()
                    if peer.request_id == request_id
                ]
            for rejected_peer in rejected_peers:
                rejected_peer.departed = True
                self._remove_peer(rejected_peer, "Join request denied")
            if rejected_peers:
                owner_name = self._owner_name()
                self.events.put(
                    ("join_result", (False, owner_name, reason))
                )
                if not self._admitted:
                    self._reset_local_room()
        self._broadcast(_encode_frame(frame))

    def _handle_room_snapshot(self, frame, peer):
        if not peer.admitted:
            return
        room_owner_id = frame.get("room_owner_id")
        members = _validate_members(frame.get("members"))
        room_config = _validate_room_config(frame)
        if (
            not _valid_uuid(room_owner_id)
            or room_owner_id not in members
        ):
            raise ValueError("Invalid room snapshot")
        if self.identity_id not in self._member_ids():
            self._adopt_room_snapshot(room_owner_id, members, room_config)
        elif (
            room_owner_id == self.room_owner_id
            and room_config["room_id"] == self.room_id
        ):
            self._merge_room_members(members)
            self._apply_room_config(room_config)

    def _handle_room_config(self, frame, peer):
        if (
            not self._admitted
            or not peer.admitted
            or peer.user_id not in self._member_ids()
        ):
            return
        config = _validate_room_config(frame)
        if config["room_id"] != self.room_id:
            return
        if self._apply_room_config(config):
            self.events.put(("room_config_changed", config))
            self._broadcast(
                _encode_frame({"type": "room_config", **config}),
                exclude=peer.connection,
            )

    def _handle_connection_rejected(self, frame, peer):
        reason = frame.get("reason")
        if not isinstance(reason, str) or not reason:
            raise ValueError("Invalid connection rejection")
        if not peer.initiated or (peer.admitted and self._admitted):
            return
        peer.departed = True
        self._remove_peer(peer, "Connection rejected")
        if peer.pending_room_snapshot is not None:
            peer.pending_room_snapshot = None
            peer.join_deadline = None
            self._admitted = True
        self.events.put(("connection_failed", reason))

    def _handle_scp_request(self, frame, peer):
        transfer_id = frame.get("transfer_id")
        sender_id = frame.get("sender_id")
        target_id = frame.get("target_id")
        filename = frame.get("filename")
        size = frame.get("size")
        if (
            not self._route_scp_frame(frame, peer, target_id)
            or not _valid_uuid(transfer_id)
            or not _valid_uuid(sender_id)
            or not _valid_file_name(filename)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            return
        with self._members_lock:
            sender = self._members.get(sender_id)
        if sender is None:
            return
        with self._scp_lock:
            if self._scp_transfer is not None:
                busy = True
            else:
                busy = False
                self._scp_transfer = {
                    "id": transfer_id,
                    "role": "receiver",
                    "phase": "pending",
                    "sender_id": sender_id,
                    "target_id": self.identity_id,
                    "sender_nickname": sender["nickname"],
                    "filename": filename,
                    "size": size,
                    "last_activity": time.monotonic(),
                    "cancelled": threading.Event(),
                }
        if busy:
            self._send_scp_frame(
                {
                    "type": "scp_reject",
                    "transfer_id": transfer_id,
                    "sender_id": sender_id,
                    "target_id": target_id,
                    "reason": "The receiver is busy with another transfer.",
                }
            )
            return
        self.events.put(
            ("scp_request", (sender["nickname"], sender["ip"], filename, size))
        )

    def _handle_scp_accept(self, frame, peer):
        if not self._route_scp_frame(frame, peer, frame.get("sender_id")):
            return
        transfer_id = frame.get("transfer_id")
        with self._scp_lock:
            group = self._scp_transfer
            target = (
                group["targets"].get(transfer_id)
                if group is not None and group["role"] == "sender"
                else None
            )
            if (
                target is None
                or target["phase"] != "pending"
                or target["user_id"] != frame.get("target_id")
            ):
                return
        try:
            source = group["source_path"].open("rb")
            if os.fstat(source.fileno()).st_size != group["size"]:
                source.close()
                raise OSError("The source file changed before acceptance.")
        except OSError as error:
            self._cancel_scp_transfer(
                group["id"],
                f"Could not read the source file: {error}",
                send_cancel=True,
                event="scp_error",
            )
            return
        with self._scp_lock:
            if (
                self._scp_transfer is not group
                or group["targets"].get(transfer_id) is not target
            ):
                source.close()
                return
            target["phase"] = "sending"
            target["last_activity"] = time.monotonic()
        self.events.put(("scp_accepted", target["nickname"]))
        threading.Thread(
            target=self._send_file_worker,
            args=(group, target, source),
            daemon=True,
        ).start()

    def _handle_scp_reject(self, frame, peer):
        if not self._route_scp_frame(frame, peer, frame.get("sender_id")):
            return
        reason = frame.get("reason")
        if not isinstance(reason, str) or not reason:
            return
        transfer_id = frame.get("transfer_id")
        with self._scp_lock:
            group = self._scp_transfer
            target = (
                group["targets"].get(transfer_id)
                if group is not None and group["role"] == "sender"
                else None
            )
            if target is None or target["user_id"] != frame.get("target_id"):
                return
        self._finish_scp_target(group["id"], transfer_id)
        self.events.put(("scp_rejected", (target["nickname"], reason)))

    def _handle_scp_chunk(self, frame, peer):
        if not self._route_scp_frame(frame, peer, frame.get("target_id")):
            return
        transfer_id = frame.get("transfer_id")
        sender_id = frame.get("sender_id")
        target_id = frame.get("target_id")
        index = frame.get("index")
        encoded = frame.get("data")
        with self._scp_lock:
            state = self._scp_transfer
            if (
                state is None
                or state["role"] != "receiver"
                or state["phase"] != "receiving"
                or state["id"] != transfer_id
                or sender_id != state["sender_id"]
                or target_id != self.identity_id
                or not isinstance(index, int)
                or isinstance(index, bool)
                or index != state["next_chunk"]
                or not isinstance(encoded, str)
                or len(encoded) > 4 * ((SCP_CHUNK_BYTES + 2) // 3)
            ):
                return
            try:
                data = base64.b64decode(encoded, validate=True)
                if (
                    not data
                    or len(data) > SCP_CHUNK_BYTES
                    or state["received"] + len(data) > state["size"]
                ):
                    raise ValueError("Invalid file data chunk.")
                written = state["file_handle"].write(data)
                if written != len(data):
                    raise OSError("Could not write the complete file chunk.")
            except (OSError, ValueError) as error:
                transfer_id = state["id"]
                failure = str(error)
            else:
                state["received"] += len(data)
                state["next_chunk"] += 1
                state["last_activity"] = time.monotonic()
                failure = None
        if failure is not None:
            self._cancel_scp_transfer(
                transfer_id,
                failure,
                send_cancel=True,
                event="scp_error",
            )

    def _handle_scp_complete(self, frame, peer):
        if not self._route_scp_frame(frame, peer, frame.get("target_id")):
            return
        with self._scp_lock:
            state = self._scp_transfer
            if (
                state is None
                or state["role"] != "receiver"
                or state["phase"] != "receiving"
                or state["id"] != frame.get("transfer_id")
                or state["sender_id"] != frame.get("sender_id")
                or state["target_id"] != frame.get("target_id")
            ):
                return
        try:
            with self._scp_lock:
                if self._scp_transfer is not state:
                    return
                if state["received"] != state["size"]:
                    raise ValueError("The received file is incomplete.")
                state["file_handle"].flush()
                os.fsync(state["file_handle"].fileno())
                state["file_handle"].close()
                os.replace(state["temporary_path"], state["destination_path"])
                filename = state["filename"]
                state["temporary_path"] = None
                self._scp_transfer = None
                state["cancelled"].set()
        except (OSError, ValueError) as error:
            self._cancel_scp_transfer(
                state["id"],
                f"Could not finish the received file: {error}",
                send_cancel=True,
                event="scp_error",
            )
            return
        self._send_scp_frame(
            {
                "type": "scp_complete_ack",
                "transfer_id": state["id"],
                "sender_id": state["sender_id"],
                "target_id": self.identity_id,
            }
        )
        self.events.put(("scp_received", filename))

    def _handle_scp_complete_ack(self, frame, peer):
        if not self._route_scp_frame(frame, peer, frame.get("sender_id")):
            return
        transfer_id = frame.get("transfer_id")
        with self._scp_lock:
            group = self._scp_transfer
            target = (
                group["targets"].get(transfer_id)
                if group is not None and group["role"] == "sender"
                else None
            )
            if (
                target is None
                or target["phase"] != "sending"
                or target["user_id"] != frame.get("target_id")
            ):
                return
            filename = group["filename"]
        self._finish_scp_target(group["id"], transfer_id)
        self.events.put(("scp_sent", (target["nickname"], filename)))

    def _handle_scp_cancel(self, frame, peer):
        sender_id = frame.get("sender_id")
        target_id = frame.get("target_id")
        from_id = frame.get("from_id")
        destination_id = target_id if from_id == sender_id else sender_id
        if (
            from_id not in (sender_id, target_id)
            or not self._route_scp_frame(frame, peer, destination_id)
        ):
            return
        transfer_id = frame.get("transfer_id")
        reason = frame.get("reason", "Cancelled by peer")
        with self._scp_lock:
            state = self._scp_transfer
            if state is None or not isinstance(reason, str):
                return
            if state["role"] == "sender":
                target = state["targets"].get(transfer_id)
                if (
                    target is None
                    or sender_id != self.identity_id
                    or target["user_id"] != target_id
                ):
                    return
                group_id = state["id"]
                nickname = target["nickname"]
            elif (
                state["id"] != transfer_id
                or state["sender_id"] != sender_id
                or state["target_id"] != target_id
                or target_id != self.identity_id
            ):
                return
            else:
                group_id = transfer_id
                nickname = state["sender_nickname"]
        if state["role"] == "sender":
            self._finish_scp_target(group_id, transfer_id)
        else:
            self._finish_scp_transfer(group_id)
        self.events.put(("scp_cancelled", (nickname, reason)))

    def _send_scp_frame(self, frame):
        if not self._remember_scp_frame(frame):
            return False
        return self._broadcast(_encode_frame(frame)) > 0

    def _route_scp_frame(self, frame, peer, destination_id):
        if (
            not self._admitted
            or not peer.admitted
            or not _valid_uuid(frame.get("sender_id"))
            or not _valid_uuid(frame.get("target_id"))
            or frame["sender_id"] not in self._member_ids()
            or frame["target_id"] not in self._member_ids()
        ):
            return False
        if destination_id != self.identity_id:
            key = (
                frame.get("transfer_id"),
                frame.get("type"),
                frame.get("index"),
            )
            with self._scp_lock:
                if key in self._seen_scp_frames:
                    return False
                self._remember_scp_frame(frame)
            self._broadcast(_encode_frame(frame), exclude=peer.connection)
            return False
        return self._remember_scp_frame(frame)

    def _remember_scp_frame(self, frame):
        key = (
            frame.get("transfer_id"),
            frame.get("type"),
            frame.get("index"),
        )
        with self._scp_lock:
            if key in self._seen_scp_frames:
                return False
            if len(self._seen_scp_frame_order) >= 8192:
                expired = self._seen_scp_frame_order.popleft()
                self._seen_scp_frames.remove(expired)
            self._seen_scp_frame_order.append(key)
            self._seen_scp_frames.add(key)
            return True

    def _finish_scp_target(self, group_id, transfer_id):
        with self._scp_lock:
            group = self._scp_transfer
            if (
                group is None
                or group["id"] != group_id
                or group["role"] != "sender"
            ):
                return None
            target = group["targets"].pop(transfer_id, None)
            if target is None:
                return None
            target["cancelled"].set()
            if not group["targets"]:
                group["cancelled"].set()
                self._scp_transfer = None
            return target

    def _send_file_worker(self, group, target, source):
        remaining = group["size"]
        index = 0
        try:
            with source:
                while remaining:
                    if group["cancelled"].is_set() or target["cancelled"].is_set():
                        return
                    data = source.read(min(SCP_CHUNK_BYTES, remaining))
                    if not data:
                        raise OSError("The source file ended before its advertised size.")
                    if not self._send_scp_frame(
                        {
                            "type": "scp_chunk",
                            "transfer_id": target["id"],
                            "sender_id": self.identity_id,
                            "target_id": target["user_id"],
                            "index": index,
                            "data": base64.b64encode(data).decode("ascii"),
                        }
                    ):
                        raise OSError("The peer connection was lost.")
                    remaining -= len(data)
                    index += 1
                    with self._scp_lock:
                        if (
                            self._scp_transfer is not group
                            or group["targets"].get(target["id"]) is not target
                        ):
                            return
                        target["last_activity"] = time.monotonic()
                if group["cancelled"].is_set() or target["cancelled"].is_set():
                    return
                if not self._send_scp_frame(
                    {
                        "type": "scp_complete",
                        "transfer_id": target["id"],
                        "sender_id": self.identity_id,
                        "target_id": target["user_id"],
                    }
                ):
                    raise OSError("The peer connection was lost.")
                with self._scp_lock:
                    target["last_activity"] = time.monotonic()
        except (OSError, ValueError) as error:
            self._send_scp_frame(
                {
                    "type": "scp_cancel",
                    "transfer_id": target["id"],
                    "sender_id": self.identity_id,
                    "target_id": target["user_id"],
                    "from_id": self.identity_id,
                    "reason": str(error),
                }
            )
            self._finish_scp_target(group["id"], target["id"])
            self.events.put(("scp_error", str(error)))

    def _scp_timeout_loop(self):
        while not self._stop.wait(1):
            now = time.monotonic()
            with self._scp_lock:
                state = self._scp_transfer
                if state is None:
                    expired = []
                elif state["role"] == "sender":
                    expired = [
                        (state["id"], transfer_id, target)
                        for transfer_id, target in state["targets"].items()
                        if now - target["last_activity"] >= SCP_TIMEOUT
                    ]
                elif now - state["last_activity"] >= SCP_TIMEOUT:
                    expired = [(state["id"], None, state)]
                else:
                    expired = []
            for group_id, transfer_id, item in expired:
                if transfer_id is None:
                    self._expire_scp_transfer(group_id)
                else:
                    self._expire_scp_target(group_id, transfer_id, item)

    def _expire_scp_target(self, group_id, transfer_id, target):
        self._send_scp_frame(
            {
                "type": "scp_cancel",
                "transfer_id": transfer_id,
                "sender_id": self.identity_id,
                "target_id": target["user_id"],
                "from_id": self.identity_id,
                "reason": "5-minute timeout",
            }
        )
        self._finish_scp_target(group_id, transfer_id)
        self.events.put(("scp_timeout", target["nickname"]))

    def _expire_scp_transfer(self, transfer_id):
        self._cancel_scp_transfer(
            transfer_id,
            "5-minute timeout",
            send_cancel=True,
            event="scp_timeout",
        )

    def _cancel_scp_transfer(
        self,
        transfer_id,
        reason,
        send_cancel,
        event,
    ):
        with self._scp_lock:
            state = self._scp_transfer
            if state is None or state["id"] != transfer_id:
                return
            sender_role = state["role"] == "sender"
            targets = list(state["targets"].items()) if sender_role else []
        if sender_role:
            if send_cancel:
                for target_id, target in targets:
                    self._send_scp_frame(
                        {
                            "type": "scp_cancel",
                            "transfer_id": target_id,
                            "sender_id": self.identity_id,
                            "target_id": target["user_id"],
                            "from_id": self.identity_id,
                            "reason": reason,
                        }
                    )
            self._finish_scp_transfer(transfer_id)
            self.events.put((event, reason))
            return
        if send_cancel:
            self._send_scp_frame(
                {
                    "type": "scp_cancel",
                    "transfer_id": transfer_id,
                    "sender_id": state["sender_id"],
                    "target_id": state["target_id"],
                    "from_id": self.identity_id,
                    "reason": reason,
                }
            )
        self._finish_scp_transfer(transfer_id)
        event_data = (
            state["sender_nickname"]
            if event == "scp_timeout"
            else reason
        )
        self.events.put((event, event_data))

    def _finish_scp_transfer(self, transfer_id):
        with self._scp_lock:
            state = self._scp_transfer
            if state is None or state["id"] != transfer_id:
                return None
            self._scp_transfer = None
            state["cancelled"].set()
            if state["role"] == "sender":
                for target in state["targets"].values():
                    target["cancelled"].set()
        file_handle = state.get("file_handle")
        if file_handle is not None:
            try:
                file_handle.close()
            except OSError:
                pass
        temporary_path = state.get("temporary_path")
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                self.events.put(("scp_error", f"Could not remove temporary file: {error}"))
        return state

    def _handle_leave(self, frame, peer):
        if not self._admitted or not peer.admitted:
            return
        user_id = frame.get("id")
        nickname = frame.get("nickname")
        if (
            not _valid_uuid(user_id)
            or not isinstance(nickname, str)
            or not nickname.strip()
        ):
            raise ValueError("Invalid leave frame")
        with self._seen_leaves_lock:
            if user_id in self._seen_leaves:
                return
            self._seen_leaves.add(user_id)
        if peer.user_id == user_id:
            peer.departed = True
        was_member = user_id in self._member_ids()
        self._remove_room_member(user_id)
        if (
            was_member
            and self.room_owner_id == self.identity_id
            and len(self._member_ids()) == 1
        ):
            self._clear_local_history()
        self._broadcast(_encode_frame(frame), exclude=peer.connection)
        self.events.put(("member_left", nickname.strip()))
        with self._peers_lock:
            peers = [
                candidate
                for candidate in self._peers.values()
                if candidate.user_id == user_id
            ]
        for candidate in peers:
            candidate.departed = True
            self._remove_peer(candidate, "Disconnected")

    def _queue_join_request(
        self,
        request_id,
        user_id,
        nickname,
        address,
        listen_port,
        peer,
        notify=True,
    ):
        address = str(ipaddress.IPv4Address(address))
        if user_id in self._member_ids():
            return
        with self._pending_lock:
            if request_id in self._seen_join_requests:
                return
            self._seen_join_requests.add(request_id)
            request = {
                "request_id": request_id,
                "user_id": user_id,
                "nickname": nickname,
                "ip": address,
                "port": listen_port,
                "peer": peer,
            }
            self._pending_joins[request_id] = request
            if peer is not None:
                self._pending_routes[request_id] = peer
        if peer is not None and notify:
            self._send_peer(
                peer,
                _encode_frame(
                    {"type": "join_pending", "nickname": self.nickname}
                ),
            )
        if notify:
            self.events.put(("join_request", (nickname, address)))

    def _forward_join_request(
        self,
        request_id,
        user_id,
        nickname,
        address,
        listen_port,
        exclude=None,
        join_key=None,
    ):
        with self._pending_lock:
            if request_id in self._seen_join_requests:
                return
            self._seen_join_requests.add(request_id)
        frame = {
            "type": "join_request",
            "request_id": request_id,
            "user_id": user_id,
            "nickname": nickname,
            "ip": address,
            "port": listen_port,
        }
        if join_key is not None:
            frame["join_key"] = join_key
        self._broadcast(_encode_frame(frame), exclude=exclude)

    def _adopt_room_snapshot(self, room_owner_id, members, room_config):
        if room_owner_id not in members:
            return
        self._replace_room_members(members)
        self.room_owner_id = room_owner_id
        self.room_id = room_config["room_id"]
        self._apply_room_config(room_config, force=True)
        self._admitted = False

    def _room_snapshot(self):
        with self._members_lock:
            snapshot = {
                "type": "room_snapshot",
                "room_owner_id": self.room_owner_id,
                "members": [
                    dict(member) for member in self._members.values()
                ],
            }
        snapshot.update(self._room_config_snapshot())
        return snapshot

    def _room_config_snapshot(self):
        with self._config_lock:
            return {
                "room_id": self.room_id,
                "room_name": self.room_name,
                "max_members": self.max_members,
                "config_revision": self._config_revision,
            }

    def _apply_room_config(self, config, force=False):
        if not force and config["room_id"] != self.room_id:
            return False
        with self._config_lock:
            if not force and config["config_revision"] <= self._config_revision:
                return False
            self.room_id = config["room_id"]
            self.room_name = config["room_name"]
            self.max_members = config["max_members"]
            self._config_revision = config["config_revision"]
            return True

    def _reset_local_room(self, clear_history=False):
        self.room_owner_id = self.identity_id
        self.room_id = uuid.uuid4().hex
        self._admitted = True
        with self._room_key_lock:
            self._room_key = _new_room_key()
        with self._config_lock:
            self.room_name = DEFAULT_ROOM_NAME
            self.max_members = DEFAULT_ROOM_MAX_MEMBERS
            self._config_revision = time.time_ns()
        with self._members_lock:
            self._members = {
                self.identity_id: {
                    "id": self.identity_id,
                    "nickname": self.nickname,
                    "ip": self._local_ip,
                    "port": self.port,
                    "order": 0,
                    "revision": self._revision,
                }
            }
            self._next_member_order = 1
        with self._users_lock:
            self._users.clear()
        self._set_user(
            self.identity_id,
            self.nickname,
            self._local_ip,
            self._revision,
        )
        with self._pending_lock:
            self._pending_joins.clear()
            self._pending_routes.clear()
            self._seen_join_requests.clear()
            self._seen_join_decisions.clear()
        with self._seen_leaves_lock:
            self._seen_leaves.clear()
        with self._seen_lock:
            self._seen_messages.clear()
            self._seen_message_order.clear()
        with self._history_transfer_lock:
            self._seen_history_chunks.clear()
            self._seen_history_order.clear()
            self._history_transfers.clear()
        self._finish_connect_attempt()
        if clear_history:
            self._clear_local_history()
        self._refresh_local_nickname_conflict()

    def _replace_local_history(self, records):
        if self.history_consumer is None:
            return
        try:
            self.history_consumer(records)
        except OSError as error:
            self.events.put(("error", f"Could not update chat history: {error}"))

    def _clear_local_history(self):
        self._replace_local_history([])
        self.events.put(("history_cleared", None))

    def _reject_connection(self, peer, reason):
        if peer.initiated:
            self.events.put(("connection_failed", reason))
        self._send_peer(
            peer,
            _encode_frame(
                {"type": "connection_rejected", "reason": reason}
            ),
        )
        peer.departed = True
        self._remove_peer(peer, "Connection rejected")

    def _reject_tls_identity(self, peer, nickname, reason):
        self.events.put(
            ("tls_identity_rejected", (nickname, peer.address[0], reason))
        )
        self._reject_connection(peer, reason)

    def _reject_full_room(self, peer):
        current_members = len(self._member_ids())
        reason = (
            f"Room is full ({current_members}/{self.max_members} members)."
        )
        self.events.put(
            (
                "join_capacity",
                (peer.nickname or "Unknown user", peer.address[0], current_members, self.max_members),
            )
        )
        self._reject_connection(peer, reason)

    def _reject_full_join_request(self, request_id, nickname, address):
        with self._pending_lock:
            if request_id in self._seen_join_requests:
                return
            self._seen_join_requests.add(request_id)
            self._seen_join_decisions.add(request_id)
        current_members = len(self._member_ids())
        reason = f"Room is full ({current_members}/{self.max_members} members)."
        self.events.put(
            (
                "join_capacity",
                (nickname, address, current_members, self.max_members),
            )
        )
        self._broadcast(
            _encode_frame(
                {
                    "type": "join_decision",
                    "request_id": request_id,
                    "accepted": False,
                    "member": None,
                    "reason": reason,
                }
            )
        )

    def _reject_join_request(self, request_id, reason):
        with self._pending_lock:
            if request_id in self._seen_join_requests:
                return
            self._seen_join_requests.add(request_id)
            self._seen_join_decisions.add(request_id)
        self._broadcast(
            _encode_frame(
                {
                    "type": "join_decision",
                    "request_id": request_id,
                    "accepted": False,
                    "member": None,
                    "reason": reason,
                }
            )
        )

    def _reject_duplicate_join_request(
        self, request_id, nickname, address, reason
    ):
        with self._pending_lock:
            if request_id in self._seen_join_requests:
                return
            self._seen_join_requests.add(request_id)
            self._seen_join_decisions.add(request_id)
        self.events.put(
            ("duplicate_nickname_rejected", (nickname, address))
        )
        self._broadcast(
            _encode_frame(
                {
                    "type": "join_decision",
                    "request_id": request_id,
                    "accepted": False,
                    "member": None,
                    "reason": reason,
                }
            )
        )

    def _finish_connect_attempt(self, peer=None):
        if peer is not None:
            if not peer.connect_attempt:
                return
            peer.connect_attempt = False
        with self._connect_lock:
            self._connect_in_progress = False

    def _member_ids(self):
        with self._members_lock:
            return set(self._members)

    def _has_peer(self, user_id):
        with self._peers_lock:
            return any(
                peer.user_id == user_id and peer.admitted
                for peer in self._peers.values()
            )

    def _replace_room_members(self, members):
        with self._members_lock:
            self._members = dict(members)
            self._next_member_order = max(
                (member["order"] for member in members.values()),
                default=-1,
            ) + 1
        self._refresh_local_nickname_conflict()

    def _merge_room_members(self, members):
        with self._members_lock:
            for user_id, member in members.items():
                self._members.setdefault(user_id, member)
            self._members = dict(
                sorted(self._members.items(), key=lambda item: item[1]["order"])
            )
            self._next_member_order = max(
                self._next_member_order,
                max((member["order"] for member in self._members.values()), default=-1) + 1,
            )
        self._refresh_local_nickname_conflict()

    def _add_member(self, member):
        members = _validate_members([member])
        user_id, normalized = next(iter(members.items()))
        with self._members_lock:
            self._members[user_id] = normalized
            self._members = dict(
                sorted(self._members.items(), key=lambda item: item[1]["order"])
            )
            self._next_member_order = max(
                self._next_member_order,
                normalized["order"] + 1,
            )
        self._refresh_local_nickname_conflict()

    def _update_member(self, user_id, **updates):
        with self._members_lock:
            member = self._members.get(user_id)
            if member is not None:
                member.update(updates)
        if member is not None:
            self._refresh_local_nickname_conflict()

    def _remove_room_member(self, user_id):
        with self._members_lock:
            self._members.pop(user_id, None)
        with self._users_lock:
            self._users.pop(user_id, None)
        self._refresh_local_nickname_conflict()

    def _nickname_conflict_reason_locked(
        self, nickname, excluding_user_id=None
    ):
        normalized = nickname.strip().casefold()
        for user_id, member in self._members.items():
            if (
                user_id != excluding_user_id
                and member["nickname"].strip().casefold() == normalized
            ):
                return f"Nickname '{nickname.strip()}' is already in use."
        return ""

    def _nickname_conflict_reason(self, nickname, excluding_user_id=None):
        with self._members_lock:
            return self._nickname_conflict_reason_locked(
                nickname,
                excluding_user_id,
            )

    def _member_has_nickname_conflict(self, user_id):
        with self._members_lock:
            member = self._members.get(user_id)
            if member is None:
                return False
            return bool(
                self._nickname_conflict_reason_locked(
                    member["nickname"],
                    excluding_user_id=user_id,
                )
            )

    def _refresh_local_nickname_conflict(self):
        conflicted = self._member_has_nickname_conflict(self.identity_id)
        with self._nickname_conflict_lock:
            if conflicted and not self._nickname_conflict_active:
                self.events.put(("nickname_conflict", self.nickname))
            self._nickname_conflict_active = conflicted

    def _owner_name(self):
        with self._members_lock:
            owner = self._members.get(self.room_owner_id)
        return owner["nickname"] if owner else "room host"

    def _activate_peer(self, peer):
        peer.admitted = True
        if not self._admitted or peer.ready:
            return
        peer.ready = True
        self.events.put(("connected", peer.nickname or "Unknown user"))
        self._send_peer(peer, _encode_frame(self._room_snapshot()))
        for user in self._presence_snapshot():
            self._send_peer(peer, _encode_frame({"type": "presence", **user}))
        if self.room_owner_id == self.identity_id:
            self._send_history(peer)

    def _activate_approved_peers(self, user_id):
        with self._peers_lock:
            peers = [
                peer
                for peer in self._peers.values()
                if peer.user_id == user_id
            ]
        for peer in peers:
            peer.admitted = True
            self._activate_peer(peer)

    def _activate_all_peers(self):
        with self._peers_lock:
            peers = [
                peer for peer in self._peers.values() if peer.admitted
            ]
        for peer in peers:
            self._activate_peer(peer)

    def _handover_room_owner(self):
        if self.room_owner_id != self.identity_id:
            return
        with self._members_lock:
            candidates = [
                member
                for user_id, member in self._members.items()
                if user_id != self.identity_id
            ]
        online_ids = {user["id"] for user in self.get_users()}
        successor = next(
            (member for member in candidates if member["id"] in online_ids),
            None,
        )
        if successor is None:
            return
        self._remove_room_member(self.identity_id)
        self._send_history_handover(successor["id"])
        frame = {
            "type": "room_owner",
            "from_id": self.identity_id,
            "id": successor["id"],
            "members": self._room_snapshot()["members"],
            **self._room_config_snapshot(),
        }
        self._broadcast(_encode_frame(frame))
        self.events.put(("room_owner_changed", successor["nickname"]))

    def _send_history_handover(self, target_id):
        records = (
            [_validate_record(record) for record in self.history_provider()]
            if self.history_provider is not None
            else []
        )[-MAX_HISTORY_ENTRIES:]
        self._send_history_transfer(target_id, "handover", records)

    def _send_history_to_member(self, target_id):
        if self.history_provider is None:
            return
        records = [
            _validate_record(record) for record in self.history_provider()
        ][-MAX_HISTORY_ENTRIES:]
        if records:
            self._send_history_transfer(target_id, "sync", records)

    def _send_history_transfer(self, target_id, purpose, records):
        batches = []
        current = []
        for record in records:
            candidate = current + [record]
            probe = {
                "type": "history_transfer",
                "transfer_id": uuid.uuid4().hex,
                "owner_id": self.identity_id,
                "target_id": target_id,
                "purpose": purpose,
                "index": MAX_HISTORY_ENTRIES,
                "total": MAX_HISTORY_ENTRIES,
                "items": candidate,
            }
            try:
                _encode_frame(probe)
            except ValueError:
                if not current:
                    raise
                batches.append(current)
                current = [record]
            else:
                current = candidate
        if current or not batches:
            batches.append(current)
        transfer_id = uuid.uuid4().hex
        total = len(batches)
        for index, batch in enumerate(batches):
            self._broadcast(
                _encode_frame(
                    {
                        "type": "history_transfer",
                        "transfer_id": transfer_id,
                        "owner_id": self.identity_id,
                        "target_id": target_id,
                        "purpose": purpose,
                        "index": index,
                        "total": total,
                        "items": batch,
                    }
                )
            )

    def _handle_presence(self, frame, peer):
        if (
            not self._admitted
            or not peer.admitted
            or peer.user_id not in self._member_ids()
        ):
            return
        user_id = frame.get("id")
        nickname = frame.get("nickname")
        ip = frame.get("ip", "")
        revision = frame.get("revision")
        if (
            not _valid_uuid(user_id)
            or not isinstance(nickname, str)
            or not nickname.strip()
            or len(nickname) > MAX_NICKNAME_LENGTH
            or not isinstance(ip, str)
            or not isinstance(revision, int)
        ):
            raise ValueError("Invalid presence frame")
        if ip:
            ipaddress.IPv4Address(ip)
        if user_id not in self._member_ids():
            return
        if self._set_user(user_id, nickname, ip, revision):
            updates = {"nickname": nickname.strip(), "revision": revision}
            if ip:
                updates["ip"] = ip
            self._update_member(user_id, **updates)
            self._broadcast_presence(user_id, exclude=peer.connection)

    def _handle_history(self, frame, peer):
        if (
            not self._admitted
            or not peer.admitted
            or peer.user_id not in self._member_ids()
            or peer.user_id != self.room_owner_id
        ):
            return
        items = frame.get("items")
        if not isinstance(items, list) or len(items) > MAX_HISTORY_ENTRIES:
            raise ValueError("Invalid history frame")
        self.events.put(("history_batch", [_validate_record(item) for item in items]))

    def _handle_history_transfer(self, frame, peer):
        if (
            not self._admitted
            or not peer.admitted
            or peer.user_id not in self._member_ids()
        ):
            return
        transfer_id = frame.get("transfer_id")
        owner_id = frame.get("owner_id")
        target_id = frame.get("target_id")
        purpose = frame.get("purpose")
        index = frame.get("index")
        total = frame.get("total")
        items = frame.get("items")
        if (
            not _valid_uuid(transfer_id)
            or not _valid_uuid(owner_id)
            or not _valid_uuid(target_id)
            or purpose not in ("sync", "handover")
            or not isinstance(index, int)
            or isinstance(index, bool)
            or not isinstance(total, int)
            or isinstance(total, bool)
            or not 1 <= total <= MAX_HISTORY_ENTRIES
            or not 0 <= index < total
            or not isinstance(items, list)
            or len(items) > MAX_HISTORY_ENTRIES
        ):
            raise ValueError("Invalid history handover")
        if owner_id != self.room_owner_id:
            return
        records = [_validate_record(item) for item in items]
        chunk_key = (transfer_id, index)
        completed_records = None
        with self._history_transfer_lock:
            if chunk_key in self._seen_history_chunks:
                return
            if len(self._seen_history_order) >= MAX_SEEN_MESSAGES:
                expired = self._seen_history_order.popleft()
                self._seen_history_chunks.remove(expired)
            self._seen_history_order.append(chunk_key)
            self._seen_history_chunks.add(chunk_key)
            if target_id == self.identity_id:
                transfer = self._history_transfers.setdefault(
                    transfer_id,
                    {
                        "target_id": target_id,
                        "purpose": purpose,
                        "total": total,
                        "chunks": {},
                    },
                )
                if (
                    transfer["target_id"] != target_id
                    or transfer["purpose"] != purpose
                    or transfer["total"] != total
                ):
                    raise ValueError("Inconsistent history transfer")
                transfer["chunks"][index] = records
                if len(transfer["chunks"]) == total:
                    completed_records = [
                        item
                        for chunk_index in range(total)
                        for item in transfer["chunks"][chunk_index]
                    ]
                    self._history_transfers.pop(transfer_id, None)
        if target_id != self.identity_id:
            self._broadcast(_encode_frame(frame), exclude=peer.connection)
        elif completed_records is not None:
            if purpose == "handover":
                self._replace_local_history(completed_records)
            elif completed_records:
                self.events.put(("history_batch", completed_records))

    def _set_user(self, user_id, nickname, ip, revision):
        now = time.monotonic()
        with self._users_lock:
            previous = self._users.get(user_id)
            if previous is not None:
                if revision < previous["revision"]:
                    previous["last_seen"] = now
                    return False
                if revision == previous["revision"]:
                    changed = False
                    if ip and ip != previous["ip"]:
                        previous["ip"] = ip
                        changed = True
                    previous["last_seen"] = now
                    return changed
            self._users[user_id] = {
                "nickname": nickname.strip(),
                "ip": ip or (previous["ip"] if previous else "unknown"),
                "revision": revision,
                "last_seen": now,
            }
            return True

    def _presence_snapshot(self):
        now = time.monotonic()
        with self._users_lock:
            return [
                {
                    "id": user_id,
                    "nickname": user["nickname"],
                    "ip": _wire_ip(user["ip"]),
                    "revision": user["revision"],
                }
                for user_id, user in self._users.items()
                if user_id == self.identity_id
                or now - user["last_seen"] <= PRESENCE_TIMEOUT
            ]

    def _broadcast_presence(self, user_id, exclude=None):
        with self._users_lock:
            user = self._users.get(user_id)
            if user is None:
                return
            frame = {
                "type": "presence",
                "id": user_id,
                "nickname": user["nickname"],
                "ip": _wire_ip(user["ip"]),
                "revision": user["revision"],
            }
        self._broadcast(_encode_frame(frame), exclude=exclude)

    def _publish_local_presence(self):
        self._revision = max(self._revision + 1, time.time_ns())
        self._set_user(
            self.identity_id,
            self.nickname,
            self._local_ip,
            self._revision,
        )
        with self._members_lock:
            member = self._members.get(self.identity_id)
            if member is not None:
                member["nickname"] = self.nickname
                member["ip"] = self._local_ip
                member["port"] = self.port
                member["revision"] = self._revision
        self._broadcast_presence(self.identity_id)
        self._refresh_local_nickname_conflict()

    def _presence_loop(self):
        while not self._stop.wait(PRESENCE_INTERVAL):
            self._publish_local_presence()

    def _join_timeout_loop(self):
        while not self._stop.wait(0.25):
            now = time.monotonic()
            with self._peers_lock:
                peers = list(self._peers.values())
            for peer in peers:
                with self._pending_lock:
                    if (
                        peer.pending_room_snapshot is None
                        or peer.join_deadline is None
                        or peer.join_deadline > now
                    ):
                        continue
                    peer.pending_room_snapshot = None
                    peer.join_deadline = None
                    self._seen_join_decisions.add(peer.request_id)
                self._admitted = True
                self.events.put(
                    (
                        "join_result",
                        (
                            False,
                            self._owner_name(),
                            "Host approval timed out after 1 minute.",
                        ),
                    )
                )
                peer.departed = True
                self._remove_peer(peer, "Host approval timed out")

    def _send_history(self, peer):
        if (
            self.history_provider is None
            or self.room_owner_id != self.identity_id
        ):
            return
        if peer.user_id is not None:
            self._send_history_to_member(peer.user_id)

    def _send_peer(self, peer, frame):
        try:
            with peer.send_lock:
                peer.connection.sendall(frame)
            return True
        except OSError:
            self._remove_peer(peer, "Connection lost")
            return False

    def _broadcast(self, frame, exclude=None):
        with self._peers_lock:
            peers = [
                peer
                for peer in self._peers.values()
                if peer.connection is not exclude and peer.admitted
            ]
        sent = 0
        for peer in peers:
            if self._send_peer(peer, frame):
                sent += 1
        return sent

    def _remember_message(self, message_id):
        with self._seen_lock:
            if message_id in self._seen_messages:
                return False
            if len(self._seen_message_order) >= MAX_SEEN_MESSAGES:
                expired = self._seen_message_order.popleft()
                self._seen_messages.remove(expired)
            self._seen_message_order.append(message_id)
            self._seen_messages.add(message_id)
            return True

    def _remove_peer(self, peer, reason):
        self._finish_connect_attempt(peer)
        with self._peers_lock:
            if self._peers.pop(peer.connection, None) is None:
                return
        try:
            peer.connection.close()
        except OSError:
            pass
        with self._pending_lock:
            if peer.remote_request_id is not None:
                self._pending_routes.pop(peer.remote_request_id, None)
                pending = self._pending_joins.get(peer.remote_request_id)
                if pending is not None and pending.get("peer") is peer:
                    self._pending_joins.pop(peer.remote_request_id, None)
        if peer.admitted and not peer.departed and peer.nickname:
            self.events.put(("disconnected", (peer.nickname, reason)))


def _discover_local_ip():
    connection = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        connection.connect((DEFAULT_CONNECT_IP, 48652))
        return connection.getsockname()[0]
    except OSError:
        try:
            addresses = socket.gethostbyname_ex(socket.gethostname())[2]
        except OSError:
            return "unknown"
        return next((address for address in addresses if not address.startswith("127.")), "unknown")
    finally:
        connection.close()


def _wire_ip(address):
    try:
        return str(ipaddress.IPv4Address(address))
    except ipaddress.AddressValueError:
        return ""
