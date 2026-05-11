# Changelog

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
