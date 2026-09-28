"""M2: a stage's stamp is what the stage was **seen** to read, not a list of what it is supposed to read.

Every stamp used to be a hand-kept list — `board.py`, `layout.core.py`, the part files, the modules
`board.py` imports, the sidecars — and every review found an input the list did not name: a helper
module `layout.core.py` imports, a JSON file it opens, a `schematic.kicad_pro` beside the schematic
that steers KiCad's ERC, an environment switch changing the shipped copper (sixth review). A plain build
then said "skipped, ok" and shipped the old output.

`tracing(stage, root)` watches a stage instead. It installs (once per process) a `sys.addaudithook`
hook and, while a stage runs, records:

- **every file the build's own Python process opens for reading** (the `open` audit event, which
  `open()`, `os.open`, `Path.read_text` and the import system's `io.open_code` all raise) that the
  stage did not write itself — a helper module (a `.pyc` is traced back to its source), a data file,
  a part file, a board an earlier stage wrote. Python's own library, installed packages and pcbc's
  own code are the tool, not the design, and are left out of `reads`;
- **for every subprocess** (the `subprocess.Popen` event: `kicad-cli`): the exact argv, and
  every file in each directory it is handed (a path argument's directory), split at the end of the
  stage into what the stage wrote (outputs, by name) and what was already there (inputs, by
  content). A `schematic.kicad_pro` lying beside the schematic `kicad-cli sch erc` is handed is an
  input, and one that appears afterwards is a file nobody stamped: both are stale;
- **the build environment that changes output**: every `PCBC_*`, `KICAD*` and `SOURCE_DATE_EPOCH`
  variable the process reads while the stage runs (`os.environ` is watched, so `PCBC_STRICT_POWER`
  and the date are recorded by the code that reads them), the `KICAD*` variables a subprocess
  inherits, and the `kicad-cli` version string of every `kicad-cli` the stage ran;
- **the generator itself**: a hash of pcbc's own source (`code_sha`). The router and the placer are
  pcbc's code, so a change to them changes the copper; a board built by an older generator is stale.

It also keeps an **ordered event log** (`Trace.events`: `(seq, kind, what, caller)`): every open of a
`.kicad_pcb`, every subprocess, every `gen.decompile` and every `mark()` the build makes (`layout_job`
marks `"emit"` just before it writes the emitted board). The native generator's proof that no board
file is an intermediate (`docs/native-plan.md` §5, P1) is read off it.

`Trace.record()` is the stage's record; `drift(record, root)` is the first thing in it that is not
so any more, worded for `build.stale_reason`, which calls it stale **from the stage that read it**.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ENV_PREFIXES = ("PCBC_", "KICAD")
ENV_NAMES = frozenset({"SOURCE_DATE_EPOCH"})
# What a subprocess is handed through its environment and may read: KiCad's own variables.
# pcbc's own `PCBC_*` switches are read by pcbc (and recorded where they are read), not by the tools.
CHILD_ENV_PREFIXES = ("KICAD",)
# The records the build writes about its own outputs: never an input, and rewritten by later stages.
STAMP_NAMES = frozenset({"inputs.json", "schematic.inputs.json"})

_ACTIVE: list["Trace"] = []
_LOCK = threading.Lock()
_INSTALLED = False
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


def tracked_env(name: str) -> bool:
    return name in ENV_NAMES or name.startswith(ENV_PREFIXES)


def raw_env(name: str) -> str | None:
    """An environment variable read without being recorded (the tracer's own reads are not the build's)."""
    try:
        return (_ORIG_GETITEM or os._Environ.__getitem__)(os.environ, name)  # type: ignore[attr-defined]
    except KeyError:
        return None


def _tool_roots() -> tuple[str, ...]:
    """Python's own library and installed packages: the tool, not the design."""
    import site
    import sysconfig

    roots = {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix}
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        try:
            roots.add(sysconfig.get_paths()[key])
        except KeyError:
            pass
    try:
        roots.update(site.getsitepackages())
    except AttributeError:
        pass
    return tuple(sorted({os.path.realpath(r) + os.sep for r in roots if r}))


