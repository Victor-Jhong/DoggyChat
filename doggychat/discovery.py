import ipaddress
import json
import socket
import threading
import time
import uuid


DISCOVERY_GROUP = "239.255.57.83"
DISCOVERY_PORT = 48653
ROOM_SLOT_COUNT = 50
ANNOUNCE_INTERVAL = 2
ROOM_TTL = 12
STARTUP_DISCOVERY_WAIT = 1.5
STARTUP_QUERY_INTERVAL = 0.25


class RoomDirectoryError(OSError):
    pass


class RoomDirectory:
    def __init__(self, network):
        self.network = network
        self._lock = threading.RLock()
        self._rooms = {}
        self._local_slot = None
        self._local_room_uuid = None
        self._socket = None
        self._stop = threading.Event()
        self._changed = threading.Event()
        self._thread = None
        self._last_announcement = 0
        self._initializing = True

    def start(self):
        discovery_socket = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM,
            socket.IPPROTO_UDP,
        )
        try:
            discovery_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            discovery_socket.bind(("", DISCOVERY_PORT))
            membership = socket.inet_aton(DISCOVERY_GROUP) + socket.inet_aton(
                "0.0.0.0"
            )
            discovery_socket.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_ADD_MEMBERSHIP,
                membership,
            )
            discovery_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            discovery_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            discovery_socket.settimeout(0.1)
        except OSError:
            discovery_socket.close()
            raise
        self._socket = discovery_socket
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + STARTUP_DISCOVERY_WAIT
        while True:
            self._query()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(STARTUP_QUERY_INTERVAL, remaining))
        self._sync_local_room()
        room_id = self._finish_startup()
        if room_id is None:
            self.close()
            raise RoomDirectoryError("All 50 room IDs are in use on this LAN.")
        return room_id

    def _finish_startup(self):
        with self._lock:
            slot = self._local_slot
            if slot is None:
                return None
            self._initializing = False
            room_id = f"id{slot}"
        self.network.events.put(("room_directory_id_changed", room_id))
        self._announce_local_room()
        return room_id

    def close(self):
        with self._lock:
            slot = self._local_slot
            room_uuid = self._local_room_uuid
        if slot is not None and room_uuid is not None:
            self._send(
                {
                    "type": "withdraw",
                    "slot": slot,
                    "room_uuid": room_uuid,
                    "owner_id": self.network.identity_id,
                }
            )
        self._stop.set()
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)
        self._thread = None

    def search_rooms(self):
        self._changed.clear()
        self._query()
        self._changed.wait(0.3)
        self._expire_rooms()
        with self._lock:
            return [
                dict(room)
                for _, room in sorted(self._rooms.items())
                if self._room_is_joinable(room)
            ]

    def find_room(self, room_id):
        normalized = str(room_id).strip().casefold()
        with self._lock:
            if "-" in normalized:
                return next(
                    (
                        dict(room)
                        for room in self._rooms.values()
                        if self._room_is_joinable(room)
                        if normalized
                        in (
                            f'{room["id"]}-{room["room_name"]}'.casefold(),
                            f'{room["id"][2:]}-{room["room_name"]}'.casefold(),
                        )
                    ),
                    None,
                )
            if not normalized.startswith("id"):
                normalized = f"id{normalized}"
            return next(
                (
                    dict(room)
                    for room in self._rooms.values()
                    if self._room_is_joinable(room)
                    if room["id"] == normalized
                ),
                None,
            )

    def _room_is_joinable(self, room):
        return (
            room["owner_id"] != self.network.identity_id
            and room["room_uuid"] != self.network.room_id
        )

    def get_local_room_id(self):
        with self._lock:
            if self._local_slot is None:
                return None
            return f"id{self._local_slot}"

    def get_room_id(self, room_uuid):
        with self._lock:
            if self._local_room_uuid == room_uuid and self._local_slot is not None:
                return f"id{self._local_slot}"
            return next(
                (
                    room["id"]
                    for room in self._rooms.values()
                    if room["room_uuid"] == room_uuid
                ),
                None,
            )

    def _run(self):
        while not self._stop.is_set():
            try:
                packet, address = self._socket.recvfrom(4096)
            except socket.timeout:
                packet = None
            except OSError:
                if self._stop.is_set():
                    return
                self.network.events.put(
                    ("error", "Room discovery socket stopped unexpectedly.")
                )
                return
            if packet:
                self._handle_packet(packet, address)
            self._expire_rooms()
            if not self._initializing:
                self._sync_local_room()
                now = time.monotonic()
                if now - self._last_announcement >= ANNOUNCE_INTERVAL:
                    self._announce_local_room()

    def _handle_packet(self, packet, address):
        try:
            frame = json.loads(packet.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(frame, dict):
            return
        frame_type = frame.get("type")
        if frame_type == "query":
            self._answer_query(address)
        elif frame_type == "announce":
            self._record_announcement(frame, address[0])
        elif frame_type == "withdraw":
            self._remove_announcement(frame)

    def _answer_query(self, _address):
        local = self._local_snapshot()
        if local is None:
            return
        with self._lock:
            slot = self._local_slot
        if slot is not None:
            self._send(self._announcement_frame(slot, local))

    def _record_announcement(self, frame, address):
        try:
            room_uuid = frame["room_uuid"]
            owner_id = frame["owner_id"]
            slot = frame["slot"]
            room_name = frame["room_name"]
            ip = str(ipaddress.IPv4Address(frame["ip"]))
            port = frame["port"]
            if (
                not _valid_uuid(room_uuid)
                or not _valid_uuid(owner_id)
                or not isinstance(slot, int)
                or isinstance(slot, bool)
                or not 1 <= slot <= ROOM_SLOT_COUNT
                or not isinstance(room_name, str)
                or not room_name.strip()
                or len(room_name) > 80
                or not isinstance(port, int)
                or isinstance(port, bool)
                or not 1 <= port <= 65535
            ):
                return
        except (KeyError, ValueError, TypeError):
            return
        now = time.monotonic()
        room = {
            "id": f"id{slot}",
            "room_uuid": room_uuid,
            "owner_id": owner_id,
            "room_name": room_name.strip(),
            "ip": ip,
            "port": port,
            "expires": now + ROOM_TTL,
        }
        if (
            owner_id == self.network.identity_id
            and self._local_room_uuid == room_uuid
        ):
            return
        with self._lock:
            existing = self._rooms.get(slot)
            if existing is not None and existing["room_uuid"] != room_uuid:
                if room_uuid > existing["room_uuid"]:
                    return
            previous_slot = next(
                (
                    known_slot
                    for known_slot, known in self._rooms.items()
                    if known["room_uuid"] == room_uuid
                ),
                None,
            )
            if previous_slot is not None and previous_slot != slot:
                self._rooms.pop(previous_slot, None)
            self._rooms[slot] = room
            if self._local_room_uuid == room_uuid:
                self._local_slot = slot
        self._changed.set()

    def _remove_announcement(self, frame):
        slot = frame.get("slot")
        room_uuid = frame.get("room_uuid")
        owner_id = frame.get("owner_id")
        if not isinstance(slot, int) or isinstance(slot, bool):
            return
        with self._lock:
            room = self._rooms.get(slot)
            if (
                room is not None
                and room["room_uuid"] == room_uuid
                and room["owner_id"] == owner_id
            ):
                self._rooms.pop(slot, None)
        self._changed.set()

    def _sync_local_room(self):
        local = self._local_snapshot()
        with self._lock:
            old_slot = self._local_slot
            old_uuid = self._local_room_uuid
            if old_slot is not None and (
                local is None or local["room_uuid"] != old_uuid
            ):
                previous = self._rooms.get(old_slot)
                if (
                    previous is not None
                    and previous["room_uuid"] == old_uuid
                    and previous["owner_id"] == self.network.identity_id
                ):
                    self._rooms.pop(old_slot, None)
            if local is None:
                self._local_slot = None
                self._local_room_uuid = None
                slot = None
            else:
                room_uuid = local["room_uuid"]
                slot = next(
                    (
                        known_slot
                        for known_slot, room in self._rooms.items()
                        if room["room_uuid"] == room_uuid
                    ),
                    None,
                )
                if slot is None and old_uuid == room_uuid:
                    slot = old_slot
                if slot is None or (
                    slot in self._rooms
                    and self._rooms[slot]["room_uuid"] != room_uuid
                ):
                    slot = next(
                        (
                            candidate
                            for candidate in range(1, ROOM_SLOT_COUNT + 1)
                            if candidate not in self._rooms
                        ),
                        None,
                    )
                self._local_slot = slot
                self._local_room_uuid = room_uuid if slot is not None else None
                if slot is not None:
                    self._rooms[slot] = {
                        "id": f"id{slot}",
                        **local,
                        "expires": time.monotonic() + ROOM_TTL,
                    }
            updated_slot = self._local_slot
        if old_slot != updated_slot and not self._initializing:
            self.network.events.put(
                (
                    "room_directory_id_changed",
                    f"id{updated_slot}" if updated_slot is not None else None,
                )
            )
        if old_slot is not None and (local is None or old_slot != slot):
            self._send(
                {
                    "type": "withdraw",
                    "slot": old_slot,
                    "room_uuid": old_uuid,
                    "owner_id": self.network.identity_id,
                }
            )
        if local is None and old_slot is not None:
            self._changed.set()

    def _local_snapshot(self):
        if self.network.room_owner_id != self.network.identity_id:
            return None
        room_uuid = self.network.room_id
        config = self.network.get_room_config()
        ip, port = self.network.get_room_endpoint()
        if not _valid_uuid(room_uuid) or not isinstance(port, int):
            return None
        try:
            ip = str(ipaddress.IPv4Address(ip))
        except ipaddress.AddressValueError:
            return None
        return {
            "room_uuid": room_uuid,
            "owner_id": self.network.identity_id,
            "room_name": config["room_name"],
            "ip": ip,
            "port": port,
        }

    def _announce_local_room(self):
        local = self._local_snapshot()
        with self._lock:
            slot = self._local_slot
        if local is not None and slot is not None:
            self._send(self._announcement_frame(slot, local))
            self._last_announcement = time.monotonic()

    @staticmethod
    def _announcement_frame(slot, local):
        return {
            "type": "announce",
            "slot": slot,
            "room_uuid": local["room_uuid"],
            "owner_id": local["owner_id"],
            "room_name": local["room_name"],
            "ip": local["ip"],
            "port": local["port"],
        }

    def _query(self):
        self._send({"type": "query"})

    def _send(self, frame, address=None):
        if self._socket is None:
            return
        destination = address or (DISCOVERY_GROUP, DISCOVERY_PORT)
        try:
            self._socket.sendto(
                json.dumps(frame, ensure_ascii=True, separators=(",", ":")).encode(
                    "ascii"
                ),
                destination,
            )
        except OSError as error:
            if not self._stop.is_set():
                self.network.events.put(
                    ("error", f"Could not send room discovery message: {error}")
                )

    def _expire_rooms(self):
        now = time.monotonic()
        with self._lock:
            stale = [
                slot
                for slot, room in self._rooms.items()
                if room["expires"] <= now
                and not (
                    slot == self._local_slot
                    and room["room_uuid"] == self._local_room_uuid
                )
            ]
            for slot in stale:
                self._rooms.pop(slot, None)


def _valid_uuid(value):
    if not isinstance(value, str) or len(value) != 32:
        return False
    try:
        return uuid.UUID(hex=value).hex == value
    except ValueError:
        return False
