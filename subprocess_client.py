"""
Kimodo Blender Bridge — Subprocess Client

Runs inside Blender's Python. Manages the bridge_server.py subprocess
which runs under the Kimodo venv Python (with PyTorch, Kimodo, etc.).

Communication: newline-delimited JSON over stdin / stdout.
"""

import json
import os
import subprocess
import threading
import time


# ---------------------------------------------------------------------------
# Module-level process state (one bridge process per Blender session)
# ---------------------------------------------------------------------------

_proc: "subprocess.Popen | None" = None
_lock = threading.Lock()
_status = "Not started"
_ready  = False


def _bridge_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge_server.py")


def _send(obj: dict) -> None:
    if _proc is None or _proc.poll() is not None:
        raise RuntimeError("Bridge is not running")
    _proc.stdin.write(json.dumps(obj) + "\n")
    _proc.stdin.flush()


def _recv() -> "dict | None":
    if _proc is None or _proc.poll() is not None:
        return None
    raw = _proc.stdout.readline()
    if not raw:
        return None
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start(python_exe: str, model_name: str, progress_callback=None) -> "tuple[bool, str]":
    """
    Launch bridge_server.py and block until the model reports ready.
    Must be called from a background thread — model loading takes 1-3 min.
    Returns (success, status_message).
    """
    global _proc, _status, _ready

    with _lock:
        if _proc is not None and _proc.poll() is None:
            return True, _status  # already running

        bridge = _bridge_path()
        if not os.path.isfile(bridge):
            return False, f"bridge_server.py not found at: {bridge}"

        python = _resolve_python(python_exe)
        _ready  = False
        _status = "Launching…"

        try:
            _proc = subprocess.Popen(
                [python, bridge, "--model", model_name],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,      # line-buffered
            )
        except FileNotFoundError:
            _proc = None
            return False, f"Python executable not found: {python}"
        except Exception as exc:
            _proc = None
            return False, f"Failed to launch bridge: {exc}"

    # Drain stderr in background to prevent pipe deadlock
    def _drain():
        for _ in _proc.stderr:
            pass
    threading.Thread(target=_drain, daemon=True).start()

    # Wait for "ready" or "error"
    deadline = time.monotonic() + 420   # 7-min ceiling (large models, slow GPU)
    while time.monotonic() < deadline:
        if _proc.poll() is not None:
            try:
                tail = _proc.stderr.read(800)
            except Exception:
                tail = ""
            _status = f"Process exited early. stderr: {tail}"
            return False, _status

        msg = _recv()
        if msg is None:
            time.sleep(0.1)
            continue

        s = msg.get("status", "")

        if s == "loading":
            _status = msg.get("message", "Loading…")
            if progress_callback:
                progress_callback(_status)

        elif s == "ready":
            _ready  = True
            _status = (
                f"Ready — {msg.get('model', model_name)} "
                f"on {msg.get('device', '?')} "
                f"({msg.get('fps', '?')} fps)"
            )
            return True, _status

        elif s == "error":
            err = msg.get("message", "Unknown error")
            _status = f"Failed: {err}"
            stop()
            return False, _status

    stop()
    return False, "Timed out waiting for Kimodo (>7 min)"


def stop() -> None:
    global _proc, _ready, _status
    with _lock:
        if _proc is not None:
            try:
                _send({"cmd": "quit"})
            except Exception:
                pass
            try:
                _proc.terminate()
                _proc.wait(timeout=5)
            except Exception:
                try:
                    _proc.kill()
                except Exception:
                    pass
            _proc = None
        _ready  = False
        _status = "Stopped"


def is_running() -> bool:
    return _proc is not None and _proc.poll() is None


def get_status() -> str:
    return _status


def generate_motion(
    prompt: str,
    duration: float,
    seed: int,
    output_format: str,
    constraints_json: "str | None" = None,
    diffusion_steps: int = 100,
    bvh_standard_tpose: bool = False,
    progress_callback=None,
) -> "tuple[bool, str]":
    """
    Send one generation request. Blocks until done or error.
    Must be called from a background thread.
    Returns (success, file_path_or_error_message).
    """
    if not is_running():
        return False, "Kimodo is not running — click 'Start Kimodo' first."

    req = {
        "cmd": "generate",
        "prompt": prompt,
        "duration": duration,
        "seed": seed if seed >= 0 else None,
        "output_format": output_format,
        "constraints_json": constraints_json,
        "diffusion_steps": diffusion_steps,
        "bvh_standard_tpose": bvh_standard_tpose,
    }

    try:
        _send(req)
    except Exception as exc:
        return False, f"Failed to send request: {exc}"

    while True:
        if not is_running():
            return False, "Kimodo process died during generation."

        msg = _recv()
        if msg is None:
            time.sleep(0.05)
            continue

        s = msg.get("status", "")

        if s == "progress":
            if progress_callback:
                progress_callback(msg.get("message", ""))

        elif s == "done":
            path = msg.get("path", "")
            if not path or not os.path.isfile(path):
                return False, f"Output file not found: {path}"
            return True, path

        elif s == "error":
            return False, msg.get("message", "Generation failed")


# ---------------------------------------------------------------------------
# Python executable resolution
# ---------------------------------------------------------------------------

def _resolve_python(hint: str) -> str:
    """
    Find a Python executable from the user's hint, auto-detecting common
    patterns like venv roots, sibling venvs, and kimodo_gen on PATH.
    """
    import shutil

    hint = (hint or "").strip()

    # Direct path to an executable
    if hint and os.path.isfile(hint):
        return hint

    # Path to a venv / conda env root — pick the python inside
    if hint and os.path.isdir(hint):
        for rel in ("bin/python3", "bin/python", "Scripts/python.exe"):
            p = os.path.join(hint, rel)
            if os.path.isfile(p):
                return p

    # Look for a venv sitting next to (or near) the addon directory
    addon_dir = os.path.dirname(os.path.abspath(__file__))
    for rel_venv in ("../venv", "../../venv", "../.venv", "../../.venv"):
        venv_root = os.path.normpath(os.path.join(addon_dir, rel_venv))
        for sub in ("bin/python3", "bin/python", "Scripts/python.exe"):
            p = os.path.join(venv_root, sub)
            if os.path.isfile(p):
                return p

    # kimodo_gen on PATH → its sibling Python is the right one
    kimodo_gen = shutil.which("kimodo_gen")
    if kimodo_gen:
        bin_dir = os.path.dirname(kimodo_gen)
        for name in ("python3", "python"):
            p = os.path.join(bin_dir, name)
            if os.path.isfile(p):
                return p

    # Last resort: whatever python3 / python is on PATH
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found

    return "python3"
