"""An icon in the Windows notification area (by the clock; new icons start among the hidden
ones behind the ^ arrow) for a program that has no window of its own. Plain ctypes, nothing
to install.

    icon = Tray("mw2tools", on_open, [("Open mw2tools", on_open), None, ("Quit", on_quit)])
    icon.start()        # its own thread; raises if the icon can't be made
    icon.notify("mw2tools is running", "Click the icon to open it.")
    icon.stop()

Left click calls on_open; right click shows the menu (None is a separator line).
"""
import ctypes
import os
import sys
import threading
from ctypes import wintypes as wt

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

WM_DESTROY, WM_CLOSE, WM_NULL, WM_USER = 0x0002, 0x0010, 0x0000, 0x0400
WM_LBUTTONUP, WM_RBUTTONUP, WM_CONTEXTMENU = 0x0202, 0x0205, 0x007B
CALLBACK = WM_USER + 20
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
MF_STRING, MF_SEPARATOR = 0x0, 0x800
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x2, 0x80, 0x100
IDI_APPLICATION = 32512
MB_OK, MB_YESNO, MB_ICONERROR, MB_ICONWARNING, MB_SETFOREGROUND, IDYES = 0x0, 0x4, 0x10, 0x30, 0x10000, 6


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", ctypes.c_ubyte * 16), ("hBalloonIcon", wt.HICON)]


def _dlls():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    sig = {
        (user32, "DefWindowProcW"): (LRESULT, [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]),
        (user32, "RegisterClassW"): (wt.ATOM, [ctypes.POINTER(WNDCLASSW)]),
        (user32, "CreateWindowExW"): (wt.HWND, [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                               ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                               wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]),
        (user32, "DestroyWindow"): (wt.BOOL, [wt.HWND]),
        (user32, "RegisterWindowMessageW"): (wt.UINT, [wt.LPCWSTR]),
        (user32, "GetMessageW"): (wt.BOOL, [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]),
        (user32, "TranslateMessage"): (wt.BOOL, [ctypes.POINTER(wt.MSG)]),
        (user32, "DispatchMessageW"): (LRESULT, [ctypes.POINTER(wt.MSG)]),
        (user32, "PostMessageW"): (wt.BOOL, [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]),
        (user32, "PostQuitMessage"): (None, [ctypes.c_int]),
        (user32, "LoadIconW"): (wt.HICON, [wt.HINSTANCE, wt.LPVOID]),
        (user32, "CreatePopupMenu"): (wt.HMENU, []),
        (user32, "AppendMenuW"): (wt.BOOL, [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]),
        (user32, "TrackPopupMenu"): (ctypes.c_int, [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
                                                   ctypes.c_int, wt.HWND, wt.LPVOID]),
        (user32, "DestroyMenu"): (wt.BOOL, [wt.HMENU]),
        (user32, "SetForegroundWindow"): (wt.BOOL, [wt.HWND]),
        (user32, "GetCursorPos"): (wt.BOOL, [ctypes.POINTER(wt.POINT)]),
        (shell32, "Shell_NotifyIconW"): (wt.BOOL, [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]),
        (shell32, "ExtractIconW"): (wt.HICON, [wt.HINSTANCE, wt.LPCWSTR, wt.UINT]),
        (kernel32, "GetModuleHandleW"): (wt.HMODULE, [wt.LPCWSTR]),
    }
    for (dll, name), (res, args) in sig.items():
        f = getattr(dll, name)
        f.restype, f.argtypes = res, args
    return user32, shell32, kernel32


def message_box(text, title="mw2tools", flags=MB_OK):
    """A Windows message box (there's no console to print to); returns the button pressed."""
    user32 = ctypes.WinDLL("user32")
    user32.MessageBoxW.restype = ctypes.c_int
    user32.MessageBoxW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT]
    return user32.MessageBoxW(None, text, title, flags | MB_SETFOREGROUND)


