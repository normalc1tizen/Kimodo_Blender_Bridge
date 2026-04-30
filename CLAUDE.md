# Kimodo Blender Bridge — Agent Guide

Blender addon (Python package) that generates AI-driven human motion via NVIDIA Kimodo and imports it directly into Blender. The addon lives entirely in this directory and is installed as a standard Blender addon.

---

## Architecture: Two-Process Bridge

Blender runs its own Python interpreter that cannot load PyTorch or Kimodo directly. The addon solves this with a two-process pattern:

```
Blender (addon Python)          Kimodo venv Python
  subprocess_client.py   ──stdin──▶  bridge_server.py
                         ◀─stdout──  (model loaded once, handles requests)
```

- **`bridge_server.py`** — standalone script launched as a subprocess. Loads the Kimodo model once at startup, then handles newline-delimited JSON requests in a loop. Writes newline-delimited JSON responses to stdout.
- **`subprocess_client.py`** — runs inside Blender. Manages the `bridge_server.py` subprocess. Exposes `generate_motion()` and `generate_motion_multi()` as blocking calls (always called from background threads).
- Communication is text-based NDJSON over stdin/stdout. Each message is one JSON line terminated by `\n`.

### Protocol

**Requests (Blender → bridge_server):**
| `cmd` | Purpose |
|---|---|
| `ping` | Health check |
| `generate` | Single-prompt generation |
| `generate_multi` | Multi-prompt generation (all prompts in one model call) |
| `quit` | Shut down bridge |

**Responses (bridge_server → Blender):**
| `status` | Meaning |
|---|---|
| `loading` | Model initialising |
| `ready` | Model ready (includes `model`, `device`, `fps`) |
| `progress` | Informational string during generation |
| `done` | Generation complete; `path` field has the output file |
| `error` | Something failed; `message` field has details |
| `pong` | Reply to ping |

---

## File Map

| File | Role |
|---|---|
| `__init__.py` | Blender addon entry point; registers/unregisters all classes |
| `bridge_server.py` | Subprocess that loads Kimodo and handles generation requests |
| `subprocess_client.py` | Blender-side subprocess manager; `generate_motion()`, `generate_motion_multi()` |
| `operators.py` | All `bpy.ops.kimodo.*` operators (UI actions, generation, retargeting, constraints) |
| `properties.py` | All `bpy.props` definitions; scene settings at `context.scene.kimodo` |
| `panels.py` | N-panel UI in View3D → Sidebar → Kimodo tab |
| `constraints.py` | Converts Blender constraint markers to Kimodo constraint JSON |
| `retarget.py` | Applies/bakes retargeting constraints from Kimodo source → user's rig |
| `timeline.py` | GPU-drawn segment bars in the Blender timeline |
| `ui_list.py` | UIList helper for the constraint list panel |
| `gradio_client.py` | Legacy REST client for Gradio-based Kimodo demo (not used in the subprocess approach) |

---

## Key Data Structures

All scene settings live at **`context.scene.kimodo`** (type: `KIMODO_SceneSettings`).

### `KIMODO_MotionSegment` (`s.motion_segments`)
One text prompt mapped to a frame range bar in the timeline.

```python
seg.prompt          # str  — text description of the motion
seg.start_frame     # int  — first Blender frame of this segment
seg.end_frame       # int  — last Blender frame (inclusive)
seg.seed            # int  — random seed (-1 = random)
seg.enabled         # bool — included in "Generate All"
seg.generated       # bool — True after a successful generation
seg.last_bvh_path   # str  — path to the output file from last generation
```

### `KIMODO_ConstraintItem` (`s.motion_constraints`)
A spatial goal for Kimodo: a Blender object (Empty or Armature) at a specific frame.

```python
ci.constraint_type  # 'root2d' | 'fullbody' | 'left_hand' | 'right_hand' | 'left_foot' | 'right_foot'
ci.frame            # Blender frame where the constraint applies
ci.marker_object    # bpy.types.Object — the spatial reference in the viewport
ci.enabled          # bool
ci.include_heading  # bool (root2d only) — also constrain facing direction
```

### `KIMODO_BoneMappingItem` (`s.bone_mappings`)
Maps one bone on the Kimodo source armature to one bone on the user's rig.

```python
bm.source_bone    # str — bone name on the Kimodo-generated armature
bm.target_bone    # str — bone name on the user's rig
bm.enabled        # bool
bm.retarget_mode  # 'COPY_ROTATION' | 'COPY_TRANSFORMS' | 'CHILD_OF'
```

