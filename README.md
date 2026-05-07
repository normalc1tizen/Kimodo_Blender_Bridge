

A Blender addon that generates AI-driven human motion via [NVIDIA Kimodo](https://github.com/nv-tlabs/kimodo) and imports it directly into your scene — no copy-pasting, no manual BVH wrangling.

Tested on **Blender 5.1** · **Arch Linux**

<img width="1237" height="1257" alt="image" src="https://github.com/user-attachments/assets/71a1666d-a460-40eb-af23-1dbb8ab750cb" />


---

## How it works

Blender's embedded Python cannot load PyTorch or Kimodo directly. The addon solves this with a two-process bridge:

```
Blender (addon)                Kimodo venv
  subprocess_client.py  ─────▶  bridge_server.py
                        ◀─────  (model loaded once, handles requests)
```

The bridge server loads the Kimodo model once at startup and then responds to generation requests over stdin/stdout. Blender stays responsive while generation runs in a background thread.

---

## Requirements

| Requirement | Notes |
|---|---|
| Blender 5.1+ | Tested on 5.1, Arch Linux. Should work on 4.x too. |
| Python 3.10+ | Provided by the Kimodo venv — not Blender's Python |
| NVIDIA GPU | 8 GB+ VRAM recommended; 16 GB+ for best results |
| CUDA | Must match your PyTorch build |
| Kimodo | Installed in a dedicated venv (see below) |

> **Low VRAM?** Run `kimodo_textencoder --device cpu` in a separate terminal before starting the bridge. This offloads the text encoder to CPU and frees several GB of VRAM.

---

## Installation

### 1 — Install Kimodo

Follow the [official Kimodo instructions](https://github.com/nv-tlabs/kimodo). The short version:

Note: Installe it via a python virtual environment and not via docker or other, as this has not been tested.

```bash
# Create a dedicated venv (conda or plain venv both work)
python -m venv ~/kimodo-env
source ~/kimodo-env/bin/activate

# Install Kimodo and its dependencies
pip install -e /path/to/kimodo-src
```

After installation, note the full path to the venv's Python binary — you will need it in step 3:

```bash
which python   # e.g. /home/you/kimodo-env/bin/python
```

### 2 — Install the Blender addon

1. Download or clone this repository. (Top right "Code" -> "Download ZIP")
2. Open Blender → **Edit → Preferences → Add-ons → Install from Disk…**
3. Select the `Kimodo_Blender_Bridge` folder (zip it first if Blender asks for a `.zip`).
4. Enable **"Kimodo Motion Generator"** in the add-on list.

### 3 — Point the addon at your Kimodo Python

In the **Kimodo** tab of the **N-Panel** (press `N` in the 3D Viewport):

1. Expand **Connection**.
2. Paste the path to your venv Python into the **Kimodo Python** field, e.g.:
   ```
   /home/you/kimodo-env/bin/python
   ```
   Leave it blank to let the addon auto-detect a `kimodo` executable on your `PATH`.

---

## Quick Start

### Generate motion from a text prompt

1. **Start** the bridge: click **Start Kimodo** in the Connection panel.  
   The status line will show *Loading model…* then *Ready* once the model is loaded (this takes 10–60 s the first time).

2. Open the **Motion Segments** panel.

3. Click **Add** to create a segment, type a prompt (e.g. `a person jogs in a circle`), and set the frame range.

4. Click **Generate Selected** (one segment) or **Generate All** (all enabled segments in one model call with smooth transitions).

5. A `Kimodo_Source` armature will appear in your scene with the generated motion applied.

> **30 FPS tip:** Kimodo always generates at 30 FPS. If your scene is set to a different frame rate, an alert will appear above the generate buttons with a **Set to 30 FPS** button.

### Use multiple segments

Each segment is an independent text prompt mapped to a frame range. **Generate All** sends every enabled segment to Kimodo in a single model call, producing one continuous animation with smooth transitions between prompts.

- Segments are listed in order. The **Start** frame of each segment after the first is automatically locked to the **End** frame of the previous segment — just drag the End frame and the next segment's Start updates automatically.
- Use **Duplicate** to copy a segment and place it immediately after.
- Use **↑ / ↓** to reorder segments.

<img width="2676" height="1181" alt="image" src="https://github.com/user-attachments/assets/a5d336e9-f32f-44c7-9aca-a09983e869d6" />


### Retarget to your own rig

After generating motion you can drive any armature from the Kimodo source:

1. Open the **Retarget** panel.
2. Set **Source** to `Kimodo_Source` and **Target** to your character rig.
3. Click **Auto-Match Bones** — the addon fuzzy-matches Kimodo bone names against your rig.
4. Review the mapping, enable/disable pairs, choose a retarget mode per bone. Its recommended to also adjust the scale of the armature to match your character and then applying it with CTRL+A.
5. Choose the type of constraint the plugin should use, "Child of", "Copy Rotation" etc...
6. Click **Apply Constraints** — Blender constraint drivers are added to your rig.
7. Click **Bake & Remove Constraints** when you are happy — keyframes are baked onto your rig and all Kimodo constraints are removed, leaving a clean, self-contained animation.

Use **Save / Load Preset** to store bone mappings for a rig and reuse them later.

<img width="1233" height="839" alt="image" src="https://github.com/user-attachments/assets/d76290db-7662-4223-9cd6-7083f89b35ca" />

### Motion constraints

Spatial goals can be given to Kimodo so the generated motion passes through specific positions:

| Constraint | What it controls |
|---|---|
| Root XZ | Where the character's root lands on the ground plane |
| Full-Body | A full joint-pose keyframe (pose a reference armature) |
| Left / Right Hand | Wrist end-effector position |
| Left / Right Foot | Foot / heel end-effector position |

To add a constraint:
1. Move the 3D cursor (or select an armature) to the desired position.
2. Set the timeline to the target frame.
3. In the **Motion Constraints** panel, click the constraint type.

**Auto-Origin** (off by default) shifts all constraint positions so the earliest root waypoint lands at Kimodo's world origin — author constraints anywhere in your scene without worrying about absolute coordinates.

---

## Panel reference

| Panel | What's in it |
|---|---|
| **Connection** | Kimodo Python path, model selector, Start / Stop bridge |
| **Motion Segments** | Prompt list, frame ranges, Generate Selected / Generate All |
| **Quick Generate** | Single-prompt generation with duration and seed controls |
| **Motion Constraints** | Spatial waypoints for the generated motion |
| **Retarget** | Bone mapping, Apply Constraints, Bake |
| **Help** | Quick-start checklist, VRAM tip |

---

## Troubleshooting

**Bridge won't start / "Failed to start"**
- Check that the Python path points to the venv Python that has Kimodo installed.
- Run the path manually in a terminal: `<python> bridge_server.py` — any import errors will print there.

**CUDA out of memory**
- Use a shorter duration or fewer segments.

**Retargeted rig is in the wrong pose**
- Try a different retarget mode per bone (Copy Rotation vs Copy Transforms vs Child Of).
- Make sure the source and target armatures are both in their rest pose / have the same pose before trying the retargeting, and have scale applied on the armature.

**Frames from imorted animation dont match**
- Kimodo generates at exactly 30 FPS. Use the **Set to 30 FPS** button that appears in the Motion Segments panel when your scene is at a different frame rate.

---

## File overview

| File | Role |
|---|---|
| `__init__.py` | Blender addon entry point |
| `bridge_server.py` | Subprocess: loads Kimodo, handles generation requests |
| `subprocess_client.py` | Blender-side bridge manager |
| `operators.py` | All `bpy.ops.kimodo.*` operators |
| `properties.py` | All `bpy.props` scene settings |
| `panels.py` | N-panel UI |
| `constraints.py` | Converts Blender constraint markers to Kimodo JSON |
| `retarget.py` | Applies / bakes retargeting constraints |
| `ui_list.py` | UIList helper for the bone mapping panel |

---

## License

See [LICENSE](LICENSE).
