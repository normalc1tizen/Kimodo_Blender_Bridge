"""
Kimodo auto-installer

Creates a managed Python venv at ~/.kimodo-venv/, installs Kimodo from the
Aero-Ex fork (offline-capable), downloads the LLM2Vec text-encoder model
locally, patches llm2vec_wrapper.py to load it from disk, and sets the
addon's Python path automatically.
"""

import os
import re
import shutil
import subprocess
import sys
import threading
import traceback

import bpy
from bpy.types import Operator

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

MANAGED_VENV  = os.path.join(os.path.expanduser("~"), ".kimodo-venv")
LLMVEC_DIR    = os.path.join(MANAGED_VENV, "llm2vec-model")
KIMODO_GIT    = "https://github.com/Aero-Ex/kimodo.git"

# ---------------------------------------------------------------------------
# Install state  (module-level; panels poll this via a redraw timer)
# ---------------------------------------------------------------------------

_state: dict = {"running": False, "lines": [], "error": "", "done": False}
_lock = threading.Lock()


def _log(msg: str) -> None:
    print(f"[Kimodo Install] {msg}", flush=True)
    with _lock:
        _state["lines"].append(msg)
        if len(_state["lines"]) > 12:
            _state["lines"] = _state["lines"][-12:]


def install_status() -> str:
    """Return a one-line summary for the UI."""
    with _lock:
        if _state["error"]:
            return f"Error: {_state['error']}"
        if _state["done"]:
            return "Installed successfully"
        if _state["running"]:
            return _state["lines"][-1] if _state["lines"] else "Installing…"
        return ""


def is_installing() -> bool:
    with _lock:
        return _state["running"]


def install_failed() -> bool:
    with _lock:
        return bool(_state["error"])


def managed_python() -> str:
    """Return path to the managed-venv Python, or '' if not present."""
    for rel in ("bin/python3", "bin/python", "Scripts/python.exe"):
        p = os.path.join(MANAGED_VENV, rel)
        if os.path.isfile(p):
            return p
    return ""


def is_installed() -> bool:
    return bool(managed_python())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_system_python() -> str:
    """Return a system Python ≥ 3.10 that is not Blender's bundled Python."""
    blender_py = os.path.realpath(sys.executable)
    for name in ("python3.12", "python3.11", "python3.10", "python3", "python"):
        found = shutil.which(name)
        if not found:
            continue
        if os.path.realpath(found) == blender_py:
            continue
        try:
            r = subprocess.run(
                [found, "-c",
                 "import sys; v=sys.version_info; print(v.major, v.minor)"],
                capture_output=True, text=True, timeout=5,
            )
            parts = r.stdout.strip().split()
            if len(parts) == 2 and int(parts[0]) == 3 and int(parts[1]) >= 10:
                return found
        except Exception:
            pass
    return ""


def _run(cmd: list, step: str) -> None:
    """Run *cmd* as a subprocess, stream output to _log, raise on failure."""
    _log(f"▶ {step}")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    for line in proc.stdout:
        stripped = line.rstrip()
        if stripped:
            _log(stripped)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"{step} failed (exit {proc.returncode})")


def _venv_pip() -> list:
    py = managed_python()
    if not py:
        raise RuntimeError(f"Venv Python not found in {MANAGED_VENV}")
    return [py, "-m", "pip"]


def _find_wrapper(venv_py: str) -> str:
    """Locate llm2vec_wrapper.py inside the venv's site-packages."""
    r = subprocess.run(
        [venv_py, "-c",
         "import importlib.util; s=importlib.util.find_spec('kimodo'); "
         "print(s.origin if s else '')"],
        capture_output=True, text=True, timeout=10,
    )
    origin = r.stdout.strip()
    if not origin:
        return ""
    candidate = os.path.join(
        os.path.dirname(origin), "model", "llm2vec", "llm2vec_wrapper.py"
    )
    return candidate if os.path.isfile(candidate) else ""


