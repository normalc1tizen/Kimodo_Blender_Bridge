# Changelog

## [1.3.0] — 2026-05-12

### Windows improvements

- **Python detection**: Skip Windows App Execution Alias stubs (`%LOCALAPPDATA%\Microsoft\WindowsApps`) that point to the Store instead of a real interpreter, preventing broken venv creation.
- **Python install prompt**: When no Python 3.10+ is found, the Connection panel shows a prominent download button linking directly to the Python 3.12 Windows installer, with instructions to tick "Add Python to PATH" and run as Administrator.
- **Python 3.13 support**: Python 3.13 is now accepted alongside 3.10–3.12.
- **Architecture-aware installer link**: The download button serves the `arm64` installer on ARM machines and `amd64` everywhere else.
- **Git-less install fallback**: When `git` is not on `PATH`, Kimodo and kimodo-viser are installed via GitHub's zip-archive endpoint instead of `git+https://`, so the installer works on stock Windows without Git.
- **Partial/broken venv recovery**: A sentinel file (`.kimodo_install_complete`) is written only on successful install. On the next session, any venv missing the sentinel is treated as broken and wiped on retry — no more silent failures after a mid-install crash.
- **NVIDIA GPU gate**: The install button is disabled and a clear error is shown when no NVIDIA GPU is detected (`nvidia-smi` not found or returns no GPUs). AMD/Intel users see an explicit "not supported" message instead of a cryptic CUDA error later.
- **GPU check on Start**: Clicking *Start Kimodo* also checks for a CUDA-capable GPU and fails fast with a readable error if none is found.
- **Blender restart reminder**: After the Python installer download prompt, the panel reminds users to restart Blender before clicking *Retry Install*.

---

## [1.2.0] — 2026-05-11

### Added

- **One-click auto-installer** (`setup_operator.py`): A new *Install Kimodo (Auto)* button in the Connection panel handles the full setup without any terminal work:
  - Creates a managed Python venv at `~/.kimodo-venv/`
  - Installs PyTorch (CUDA 12.1) and the [Aero-Ex offline fork](https://github.com/Aero-Ex/kimodo) of Kimodo, including a pre-built `motion_correction` wheel (no MSVC / CMake required on Windows)
  - Installs the [NVIDIA kimodo-viser fork](https://github.com/nv-tlabs/kimodo-viser) which provides `viser._timeline_api` (not available in PyPI viser)
  - Installs all undeclared Kimodo dependencies discovered by source audit: `bitsandbytes`, `safetensors`, `psutil`
  - Downloads the `Aero-Ex/KIMODO-Meta3_llm2vec_NF4` LLM2Vec text-encoder model locally and patches `llm2vec_wrapper.py` so it loads from disk
  - Downloads `nvidia/Kimodo-SOMA-RP-v1` model weights into the HF cache
  - Auto-fills the Python path field on completion
  - Shows live progress in the Connection panel; full log printed to the system console with `[Kimodo Install]` prefix
  - Failed installs show a *Retry Install* button that wipes the partial venv and starts clean

- **Offline operation**: After the initial install, Kimodo runs with no internet access. The bridge subprocess is launched with `TRANSFORMERS_OFFLINE=1` and `HF_DATASETS_OFFLINE=1` when the managed venv is detected.

- **Bridge console logging**: All output from `bridge_server.py` (PyTorch errors, loading progress, model ready) is now streamed to the system console with a `[Kimodo Bridge]` prefix, making startup failures easy to diagnose.

- **Use Installed Kimodo** button: If the managed venv exists but the Python path is not set, a one-click button sets it automatically.

### Changed

- Connection panel now shows a contextual install section at the top: install prompt → live progress → completion/error state, depending on installer state.
- Failed bridge startup now reports the process exit code and directs the user to the console instead of showing a truncated stderr snippet.

---

## [1.1.0]

- Multi-segment generation: **Generate All** sends all enabled segments in a single model call with smooth transitions.
- Segment frame ranges auto-link (end of segment N locks to start of segment N+1).
- Duplicate / reorder segment operators.
- Seed control per segment.

## [1.0.0]

- Initial release.
- Subprocess bridge architecture (Blender ↔ bridge_server.py over stdin/stdout JSON).
- BVH import into `Kimodo_Source` armature.
- Constraint-based retargeting with bake.
- Bone mapping presets (save/load).
- Motion constraints: Root XZ, Hand, Foot waypoints.