_PCBC_CODE = os.path.realpath(os.path.dirname(os.path.abspath(__file__))) + os.sep


# The operating system's own files (`platform` reads the macOS version plist on import): not the design.
OS_ROOTS = ("/System/", "/usr/lib/", "/usr/share/", "/etc/", "/private/etc/", "/private/var/db/", "/dev/", "/proc/", "/sys/")


def is_tool_file(path: str) -> bool:
    """Python's library, an installed package, the operating system's files, or pcbc's own code (its
    `.py`/`.pyc`; pcbc's shared data — the bundled footprints — is read like any other input)."""
    real = os.path.realpath(path)
    if real.startswith(_tool_roots_cached()) or real.startswith(OS_ROOTS) or path.startswith(OS_ROOTS):
        return True
    return real.startswith(_PCBC_CODE) and (real.endswith((".py", ".pyc")) or "__pycache__" in real)


_ROOTS: tuple[str, ...] | None = None


def _tool_roots_cached() -> tuple[str, ...]:
    global _ROOTS
    if _ROOTS is None:
        _ROOTS = _tool_roots()
    return _ROOTS


def _source_of(path: str) -> str:
    """A `.pyc` read by the import system stands for its source: an edit to the `.py` is what changes."""
    if path.endswith(".pyc"):
        import importlib.util

        try:
            src = importlib.util.source_from_cache(path)
        except ValueError:
            return path
        if os.path.exists(src):
            return src
    return path


class Trace:
    """What one stage was seen to read. See the module doc."""

    def __init__(self, stage: str, root: Path):
        self.stage = stage
        self.root = os.path.realpath(str(root))
        self.start_ns = time.time_ns()
        self.reads: set[str] = set()
        self.writes: set[str] = set()
        self.env: dict[str, str | None] = {}
        self.child_env: dict[str, str] = {}
        self.argv: list[list[str]] = []
        self.dirs: set[str] = set()
        self.tools: dict[str, str] = {}
        self.events: list[tuple[int, str, object, str]] = []

    # -- events (called from the audit hook: never raise, never open a file)
    def on_open(self, path, mode, flags) -> None:
        if isinstance(path, int):
            return
        p = os.fsdecode(path) if isinstance(path, (bytes, os.PathLike)) else str(path)
        if not p or p.startswith("/dev/"):
            return
        p = os.path.abspath(p)
        write = isinstance(flags, int) and bool(flags & _WRITE_FLAGS)
        if not write and isinstance(mode, str) and any(c in mode for c in "wax+"):
            write = True
        (self.writes if write else self.reads).add(os.path.realpath(p))
        if p.endswith(".kicad_pcb"):
            self.events.append((next(_SEQ), "open-w" if write else "open-r", os.path.realpath(p), _caller()))

    def on_popen(self, executable, args, cwd, env) -> None:
        argv = [os.fsdecode(a) if isinstance(a, (bytes, os.PathLike)) else str(a) for a in (args if isinstance(args, (list, tuple)) else [args])]
        self.argv.append(argv)
        self.events.append((next(_SEQ), "popen", tuple(argv), _caller()))
        exe = os.path.basename(argv[0]) if argv else ""
        if exe.startswith("kicad-cli"):
            self.tools["kicad-cli"] = os.path.realpath(argv[0]) if os.path.sep in argv[0] else argv[0]
        base = os.path.realpath(cwd) if cwd else os.getcwd()
        for a in argv[1:]:
            if not a or a.startswith("-"):
                continue
            ap = os.path.realpath(os.path.join(base, a))
            if os.path.isdir(ap):
                self.dirs.add(ap)
            elif os.path.isfile(ap) or (os.sep in a and os.path.isdir(os.path.dirname(ap))):
                self.dirs.add(os.path.dirname(ap))
        try:
            if env is None:
                for k in list(os.environ):
                    if isinstance(k, str) and k.startswith(CHILD_ENV_PREFIXES):
                        self.child_env[k] = str(raw_env(k))
            else:
                for k, v in dict(env).items():
                    k = os.fsdecode(k) if isinstance(k, bytes) else str(k)
                    if k.startswith(CHILD_ENV_PREFIXES):
                        self.child_env[k] = os.fsdecode(v) if isinstance(v, bytes) else str(v)
        except Exception:  # noqa: BLE001 - an audit hook must never raise
            pass

    def on_env(self, name: str, value: str | None) -> None:
        self.env[name] = value

    # -- the record
    def _key(self, path: str) -> str:
        return _key(path, self.root)

    def _written_here(self, path: str) -> bool:
        if path in self.writes:
            return True
        try:
            return os.stat(path).st_mtime_ns >= self.start_ns
        except OSError:
            return True

    def record(self) -> dict:
        """The stage's record. The tracer's own reads (hashing, `kicad-cli version`) are not the
        stage's: it stops watching while it writes this."""
        live = self in _ACTIVE
        if live:
            _ACTIVE.remove(self)
        try:
            return self._record()
        finally:
            if live:
                _ACTIVE.append(self)

    def _record(self) -> dict:
        reads: dict[str, str] = {}
        for p in sorted(self.reads):
            if p in self.writes or is_tool_file(p):
                continue
            src = _source_of(p)
            if src != p and is_tool_file(src):
                continue
            if not os.path.isfile(src) or self._written_here(src):
                continue
            reads[self._key(src)] = sha(src)
        dirs: dict[str, dict] = {}
        for d in sorted(self.dirs):
            if not os.path.isdir(d) or is_tool_file(d + os.sep):
                continue
            inputs, outputs = {}, []
            for name in sorted(os.listdir(d)):
                f = os.path.join(d, name)
                if name in STAMP_NAMES or not os.path.isfile(f):
                    continue
                if self._written_here(f):
                    outputs.append(name)
                else:
                    inputs[name] = sha(f)
            dirs[self._key(d)] = {"inputs": inputs, "outputs": outputs}
        tools = {}
        if "kicad-cli" in self.tools:
            tools["kicad-cli"] = {"path": self.tools["kicad-cli"], "version": kicad_cli_version(self.tools["kicad-cli"])}
        tools["pcbc"] = code_sha()
        return {
            "reads": reads,
            "dirs": dirs,
            "env": dict(sorted(self.env.items())),
            "child_env": dict(sorted(self.child_env.items())),
            "tools": tools,
            "argv": [[_norm_arg(a, self.root) for a in argv] for argv in self.argv],
        }