class Tray:
    def __init__(self, tip, on_click, menu):
        self.tip, self.on_click, self.menu = tip[:127], on_click, menu
        self.hwnd = None
        self._ready = threading.Event()
        self._error = None

    def start(self):
        """Puts the icon up (from a thread of its own, which runs its messages). Raises if it
        couldn't be made."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(10)
        if self._error is not None:
            raise self._error
        if self.hwnd is None:
            raise OSError("the notification area icon didn't come up")

    def stop(self):
        """Takes the icon down (and waits a moment for that, so none is left by the clock)."""
        if self.hwnd:
            self.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            if threading.current_thread() is not self._thread:
                self._thread.join(2)

    def notify(self, title, text):
        """A pop-up note from the icon (Windows shows it as a notification)."""
        if not self.hwnd:
            return
        nid = self._nid(NIF_INFO)
        nid.szInfoTitle, nid.szInfo = title[:63], text[:255]
        self.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    # ------------------------------------------------------------ inside the icon's thread

    def _nid(self, flags):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd, nid.uID, nid.uFlags = self.hwnd, 1, flags
        return nid

    def _add(self):
        nid = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        nid.uCallbackMessage, nid.hIcon, nid.szTip = CALLBACK, self.icon, self.tip
        if not self.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            raise ctypes.WinError(ctypes.get_last_error())

    def _run(self):
        try:
            self.user32, self.shell32, kernel32 = _dlls()
            hinst = kernel32.GetModuleHandleW(None)
            # The icon: pythonw's own (the Python logo), else Windows' plain program icon.
            exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            self.icon = self.shell32.ExtractIconW(hinst, exe if os.path.exists(exe) else sys.executable, 0)
            if not self.icon or self.icon == 1:
                self.icon = self.user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
            self._proc = WNDPROC(self._wndproc)       # kept: Windows calls it for as long as the window lives
            cls = WNDCLASSW(lpfnWndProc=self._proc, hInstance=hinst, lpszClassName="mw2tools_tray_%d" % os.getpid())
            if not self.user32.RegisterClassW(ctypes.byref(cls)):
                raise ctypes.WinError(ctypes.get_last_error())
            # Explorer sends this when it starts again (after a crash): the icon goes back up then.
            self._taskbar_created = self.user32.RegisterWindowMessageW("TaskbarCreated")
            self.hwnd = self.user32.CreateWindowExW(0, cls.lpszClassName, self.tip, 0, 0, 0, 0, 0,
                                                    None, None, hinst, None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self._add()
        except Exception as e:  # noqa: BLE001 - handed to start()
            self._error = e
            self.hwnd = None
            self._ready.set()
            return
        self._ready.set()
        msg = wt.MSG()
        while self.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self.user32.TranslateMessage(ctypes.byref(msg))
            self.user32.DispatchMessageW(ctypes.byref(msg))

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == CALLBACK:
                event = lparam & 0xFFFF
                if event == WM_LBUTTONUP:
                    self._call(self.on_click)
                elif event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._show_menu()
                return 0
            if msg == self._taskbar_created:
                self._add()
                return 0
            if msg == WM_CLOSE:
                self.user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                self.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid(0)))
                self.hwnd = None
                self.user32.PostQuitMessage(0)
                return 0
        except Exception:  # noqa: BLE001 - never let an error escape into Windows
            import traceback
            traceback.print_exc()
        return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _show_menu(self):
        menu = self.user32.CreatePopupMenu()
        for i, item in enumerate(self.menu):
            if item is None:
                self.user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            else:
                self.user32.AppendMenuW(menu, MF_STRING, i + 1, item[0])
        pt = wt.POINT()
        self.user32.GetCursorPos(ctypes.byref(pt))
        # Without this, the menu doesn't go away when you click elsewhere.
        self.user32.SetForegroundWindow(self.hwnd)
        cmd = self.user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD,
                                         pt.x, pt.y, 0, self.hwnd, None)
        self.user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        self.user32.DestroyMenu(menu)
        if cmd:
            self._call(self.menu[cmd - 1][1])

    @staticmethod
    def _call(fn):
        # Off the icon's thread, so a slow action (or a message box) doesn't freeze the icon.
        threading.Thread(target=fn, daemon=True).start()
