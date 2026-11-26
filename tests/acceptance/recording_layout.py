"""Select an already installed input layout on the test edit's owner thread."""
import ctypes as ct
from ctypes import wintypes as wt


class EditorLayout:
    def __init__(self, user, edit):
        self.user = user
        self.edit = edit
        user.GetKeyboardLayout.argtypes = [wt.DWORD]
        user.GetKeyboardLayout.restype = wt.HANDLE
        user.GetKeyboardLayoutList.argtypes = [ct.c_int, ct.POINTER(wt.HANDLE)]
        user.ActivateKeyboardLayout.argtypes = [wt.HANDLE, wt.UINT]
        user.ActivateKeyboardLayout.restype = wt.HANDLE
        self.imm = ct.WinDLL("imm32")
        self.imm.ImmGetContext.argtypes = [wt.HWND]
        self.imm.ImmGetContext.restype = wt.HANDLE
        self.imm.ImmGetOpenStatus.argtypes = [wt.HANDLE]
        self.imm.ImmIsIME.argtypes = [wt.HANDLE]
        self.imm.ImmReleaseContext.argtypes = [wt.HWND, wt.HANDLE]
        self.original = None

    def state(self):
        thread = self.user.GetWindowThreadProcessId(self.edit, None)
        layout = self.user.GetKeyboardLayout(thread)
        context = self.imm.ImmGetContext(self.edit)
        try:
            return {"thread_id": thread, "layout": hex(layout),
                    "primary_language": layout & 0x3ff,
                    "is_ime": bool(self.imm.ImmIsIME(layout)),
                    "ime_open": bool(context and self.imm.ImmGetOpenStatus(context))}
        finally:
            if context:
                self.imm.ImmReleaseContext(self.edit, context)

    def select(self, primary_language):
        if self.original is None:
            self.original = self.user.GetKeyboardLayout(0)
        count = self.user.GetKeyboardLayoutList(0, None)
        layouts = (wt.HANDLE * count)()
        n = self.user.GetKeyboardLayoutList(count, layouts)
        layout = next((h for h in layouts[:n] if h & 0x3ff == primary_language), None)
        if layout and self.user.GetKeyboardLayout(0) != layout:
            self.user.ActivateKeyboardLayout(layout, 0)
        return self.state()

    def restore(self):
        if self.original and self.user.GetKeyboardLayout(0) != self.original:
            self.user.ActivateKeyboardLayout(self.original, 0)
            if self.user.GetKeyboardLayout(0) != self.original:
                raise RuntimeError("Could not restore the fixture thread's keyboard layout")
        self.original = None
        return self.state()