def _extract_hf_model_id(wrapper_path: str) -> str:
    """Read llm2vec_wrapper.py and extract the HuggingFace repo ID."""
    with open(wrapper_path) as f:
        text = f.read()

    patterns = [
        # snapshot_download("owner/repo-name")
        r'snapshot_download\s*\(\s*["\']([^"\']+)["\']',
        # from_pretrained("owner/repo-name")
        r'from_pretrained\s*\(\s*["\']([^"\']+)["\']',
        # any "owner/KIMODO..." string
        r'["\']([A-Za-z0-9_\-]+/KIMODO[A-Za-z0-9_\-\.]+)["\']',
        # any "owner/...llm2vec..." string
        r'["\']([A-Za-z0-9_\-]+/[A-Za-z0-9_\-\.]*[Ll][Ll][Mm]2[Vv][Ee][Cc][A-Za-z0-9_\-\.]*)["\']',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return ""


def _patch_wrapper(wrapper_path: str, local_dir: str) -> None:
    """
    Set custom_dir in llm2vec_wrapper.py to *local_dir* so the model is
    loaded from disk instead of downloaded from Hugging Face.
    """
    with open(wrapper_path) as f:
        text = f.read()

    escaped = local_dir.replace("\\", "\\\\")

    # Replace any existing assignment: custom_dir = <expr>
    patched, n = re.subn(
        r'(custom_dir\s*=\s*)[^\n]+',
        lambda m: f'{m.group(1)}"{escaped}"',
        text,
    )
    if n == 0:
        # Variable not found — prepend it right after the last import block
        last_import = max(
            (m.end() for m in re.finditer(r'^(?:import|from)\s+\S+', text, re.M)),
            default=0,
        )
        insert = f'\ncustom_dir = "{escaped}"\n'
        patched = text[:last_import] + insert + text[last_import:]
        _log("custom_dir not found — inserted after imports")

    with open(wrapper_path, "w") as f:
        f.write(patched)


# ---------------------------------------------------------------------------
# Background install thread
# ---------------------------------------------------------------------------

def _do_install() -> None:
    global _state
    try:
        # 1 — Find a system Python ≥ 3.10
        _log("Searching for system Python 3.10+…")
        sys_py = _find_system_python()
        if not sys_py:
            raise RuntimeError(
                "No Python 3.10+ found on PATH. "
                "Install Python 3.10–3.12 and make sure it is on your PATH, "
                "then try again."
            )
        _log(f"Found: {sys_py}")

        # 2 — Create venv
        _run([sys_py, "-m", "venv", MANAGED_VENV], "Creating venv")

        venv_py = managed_python()
        if not venv_py:
            raise RuntimeError("Venv was created but Python binary not found.")

        # 3 — Upgrade pip
        _run([*_venv_pip(), "install", "--upgrade", "pip"], "Upgrading pip")

        # 4 — Install PyTorch (CUDA 12.1 index; covers most modern GPUs)
        _log("Installing PyTorch with CUDA 12.1 support — this may take several minutes…")
        _run(
            [*_venv_pip(), "install", "torch",
             "--index-url", "https://download.pytorch.org/whl/cu121"],
            "Installing PyTorch",
        )

        # 5 — Install build tools needed by Kimodo's C extension.
        #     cmake/ninja are installed into the venv so they are on PATH.
        #     setuptools/wheel are needed because we use --no-build-isolation
        #     below (which skips pip's own isolated build env).
        _log("Installing build tools (cmake, ninja, setuptools, wheel)…")
        _run(
            [*_venv_pip(), "install", "cmake", "ninja", "setuptools", "wheel"],
            "Installing build tools",
        )

        # 6 — Install Kimodo from Aero-Ex fork.
        #     --no-build-isolation makes pip build inside the venv so it picks
        #     up the cmake/ninja we just installed, instead of creating a fresh
        #     temporary env that has no cmake.
        _log("Installing Kimodo (Aero-Ex offline fork)…")
        _run(
            [*_venv_pip(), "install", "--no-build-isolation", f"git+{KIMODO_GIT}"],
            "Installing Kimodo",
        )

        # 7 — Locate llm2vec_wrapper.py
        _log("Locating LLM2Vec wrapper in installed package…")
        wrapper = _find_wrapper(venv_py)
        if not wrapper:
            raise RuntimeError(
                "llm2vec_wrapper.py not found after installation. "
                "Kimodo may not have installed correctly — check the log above."
            )
        _log(f"Found wrapper: {wrapper}")

        # 8 — Determine HuggingFace model ID from the wrapper source
        model_id = _extract_hf_model_id(wrapper)
        if not model_id:
            raise RuntimeError(
                "Could not determine the LLM2Vec model ID from llm2vec_wrapper.py. "
                "The Aero-Ex fork layout may have changed — please file an issue."
            )
        _log(f"LLM2Vec model ID: {model_id}")

        # 9 — Download the model to a local folder
        _log(f"Downloading LLM2Vec model to {LLMVEC_DIR}…  (can be several GB)")
        os.makedirs(LLMVEC_DIR, exist_ok=True)
        dl_script = (
            "from huggingface_hub import snapshot_download; "
            f"snapshot_download(repo_id={model_id!r}, local_dir={LLMVEC_DIR!r})"
        )
        _run([venv_py, "-c", dl_script], "Downloading LLM2Vec model")

        # 10 — Patch wrapper for fully offline operation
        _log("Patching llm2vec_wrapper.py for offline use…")
        _patch_wrapper(wrapper, LLMVEC_DIR)
        _log("Patch applied.")

        # 11 — Update the addon's Python path on the main thread
        def _set_path():
            try:
                for scene in bpy.data.scenes:
                    if not scene.kimodo.python_executable:
                        scene.kimodo.python_executable = venv_py
            except Exception:
                pass
        bpy.app.timers.register(_set_path, first_interval=0.1)

        with _lock:
            _state["done"] = True
        _log("Installation complete!  You can now click 'Start Kimodo'.")

    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[Kimodo Install] FAILED:\n{tb}", flush=True)
        with _lock:
            _state["error"] = str(exc)
    finally:
        with _lock:
            _state["running"] = False


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class KIMODO_OT_InstallKimodo(Operator):
    bl_idname      = "kimodo.install_kimodo"
    bl_label       = "Install Kimodo (Auto)"
    bl_description = (
        "Create ~/.kimodo-venv, install Kimodo from the Aero-Ex offline fork, "
        "download the LLM2Vec text encoder locally, and configure the addon "
        "automatically. Requires internet access and ~5–10 GB of disk space."
    )

    def execute(self, context):
        if is_installing():
            self.report({"WARNING"}, "Installation is already in progress.")
            return {"CANCELLED"}

        # If a previous attempt left a partial venv behind, remove it so we
        # start clean.  Only do this when the previous run actually failed;
        # never touch a venv the user set up themselves (done=True).
        if install_failed() and os.path.isdir(MANAGED_VENV):
            _log(f"Removing partial venv for clean retry: {MANAGED_VENV}")
            try:
                shutil.rmtree(MANAGED_VENV)
            except Exception as exc:
                self.report({"ERROR"}, f"Could not remove partial venv: {exc}")
                return {"CANCELLED"}

        if is_installed():
            self.report({"INFO"}, "Managed Kimodo venv already exists.")
            return {"CANCELLED"}

        with _lock:
            _state.update(running=True, lines=[], error="", done=False)

        threading.Thread(target=_do_install, daemon=True).start()

        # Keep the N-panel refreshing while the install runs
        def _redraw():
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == "VIEW_3D":
                        area.tag_redraw()
            return 0.5 if is_installing() else None

        bpy.app.timers.register(_redraw, first_interval=0.5)
        self.report({"INFO"}, "Kimodo installation started — watch the Connection panel.")
        return {"FINISHED"}


class KIMODO_OT_UseInstalledKimodo(Operator):
    bl_idname      = "kimodo.use_installed_kimodo"
    bl_label       = "Use Installed Kimodo"
    bl_description = "Point the addon at the managed ~/.kimodo-venv Python"

    def execute(self, context):
        py = managed_python()
        if not py:
            self.report({"ERROR"}, f"Managed venv not found at {MANAGED_VENV}")
            return {"CANCELLED"}
        context.scene.kimodo.python_executable = py
        self.report({"INFO"}, f"Python path set to: {py}")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = [KIMODO_OT_InstallKimodo, KIMODO_OT_UseInstalledKimodo]


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
