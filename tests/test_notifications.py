import subprocess
import unittest
from unittest import mock

from doggychat.notifications import DesktopNotifier


class DesktopNotifierTests(unittest.TestCase):
    @mock.patch("doggychat.notifications.subprocess.run")
    @mock.patch("doggychat.notifications.shutil.which", return_value="/usr/bin/notify-send")
    @mock.patch("doggychat.notifications.sys.platform", "linux")
    def test_linux_notification_uses_system_notification_command(
        self, _which, run
    ):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        notifier = DesktopNotifier()

        self.assertIsNone(notifier.notify("Message from Lisa", "Hello"))

        run.assert_called_once_with(
            [
                "/usr/bin/notify-send",
                "--app-name=DoggyChat",
                "Message from Lisa",
                "Hello",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=3,
        )

    @mock.patch("doggychat.notifications.shutil.which", return_value=None)
    @mock.patch("doggychat.notifications.sys.platform", "linux")
    def test_missing_notification_service_reports_once(self, _which):
        notifier = DesktopNotifier()

        error = notifier.notify("Message from Lisa", "Hello")

        self.assertIn("install notify-send", error)
        self.assertIsNone(notifier.notify("Message from Lisa", "Again"))
