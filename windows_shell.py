import ctypes
from ctypes import wintypes

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


def _configure_api():
    kernel = ctypes.windll.kernel32
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel.GetModuleHandleW.restype = wintypes.HMODULE
    user = ctypes.windll.user32
    user.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
    user.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
    user.CreateWindowExW.restype = wintypes.HWND
    user.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.DefWindowProcW.restype = LRESULT
    user.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    user.LoadIconW.restype = wintypes.HICON
    user.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
        ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user.LoadImageW.restype = wintypes.HANDLE
    user.DestroyIcon.argtypes = [wintypes.HICON]
    user.GetSystemMetrics.argtypes = [ctypes.c_int]
    user.CreatePopupMenu.restype = wintypes.HMENU
    user.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    user.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
    user.SetForegroundWindow.argtypes = [wintypes.HWND]
    user.DestroyMenu.argtypes = [wintypes.HMENU]
    user.DestroyWindow.argtypes = [wintypes.HWND]
    user.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]


_configure_api()


ERROR_ALREADY_EXISTS = 183
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_USER = 0x0400
WM_APP = 0x8000
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
NIF_MESSAGE = 0x0001
NIF_ICON = 0x0002
NIF_TIP = 0x0004
NIM_ADD = 0x0000
NIM_DELETE = 0x0002
TPM_LEFTALIGN = 0x0000
TPM_BOTTOMALIGN = 0x0020
MF_STRING = 0x0000
MF_SEPARATOR = 0x0800


class SingleInstance:
    def __init__(self, name):
        self.handle = ctypes.windll.kernel32.CreateMutexW(None, False, name)
        if not self.handle:
            raise ctypes.WinError()
        self.already_exists = bool(self.handle) and ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS

    def close(self):
        if self.handle:
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


class _NotifyIconData(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON),
    ]


class TrayIcon:
    def __init__(self, root, on_show, on_exit, icon_path=None):
        self.root, self.on_show, self.on_exit = root, on_show, on_exit
        self.hwnd = None
        self.hicon = None
        self.icon_path = icon_path
        self.owns_icon = False
        self._wndproc = None
        self._class_name = "GameSoundboardTrayWindow"
        self.taskbar_created = ctypes.windll.user32.RegisterWindowMessageW("TaskbarCreated")
        self._create_window()

    def _create_window(self):
        user32 = ctypes.windll.user32
        self._wndproc = WNDPROC(self._window_proc)
        class_name = self._class_name
        wc = WNDCLASS()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
        wc.lpszClassName = class_name
        user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, class_name, class_name, 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None)
        if not self.hwnd:
            raise ctypes.WinError()
        if self.icon_path is not None:
            self.hicon = user32.LoadImageW(None, str(self.icon_path), 1,
                user32.GetSystemMetrics(49), user32.GetSystemMetrics(50), 0x0010)
            self.owns_icon = bool(self.hicon)
        if not self.hicon:
            self.hicon = user32.LoadIconW(None, ctypes.c_void_p(32512))
        self._add_icon()

    def _add_icon(self):
        data = _NotifyIconData()
        data.cbSize = ctypes.sizeof(data)
        data.hWnd, data.uID = self.hwnd, 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_APP + 1
        data.hIcon = self.hicon
        data.szTip = "Game Soundboard"
        if not ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
            raise ctypes.WinError()

    def _window_proc(self, hwnd, msg, wparam, lparam):
        if msg == self.taskbar_created:
            self.root.after(0, self._add_icon)
            return 0
        if msg == WM_APP + 1:
            if lparam == WM_LBUTTONUP:
                self.root.after(0, self.on_show)
            elif lparam == WM_RBUTTONUP:
                self.root.after(0, self._menu)
            return 0
        if msg == WM_COMMAND:
            if wparam == 1001:
                self.root.after(0, self.on_show)
            elif wparam == 1002:
                self.root.after(0, self.on_exit)
            return 0
        return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _menu(self):
        user32 = ctypes.windll.user32
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, 1001, "显示窗口")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, 1002, "退出程序")
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(self.hwnd)
        user32.TrackPopupMenu(menu, TPM_LEFTALIGN | TPM_BOTTOMALIGN, point.x, point.y, 0, self.hwnd, None)
        user32.DestroyMenu(menu)

    def delete(self):
        if not self.hwnd:
            return
        data = _NotifyIconData()
        data.cbSize, data.hWnd, data.uID = ctypes.sizeof(data), self.hwnd, 1
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
        ctypes.windll.user32.DestroyWindow(self.hwnd)
        self.hwnd = None
        if self.owns_icon:
            ctypes.windll.user32.DestroyIcon(self.hicon)
            self.hicon = None
            self.owns_icon = False
