/* An ordinary Win32 EDIT and its local test oracle. Build in an x64 VS prompt:
   cl /nologo /W4 /WX /utf-8 recording_edit_fixture.c user32.lib imm32.lib /link /subsystem:windows
   The program takes no arguments. It counts only the test's fixed A in its own edit. */
#define UNICODE
#define _UNICODE
#include <windows.h>
#include <imm.h>

#define QUERY (WM_APP + 10)
#define MASK  (WM_APP + 11)
#define EDGE (WM_APP + 12)
#define ENGLISH (WM_APP + 13)
static HWND edit;
static unsigned edge_count;
static unsigned edges[4];
static unsigned masked_count;
static unsigned masked_edges[2];

static unsigned ime_state(void) {
    HIMC context = ImmGetContext(edit);
    unsigned flags = ImmIsIME(GetKeyboardLayout(0)) ? 1u : 0u;
    if (context) {
        flags |= 2u;
        if (ImmGetOpenStatus(context)) flags |= 4u;
        ImmReleaseContext(edit, context);
    }
    return flags;
}

static BOOL english_direct(void) {
    return PRIMARYLANGID(LOWORD((UINT_PTR)GetKeyboardLayout(0))) == LANG_ENGLISH && !(ime_state() & 4u);
}

static BOOL select_english(void) {
    HKL layouts[64], chosen = NULL;
    int i, count = GetKeyboardLayoutList(ARRAYSIZE(layouts), layouts);
    HIMC context;
    for (i = 0; i < count; i++) {
        if (PRIMARYLANGID(LOWORD((UINT_PTR)layouts[i])) == LANG_ENGLISH) {
            chosen = layouts[i];
            if (LOWORD((UINT_PTR)chosen) == 0x0409) break;
        }
    }
    if (!chosen) return FALSE;
    if (GetKeyboardLayout(0) != chosen) ActivateKeyboardLayout(chosen, 0);
    context = ImmGetContext(edit);
    if (context) {
        if (ImmGetOpenStatus(context)) ImmSetOpenStatus(context, FALSE);
        ImmReleaseContext(edit, context);
    }
    return english_direct();
}

static LRESULT CALLBACK window_proc(HWND window, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
    case QUERY:
        if (wp == 0) return (LRESULT)edit;
        if (wp == 1) return (LRESULT)edge_count;
        if (wp == 2) return english_direct();
        if (wp == 3) return (LRESULT)ime_state();
        if (wp == 4) return (LRESULT)masked_count;
        return 0;
    case ENGLISH: return select_english();
    case MASK:
        SendMessageW(edit, EM_SETPASSWORDCHAR, wp ? L'*' : 0, 0);
        return SendMessageW(edit, EM_GETPASSWORDCHAR, 0, 0);
    case EDGE:
        if (wp >= 0x10000) {
            wp -= 0x10000;
            return (wp < masked_count && wp < ARRAYSIZE(masked_edges)) ? masked_edges[wp] : 0;
        }
        return (wp < edge_count && wp < ARRAYSIZE(edges)) ? edges[wp] : 0;
    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(window, msg, wp, lp);
}

int APIENTRY wWinMain(HINSTANCE instance, HINSTANCE previous, LPWSTR command, int show) {
    WNDCLASSW cls = {0};
    HWND window;
    MSG msg;
    (void)previous; (void)command; (void)show;
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    cls.hInstance = instance;
    cls.lpfnWndProc = window_proc;
    cls.lpszClassName = L"ScreenTrigger.Test.OrdinaryEdit";
    cls.hCursor = LoadCursorW(NULL, IDC_ARROW);
    cls.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
    if (!RegisterClassW(&cls)) return 1;
    window = CreateWindowW(cls.lpszClassName, L"ScreenTrigger ordinary edit test",
                           WS_OVERLAPPEDWINDOW, 120, 140, 500, 260,
                           NULL, NULL, instance, NULL);
    if (!window) return 2;
    edit = CreateWindowW(L"EDIT", L"", WS_CHILD | WS_VISIBLE | WS_BORDER | ES_AUTOHSCROLL,
                         16, 20, 380, 130, window, NULL, instance, NULL);
    if (!edit) return 3;
    ShowWindow(window, SW_SHOW);
    SetForegroundWindow(window);
    SetFocus(edit);
    while (GetMessageW(&msg, NULL, 0, 0) > 0) {
        if (msg.hwnd == edit && (msg.message == WM_KEYDOWN || msg.message == WM_KEYUP)
            && msg.wParam == 'A' && ((msg.lParam >> 16) & 255) == 0x1e
            && GetFocus() == edit && GetForegroundWindow() == window) {
            if (SendMessageW(edit, EM_GETPASSWORDCHAR, 0, 0)) {
                if (masked_count < ARRAYSIZE(masked_edges)) masked_edges[masked_count] = msg.message;
                ++masked_count;
            } else {
                if (edge_count < ARRAYSIZE(edges)) edges[edge_count] = msg.message;
                ++edge_count;
            }
        }
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
    return 0;
}
