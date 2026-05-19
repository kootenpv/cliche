"""Best-effort process title cleanup for fast-shim Python fallbacks."""
from __future__ import annotations

import os
import shlex
import sys

_KEEPALIVE = None


def _pretty_argv_title() -> str | None:
    argv0 = sys.argv[0] or ""
    if not argv0 or argv0 == "-c":
        return None
    return shlex.join([os.path.basename(argv0), *sys.argv[1:]])


def _linux_argv_env_span() -> tuple[int, int] | None:
    try:
        stat = open("/proc/self/stat", "r", encoding="ascii").read()
        fields = stat.rsplit(") ", 1)[1].split()
        # fields[0] is proc(5)'s field 3 ("state"), so field N is fields[N-3].
        arg_start = int(fields[48 - 3])
        env_end = int(fields[51 - 3])
    except (OSError, ValueError, IndexError):
        return None
    if arg_start <= 0 or env_end <= arg_start:
        return None
    return arg_start, env_end


def _linux_set_full_title(title: str) -> bool:
    """Rewrite Linux's /proc/<pid>/cmdline backing memory.

    Linux stores argv and environ strings in one contiguous process memory
    span. We first copy environ to heap and repoint libc's ``environ`` global,
    then overwrite the original span with a single NUL-terminated title.
    This is the same broad strategy used by setproctitle-style libraries, but
    kept small and local to cliche's narrow fallback-launcher use case.
    """
    span = _linux_argv_env_span()
    if span is None:
        return False
    start, end = span
    size = end - start
    if size <= 1:
        return False

    try:
        import ctypes

        libc = ctypes.CDLL(None)
        envp = ctypes.POINTER(ctypes.c_char_p).in_dll(libc, "environ")
        copied = []
        i = 0
        while envp[i]:
            copied.append(ctypes.create_string_buffer(envp[i]))
            i += 1
        new_environ = (ctypes.c_char_p * (len(copied) + 1))()
        for i, buf in enumerate(copied):
            new_environ[i] = ctypes.cast(buf, ctypes.c_char_p)
        new_environ[len(copied)] = None
        ctypes.c_void_p.in_dll(libc, "environ").value = ctypes.addressof(new_environ)

        data = title.encode("utf-8", "replace")[:size - 1] + b"\0"
        ctypes.memset(start, 0, size)
        ctypes.memmove(start, data, len(data))

        try:
            short = os.path.basename(title).encode("utf-8", "replace")[:15]
            libc.prctl(15, ctypes.c_char_p(short), 0, 0, 0)
        except Exception:
            pass

        global _KEEPALIVE
        _KEEPALIVE = (copied, new_environ)
        return True
    except Exception:
        return False


def _darwin_set_full_title(title: str) -> bool:
    """Rewrite macOS argv/environ strings via crt_externs accessors."""
    try:
        import ctypes

        lib = ctypes.CDLL(None)
        lib._NSGetArgc.restype = ctypes.POINTER(ctypes.c_int)
        lib._NSGetArgv.restype = ctypes.POINTER(ctypes.POINTER(ctypes.c_char_p))
        lib._NSGetEnviron.restype = ctypes.POINTER(ctypes.POINTER(ctypes.c_char_p))

        argc = lib._NSGetArgc().contents.value
        argv = lib._NSGetArgv().contents
        argv_addrs = ctypes.cast(argv, ctypes.POINTER(ctypes.c_void_p))
        envpp = lib._NSGetEnviron()
        env = envpp.contents
        env_addrs = ctypes.cast(env, ctypes.POINTER(ctypes.c_void_p))

        ptrs = []
        for i in range(argc):
            if argv_addrs[i]:
                ptrs.append(argv_addrs[i])

        copied = []
        i = 0
        while env_addrs[i]:
            addr = env_addrs[i]
            ptrs.append(addr)
            copied.append(ctypes.create_string_buffer(ctypes.string_at(addr)))
            i += 1

        if not ptrs:
            return False
        start = min(ptrs)
        end = max(addr + len(ctypes.string_at(addr)) + 1 for addr in ptrs)
        size = end - start
        if size <= 1:
            return False

        new_environ = (ctypes.c_char_p * (len(copied) + 1))()
        for i, buf in enumerate(copied):
            new_environ[i] = ctypes.cast(buf, ctypes.c_char_p)
        new_environ[len(copied)] = None
        envpp[0] = ctypes.cast(new_environ, ctypes.POINTER(ctypes.c_char_p))

        data = title.encode("utf-8", "replace")[:size - 1] + b"\0"
        ctypes.memset(start, 0, size)
        ctypes.memmove(start, data, len(data))

        global _KEEPALIVE
        _KEEPALIVE = (copied, new_environ)
        return True
    except Exception:
        return False


def set_cli_process_title() -> None:
    """Make long-running fallback processes look like the invoked CLI.

    The fast-shim falls back through ``python -c ...`` when clichec defers to
    Python. Tools that display the kernel command line (htop/ps) then show the
    bootstrap snippet instead of ``/path/to/tool args``.

    This is necessarily best-effort:
      - on Linux, cliche rewrites the argv/environ memory span that backs
        /proc/<pid>/cmdline directly, after moving environ aside;
      - on macOS, cliche uses _NSGetArgv/_NSGetEnviron to find and rewrite
        the same process-owned argv/environ string area;
      - the third-party ``setproctitle`` package rewrites the full argv title
        on platforms where the local fallback is unavailable;
      - macOS libc exposes ``setproctitle`` on many builds, so try it directly;
      - Linux ``prctl(PR_SET_NAME)`` is retained as a last-resort short-name
        fallback if the full-title rewrite fails.
    """
    if os.environ.get("CLICHE_NO_PROCTITLE"):
        return
    title = _pretty_argv_title()
    if not title:
        return

    if sys.platform.startswith("linux") and _linux_set_full_title(title):
        return
    if sys.platform == "darwin" and _darwin_set_full_title(title):
        return

    try:
        from setproctitle import setproctitle  # type: ignore
    except Exception:
        pass
    else:
        try:
            setproctitle(title)
            return
        except Exception:
            pass

    if sys.platform == "darwin":
        try:
            import ctypes
            import ctypes.util

            libc_path = ctypes.util.find_library("c")
            if not libc_path:
                return
            libc = ctypes.CDLL(libc_path)
            fn = getattr(libc, "setproctitle")
            fn.argtypes = [ctypes.c_char_p]
            fn.restype = None
            fn(b"%s", title.encode("utf-8", "replace"))
            return
        except Exception:
            return

    if sys.platform.startswith("linux"):
        try:
            import ctypes
            import ctypes.util

            libc_path = ctypes.util.find_library("c")
            libc = ctypes.CDLL(libc_path or None)
            PR_SET_NAME = 15
            short = os.path.basename(sys.argv[0])[:15].encode("utf-8", "replace")
            libc.prctl(PR_SET_NAME, ctypes.c_char_p(short), 0, 0, 0)
        except Exception:
            return
