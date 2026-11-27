import argparse
import getpass
import sys

from .console import run_console
from .discovery import RoomDirectory
from .history import ChatHistory, default_history_path, history_path_for_port
from .network import DEFAULT_PORT, ChatNetwork, create_listener
from .security import (
    TrustStore,
    load_or_create_local_identity,
)
from .version import RELEASE_TIME, VERSION


def main():
    parser = argparse.ArgumentParser(description="DoggyChat peer-to-peer LAN messenger")
    parser.add_argument(
        "--version",
        action="version",
        version=f"DoggyChat {VERSION} (released {RELEASE_TIME})",
    )
    parser.add_argument(
        "--name",
        default=getpass.getuser()[:32] or "Guest",
        help="nickname to show in chat (default: current login name)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="TCP port to listen on (default: randomly assigned)",
    )
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be 0 (random) or between 1 and 65535")
    if not args.name.strip() or len(args.name) > 32:
        parser.error("--name must contain 1 to 32 characters")

    try:
        listener = create_listener(args.port)
    except OSError as error:
        if args.port:
            message = f"Could not bind TCP port {args.port}: {error}"
        else:
            message = f"Could not choose a random TCP port: {error}"
        print(message, file=sys.stderr)
        raise SystemExit(1) from error
    port = listener.getsockname()[1]
    try:
        history_path = history_path_for_port(port, DEFAULT_PORT)
        history = ChatHistory(history_path)
        security_directory = default_history_path().parent
        tls_identity = load_or_create_local_identity(security_directory)
        trust_store = TrustStore(security_directory / "trusted.json")
        network = ChatNetwork(
            port=port,
            identity_id=history.identity_id,
            nickname=args.name,
            history_provider=history.entries,
            history_consumer=history.replace,
            tls_identity=tls_identity,
            trust_store=trust_store,
        )
    except (OSError, ValueError) as error:
        listener.close()
        print(f"Could not initialize room security or history: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    try:
        network.start(listener)
    except OSError as error:
        print(
            f"Could not listen on TCP port {port}: {error}. "
            "Restart to choose another random port or pass --port PORT.",
            file=sys.stderr,
        )
        raise SystemExit(1) from error

    room_directory = RoomDirectory(network)
    try:
        room_id = room_directory.start()
        network.attach_room_directory(room_directory)
    except OSError as error:
        network.close()
        print(f"Could not start LAN room discovery: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    network.events.put(("first_run_hint", room_id))

    try:
        run_console(network, args.name, history_store=history)
    except KeyboardInterrupt:
        print("\nExiting DoggyChat.")
    finally:
        room_directory.close()
        network.close()


if __name__ == "__main__":
    main()