### Other important scene properties

```python
s.python_executable      # path to Kimodo venv Python (auto-detected if empty)
s.kimodo_model           # 'Kimodo-SOMA-RP-v1' | 'Kimodo-SMPLX-RP-v1' | 'Kimodo-G1-RP-v1'
s.is_connected           # bool — bridge process is running and ready
s.is_generating          # bool — a generation is in progress (modal operator running)
s.generation_progress    # str  — shown in the UI during generation
s.source_armature        # the Kimodo-generated armature (imported BVH)
s.target_armature        # the user's character rig
s.reuse_source_armature  # bool — transfer action to existing Kimodo_Source instead of creating new
s.kimodo_fps             # float — Kimodo's generation FPS (default 30); used for frame index conversion
s.auto_canonicalize      # bool — offset constraints so earliest waypoint lands at Kimodo origin (0,0)
s.output_format          # 'bvh' | 'npz'
s.bvh_standard_tpose     # bool — use standard T-pose rest pose in BVH export
```

---

## Multi-Prompt Generation Flow

"Generate All" sends **all enabled segments to Kimodo in a single model call**, producing one continuous BVH with smooth transitions between prompts.

```
GenerateAllSegments.invoke()
  ├─ _enforce_segment_continuity()   # auto-fix any gaps between segments
  ├─ _build_multi_prompt_constraints() # build constraint JSON for the full sequence
  └─ background thread: _run_all()
       └─ sc.generate_motion_multi(prompts=[…], durations=[…], constraints_json=…)
            └─ sends {"cmd": "generate_multi", "prompts": […], "durations": […], …}
                 └─ bridge_server._generate_multi()
                      └─ model([text1, text2, …], [frames1, frames2, …],
                               multi_prompt=True, num_transition_frames=5, …)
                           └─ returns one combined BVH
modal() on TIMER:
  └─ import_bvh_at_frame(filepath=…, start_frame=first_segment.start_frame)
       # The single BVH is placed at the first segment's start_frame;
       # Kimodo handles all transitions internally.
```

"Generate Selected" (single segment) still calls `sc.generate_motion()` with one prompt.

### Segment Continuity
`_enforce_segment_continuity(ordered_segs)` ensures segments are contiguous before generation. It sets each segment's `start_frame = previous.end_frame + 1` while preserving each clip's frame duration. Called automatically at the start of "Generate All".

---

## Constraint System

Constraints are authored in the Blender viewport as Empty objects (or Armature poses for `fullbody`). `build_constraints_json()` in `constraints.py` converts them to Kimodo's JSON format.

### Frame Conversion
```python
# Blender frame → 0-based Kimodo frame index
kimodo_frame = round((blender_frame - scene_start) / blender_fps * kimodo_fps)
```

