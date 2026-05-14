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

# Approximate T-pose offsets of each end-effector joint relative to the
# root (Hips) in Kimodo Y-up space (X right, Y up, Z forward), in metres.
#
# FK formula (from kimodo/skeleton/kinematics.py):
#   posed_joints[j] = neutral_joints_centred[j] + root_positions
#
# With identity (T-pose) rotations, neutral_joints_centred[j] = T-pose offset.
# So to place effector j at world position P:
#   root_positions = P - T_POSE_OFFSET[j]
#   → FK gives:  P - T_POSE_OFFSET[j] + T_POSE_OFFSET[j] = P  ✓
#
# Values are estimates for the default SOMA 1.75 m character; adjust the
# per-constraint tpose_offset_delta in the Effector Debug panel if needed.
_T_POSE_OFFSET = {
    'left_hand':  mathutils.Vector((-0.56,  0.30, 0.0)),
    'right_hand': mathutils.Vector(( 0.56,  0.30, 0.0)),
    'left_foot':  mathutils.Vector((-0.10, -0.90, 0.0)),
    'right_foot': mathutils.Vector(( 0.10, -0.90, 0.0)),
}

# Bone names for SOMASkeleton30, exactly matching Kimodo's bone_order_names_with_parents.
# The INDEX in this list is the slot index that Kimodo reads from local_joints_rot.
# All names exist in somaskel77 (the BVH skeleton) so pose_bones.get() will find them.
SOMA_JOINT_ORDER = [
    "Hips",              # 0  — root
    "Spine1",            # 1
    "Spine2",            # 2
    "Chest",             # 3
    "Neck1",             # 4
    "Neck2",             # 5
    "Head",              # 6
    "Jaw",               # 7
    "LeftEye",           # 8
    "RightEye",          # 9
    "LeftShoulder",      # 10
    "LeftArm",           # 11
    "LeftForeArm",       # 12
    "LeftHand",          # 13
    "LeftHandThumbEnd",  # 14
    "LeftHandMiddleEnd", # 15
    "RightShoulder",     # 16
    "RightArm",          # 17
    "RightForeArm",      # 18
    "RightHand",         # 19
    "RightHandThumbEnd", # 20
    "RightHandMiddleEnd",# 21
    "LeftLeg",           # 22 — hip (not "LeftUpLeg")
    "LeftShin",          # 23 — knee (not "LeftLeg")
    "LeftFoot",          # 24
    "LeftToeBase",       # 25
    "RightLeg",          # 26 — hip
    "RightShin",         # 27 — knee
    "RightFoot",         # 28
    "RightToeBase",      # 29
]

# Parent index for each joint in SOMA_JOINT_ORDER (mirrors SOMASkeleton30.bone_order_names_with_parents).
# -1 = root (no parent).  Used to compute local rotations from world rotations.
SOMA_JOINT_PARENTS = [
    -1,  # 0  Hips        (root)
     0,  # 1  Spine1
     1,  # 2  Spine2
     2,  # 3  Chest
     3,  # 4  Neck1
     4,  # 5  Neck2
     5,  # 6  Head
     6,  # 7  Jaw
     6,  # 8  LeftEye
     6,  # 9  RightEye
     3,  # 10 LeftShoulder
    10,  # 11 LeftArm
    11,  # 12 LeftForeArm
    12,  # 13 LeftHand
    13,  # 14 LeftHandThumbEnd
    13,  # 15 LeftHandMiddleEnd
     3,  # 16 RightShoulder
    16,  # 17 RightArm
    17,  # 18 RightForeArm
    18,  # 19 RightHand
    19,  # 20 RightHandThumbEnd
    19,  # 21 RightHandMiddleEnd
     0,  # 22 LeftLeg
    22,  # 23 LeftShin
    23,  # 24 LeftFoot
    24,  # 25 LeftToeBase
     0,  # 26 RightLeg
    26,  # 27 RightShin
    27,  # 28 RightFoot
    28,  # 29 RightToeBase
]

# Coordinate-change matrix: v_kimodo = _M_BK @ v_blender
# Derived from blender_to_kimodo_pos: (Bx, By, Bz) → (Bx, Bz, -By)
# M_BK = [[1,0,0],[0,0,1],[0,-1,0]] — orthogonal, det=1
# For rotation matrices: R_kimodo = M_BK @ R_blender @ M_BK.T


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

def _rot3_to_axis_angle(m: mathutils.Matrix) -> list[float]:
    """3×3 rotation matrix (already in Kimodo Y-up space) → axis-angle 3-vector."""
    q = m.to_quaternion().normalized()
    angle = 2.0 * math.acos(max(-1.0, min(1.0, q.w)))
    s = math.sqrt(max(0.0, 1.0 - q.w * q.w))
    if s < 1e-6:
        return [0.0, 0.0, 0.0]
    return [q.x / s * angle, q.y / s * angle, q.z / s * angle]


