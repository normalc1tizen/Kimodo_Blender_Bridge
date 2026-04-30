"""
Kimodo Blender Bridge — Constraints
====================================
Translates Blender scene objects (Empties, posed Armatures) into the
Kimodo constraints JSON format that gets sent alongside the text prompt.

Coordinate space conversion
----------------------------
Blender world space (Z-up)  →  Kimodo motion space (Y-up)
  X  (right)    →  X  (right)     same
  Y  (forward)  →  -Z (forward)   Y → -Z  (sign flip — see BVH import axis_forward='-Z')
  Z  (up)       →  Y  (up)        Z →  Y

The sign flip on Y is required because the BVH importer maps Kimodo +Z
back to Blender -Y (axis_forward='-Z' in operators.py).  Negating the
Blender-Y → Kimodo-Z conversion here keeps constraint positions consistent
with the imported animation.

All positions in meters (Blender scenes should use metric units).

Canonicalization
-----------------
Kimodo expects the root to start near (0,0) in XZ.  We auto-offset all
constraint positions by subtracting the XZ of the *earliest root2d* or
*earliest fullbody root* so the user can author constraints anywhere in
the scene.  If no frame-0 constraint exists the model still works — it
just starts from its learned distribution.

Frame mapping
-------------
  kimodo_frame = round((blender_frame - scene.frame_start) * kimodo_fps / blender_fps)
The Kimodo FPS defaults to 30; override it in the constraint settings.
"""

import bpy
import math
import json
import mathutils
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Bone names for SOMA/somaskel30, in Kimodo's expected order.
# Used when building fullbody local_joints_rot arrays.
SOMA_JOINT_ORDER = [
    "Hips", "Spine", "Spine1", "Spine2", "Neck", "Head",
    "LeftShoulder", "LeftArm", "LeftForeArm", "LeftHand",
    "RightShoulder", "RightArm", "RightForeArm", "RightHand",
    "LeftUpLeg", "LeftLeg", "LeftFoot", "LeftToeBase",
    "RightUpLeg", "RightLeg", "RightFoot", "RightToeBase",
    "LeftHandThumb1", "LeftHandIndex1", "LeftHandMiddle1",
    "RightHandThumb1", "RightHandIndex1", "RightHandMiddle1",
    # somaskel30 ends here (28 joints common, 2 optional)
    "LeftHandRing1", "RightHandRing1",
]

# End-effector joint name mapping: Blender-friendly → Kimodo joint_name
EFFECTOR_JOINT_NAMES = {
    "left_hand":  "left_hand",
    "right_hand": "right_hand",
    "left_foot":  "left_foot",
    "right_foot": "right_foot",
    "left_wrist": "left_hand",
    "right_wrist":"right_hand",
    "left_ankle": "left_foot",
    "right_ankle":"right_foot",
}


# ---------------------------------------------------------------------------
# Coordinate conversion helpers
# ---------------------------------------------------------------------------

def blender_to_kimodo_pos(v: mathutils.Vector) -> list[float]:
    """Convert Blender world position (Z-up) to Kimodo Y-up position [x, y, z]."""
    return [v.x, v.z, -v.y]   # X same; Z→Y (up); -Y→Z (sign flip for forward axis)


def blender_to_kimodo_2d(v: mathutils.Vector) -> list[float]:
    """Extract 2D ground-plane position [x, z_kimodo] from Blender world pos."""
    return [v.x, -v.y]   # Blender X→Kimodo X; Blender -Y→Kimodo Z (sign flip)


def quat_to_axis_angle_vec(q: mathutils.Quaternion) -> list[float]:
    """Convert Blender Quaternion to axis-angle 3-vector [ax, ay, az] * angle."""
    q = q.normalized()
    angle = 2.0 * math.acos(max(-1.0, min(1.0, q.w)))
    s = math.sqrt(max(0.0, 1.0 - q.w * q.w))
    if s < 1e-6:
        return [0.0, 0.0, 0.0]
    # Convert axis from Blender to Kimodo space too
    ax = q.x / s
    ay = q.z / s    # Blender Z → Kimodo Y
    az = -q.y / s   # Blender -Y → Kimodo Z (sign flip matches position conversion)
    return [ax * angle, ay * angle, az * angle]


def euler_to_axis_angle_vec(e: mathutils.Euler) -> list[float]:
    """Convert Blender Euler to axis-angle 3-vector."""
    return quat_to_axis_angle_vec(e.to_quaternion())


def heading_from_angle(angle_rad: float) -> list[float]:
    """Kimodo expects heading as [cos(θ), sin(θ)]."""
    return [math.cos(angle_rad), math.sin(angle_rad)]


# ---------------------------------------------------------------------------
# Frame conversion
# ---------------------------------------------------------------------------

