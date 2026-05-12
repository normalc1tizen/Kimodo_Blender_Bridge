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

MANAGED_VENV        = os.path.join(os.path.expanduser("~"), ".kimodo-venv")
LLMVEC_DIR          = os.path.join(MANAGED_VENV, "llm2vec-model")
LLMVEC_MODEL_ID     = "Aero-Ex/KIMODO-Meta3_llm2vec_NF4"
# Written at the very end of a successful install; absence means partial/broken.
_SENTINEL           = os.path.join(MANAGED_VENV, ".kimodo_install_complete")
# Placeholder string in Aero-Ex's llm2vec_wrapper.py that we replace with LLMVEC_DIR
_WRAPPER_PLACEHOLDER = "path_to_your_Llama_text-encoders"

# ---------------------------------------------------------------------------
# Install state  (module-level; panels poll this via a redraw timer)
# ---------------------------------------------------------------------------

_state: dict = {"running": False, "lines": [], "error": "", "done": False,
                "needs_python": False}
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


def needs_python() -> bool:
    with _lock:
        return _state["needs_python"]


def managed_python() -> str:
    """Return path to the managed-venv Python, or '' if not present."""
    for rel in ("bin/python3", "bin/python", "Scripts/python.exe"):
        p = os.path.join(MANAGED_VENV, rel)
        if os.path.isfile(p):
            return p
    return ""


def venv_exists() -> bool:
    """True if the venv directory is present (even if install is incomplete)."""
    return os.path.isdir(MANAGED_VENV)


def is_installed() -> bool:
    """True only when the venv has a Python binary AND the install completed."""
    return bool(managed_python()) and os.path.isfile(_SENTINEL)


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
        # Skip Windows App Execution Alias stubs — they live under
        # %LOCALAPPDATA%\Microsoft\WindowsApps and open the Store instead of
        # running a real interpreter, which breaks venv creation and pip.
        if os.name == "nt" and "windowsapps" in found.lower():
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