def get_armature_joint_rots(
    armature_obj: bpy.types.Object,
    joint_order: list[str],
) -> list[list[float]]:
    """Extract SOMASkeleton30 local joint rotations from a BVH-imported armature.

    bvhio writes ZYX Euler channels (Zrotation, Yrotation, Xrotation) whose
    angles are in Kimodo's Y-up coordinate space.  Blender remaps the ROOT
    bone's channels into its own Z-up convention (because the armature object
    rotation compensates for the Y-up→Z-up import), but child-bone Euler
    values are stored as-is from the BVH.

    Root bone (index 0):
        Blender's to_quaternion() correctly describes the Z-up-remapped root
        orientation; quat_to_axis_angle_vec then converts back to Kimodo Y-up.

    Child bones (index 1-29):
        The Euler angles are in Kimodo's Y-up space, so we build the rotation
        matrix directly with standard right-hand math — Rz @ Ry @ Rx for ZYX
        mode — and skip Blender's to_quaternion() entirely (which would wrongly
        apply Blender's Z-up axis labels).  mathutils.Matrix.Rotation uses the
        same formulas in any right-hand system, so no extra remapping is needed.
    """
    pose_bones = armature_obj.pose.bones
    result: list[list[float]] = []

    for i, name in enumerate(joint_order):
        pb = pose_bones.get(name)
        if pb is None:
            result.append([0.0, 0.0, 0.0])
            continue

        mode = pb.rotation_mode

        if i == 0:
            # Root: Blender has remapped these channels to Z-up, so go through
            # Blender's quaternion and apply the standard [X,Z,-Y] axis remap.
            if mode == 'QUATERNION':
                q = pb.rotation_quaternion
            elif mode == 'AXIS_ANGLE':
                q = mathutils.Quaternion(
                    mathutils.Vector(pb.rotation_axis_angle[1:4]),
                    pb.rotation_axis_angle[0],
                )
            else:
                q = pb.rotation_euler.to_quaternion()
            result.append(quat_to_axis_angle_vec(q))

        elif mode == 'QUATERNION':
            # Child bone manually posed in quaternion mode — treat axis-angle
            # as-is in Kimodo's local space (no coordinate remap).
            q = pb.rotation_quaternion.normalized()
            angle = 2.0 * math.acos(max(-1.0, min(1.0, q.w)))
            s = math.sqrt(max(0.0, 1.0 - q.w * q.w))
            result.append([0.0, 0.0, 0.0] if s < 1e-6
                          else [q.x / s * angle, q.y / s * angle, q.z / s * angle])

        elif mode == 'AXIS_ANGLE':
            q = mathutils.Quaternion(
                mathutils.Vector(pb.rotation_axis_angle[1:4]),
                pb.rotation_axis_angle[0],
            ).normalized()
            angle = 2.0 * math.acos(max(-1.0, min(1.0, q.w)))
            s = math.sqrt(max(0.0, 1.0 - q.w * q.w))
            result.append([0.0, 0.0, 0.0] if s < 1e-6
                          else [q.x / s * angle, q.y / s * angle, q.z / s * angle])

        else:
            # Child bone with Euler mode (ZYX for BVH import).
            # The angles are in Kimodo's Y-up convention.  Build the rotation
            # matrix directly — no to_quaternion(), which would mis-apply
            # Blender's Z-up axis labels to Kimodo's Y-up angle values.
            x = pb.rotation_euler.x
            y = pb.rotation_euler.y
            z = pb.rotation_euler.z
            Rx = mathutils.Matrix.Rotation(x, 3, 'X')
            Ry = mathutils.Matrix.Rotation(y, 3, 'Y')
            Rz = mathutils.Matrix.Rotation(z, 3, 'Z')
            if mode == 'ZYX':
                R = Rz @ Ry @ Rx
            elif mode == 'ZXY':
                R = Rz @ Rx @ Ry
            elif mode == 'YZX':
                R = Ry @ Rz @ Rx
            elif mode == 'YXZ':
                R = Ry @ Rx @ Rz
            elif mode == 'XZY':
                R = Rx @ Rz @ Ry
            else:   # XYZ or unknown
                R = Rx @ Ry @ Rz
            result.append(_rot3_to_axis_angle(R))

    return result


