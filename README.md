# DoggyChat

DoggyChat is a terminal-based peer-to-peer group chat for Linux and Windows.
It connects directly over TCP and does not require a central server.

## Release

Current version: **1.2.3**
Release time: **2026-11-27 16:19:41+08:00**

DoggyChat encrypts peer connections with TLS 1.2 or newer. It uses
trust-on-first-use (TOFU): the first certificate fingerprint for each local
TLS identity is saved, and later changes are rejected. Because messages are relayed through
room members, encryption is between each pair of connected peers, not
end-to-end. Each launch chooses a random listening port unless `--port` is
specified. Chat history is kept separate for each port, while the local TLS
identity and TOFU trust store persist across launches.
See [CHANGELOG.md](CHANGELOG.md) for changes in each version.
Active rooms are discovered on the local network using UDP multicast. Room IDs
are assigned from `id1` through `id50`; multicast discovery does not cross
routers or isolated Wi-Fi networks. IDs are unique only within the same
multicast discovery network, so separate subnets may independently use `id1`.
Room search and connect completion omit the room you are currently in.

The version and release time appear at the top of the chat. For each new
release, run `python scripts/bump_version.py` before building, then add the
release changes to `CHANGELOG.md`. The script increments the version and
updates the release time; for example, `1.0.9` becomes `1.1.0`.

## Start the chat

Python 3.9 or newer is required to run from source.

**Linux source:**

```sh
python3 -m doggychat
```

**Linux standalone executable:**

```sh
./dist/DoggyChat
```

The standalone binary is for x86_64 Linux and requires glibc 2.27 or newer.

**Windows source:**

```powershell
py -m doggychat
```

To build a standalone Windows executable, run `build_windows.bat` on Windows.
It downloads a temporary Python 3 build runtime and creates
`dist\DoggyChat.exe`; the build computer needs PowerShell and internet access,
but does not need Python installed. The resulting executable includes Python
and its dependencies.

Set your nickname at launch with `--name`. A random available listening port
is selected by default and shown in the terminal header. Pass `--port` to use
a fixed port instead:

```sh
python3 -m doggychat --name Buddy --port 48652
```

To host two separate rooms on one computer, open two terminals and start two
instances; each will choose a different available port:

```sh
python3 -m doggychat --name HostA
python3 -m doggychat --name HostB
```

For the standalone executable, pass the same options, for example
`./dist/DoggyChat --name HostB --port 48653`. On Windows, use
`py -m doggychat --name HostB`.

## Connect and chat

1. Everyone starts DoggyChat. Each instance creates a room and receives a
   room ID automatically. A short tutorial explaining the main commands appears
   every time the app starts.
2. Run `/control search` to list rooms visible on the LAN, then join with
   `/control connect <room id-room name>` (for example,
   `/control connect 2-My room`). Numeric IDs alone remain supported. The host
   can approve new members with `/manager approve <name-ip>` or deny them with
   `/manager denny <name-ip>`. Alternatively, the host can share the room's
   four-digit key, available with `/manager key print`; join using
   `/control connect <room id> <key>` or
   `/control connect <room id-room name> <key>` to skip manual approval. A
   room key is created for each new room and can be changed with
   `/manager key set <key>`.
   Without a key, the room host has one minute to respond; until approval,
   your room ID and title remain unchanged.
   Legacy `/control connect <ip> <port>` remains available.
   Connecting to your own room is rejected.
3. There can be up to 50 active room IDs (`id1`–`id50`) on the same LAN
   discovery network. After joining a room, the instance releases its original
   room ID. It keeps its TCP listener for peer connections and host handover.
4. Rooms allow 15 members by default, including the host. The host can change
   the limit from 1 to 100 with `/manager set maxmember <number>`.
5. A user can connect to multiple members of the same room, but cannot connect
   to a different room until using `/control disconnect`.
6. The default room name is `DoggyChat 1.2.3 - Unknow chat room`. The host can
   change it with `/manager set roomname <name>`; it appears in brackets at
   the top of the terminal, followed by `Room id: <number> TCP <port>`. The
   release time is shown beneath it.
7. Nicknames must be unique regardless of letter case. Duplicate joins and
   rename attempts are rejected. If an older room contains duplicate names,
   affected users must rename before sending messages.
8. `/who` lists the host first, followed by other users in join order. Your
   name is blue and other members' names are green, including the host.
   Your mentions stay in the default color in messages you send; mentions in
   received messages are red. Type `@` and press Tab to complete a room member.
   System messages are yellow and errors are red. Chat messages show local
   date and time; your messages start with `*` and incoming messages start
   with `-`.
