import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from doggychat import __main__ as app


class MainTests(unittest.TestCase):
    def test_startup_hint_is_enqueued_every_launch(self):
        listener = MagicMock()
        listener.getsockname.return_value = ("127.0.0.1", 12345)
        history = MagicMock()
        history.identity_id = "identity"
        network = MagicMock()
        directory = MagicMock()
        directory.start.return_value = "id7"

        with (
            patch.object(app, "create_listener", return_value=listener),
            patch.object(app, "history_path_for_port", return_value=Path("/tmp/history")),
            patch.object(app, "ChatHistory", return_value=history),
            patch.object(app, "default_history_path", return_value=Path("/tmp/doggychat")),
            patch.object(app, "load_or_create_local_identity"),
            patch.object(app, "TrustStore"),
            patch.object(app, "ChatNetwork", return_value=network),
            patch.object(app, "RoomDirectory", return_value=directory),
            patch.object(app, "run_console"),
            patch.object(sys, "argv", ["doggychat"]),
        ):
            app.main()
            app.main()

        self.assertEqual(
            network.events.put.call_args_list,
            [call(("first_run_hint", "id7")), call(("first_run_hint", "id7"))],
        )


if __name__ == "__main__":
    unittest.main()