The `scene_start_override` parameter overrides `scene.frame_start`. For multi-prompt generation, pass `scene_start_override=first_segment.start_frame` so frame indices are relative to the start of the generated sequence (Kimodo frame 0 = first segment's start_frame).

### Constraint Types → Kimodo API
| Blender type | Kimodo JSON `type` | What it constrains |
|---|---|---|
| `root2d` | `root2d` | Ground-plane (XZ) position of the character root |
| `fullbody` | `fullbody` | Full joint pose keyframe (all 30 SOMA joints) |
| `left_hand` | `left-hand` | Left wrist end-effector |
| `right_hand` | `right-hand` | Right wrist end-effector |
| `left_foot` | `left-foot` | Left foot/heel end-effector |
| `right_foot` | `right-foot` | Right foot/heel end-effector |

Auto-canonicalization (default on) shifts all XZ positions so the earliest root waypoint lands at Kimodo's origin (0, 0). This lets users author constraints anywhere in the Blender scene.

---

## Coordinate Spaces

| Space | Forward | Up | Notes |
|---|---|---|---|
| Blender | Y | Z | Standard Blender world space |
| Kimodo | Z | Y | Right-hand Y-up |
| BVH import | `-Z` forward, `Y` up | | Set via `axis_forward='-Z', axis_up='Y'` in `import_anim.bvh` |

BVH files from Kimodo are in centimetres; Blender expects metres. Import uses `global_scale=0.01`.

---

## Retargeting

After generating a BVH, the user can drive their own rig from the Kimodo source armature using Blender constraints. The workflow:

1. **Auto-Map** (`kimodo.auto_map_bones`) — fuzzy-matches Kimodo bone names against target rig bone names.
2. **Edit mapping** — enable/disable pairs, choose retarget mode per bone.
3. **Apply Retarget** (`kimodo.apply_retarget`) — adds `COPY_ROTATION` / `COPY_TRANSFORMS` / `CHILD_OF` constraints to each target bone, driving it from the corresponding source bone.
4. **Bake** (`kimodo.bake_retarget`) — bakes visual keyframes for the frame range, then removes all Kimodo constraints so the rig is self-contained.

Constraints added by the addon are prefixed with `KIMODO_` for reliable identification and cleanup.

---

## Operators Reference

Key operators in `operators.py`:

| `bl_idname` | What it does |
|---|---|
| `kimodo.start_kimodo` | Launches `bridge_server.py` subprocess; modal until ready |
| `kimodo.stop_kimodo` | Sends `quit` and terminates the subprocess |
| `kimodo.generate_quick` | Single-prompt generation (Quick Generate panel) |
| `kimodo.generate_segment` | Single-segment generation (Generate Selected) |
| `kimodo.generate_all_segments` | Multi-prompt generation (all enabled segments in one call) |
| `kimodo.import_bvh_at_frame` | Imports a BVH and shifts all keyframes to `start_frame` |
| `kimodo.add_segment` / `remove_segment` | Manage the segment list |
| `kimodo.add_constraint` | Add a constraint marker at the 3D cursor / current frame |
| `kimodo.auto_map_bones` | Auto-match Kimodo → target bone names |
| `kimodo.apply_retarget` | Apply Blender constraints for retargeting |
| `kimodo.bake_retarget` | Bake visual keyframes and remove constraints |

### Shared Async State
Modal operators communicate with background threads via the module-level dict in `operators.py`:

```python
_generation_state = {
    "running": bool,
    "done":    bool,
    "success": bool,
    "result":  str,   # file path on success, error message on failure
    "progress": str,  # shown in the UI
}
```

The modal operator polls this dict every 0.5 s on a `TIMER` event.

---

## BVH Import Details (`kimodo.import_bvh_at_frame`)

```python
bpy.ops.import_anim.bvh(
    filepath=…,
    axis_forward='-Z', axis_up='Y',  # Kimodo → Blender axis remap
    target='ARMATURE',
    global_scale=0.01,               # cm → m
    frame_start=start_frame,         # shifts all keyframes to this Blender frame
    use_fps_scale=False,
    update_scene_fps=False,
    update_scene_duration=False,
    use_cyclic=False,
    rotate_mode='NATIVE',
)
```

After import, `_apply_to_existing_source()` checks `s.reuse_source_armature`. If enabled and a `Kimodo_Source` armature already exists, it transfers the action from the new armature to the existing one and deletes the new armature. This preserves retargeting constraints that already point at `Kimodo_Source`.

---

## Adding a New Feature — Checklist

1. **New data** → add a property to `KIMODO_SceneSettings` (or a sub-`PropertyGroup`) in `properties.py`.
2. **New action** → add an `Operator` class in `operators.py`; register it in `operators._classes`.
3. **New UI** → add to an existing panel in `panels.py`, or create a new `Panel` subclass.
4. **New bridge command** → add a handler function in `bridge_server.py` and wire it in `main()`'s request loop; add a client function in `subprocess_client.py`.
5. **New constraint type** → extend the `constraint_type` enum in `properties.py` and handle it in `constraints.py`'s `build_constraints_json()`.

---

## Common Pitfalls

- **Never call `sc.generate_motion*()` from the main Blender thread.** These are blocking calls; always run them in a `threading.Thread` and poll results via a modal operator with a `TIMER`.
- **`_generation_state` is shared and reset by `_reset_state()`.** Only one generation can run at a time — always check `s.is_generating` before starting.
- **BVH frame_start shifts all keyframes.** The BVH content always starts at time 0; `frame_start` is a Blender import parameter that offsets the whole clip.
- **Constraints use `scene_start_override`** for multi-prompt generation. If you add a new code path that imports a BVH at a non-`scene.frame_start` frame, pass that frame as `scene_start_override` to `build_constraints_json()`.
- **`_enforce_segment_continuity()` mutates the actual PropertyGroup values** (visible in the UI). Call it before reading `start_frame`/`end_frame` in the generation path.
- **Axis mapping is critical.** Kimodo's Z-forward, Y-up space maps to Blender's -Z forward via the BVH import flags. Getting this wrong produces sideways or inverted animations.