9. New incoming messages show desktop notifications. Linux requires
   `notify-send`; Windows uses the system notification area.

Messages are relayed through connected participants, so users can form a
group by connecting to members of the same room. All participants need a
version that supports the current chat protocol.
Allow UDP port `48653` for room discovery and each advertised TCP room port
through the local firewall.

## Commands and permissions

- `/control search` — any user; list discovered LAN rooms as room ID, name,
  IP, and port.
- `/control connect <room id-room name>` — any user; connect by the room ID
  and name shown by Tab completion. Numeric IDs alone remain supported.
- `/control connect <room id> <key>` — any user; join a room without waiting
  for manual host approval when the four-digit key matches.
- `/control connect <room id-room name> <key>` — any user; the same key
  shortcut when connecting with the ID-name selector.
- `/control connect <ip> <port>` — any user; connect directly by endpoint.
- `/control disconnect` — any user; leave the current room and connect to
  another.
- `/control user rename <name>` — any user; change your nickname.
- `@<name>` + Tab — mention a room member in a chat message; the mention is red.
- `/manager approve <name-ip>` — host only; approve the pending request
  identified by nickname and IP.
- `/manager denny <name-ip>` — host only; deny the pending request identified
  by nickname and IP.
- `/manager set maxmember <number>` — host only; set the room limit (1–100).
- `/manager set roomname <name>` — host only; change the room name.
- `/manager key print` — host only; display this room's four-digit key.
- `/manager key set <key>` — host only; set a new four-digit room key.
- `/who` — any user; list the host first, then other members in join order,
  with their IP addresses.
- `/show` — any user; show the room name, room ID and endpoint, member count,
  member limit, and history record limit.
- `/scp <user name> <file path>` — any user; request a file transfer
  to one member. The file picker appears automatically when the file argument
  begins and shows up to three entries. Up/Down selects an entry and Enter
  confirms a file; Tab opens a directory, while `..` navigates to its parent.
  No `@` prefix is needed. The recipient must run `/scp accept`.
- `/scp all <file path>` — any user; offer the file independently to
  every other member. Each recipient must accept separately and receives a
  copy.
- `/scp accept` — any user; accept an incoming file.
- `/scp exit` — any user; cancel the active transfer.
- `/help` — any user; show the commands and their permissions.
- `/quit` — any user; exit DoggyChat.

Tab completes commands in order, including `/control user rename`, `/manager
set maxmember`, `/manager set roomname`, `/manager key print`, the room
ID-name selectors, and the host's pending name-IP choices.
The first Tab after `/scp` lists actions and available recipients. The file
picker opens when the recipient is followed by the file argument; Up/Down
selects among at most three visible entries, Enter confirms the selected file,
and Tab opens the selected directory. Select `..` and press Tab to move up.
Direct file paths and `~/` paths remain supported. Up/Down recalls previous
messages and commands outside the file picker.

On first contact, DoggyChat automatically saves a peer's TLS certificate
fingerprint (TOFU). If a known TLS identity presents a different certificate,
connection is refused. The local certificate and trust store live beside the
default application data files (`~/.local/share/doggychat` on Linux or
`%APPDATA%\DoggyChat` on Windows). For source installs, install runtime dependencies with
`python -m pip install -r requirements.txt`.

## Chat history

Only the room host stores chat history, up to 500 records. New members receive
the room's history from the host. When the host leaves, history is transferred
to the next host; when the final member leaves, the history is cleared.

The host's local history file is stored at:

- Default port: `~/.local/share/doggychat/history.json` (Linux) or
  `%APPDATA%\DoggyChat\history.json` (Windows).
- Other ports: `history-<port>.json` in the same directory.

The history file is not encrypted. Chat traffic is encrypted per peer
connection, but relaying members can access message contents; use DoggyChat
only on networks you trust and do not send sensitive information.

Files are streamed in chunks over the encrypted peer mesh. Incoming files are
saved beside the running executable (or in the project directory when run from
source); an existing file is replaced only after the new file is fully
received. Unanswered requests expire after five minutes, and active transfers
time out after five minutes without progress.

Each instance listens on its own randomly assigned TCP port unless `--port`
is specified. The TCP listener remains active after joining a room so members
can connect and the room can hand over its host. Internet connections may
require router port forwarding; room search itself is LAN-only.

## Tests

Run the test suite from this directory:

```sh
python3 -m unittest discover -s tests
```