def _git_available() -> bool:
    """Return True if git is on PATH and runnable."""
    try:
        r = subprocess.run(
            ["git", "--version"], capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _github_install_url(owner: str, repo: str) -> str:
    """
    Return a pip-installable URL for a GitHub repo.  Uses the git+https form
    when git is available; falls back to the zip archive (no git required).
    """
    if _git_available():
        return f"git+https://github.com/{owner}/{repo}.git"
    # GitHub serves the default branch as a zip that pip can install directly.
    return f"https://github.com/{owner}/{repo}/archive/HEAD.zip"


def _run(cmd: list, step: str, env: "dict | None" = None) -> None:
    """Run *cmd* as a subprocess, stream output to _log, raise on failure."""
    _log(f"▶ {step}")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
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
    # Kept for API compatibility; model ID is now hardcoded as LLMVEC_MODEL_ID.
    return LLMVEC_MODEL_ID


def _patch_wrapper(wrapper_path: str, local_dir: str) -> None:
    """
    Replace the placeholder path in llm2vec_wrapper.py with *local_dir* so
    the model loads from disk.  The Aero-Ex fork uses the literal string
    'path_to_your_Llama_text-encoders' as the user-editable slot.
    """
    with open(wrapper_path) as f:
        text = f.read()

    if _WRAPPER_PLACEHOLDER not in text:
        _log("Warning: placeholder not found in llm2vec_wrapper.py — already patched?")
        return

    # Use a raw string so Windows backslashes survive the replacement.
    safe_dir = local_dir.replace("\\", "\\\\")
    patched = text.replace(_WRAPPER_PLACEHOLDER, safe_dir, 1)

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
            with _lock:
                _state["needs_python"] = True
            raise RuntimeError(
                "No Python 3.10–3.12 found. "
                "Install it from python.org (tick 'Add Python to PATH'), "
                "then click Retry Install."
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

        # 5 — Install the pre-built motion_correction wheel from Aero-Ex.
        #     motion_correction is a C extension inside Kimodo's setup.py.
        #     Building it from source requires MSVC on Windows (not just cmake),
        #     so the Aero-Ex fork ships pre-built wheels for each Python version.
        #     We install the wheel first; then tell setup.py to skip rebuilding it.
        _log("Installing pre-built motion_correction wheel…")
        r = subprocess.run(
            [venv_py, "-c",
             "import sys; print(f'cp{sys.version_info.major}{sys.version_info.minor}')"],
            capture_output=True, text=True, timeout=5,
        )
        py_tag = r.stdout.strip()  # e.g. "cp312"

        if os.name == "nt":
            platform_tag = "win_amd64"
        elif sys.platform.startswith("linux"):
            platform_tag = "manylinux_2_27_x86_64.manylinux_2_28_x86_64"
        else:
            platform_tag = None  # macOS: no pre-built wheel available

        if platform_tag:
            wheel_url = (
                "https://github.com/Aero-Ex/kimodo/releases/download/v1.0.0/"
                f"motion_correction-1.0.0-{py_tag}-{py_tag}-{platform_tag}.whl"
            )
            _log(f"Wheel: {wheel_url}")
            _run([*_venv_pip(), "install", wheel_url], "Installing motion_correction")
        else:
            _log("macOS: no pre-built wheel — motion_correction will build from source "
                 "(requires Xcode Command Line Tools)")

        # 6 — Install packages that Kimodo imports but does not declare in
        #     pyproject.toml (discovered by auditing every import in the source):
        #
        #   bitsandbytes  — NF4 quantization for the LLM2Vec text encoder
        #   safetensors   — hard import in kimodo/model/loading.py (load_file)
        #   psutil        — top-level import in kimodo/demo/memory_manager.py
        #
        #   PyGLM / SpatialTransform are also imported in bvh.py but both
        #   arrive transitively via bvhio (pyglm, spatial-transform) so they
        #   do not need to be listed here.
        _log("Installing undeclared Kimodo dependencies…")
        _run(
            [*_venv_pip(), "install",
             "bitsandbytes>=0.46.1",
             "safetensors",
             "psutil"],
            "Installing undeclared dependencies",
        )

        # 7 — Install Kimodo from Aero-Ex fork.
        #     SKIP_MOTION_CORRECTION_IN_SETUP=1 tells setup.py not to rebuild
        #     motion_correction (we already installed it in step 5).
        kimodo_url = _github_install_url("Aero-Ex", "kimodo")
        _log(f"Installing Kimodo (Aero-Ex offline fork) via {kimodo_url}…")
        kimodo_env = os.environ.copy()
        kimodo_env["SKIP_MOTION_CORRECTION_IN_SETUP"] = "1"
        _run(
            [*_venv_pip(), "install", kimodo_url],
            "Installing Kimodo",
            env=kimodo_env,
        )

        # 8 — Install the NVIDIA kimodo-viser fork.
        #     PyPI viser does not have viser._timeline_api — that submodule is
        #     exclusive to the nv-tlabs fork used by the Kimodo demo.
        #     Kimodo lists this under [project.optional-dependencies] demo = [...],
        #     so it is not pulled in by a plain pip install.
        viser_url = _github_install_url("nv-tlabs", "kimodo-viser")
        _log(f"Installing kimodo-viser fork via {viser_url}…")
        _run(
            [*_venv_pip(), "install", viser_url],
            "Installing kimodo-viser",
        )

        # 10 — Locate llm2vec_wrapper.py
        _log("Locating LLM2Vec wrapper in installed package…")
        wrapper = _find_wrapper(venv_py)
        if not wrapper:
            raise RuntimeError(
                "llm2vec_wrapper.py not found after installation. "
                "Kimodo may not have installed correctly — check the log above."
            )
        _log(f"Found wrapper: {wrapper}")

        # 11 — Download the LLM2Vec text-encoder model to a local folder.
        #     The Aero-Ex fork hosts the model at Aero-Ex/KIMODO-Meta3_llm2vec_NF4
        #     on HuggingFace.  We download it once and point the wrapper at it.
        _log(f"Downloading LLM2Vec model ({LLMVEC_MODEL_ID}) — this may take a while…")
        os.makedirs(LLMVEC_DIR, exist_ok=True)
        dl_script = (
            "from huggingface_hub import snapshot_download; "
            f"snapshot_download(repo_id={LLMVEC_MODEL_ID!r}, local_dir={LLMVEC_DIR!r})"
        )
        _run([venv_py, "-c", dl_script], "Downloading LLM2Vec model")

        # 12 — Patch wrapper for fully offline operation
        _log("Patching llm2vec_wrapper.py for offline use…")
        _patch_wrapper(wrapper, LLMVEC_DIR)
        _log("Patch applied.")

        # 13 — Download Kimodo model weights into the HF cache.
        #      load_model.py calls snapshot_download unconditionally ("will check
        #      online no matter what"), so the weights must be in the local cache
        #      before we enable HF_HUB_OFFLINE at bridge launch time.
        #      We only download the default SOMA model; the other two are
        #      unsupported in the addon UI and can be fetched later if needed.
        _log("Downloading Kimodo-SOMA-RP-v1 model weights — this may take a while…")
        dl_weights = (
            "from huggingface_hub import snapshot_download; "
            "snapshot_download(repo_id='nvidia/Kimodo-SOMA-RP-v1')"
        )
        _run([venv_py, "-c", dl_weights], "Downloading Kimodo-SOMA-RP-v1 weights")

        # 14 — Update the addon's Python path on the main thread
        def _set_path():
            try:
                for scene in bpy.data.scenes:
                    if not scene.kimodo.python_executable:
                        scene.kimodo.python_executable = venv_py
            except Exception:
                pass
        bpy.app.timers.register(_set_path, first_interval=0.1)

        # Mark the install as complete so a partial venv is never mistaken for
        # a successful one after a Blender restart.
        open(_SENTINEL, "w").close()

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

        # Remove any partial venv so we always start clean on a retry.
        # A complete install is guarded by the sentinel file; if that's absent
        # the venv is broken and safe to wipe regardless of session state.
        if venv_exists() and not is_installed():
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
            _state.update(running=True, lines=[], error="", done=False, needs_python=False)

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


class KIMODO_OT_ResetVenv(Operator):
    bl_idname      = "kimodo.reset_venv"
    bl_label       = "Reset Venv"
    bl_description = (
        "Delete ~/.kimodo-venv and allow a fresh install. "
        "Use this when a previous install failed or is stuck."
    )

    def execute(self, context):
        if is_installing():
            self.report({"WARNING"}, "Cannot reset while installation is in progress.")
            return {"CANCELLED"}
        if not venv_exists():
            self.report({"INFO"}, "No venv found — nothing to reset.")
            return {"CANCELLED"}
        try:
            shutil.rmtree(MANAGED_VENV)
        except Exception as exc:
            self.report({"ERROR"}, f"Could not remove venv: {exc}")
            return {"CANCELLED"}
        with _lock:
            _state.update(running=False, lines=[], error="", done=False)
        self.report({"INFO"}, f"Removed {MANAGED_VENV} — ready for a fresh install.")
        return {"FINISHED"}


class KIMODO_OT_OpenPythonDownload(Operator):
    bl_idname      = "kimodo.open_python_download"
    bl_label       = "Download Python 3.12"
    bl_description = "Open python.org/downloads in your browser"

    def execute(self, context):
        import webbrowser
        webbrowser.open("https://www.python.org/downloads/")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = [
    KIMODO_OT_InstallKimodo,
    KIMODO_OT_UseInstalledKimodo,
    KIMODO_OT_ResetVenv,
    KIMODO_OT_OpenPythonDownload,
]


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