def blender_frame_to_kimodo(
    blender_frame: int,
    scene_start: int,
    blender_fps: float,
    kimodo_fps: float = 30.0,
) -> int:
    """Convert a Blender frame number to a 0-based Kimodo frame index."""
    elapsed_sec = (blender_frame - scene_start) / blender_fps
    return max(0, round(elapsed_sec * kimodo_fps))


# ---------------------------------------------------------------------------
# Pose reading helpers
# ---------------------------------------------------------------------------

def get_bone_axis_angle(pose_bone: bpy.types.PoseBone) -> list[float]:
    """Read a pose bone's LOCAL rotation in axis-angle (Kimodo-space)."""
    mode = pose_bone.rotation_mode
    if mode == 'QUATERNION':
        q = pose_bone.rotation_quaternion
    elif mode == 'AXIS_ANGLE':
        angle = pose_bone.rotation_axis_angle[0]
        axis = mathutils.Vector(pose_bone.rotation_axis_angle[1:4])
        q = mathutils.Quaternion(axis, angle)
    else:
        # Euler
        q = pose_bone.rotation_euler.to_quaternion()
    return quat_to_axis_angle_vec(q)


def get_armature_joint_rots(
    armature_obj: bpy.types.Object,
    joint_order: list[str],
) -> list[list[float]]:
    """
    Read all joint rotations from an armature in the given bone order.
    Returns [[ax,ay,az]*angle, ...] for each bone (axis-angle 3-vector).
    Missing bones get [0,0,0] (identity).
    """
    pose_bones = armature_obj.pose.bones
    result = []
    for name in joint_order:
        pb = pose_bones.get(name)
        if pb:
            result.append(get_bone_axis_angle(pb))
        else:
            result.append([0.0, 0.0, 0.0])
    return result


def get_root_position(armature_obj: bpy.types.Object) -> list[float]:
    """Get hip/root position from armature's world transform → Kimodo [x,y,z]."""
    loc = armature_obj.location
    return blender_to_kimodo_pos(loc)


def get_bone_world_position(
    armature_obj: bpy.types.Object,
    bone_name: str,
) -> list[float] | None:
    """World position of a specific pose bone → Kimodo [x,y,z]."""
    pb = armature_obj.pose.bones.get(bone_name)
    if not pb:
        return None
    # head world position
    world_pos = armature_obj.matrix_world @ pb.head
    return blender_to_kimodo_pos(world_pos)


def get_bone_world_rotation(
    armature_obj: bpy.types.Object,
    bone_name: str,
) -> list[float]:
    """World rotation of a specific pose bone → axis-angle 3-vector (Kimodo space)."""
    pb = armature_obj.pose.bones.get(bone_name)
    if not pb:
        return [0.0, 0.0, 0.0]
    world_mat = armature_obj.matrix_world @ pb.matrix
    q = world_mat.to_quaternion()
    return quat_to_axis_angle_vec(q)


# ---------------------------------------------------------------------------
# Constraint JSON builder
# ---------------------------------------------------------------------------