def sha(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def _key(path: str, root: str) -> str:
    """A path as a stamp writes it: relative to the board's directory when under it (so two builds
    of one board from two directories stamp the same bytes), absolute otherwise."""
    real = os.path.realpath(path)
    if real == root:
        return "."
    if real.startswith(root + os.sep):
        return os.path.relpath(real, root).replace(os.sep, "/")
    return real


def _abs(key: str, root: str) -> str:
    return key if os.path.isabs(key) else os.path.join(root, key)


def _tmp_roots() -> tuple[str, ...]:
    import tempfile

    t = tempfile.gettempdir()
    return tuple(sorted({os.path.realpath(t) + os.sep, os.path.abspath(t) + os.sep}, key=len, reverse=True))


_TMP_NAME = re.compile(r"^[^/]+")


def _norm_arg(a: str, root: str) -> str:
    """An argv entry with the board's directory as `.` and a scratch directory as `<tmp>`: the argv is
    part of the stamp, and two builds from two directories must stamp the same bytes."""
    if not a or a.startswith("-"):
        return a
    for form in {root, os.path.realpath(root)}:
        if a == form:
            return "."
        if a.startswith(form + os.sep):
            return "./" + a[len(form) + 1 :]
    real = os.path.realpath(a) if os.path.isabs(a) else a
    if real.startswith(root + os.sep):
        return "./" + real[len(root) + 1 :]
    for t in _tmp_roots():
        for cand in (a, real):
            if cand.startswith(t):
                rest = cand[len(t) :]
                return "<tmp>/" + _TMP_NAME.sub("", rest, count=1).lstrip("/")
    return a


_VERSIONS: dict[tuple, str] = {}


def kicad_cli_version(path: str) -> str:
    """`kicad-cli version`, once per binary per process (keyed by path, size and mtime)."""
    import shutil
    import subprocess

    exe = path if os.path.sep in path else (shutil.which(path) or path)
    try:
        st = os.stat(exe)
        key = (os.path.realpath(exe), st.st_size, st.st_mtime_ns)
    except OSError:
        return "missing"
    if key not in _VERSIONS:
        try:
            run = subprocess.run([exe, "version"], capture_output=True, text=True, timeout=60)
            _VERSIONS[key] = (run.stdout or run.stderr).strip() or f"exit {run.returncode}"
        except (OSError, subprocess.SubprocessError) as exc:
            _VERSIONS[key] = f"unrunnable: {type(exc).__name__}"
    return _VERSIONS[key]


def git_head(repo: str) -> str | None:
    """The commit a git checkout is at, read from `.git` without running git."""
    g = os.path.join(repo, ".git")
    try:
        head = open(os.path.join(g, "HEAD")).read().strip()
    except OSError:
        return None
    if not head.startswith("ref: "):
        return head
    ref = head[5:].strip()
    try:
        return open(os.path.join(g, ref)).read().strip()
    except OSError:
        pass
    try:
        for line in open(os.path.join(g, "packed-refs")):
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    except OSError:
        pass
    return None


_CODE_SHA: str | None = None


def code_sha() -> str:
    """One hash of pcbc's own source: every `.py` under the package, by relative path and content.
    Once per process (the source a process imported does not change under it)."""
    global _CODE_SHA
    if _CODE_SHA is None:
        h = hashlib.sha256()
        root = os.path.dirname(os.path.abspath(__file__))
        for dirpath, dirnames, filenames in sorted(os.walk(root)):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
            for f in sorted(filenames):
                if f.endswith(".py"):
                    path = os.path.join(dirpath, f)
                    h.update(os.path.relpath(path, root).encode() + b"\0")
                    with open(path, "rb") as fh:
                        h.update(fh.read())
        _CODE_SHA = h.hexdigest()
    return _CODE_SHA


import itertools

_SEQ = itertools.count(1)


def _caller(depth: int = 2) -> str:
    """The pcbc module (below `trace` itself) whose code caused the event, or "" — so a proof can say
    *who* opened a board, not only that one was opened."""
    try:
        f = sys._getframe(depth)
    except ValueError:
        return ""
    here = os.path.abspath(__file__)
    while f is not None:
        fn = os.path.abspath(f.f_code.co_filename)
        if fn.startswith(_PCBC_CODE) and fn != here:
            return os.path.splitext(os.path.relpath(fn, _PCBC_CODE))[0].replace(os.sep, ".")
        f = f.f_back
    return ""


def mark(name: str) -> None:
    """A named point in every active trace's event log (`layout_job` marks "emit")."""
    for t in list(_ACTIVE):
        t.events.append((next(_SEQ), "mark", name, _caller()))


def note(kind: str, what: str) -> None:
    """An event a pcbc function reports about itself (`gen.decompile` reports every call): the caller
    recorded is the module that called that function, not the function's own."""
    if not _ACTIVE:
        return
    who = _caller(3)
    for t in list(_ACTIVE):
        t.events.append((next(_SEQ), kind, what, who))


# ------------------------------------------------------------------ the hook and the environment


def _hook(event: str, args: tuple) -> None:
    if not _ACTIVE:
        return
    try:
        if event == "open":
            for t in list(_ACTIVE):
                t.on_open(*args[:3])
        elif event == "subprocess.Popen":
            for t in list(_ACTIVE):
                t.on_popen(*args[:4])
    except Exception:  # noqa: BLE001 - an audit hook that raises would abort the open itself
        pass


_ORIG_GETITEM = None


def _install() -> None:
    global _INSTALLED, _ORIG_GETITEM
    with _LOCK:
        if _INSTALLED:
            return
        sys.addaudithook(_hook)
        _ORIG_GETITEM = os._Environ.__getitem__  # type: ignore[attr-defined]

        def getitem(self, key):
            try:
                value = _ORIG_GETITEM(self, key)
            except KeyError:
                if _ACTIVE and self is os.environ and isinstance(key, str) and tracked_env(key):
                    for t in list(_ACTIVE):
                        t.on_env(key, None)
                raise
            if _ACTIVE and self is os.environ and isinstance(key, str) and tracked_env(key):
                for t in list(_ACTIVE):
                    t.on_env(key, value)
            return value

        os._Environ.__getitem__ = getitem  # type: ignore[attr-defined,method-assign]
        _INSTALLED = True


@contextmanager
def tracing(stage: str, root: Path):
    """Watch one stage. `root` is the board's directory (paths under it are stamped relative)."""
    _install()
    t = Trace(stage, root)
    _ACTIVE.append(t)
    try:
        yield t
    finally:
        if t in _ACTIVE:
            _ACTIVE.remove(t)


# ------------------------------------------------------------------ is it still so


def drift(rec: dict | None, root: Path, stage: str) -> str | None:
    """The first input `rec` recorded that is not so now, as a stale reason; None when all hold.
    A record from before M2 (no `traced`) is an older pcbc's and is stale."""
    if not isinstance(rec, dict):
        return f"the {stage} stage has no record of what it read (it was made by an older pcbc)"
    root_s = os.path.realpath(str(root))
    for key, was in sorted((rec.get("reads") or {}).items()):
        if sha(_abs(key, root_s)) != was:
            return f"{key} changed since the {stage} stage read it (a file the {stage} stage opened; {'deleted' if not os.path.exists(_abs(key, root_s)) else 'edited'})"
    for key, d in sorted((rec.get("dirs") or {}).items()):
        path = _abs(key, root_s) if key != "." else root_s
        if not os.path.isdir(path):
            continue  # the directory went with its outputs; those have their own records
        outs = set(d.get("outputs") or ())
        ins = d.get("inputs") or {}
        for name in sorted(os.listdir(path)):
            f = os.path.join(path, name)
            if name in STAMP_NAMES or name in outs or not os.path.isfile(f):
                continue
            if name not in ins:
                return f"{_join(key, name)} is beside a file a {stage}-stage tool was handed and was not there when it ran (a tool reads its directory: KiCad reads a .kicad_pro beside the file it opens); delete it, or rebuild"
            if sha(f) != ins[name]:
                return f"{_join(key, name)} changed since the {stage} stage handed its directory to a tool"
        for name in sorted(ins):
            if not os.path.isfile(os.path.join(path, name)):
                return f"{_join(key, name)} was deleted since the {stage} stage handed its directory to a tool"
    for name, was in sorted((rec.get("env") or {}).items()):
        now = raw_env(name)
        if now != was:
            return f"the environment changed since the {stage} stage read it: {name} was {_show(was)}, now {_show(now)}"
    now_child = {k: raw_env(k) for k in os.environ if k.startswith(CHILD_ENV_PREFIXES)} if (rec.get("argv")) else {}
    for name in sorted(set(rec.get("child_env") or {}) | set(now_child)):
        was, now = (rec.get("child_env") or {}).get(name), now_child.get(name)
        if now != was:
            return f"the environment changed since the {stage} stage's tools ran: {name} was {_show(was)}, now {_show(now)}"
    tools = rec.get("tools") or {}
    if "kicad-cli" in tools:
        from .fab import kicad_cli

        cli = str(kicad_cli())
        now = {"path": os.path.realpath(cli) if os.path.sep in cli else cli, "version": kicad_cli_version(cli)}
        if now != tools["kicad-cli"]:
            return f"kicad-cli changed since the {stage} stage ran it ({tools['kicad-cli'].get('path')} {tools['kicad-cli'].get('version')} -> {now['path']} {now['version']})"
    if "pcbc" in tools and tools["pcbc"] != code_sha():
        return f"pcbc's own code (the placer, the router, the emitter) changed since the {stage} stage ran (source {tools['pcbc'][:12]} -> {code_sha()[:12]})"
    return None


def _join(key: str, name: str) -> str:
    return name if key == "." else f"{key}/{name}"


def _show(v) -> str:
    return "unset" if v is None else repr(v)
