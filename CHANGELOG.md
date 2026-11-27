# DoggyChat Changelog

## 1.2.3 — 2026-11-27 16:19:41 +08:00

- Include the local user's nickname when detecting received mentions, so the
  person being tagged sees their name highlighted red.

## 1.2.2 — 2026-11-27 16:12:51 +08:00

- Keep `@mentions` in the default color for the sender; highlight them red in
  recipients' received messages.

## 1.2.1 — 2026-11-27 16:09:29 +08:00

- Parse a four-digit key after an ID-name room selector as the key argument,
  rather than including it in the room name. Exact room names ending in four
  digits remain valid.

## 1.2.0 — 2026-11-27 15:52:10 +08:00

- Show the SCP file picker only after the recipient, when the file argument
  begins. It displays at most three entries; Up/Down selects and Enter confirms
  a file, while Tab enters a directory or opens `..` to move to its parent.
- File selection no longer requires an `@` prefix.

## 1.1.9 — 2026-11-27 14:51:47 +08:00

- Add `@` + Tab completion for room members; mentioned names are highlighted
  red, while chat and `/who` colors distinguish only the local user from others.
- Show current-folder entries above the prompt while entering `/scp`, with
  `@` + Tab completion for local files and directories.

## 1.1.8 — 2026-11-27 14:32:13 +08:00

- Generate a random four-digit key for each new room. Hosts can view or change
  it with `/manager key print` and `/manager key set <key>`.
- Allow `/control connect <room id> <key>` to join without manual approval;
  invalid keys are rejected, including when the request is relayed through a
  room member.
- Preserve the local room identity and title when a connection is rejected
  before approval.

## 1.1.7 — 2026-11-27 12:10:42 +08:00

- Display the default-color usage hint every time DoggyChat starts, rather than
  suppressing it after the first launch.

## 1.1.6 — 2026-11-27 12:02:03 +08:00

- Add `/control search` to the first-run guide before the connect step.
- Hide the current room from search results and connect completion, and reject
  direct selection of the current room.
- Repeat LAN discovery queries during startup so existing rooms are more likely
  to be observed before assigning a local room ID.
- Clarify that room IDs are unique only within one multicast discovery network;
  isolated subnets can independently assign the same ID.

## 1.1.5 — 2026-11-27 11:00:01 +08:00

- Show `/control connect` Tab choices as `<room id>-<room name>` and cycle
  through the full selectors, including room names with spaces.
- Accept ID-name selectors when connecting while preserving numeric IDs and
  direct IP/port connections.
- Update command help and the first-run tutorial to show the ID-name syntax.

## 1.1.4 — 2026-11-27 10:30:51 +08:00

- Reject attempts to connect to the local room.
- Change host approval to `/manager approve <name-ip>` and use the same
  nickname-IP selector for `/manager denny`; update help and Tab completion.
- Keep the current room title and ID until the host approves a join. Restore
  the original room on denial, and time out unanswered approval requests after
  one minute.
- Show a default-color tutorial once per user account on first launch.
- Rebuild the Linux executable with bundled libraries compatible with glibc
  2.27.

## 1.1.3 — 2026-11-27 09:35:26 +08:00

- Wait for LAN room announcements before assigning the first free room ID, and
  publish only the final startup ID to avoid stale assignment messages.
- Format the startup header with separators, room ID, TCP port, and release time.

## 1.1.2 — 2026-11-26 21:21:09 +08:00

- Keep cycling numeric room IDs when `/control connect` is completed repeatedly
  with Tab, including after a completion inserts trailing whitespace.
- Show available SCP actions and recipients on the first Tab after `/scp`.
- Capitalize the `/show` labels for room IP and port.

## 1.1.1 — 2026-11-26 21:07:40 +08:00

- Use the current application version in the default room name.
- Label the terminal room identifier as `Room id: <number>` and show numeric
  room IDs consistently; `/control connect` accepts the number without `id`.
- Add `/scp` Tab completion for other room members and filesystem paths,
  including `~/` expansion and cycling through matching entries.

## 1.1.0 — 2026-11-26 20:49:59 +08:00

- Create and advertise a room automatically at startup; discover LAN rooms as
  `id1`–`id50` with `/control search`, then join by ID with
  `/control connect <room id>`. Joining releases the instance's original room
  ID while retaining its TCP listener for peer connections and host handover.
- Show room ID, IP, and port in `/show`; include the room ID in the terminal
  header.
- Change system status messages to yellow and errors to red.
- Add consent-based `/scp <user name> <file path>` and `/scp all <file path>`.
  Each recipient independently accepts; files stream over the encrypted peer
  mesh, incomplete transfers do not overwrite existing files, and transfers
  expire after five minutes without a response or progress.
- Document LAN discovery, room IDs, firewall ports, SCP behavior, and updated
  commands.

## 1.0.9 — 2026-11-26 20:00:40 +08:00

- Encrypt peer-to-peer connections using TLS 1.2+ and verify self-signed
  identities with TOFU fingerprint pinning.
- Choose a random available listening port by default; keep `--port` for fixed
  ports and show the actual port in the terminal.
- Require the host's displayed IP and port to join a random-port room.
- Keep chat histories isolated per listening port while reusing a stable local
  TLS identity and TOFU pins across launches.

## 1.0.8 — 2026-11-26 19:41:15 +08:00

- Support multiple room instances on one computer by listening on distinct
  ports.
- Add optional target ports to `/control connect`; omitted IP and port use
  `10.10.13.114` and `48652`.
- Store non-default-port chat histories in separate files.

## 1.0.7 — 2026-11-26 17:36:34 +08:00

- List the host first in `/who`, then list other members in join order.
- Bracket the room name in the terminal header and show the current member count
  in `/show`.
- Reject duplicate nicknames case-insensitively; prompt users with existing
  conflicts to rename and block their messages until resolved.
- Refresh the Linux executable and ZIP with the 1.0.7 application.

## 1.0.6 — 2026-11-26 17:08:21 +08:00

- Rebuild the Linux executable against glibc 2.27 so it can run on systems that do not provide `GLIBC_2.29`.

## 1.0.5 — 2026-11-26 16:26:03 +08:00

- Color system status prompts, including join and connection notifications, green.
- Keep other members' chat names green.

## 1.0.4 — 2026-11-26 16:11:51 +08:00

- Display other participants' chat names in green.

## 1.0.3 — 2026-11-26 14:36:46 +08:00

- Add host-managed room names and member limits, with a default limit of 15.
- Add `/manager`, `/control`, `/show`, and expanded `/help` command behavior.
- Keep history on the host, transfer it when the host changes, and clear it when the room empties.
- Restrict users to one room at a time and show the host first in `/who`.

## 1.0.2 — 2026-11-26 14:18:39 +08:00

- Add host handoff and desktop notifications for incoming messages.
- Rebuild the Linux executable for glibc 2.31 compatibility.

## 1.0.1 — 2026-11-26 14:06:30 +08:00

- Add host-only approval and denial of room join requests.
- Hide host-only controls from non-host command help and Tab completion.

## 1.0.0

- Establish the terminal-based, IP-connected multi-user chat.
- Add timestamps, message markers, host identity display, `/who`, chat history, input history, and command completion.