def build_constraints_json(
    constraint_items,           # iterable of KIMODO_ConstraintItem
    scene: bpy.types.Scene,
    kimodo_fps: float = 30.0,
    auto_canonicalize: bool = True,
    scene_start_override: "int | None" = None,
) -> list[dict]:
    """
    Convert Blender constraint items to Kimodo constraints JSON list.

    Parameters
    ----------
    constraint_items : iterable of KIMODO_ConstraintItem PropertyGroup entries
    scene            : bpy.context.scene
    kimodo_fps       : Kimodo's motion FPS (default 30)
    auto_canonicalize: subtract XZ of earliest root waypoint so it lands at (0,0)

    Returns
    -------
    list of dicts ready to be json.dumps()-ed and sent to Kimodo
    """
    blender_fps = scene.render.fps / scene.render.fps_base
    scene_start = scene.frame_start if scene_start_override is None else scene_start_override

    # Collect enabled items sorted by frame
    items = sorted(
        [ci for ci in constraint_items if ci.enabled and ci.marker_object],
        key=lambda ci: ci.frame,
    )
    if not items:
        return []

    # -----------------------------------------------------------------------
    # Auto-canonicalization: find the earliest XZ root position and use it
    # as the origin offset so the user can author constraints anywhere.
    # -----------------------------------------------------------------------
    origin_offset = [0.0, 0.0]  # [x_offset, z_offset] in Kimodo space
    if auto_canonicalize:
        for ci in items:
            ctype = ci.constraint_type
            if ctype in ('root2d',):
                pos2d = blender_to_kimodo_2d(ci.marker_object.location)
                origin_offset = pos2d
                break
            elif ctype == 'fullbody':
                pos3d = get_root_position(ci.marker_object)
                origin_offset = [pos3d[0], pos3d[2]]
                break

    def apply_offset_2d(pos2d):
        return [pos2d[0] - origin_offset[0], pos2d[1] - origin_offset[1]]

    def apply_offset_3d(pos3d):
        return [pos3d[0] - origin_offset[0], pos3d[1], pos3d[2] - origin_offset[1]]

    # -----------------------------------------------------------------------
    # Group items by type so we can emit separate constraint blocks per type
    # -----------------------------------------------------------------------
    out_constraints = []

    # Group consecutive same-type constraints into one block
    # (Kimodo allows one block per type with multiple frame_indices)
    from itertools import groupby
    for ctype, group in groupby(items, key=lambda ci: ci.constraint_type):
        group = list(group)
        frame_indices = []
        smooth_root_2d = []
        root_positions = []
        local_joints_rot = []
        global_root_heading = []
        joint_names_set = set()

        for ci in group:
            kframe = blender_frame_to_kimodo(ci.frame, scene_start, blender_fps, kimodo_fps)
            frame_indices.append(kframe)
            obj = ci.marker_object

            if ctype == 'root2d':
                pos2d = blender_to_kimodo_2d(obj.location)
                smooth_root_2d.append(apply_offset_2d(pos2d))
                if ci.include_heading:
                    global_root_heading.append(heading_from_angle(ci.heading_angle))

            elif ctype == 'fullbody':
                if obj.type == 'ARMATURE':
                    pos3d = get_root_position(obj)
                    root_positions.append(apply_offset_3d(pos3d))
                    # Evaluate pose at this frame
                    _evaluate_frame(scene, ci.frame)
                    jrot = get_armature_joint_rots(obj, SOMA_JOINT_ORDER)
                    local_joints_rot.append(jrot)
                    pos2d = [pos3d[0], pos3d[2]]
                    smooth_root_2d.append(apply_offset_2d(pos2d))
                else:
                    # Empty used for fullbody — just root position
                    pos3d = blender_to_kimodo_pos(obj.location)
                    root_positions.append(apply_offset_3d(pos3d))
                    pos2d = [pos3d[0], pos3d[2]]
                    smooth_root_2d.append(apply_offset_2d(pos2d))
                    local_joints_rot.append([[0.0, 0.0, 0.0]] * len(SOMA_JOINT_ORDER))

            elif ctype in ('left_hand', 'right_hand', 'left_foot', 'right_foot'):
                # End-effector: read from an Empty or armature
                if obj.type == 'ARMATURE':
                    bone_hint = {
                        'left_hand': 'LeftHand', 'right_hand': 'RightHand',
                        'left_foot': 'LeftFoot', 'right_foot': 'RightFoot',
                    }[ctype]
                    pos3d_raw = get_bone_world_position(obj, bone_hint) or blender_to_kimodo_pos(obj.location)
                    rot_aa = get_bone_world_rotation(obj, bone_hint)
                else:
                    pos3d_raw = blender_to_kimodo_pos(obj.location)
                    rot_aa = [0.0, 0.0, 0.0]

                pos3d = apply_offset_3d(pos3d_raw)
                root_positions.append(pos3d)
                smooth_root_2d.append([pos3d[0], pos3d[2]])
                # local_joints_rot: only the effector bone matters; rest identity
                jrot = [[0.0, 0.0, 0.0]] * len(SOMA_JOINT_ORDER)
                bone_idx = {
                    'left_hand': 9, 'right_hand': 13,
                    'left_foot': 17, 'right_foot': 21,
                }.get(ctype, 0)
                if bone_idx < len(jrot):
                    jrot[bone_idx] = rot_aa
                local_joints_rot.append(jrot)

        # Build the constraint dict
        block: dict[str, Any] = {
            "type": ctype.replace("_", "-"),  # left_hand → left-hand
            "frame_indices": frame_indices,
        }

        if smooth_root_2d:
            block["smooth_root_2d"] = smooth_root_2d
        if root_positions:
            block["root_positions"] = root_positions
        if local_joints_rot:
            block["local_joints_rot"] = local_joints_rot
        if global_root_heading and len(global_root_heading) == len(frame_indices):
            block["global_root_heading"] = global_root_heading

        out_constraints.append(block)

    return out_constraints


def _evaluate_frame(scene: bpy.types.Scene, frame: int):
    """Move timeline to a frame and update the depsgraph (for pose sampling)."""
    scene.frame_set(frame)
    bpy.context.view_layer.update()


def constraints_to_json_string(
    constraint_items,
    scene: bpy.types.Scene,
    kimodo_fps: float = 30.0,
    auto_canonicalize: bool = True,
    indent: int = 2,
) -> str:
    """Return the constraints as a formatted JSON string."""
    data = build_constraints_json(constraint_items, scene, kimodo_fps, auto_canonicalize)
    return json.dumps(data, indent=indent)