def get_root_position(armature_obj: bpy.types.Object) -> list[float]:
    """Get hip/root world position from armature → Kimodo [x,y,z].

    Reads the Hips bone head in world space so the result is correct
    regardless of where the armature object's origin sits (BVH imports
    always place the armature origin at the scene origin; the Hips bone
    moves via keyframes/pose, not via the object location).
    """
    for bone_name in ("Hips", "hips", "Hip", "pelvis", "Pelvis"):
        pb = armature_obj.pose.bones.get(bone_name)
        if pb:
            world_pos = armature_obj.matrix_world @ pb.head
            return blender_to_kimodo_pos(world_pos)
    return blender_to_kimodo_pos(armature_obj.location)


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

    # Save scene frame — _evaluate_frame mutates it as a side effect.
    saved_frame = scene.frame_current

    try:
        # -----------------------------------------------------------------------
        # Auto-canonicalization: find the earliest XZ root position and use it
        # as the origin offset so the user can author constraints anywhere.
        # -----------------------------------------------------------------------
        origin_offset = [0.0, 0.0]  # [x_offset, z_offset] in Kimodo space
        if auto_canonicalize:
            for ci in items:
                ctype = ci.constraint_type
                if ctype == 'root2d':
                    pos2d = blender_to_kimodo_2d(ci.marker_object.location)
                    origin_offset = pos2d
                    break
                elif ctype == 'fullbody':
                    # Must evaluate the frame before reading the Hips bone position.
                    _evaluate_frame(scene, ci.frame)
                    pos3d = get_root_position(ci.marker_object)
                    origin_offset = [pos3d[0], pos3d[2]]
                    break

        def apply_offset_2d(pos2d):
            return [pos2d[0] - origin_offset[0], pos2d[1] - origin_offset[1]]

        def apply_offset_3d(pos3d):
            return [pos3d[0] - origin_offset[0], pos3d[1], pos3d[2] - origin_offset[1]]

        # -----------------------------------------------------------------------
        # Group ALL items of the same type into one block (not just consecutive).
        # Kimodo expects one block per constraint type with all frame_indices.
        # -----------------------------------------------------------------------
        type_order: list[str] = []
        type_to_items: dict[str, list] = {}
        for ci in items:
            ct = ci.constraint_type
            if ct not in type_to_items:
                type_order.append(ct)
                type_to_items[ct] = []
            type_to_items[ct].append(ci)

        _EFFECTOR_SET = frozenset(['left_hand', 'right_hand', 'left_foot', 'right_foot'])

        # ------------------------------------------------------------------
        # Helper: compute (pos3d, smooth2d_or_None, jrot) for one effector
        # constraint item.  Called for both single- and multi-effector cases.
        # ------------------------------------------------------------------
        def _effector_item_data(ci, ctype, obj):
            _EFFECTOR_BONE = {
                'left_hand': 'LeftHand', 'right_hand': 'RightHand',
                'left_foot': 'LeftFoot', 'right_foot': 'RightFoot',
            }[ctype]
            _EFFECTOR_IDX = {
                'left_hand': 13, 'right_hand': 19,
                'left_foot': 24, 'right_foot': 28,
            }[ctype]

            is_armature = obj.type == 'ARMATURE'
            if is_armature:
                _evaluate_frame(scene, ci.frame)

            if ci.effector_space == 'LOCAL' and obj.parent:
                effector_blender_vec = obj.location.copy()
            else:
                effector_blender_vec = obj.matrix_world.translation.copy()

            effector_pos3d = blender_to_kimodo_pos(effector_blender_vec)

            if is_armature:
                bone_world_pos = get_bone_world_position(obj, _EFFECTOR_BONE)
                if bone_world_pos is not None:
                    if ci.effector_space == 'LOCAL' and obj.parent:
                        pb = obj.pose.bones.get(_EFFECTOR_BONE)
                        if pb:
                            local_vec = obj.parent.matrix_world.inverted() @ (
                                obj.matrix_world @ pb.head)
                            effector_pos3d = blender_to_kimodo_pos(local_vec)
                    else:
                        effector_pos3d = bone_world_pos
                effector_rot_aa = get_bone_world_rotation(obj, _EFFECTOR_BONE)
            else:
                effector_rot_aa = [0.0, 0.0, 0.0]

            # Root position
            rps = ci.root_pos_source
            if rps == 'MANUAL':
                pos3d = apply_offset_3d(list(ci.manual_root_pos))
            elif rps == 'EFFECTOR':
                pos3d = apply_offset_3d(effector_pos3d)
            elif rps == 'HIPS' and is_armature:
                pos3d = apply_offset_3d(get_root_position(obj))
            else:  # AUTO
                if is_armature:
                    pos3d = apply_offset_3d(get_root_position(obj))
                else:
                    base = _T_POSE_OFFSET.get(ctype, mathutils.Vector((0, 0, 0)))
                    delta = mathutils.Vector(ci.tpose_offset_delta)
                    offset = base + delta
                    ep = mathutils.Vector(effector_pos3d)
                    pos3d = apply_offset_3d([ep.x - offset.x, ep.y - offset.y, ep.z - offset.z])

            # Smooth root 2D
            s2d = ci.smooth_root_2d_mode
            if s2d == 'FROM_ROOT':
                smooth2d = apply_offset_2d([pos3d[0], pos3d[2]])
            elif s2d == 'FROM_EFFECTOR':
                ep2 = apply_offset_3d(effector_pos3d)
                smooth2d = apply_offset_2d([ep2[0], ep2[2]])
            elif s2d == 'AUTO' and is_armature:
                smooth2d = apply_offset_2d([pos3d[0], pos3d[2]])
            else:
                smooth2d = None

            # Joint rotations
            jrm = ci.joint_rots_mode
            if jrm == 'FULL_POSE' and is_armature:
                jrot = get_armature_joint_rots(obj, SOMA_JOINT_ORDER)
            elif jrm == 'EFFECTOR_ONLY':
                jrot = [[0.0, 0.0, 0.0]] * len(SOMA_JOINT_ORDER)
                jrot[_EFFECTOR_IDX] = effector_rot_aa
            else:
                jrot = [[0.0, 0.0, 0.0]] * len(SOMA_JOINT_ORDER)

            return pos3d, smooth2d, jrot, _EFFECTOR_IDX

        out_constraints = []

        for ctype in type_order:
            group = type_to_items[ctype]
            frame_indices: list[int] = []
            smooth_root_2d: list = []
            root_positions: list = []
            local_joints_rot: list = []
            global_root_heading: list = []

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
                        # Evaluate the frame FIRST so Hips bone is at the right
                        # world position (BVH armatures animate via bone keyframes,
                        # not via the object location, so position must be sampled
                        # at the correct frame).
                        _evaluate_frame(scene, ci.frame)
                        pos3d = get_root_position(obj)
                        root_positions.append(apply_offset_3d(pos3d))
                        jrot = get_armature_joint_rots(obj, SOMA_JOINT_ORDER)
                        local_joints_rot.append(jrot)
                        pos2d = [pos3d[0], pos3d[2]]
                        smooth_root_2d.append(apply_offset_2d(pos2d))
                    else:
                        # Empty used for fullbody — just root position, identity pose.
                        pos3d = blender_to_kimodo_pos(obj.location)
                        root_positions.append(apply_offset_3d(pos3d))
                        pos2d = [pos3d[0], pos3d[2]]
                        smooth_root_2d.append(apply_offset_2d(pos2d))
                        local_joints_rot.append([[0.0, 0.0, 0.0]] * len(SOMA_JOINT_ORDER))

                elif ctype in _EFFECTOR_SET:
                    # Effectors are handled after this loop to detect and merge
                    # same-frame conflicts into a single end-effector block.
                    pass

            if ctype in _EFFECTOR_SET:
                continue  # processed in the effector pass below

            block: dict[str, Any] = {
                "type": ctype.replace("_", "-"),
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

        # -----------------------------------------------------------------------
        # Effector pass: one named-type block per effector type, all frames.
        #
        # Each block is built independently so its FK back-solve is exact for
        # its own effector (root = target - T_pose_offset → FK gives target).
        # When two effectors share the same frame their root_positions will
        # differ — Kimodo treats all constraints as soft guides and will find
        # a natural pose that reaches both targets from a compromise root.
        # -----------------------------------------------------------------------
        for ctype in [ct for ct in type_order if ct in _EFFECTOR_SET]:
            entries = []
            for ci in type_to_items[ctype]:
                kf = blender_frame_to_kimodo(ci.frame, scene_start, blender_fps, kimodo_fps)
                entries.append((kf, ci))
            entries.sort(key=lambda x: x[0])

            f_idx, r_pos, s2d_out, j_rot = [], [], [], []
            for kf, ci in entries:
                obj = ci.marker_object
                pos3d, smooth2d, jrot, _ = _effector_item_data(ci, ctype, obj)
                f_idx.append(kf)
                r_pos.append(pos3d)
                j_rot.append(jrot)
                if smooth2d is not None:
                    s2d_out.append(smooth2d)

            block: dict[str, Any] = {
                "type": ctype.replace("_", "-"),
                "frame_indices": f_idx,
                "root_positions": r_pos,
                "local_joints_rot": j_rot,
            }
            if s2d_out:
                block["smooth_root_2d"] = s2d_out
            out_constraints.append(block)

        return out_constraints

    finally:
        # Restore the frame so constraint-building has no visible side effect.
        scene.frame_set(saved_frame)
        bpy.context.view_layer.update()


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
