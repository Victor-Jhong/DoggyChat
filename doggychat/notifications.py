import ctypes
import shutil
import subprocess
import sys
from ctypes import wintypes


class DesktopNotifier:
    def __init__(self):
        self._windows_notifier = None
        self._disabled = False

    def notify(self, title, body):
        if self._disabled:
            return None
        try:
            if sys.platform == "win32":
                self._notify_windows(title, body)
            elif sys.platform.startswith("linux"):
                self._notify_linux(title, body)
            else:
                raise RuntimeError("this operating system is not supported")
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            self._disabled = True
            return f"! Desktop notifications unavailable: {error}"
        return None

    def close(self):
        if self._windows_notifier is not None:
            self._windows_notifier.close()
            self._windows_notifier = None

    def _notify_linux(self, title, body):
        command = shutil.which("notify-send")
        if command is None:
            raise RuntimeError("install notify-send to enable desktop notifications")
        result = subprocess.run(
            [command, "--app-name=DoggyChat", title[:63], body[:500]],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=3,
        )
        if result.returncode:
            detail = result.stderr.strip()
            raise RuntimeError(detail or f"notify-send exited with {result.returncode}")

    def _notify_windows(self, title, body):
        if self._windows_notifier is None:
            self._windows_notifier = _WindowsNotifier()
        self._windows_notifier.show(title, body)


class _WindowsNotifier:
    NIM_ADD = 0
    NIM_MODIFY = 1
    NIM_DELETE = 2
    NIF_ICON = 0x2
    NIF_TIP = 0x4
    NIF_INFO = 0x10
    NIIF_INFO = 0x1

    class _NotifyIconData(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeout", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", wintypes.HICON),
        ]

    def __init__(self):
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            ctypes.c_void_p,
            wintypes.HINSTANCE,
            ctypes.c_void_p,
        ]
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        user32.LoadIconW.restype = wintypes.HICON
        user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
        shell32.Shell_NotifyIconW.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(self._NotifyIconData),
        ]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        self._user32 = user32
        self._shell32 = shell32
        self._hwnd = user32.CreateWindowExW(
            0,
            "STATIC",
            "DoggyChat",
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            kernel32.GetModuleHandleW(None),
            None,
        )
        if not self._hwnd:
            raise OSError("could not create the notification window")
        icon_id = ctypes.cast(ctypes.c_void_p(32512), wintypes.LPCWSTR)
        icon = user32.LoadIconW(None, icon_id)
        data = self._data()
        data.uFlags = self.NIF_ICON | self.NIF_TIP
        data.hIcon = icon
        data.szTip = "DoggyChat"
        if not shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(data)):
            user32.DestroyWindow(self._hwnd)
            self._hwnd = None
            raise OSError("could not register the DoggyChat notification icon")

    def show(self, title, body):
        data = self._data()
        data.uFlags = self.NIF_INFO
        data.szInfoTitle = title[:63]
        data.szInfo = body[:255]
        data.dwInfoFlags = self.NIIF_INFO
        if not self._shell32.Shell_NotifyIconW(
            self.NIM_MODIFY, ctypes.byref(data)
        ):
            raise OSError("Windows could not display the notification")

    def close(self):
        if self._hwnd is None:
            return
        data = self._data()
        self._shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(data))
        self._user32.DestroyWindow(self._hwnd)
        self._hwnd = None

    def _data(self):
        data = self._NotifyIconData()
        data.cbSize = ctypes.sizeof(data)
        data.hWnd = self._hwnd
        data.uID = 1
        return data
