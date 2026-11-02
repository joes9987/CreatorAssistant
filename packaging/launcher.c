/*
 * Tiny Win32 launcher. PyInstaller's bootloader fails with
 * "Failed to load Python DLL ... _internal\python313.dll" before any
 * Python code runs, so the friendly message has to live here.
 *
 * Layout (same folder):
 *   CreatorAssistant.exe       this launcher
 *   CreatorAssistant-core.exe  PyInstaller onedir bootloader
 *   _internal\python313.dll    must match the Python used to build the core
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <wchar.h>

#define PATH_CHARS 32768

static void dirname_of(wchar_t *path) {
    wchar_t *slash = wcsrchr(path, L'\\');
    if (slash) {
        *slash = L'\0';
    }
}

static void show_error(const wchar_t *text) {
    MessageBoxW(NULL, text, L"CreatorAssistant", MB_OK | MB_ICONERROR);
}

/* Skip argv[0] in GetCommandLineW(), including a quoted path with spaces. */
static wchar_t *args_after_argv0(wchar_t *cmdline) {
    wchar_t *p = cmdline;
    if (*p == L'"') {
        p++;
        while (*p && *p != L'"') {
            p++;
        }
        if (*p == L'"') {
            p++;
        }
    } else {
        while (*p && *p != L' ' && *p != L'\t') {
            p++;
        }
    }
    while (*p == L' ' || *p == L'\t') {
        p++;
    }
    return p;
}

int WINAPI WinMain(HINSTANCE instance, HINSTANCE prev, LPSTR cmdline, int show) {
    (void)instance;
    (void)prev;
    (void)cmdline;
    (void)show;

    wchar_t exe_path[PATH_CHARS];
    DWORD n = GetModuleFileNameW(NULL, exe_path, PATH_CHARS);
    if (n == 0 || n >= PATH_CHARS) {
        show_error(L"Could not locate CreatorAssistant.");
        return 1;
    }

    wchar_t dir[PATH_CHARS];
    if (wcscpy_s(dir, PATH_CHARS, exe_path) != 0) {
        show_error(L"Could not locate CreatorAssistant.");
        return 1;
    }
    dirname_of(dir);

    wchar_t dll_path[PATH_CHARS];
    if (swprintf_s(dll_path, PATH_CHARS, L"%s\\_internal\\python313.dll", dir) < 0) {
        show_error(L"Could not locate CreatorAssistant.");
        return 1;
    }
    if (GetFileAttributesW(dll_path) == INVALID_FILE_ATTRIBUTES) {
        show_error(
            L"Missing _internal folder. Please extract the full zip, don't copy only the .exe.");
        return 1;
    }

    wchar_t core_path[PATH_CHARS];
    if (swprintf_s(core_path, PATH_CHARS, L"%s\\CreatorAssistant-core.exe", dir) < 0) {
        show_error(L"Could not start CreatorAssistant-core.exe. Extract the full zip and try again.");
        return 1;
    }

    wchar_t *full = GetCommandLineW();
    wchar_t *args = args_after_argv0(full);
    wchar_t child_cmd[PATH_CHARS];
    int wrote;
    if (args[0] == L'\0') {
        wrote = swprintf_s(child_cmd, PATH_CHARS, L"\"%s\"", core_path);
    } else {
        wrote = swprintf_s(child_cmd, PATH_CHARS, L"\"%s\" %s", core_path, args);
    }
    if (wrote < 0) {
        show_error(L"Could not start CreatorAssistant-core.exe. Extract the full zip and try again.");
        return 1;
    }

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));

    if (!CreateProcessW(core_path, child_cmd, NULL, NULL, FALSE, 0, NULL, dir, &si, &pi)) {
        show_error(L"Could not start CreatorAssistant-core.exe. Extract the full zip and try again.");
        return 1;
    }

    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    if (!GetExitCodeProcess(pi.hProcess, &code)) {
        code = 1;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return (int)code;
}
