"""
Kimodo Blender Bridge — Retargeting
Constraint-based motion retargeting from Kimodo armature → user's rig.

Three modes per bone pair
--------------------------
COPY_ROTATION   Copy Rotation in LOCAL space.
                Root bone additionally gets Copy Location.
                Good for rigs with the same rest pose as Kimodo.

COPY_TRANSFORMS Copy Transforms in LOCAL space (loc + rot + scale together).
                Simpler than the split loc/rot approach; useful when the
                target rig's bone lengths don't match the source.

CHILD_OF        Full parent-child relationship via a Child Of constraint.
                The inverse matrix is set to identity so the target bone
                snaps to the source; set it manually in the UI if you need
                a rest-pose offset.  Best for floating / weapon bones or
                when you want exact world-space tracking.

Baking
-------
Blender's NLA bake (visual_keying=True) is used to push the constraint-
driven animation into actual keyframes; the constraints are cleared
afterwards so the rig is fully self-contained.
"""

import bpy
import json
import mathutils
import re


CONSTRAINT_PREFIX = "KIMODO_"   # prefix for all constraints we add


# ---------------------------------------------------------------------------
# Bone name auto-matching
# ---------------------------------------------------------------------------

# Common bone name pairs: (kimodo/bvh_name, common_rig_names...)
# Kimodo SOMA skeleton uses these bone names (based on BVH output conventions).
# These are heuristic — the user can always override in the UI.
_SOMA_BONE_MAP_HINTS = [
    # Kimodo name        # common alternatives in user rigs
    ("Hips",            ["hips", "pelvis", "root", "Hip", "Pelvis", "mixamorig:Hips"]),
    ("Spine",           ["spine", "Spine1", "mixamorig:Spine"]),
    ("Spine1",          ["spine1", "spine_01", "mixamorig:Spine1"]),
    ("Spine2",          ["spine2", "chest", "mixamorig:Spine2"]),
    ("Neck",            ["neck", "Neck1", "mixamorig:Neck"]),
    ("Head",            ["head", "Head", "mixamorig:Head"]),
    ("LeftShoulder",    ["l_shoulder", "shoulder.L", "mixamorig:LeftShoulder", "LeftShoulder"]),
    ("LeftArm",         ["upper_arm.L", "l_arm", "mixamorig:LeftArm", "LeftUpArm"]),
    ("LeftForeArm",     ["forearm.L", "l_forearm", "mixamorig:LeftForeArm"]),
    ("LeftHand",        ["hand.L", "l_hand", "mixamorig:LeftHand"]),
    ("RightShoulder",   ["r_shoulder", "shoulder.R", "mixamorig:RightShoulder", "RightShoulder"]),
    ("RightArm",        ["upper_arm.R", "r_arm", "mixamorig:RightArm", "RightUpArm"]),
    ("RightForeArm",    ["forearm.R", "r_forearm", "mixamorig:RightForeArm"]),
    ("RightHand",       ["hand.R", "r_hand", "mixamorig:RightHand"]),
    ("LeftUpLeg",       ["thigh.L", "l_thigh", "mixamorig:LeftUpLeg", "LeftThigh"]),
    ("LeftLeg",         ["shin.L", "l_shin", "mixamorig:LeftLeg", "LeftShin"]),
    ("LeftFoot",        ["foot.L", "l_foot", "mixamorig:LeftFoot"]),
    ("LeftToeBase",     ["toe.L", "l_toe", "mixamorig:LeftToeBase"]),
    ("RightUpLeg",      ["thigh.R", "r_thigh", "mixamorig:RightUpLeg", "RightThigh"]),
    ("RightLeg",        ["shin.R", "r_shin", "mixamorig:RightLeg", "RightShin"]),
    ("RightFoot",       ["foot.R", "r_foot", "mixamorig:RightFoot"]),
    ("RightToeBase",    ["toe.R", "r_toe", "mixamorig:RightToeBase"]),
]

_SMPLX_EXTRA_HINTS = [
    ("LeftHandIndex1",  ["f_index.01.L", "mixamorig:LeftHandIndex1"]),
    ("RightHandIndex1", ["f_index.01.R", "mixamorig:RightHandIndex1"]),
    ("LeftHandThumb1",  ["thumb.01.L",   "mixamorig:LeftHandThumb1"]),
    ("RightHandThumb1", ["thumb.01.R",   "mixamorig:RightHandThumb1"]),
    ("LeftHandMiddle1", ["f_middle.01.L","mixamorig:LeftHandMiddle1"]),
    ("RightHandMiddle1",["f_middle.01.R","mixamorig:RightHandMiddle1"]),
    ("LeftHandRing1",   ["f_ring.01.L",  "mixamorig:LeftHandRing1"]),
    ("RightHandRing1",  ["f_ring.01.R",  "mixamorig:RightHandRing1"]),
    ("LeftHandPinky1",  ["f_pinky.01.L", "mixamorig:LeftHandPinky1"]),
    ("RightHandPinky1", ["f_pinky.01.R", "mixamorig:RightHandPinky1"]),
]


# ---------------------------------------------------------------------------
# Built-in rig presets
# ---------------------------------------------------------------------------

# Each entry: preset_id → {label, description, pairs:[{src, tgt, en, mode}]}
# Bone names are the exact strings used by each rig type out of the box.
BUILTIN_RIG_PRESETS = {
    "Mixamo": {
        "label":       "Mixamo",
        "description": "Standard Mixamo skeleton (mixamorig: namespace prefix)",
        "pairs": [
            {"src": "Hips",          "tgt": "mixamorig:Hips",          "en": True, "mode": "COPY_ROTATION"},
            {"src": "Spine",         "tgt": "mixamorig:Spine",         "en": True, "mode": "COPY_ROTATION"},
            {"src": "Spine1",        "tgt": "mixamorig:Spine1",        "en": True, "mode": "COPY_ROTATION"},
            {"src": "Spine2",        "tgt": "mixamorig:Spine2",        "en": True, "mode": "COPY_ROTATION"},
            {"src": "Neck",          "tgt": "mixamorig:Neck",          "en": True, "mode": "COPY_ROTATION"},
            {"src": "Head",          "tgt": "mixamorig:Head",          "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftShoulder",  "tgt": "mixamorig:LeftShoulder",  "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftArm",       "tgt": "mixamorig:LeftArm",       "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftForeArm",   "tgt": "mixamorig:LeftForeArm",   "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftHand",      "tgt": "mixamorig:LeftHand",      "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightShoulder", "tgt": "mixamorig:RightShoulder", "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightArm",      "tgt": "mixamorig:RightArm",      "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightForeArm",  "tgt": "mixamorig:RightForeArm",  "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightHand",     "tgt": "mixamorig:RightHand",     "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftUpLeg",     "tgt": "mixamorig:LeftUpLeg",     "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftLeg",       "tgt": "mixamorig:LeftLeg",       "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftFoot",      "tgt": "mixamorig:LeftFoot",      "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftToeBase",   "tgt": "mixamorig:LeftToeBase",   "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightUpLeg",    "tgt": "mixamorig:RightUpLeg",    "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightLeg",      "tgt": "mixamorig:RightLeg",      "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightFoot",     "tgt": "mixamorig:RightFoot",     "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightToeBase",  "tgt": "mixamorig:RightToeBase",  "en": True, "mode": "COPY_ROTATION"},
        ],
    },
    "Rigify": {
        "label":       "Rigify (Human Meta-Rig)",
        "description": "Blender's built-in Rigify addon — human meta-rig FK controls",
        "pairs": [
            # torso = the root/pelvis control that drives position + hip rotation
            {"src": "Hips",          "tgt": "torso",           "en": True, "mode": "COPY_ROTATION"},
            {"src": "Spine",         "tgt": "spine_fk",        "en": True, "mode": "COPY_ROTATION"},
            {"src": "Spine1",        "tgt": "spine_fk.001",    "en": True, "mode": "COPY_ROTATION"},
            {"src": "Spine2",        "tgt": "chest",           "en": True, "mode": "COPY_ROTATION"},
            {"src": "Neck",          "tgt": "neck",            "en": True, "mode": "COPY_ROTATION"},
            {"src": "Head",          "tgt": "head",            "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftShoulder",  "tgt": "shoulder.L",      "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftArm",       "tgt": "upper_arm_fk.L",  "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftForeArm",   "tgt": "forearm_fk.L",    "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftHand",      "tgt": "hand_fk.L",       "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightShoulder", "tgt": "shoulder.R",      "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightArm",      "tgt": "upper_arm_fk.R",  "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightForeArm",  "tgt": "forearm_fk.R",    "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightHand",     "tgt": "hand_fk.R",       "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftUpLeg",     "tgt": "thigh_fk.L",      "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftLeg",       "tgt": "shin_fk.L",       "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftFoot",      "tgt": "foot_fk.L",       "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftToeBase",   "tgt": "toe.L",           "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightUpLeg",    "tgt": "thigh_fk.R",      "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightLeg",      "tgt": "shin_fk.R",       "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightFoot",     "tgt": "foot_fk.R",       "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightToeBase",  "tgt": "toe.R",           "en": True, "mode": "COPY_ROTATION"},
        ],
    },
    "AutoRigPro": {
        "label":       "AutoRig Pro",
        "description": "AutoRig Pro addon — FK controls (c_*_fk.l/r naming)",
        "pairs": [
            # c_root.x drives pelvis position+rotation; c_pos.x drives world translation
            {"src": "Hips",          "tgt": "c_root.x",         "en": True, "mode": "COPY_ROTATION"},
            {"src": "Spine",         "tgt": "c_spine_01.x",     "en": True, "mode": "COPY_ROTATION"},
            {"src": "Spine1",        "tgt": "c_spine_02.x",     "en": True, "mode": "COPY_ROTATION"},
            {"src": "Spine2",        "tgt": "c_spine_03.x",     "en": True, "mode": "COPY_ROTATION"},
            {"src": "Neck",          "tgt": "c_neck_01.x",      "en": True, "mode": "COPY_ROTATION"},
            {"src": "Head",          "tgt": "c_head.x",         "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftShoulder",  "tgt": "c_shoulder.l",     "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftArm",       "tgt": "c_arm_fk.l",       "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftForeArm",   "tgt": "c_forearm_fk.l",   "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftHand",      "tgt": "c_hand_fk.l",      "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightShoulder", "tgt": "c_shoulder.r",     "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightArm",      "tgt": "c_arm_fk.r",       "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightForeArm",  "tgt": "c_forearm_fk.r",   "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightHand",     "tgt": "c_hand_fk.r",      "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftUpLeg",     "tgt": "c_thigh_fk.l",     "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftLeg",       "tgt": "c_leg_fk.l",       "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftFoot",      "tgt": "c_foot_fk.l",      "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftToeBase",   "tgt": "c_toes_fk.l",      "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightUpLeg",    "tgt": "c_thigh_fk.r",     "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightLeg",      "tgt": "c_leg_fk.r",       "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightFoot",     "tgt": "c_foot_fk.r",      "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightToeBase",  "tgt": "c_toes_fk.r",      "en": True, "mode": "COPY_ROTATION"},
        ],
    },
    "UEMannequin": {
        "label":       "UE Mannequin (UE4/UE5)",
        "description": "Unreal Engine mannequin skeleton (pelvis/spine_0N/thigh_l naming)",
        "pairs": [
            {"src": "Hips",          "tgt": "pelvis",     "en": True, "mode": "COPY_ROTATION"},
            {"src": "Spine",         "tgt": "spine_01",   "en": True, "mode": "COPY_ROTATION"},
            {"src": "Spine1",        "tgt": "spine_02",   "en": True, "mode": "COPY_ROTATION"},
            {"src": "Spine2",        "tgt": "spine_03",   "en": True, "mode": "COPY_ROTATION"},
            {"src": "Neck",          "tgt": "neck_01",    "en": True, "mode": "COPY_ROTATION"},
            {"src": "Head",          "tgt": "head",       "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftShoulder",  "tgt": "clavicle_l", "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftArm",       "tgt": "upperarm_l", "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftForeArm",   "tgt": "lowerarm_l", "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftHand",      "tgt": "hand_l",     "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightShoulder", "tgt": "clavicle_r", "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightArm",      "tgt": "upperarm_r", "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightForeArm",  "tgt": "lowerarm_r", "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightHand",     "tgt": "hand_r",     "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftUpLeg",     "tgt": "thigh_l",    "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftLeg",       "tgt": "calf_l",     "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftFoot",      "tgt": "foot_l",     "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftToeBase",   "tgt": "ball_l",     "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightUpLeg",    "tgt": "thigh_r",    "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightLeg",      "tgt": "calf_r",     "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightFoot",     "tgt": "foot_r",     "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightToeBase",  "tgt": "ball_r",     "en": True, "mode": "COPY_ROTATION"},
        ],
    },
    "DazGenesis8": {
        "label":       "Daz Genesis 8/9",
        "description": "Daz3D Genesis 8 and 9 figures (hip/lShldrBend/lThighBend naming)",
        "pairs": [
            {"src": "Hips",          "tgt": "hip",           "en": True, "mode": "COPY_ROTATION"},
            {"src": "Spine",         "tgt": "abdomenLower",  "en": True, "mode": "COPY_ROTATION"},
            {"src": "Spine1",        "tgt": "abdomenUpper",  "en": True, "mode": "COPY_ROTATION"},
            {"src": "Spine2",        "tgt": "chest",         "en": True, "mode": "COPY_ROTATION"},
            {"src": "Neck",          "tgt": "neckLower",     "en": True, "mode": "COPY_ROTATION"},
            {"src": "Head",          "tgt": "head",          "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftShoulder",  "tgt": "lCollar",       "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftArm",       "tgt": "lShldrBend",    "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftForeArm",   "tgt": "lForearmBend",  "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftHand",      "tgt": "lHand",         "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightShoulder", "tgt": "rCollar",       "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightArm",      "tgt": "rShldrBend",    "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightForeArm",  "tgt": "rForearmBend",  "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightHand",     "tgt": "rHand",         "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftUpLeg",     "tgt": "lThighBend",    "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftLeg",       "tgt": "lShin",         "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftFoot",      "tgt": "lFoot",         "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftToeBase",   "tgt": "lToe",          "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightUpLeg",    "tgt": "rThighBend",    "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightLeg",      "tgt": "rShin",         "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightFoot",     "tgt": "rFoot",         "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightToeBase",  "tgt": "rToe",          "en": True, "mode": "COPY_ROTATION"},
        ],
    },
    "AccuRig": {
        "label":       "AccuRig / CC4",
        "description": "Reallusion AccuRig and Character Creator 4 (CC_Base_* naming)",
        "pairs": [
            {"src": "Hips",          "tgt": "CC_Base_Hip",          "en": True, "mode": "COPY_ROTATION"},
            {"src": "Spine",         "tgt": "CC_Base_Waist",        "en": True, "mode": "COPY_ROTATION"},
            {"src": "Spine1",        "tgt": "CC_Base_Spine01",      "en": True, "mode": "COPY_ROTATION"},
            {"src": "Spine2",        "tgt": "CC_Base_Spine02",      "en": True, "mode": "COPY_ROTATION"},
            {"src": "Neck",          "tgt": "CC_Base_NeckTwist01",  "en": True, "mode": "COPY_ROTATION"},
            {"src": "Head",          "tgt": "CC_Base_Head",         "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftShoulder",  "tgt": "CC_Base_L_Clavicle",   "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftArm",       "tgt": "CC_Base_L_Upperarm",   "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftForeArm",   "tgt": "CC_Base_L_Forearm",    "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftHand",      "tgt": "CC_Base_L_Hand",       "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightShoulder", "tgt": "CC_Base_R_Clavicle",   "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightArm",      "tgt": "CC_Base_R_Upperarm",   "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightForeArm",  "tgt": "CC_Base_R_Forearm",    "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightHand",     "tgt": "CC_Base_R_Hand",       "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftUpLeg",     "tgt": "CC_Base_L_Thigh",      "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftLeg",       "tgt": "CC_Base_L_Calf",       "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftFoot",      "tgt": "CC_Base_L_Foot",       "en": True, "mode": "COPY_ROTATION"},
            {"src": "LeftToeBase",   "tgt": "CC_Base_L_ToeBase",    "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightUpLeg",    "tgt": "CC_Base_R_Thigh",      "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightLeg",      "tgt": "CC_Base_R_Calf",       "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightFoot",     "tgt": "CC_Base_R_Foot",       "en": True, "mode": "COPY_ROTATION"},
            {"src": "RightToeBase",  "tgt": "CC_Base_R_ToeBase",    "en": True, "mode": "COPY_ROTATION"},
        ],
    },
}


def _normalize(name: str) -> str:
    """Lowercase, strip prefix up to ':', remove non-alphanumeric."""
    name = name.lower()
    if ":" in name:
        name = name.split(":")[-1]
    return re.sub(r"[^a-z0-9]", "", name)


def auto_build_mapping(source_arm: bpy.types.Object,
                       target_arm: bpy.types.Object,
                       model: str = "smpl") -> list[tuple[str, str]]:
    """
    Attempt to auto-match bones between source (Kimodo) and target (user) armatures.
    Returns list of (source_bone_name, target_bone_name) pairs.
    """
    hints = _SOMA_BONE_MAP_HINTS[:]
    if model == "smplx":
        hints += _SMPLX_EXTRA_HINTS

    src_bones = {b.name for b in source_arm.data.bones}
    tgt_bones = {b.name: _normalize(b.name) for b in target_arm.data.bones}

    result = []
    for src_name, alternatives in hints:
        if src_name not in src_bones:
            continue
        # Try exact match first
        matched = None
        for tgt_name in target_arm.data.bones.keys():
            if tgt_name == src_name:
                matched = tgt_name
                break
        # Try normalized match
        if not matched:
            src_norm = _normalize(src_name)
            for tgt_name, tgt_norm in tgt_bones.items():
                if src_norm == tgt_norm:
                    matched = tgt_name
                    break
        # Try alternatives
        if not matched:
            for alt in alternatives:
                if alt in tgt_bones:
                    matched = alt
                    break
                alt_norm = _normalize(alt)
                for tgt_name, tgt_norm in tgt_bones.items():
                    if alt_norm == tgt_norm:
                        matched = tgt_name
                        break
                if matched:
                    break
        if matched:
            result.append((src_name, matched))

    return result


# ---------------------------------------------------------------------------
# Constraint setup
# ---------------------------------------------------------------------------

def apply_retargeting_constraints(
    source_arm: bpy.types.Object,
    target_arm: bpy.types.Object,
    bone_pairs: "list[tuple[str, str, bool, str]]",  # (src, tgt, enabled, mode)
    root_bone: str = "",
) -> "tuple[int, list[str]]":
    """
    Add retargeting constraints to each enabled bone pair.
    Returns (n_applied, [warning_messages]).

    bone_pairs tuples: (source_bone, target_bone, enabled, retarget_mode)
    retarget_mode:  'COPY_ROTATION' | 'COPY_TRANSFORMS' | 'CHILD_OF'
    """
    source_arm.hide_viewport = False
    target_arm.hide_viewport = False

    tgt_pose = target_arm.pose
    applied  = 0
    warnings = []

    for entry in bone_pairs:
        # Accept both 3-tuples (legacy) and 4-tuples (with mode)
        if len(entry) == 4:
            src_name, tgt_name, enabled, mode = entry
        else:
            src_name, tgt_name, enabled = entry
            mode = "COPY_ROTATION"

        if not enabled:
            continue

        tgt_pbone = tgt_pose.bones.get(tgt_name)
        if not tgt_pbone:
            warnings.append(f"Target bone '{tgt_name}' not found — skipped.")
            continue
        if src_name not in source_arm.data.bones:
            warnings.append(f"Source bone '{src_name}' not in Kimodo armature — skipped.")
            continue

        # Clear previous Kimodo constraints on this bone
        for c in list(tgt_pbone.constraints):
            if c.name.startswith(CONSTRAINT_PREFIX):
                tgt_pbone.constraints.remove(c)

        is_root = (tgt_name == root_bone) or (src_name.lower() in ("hips", "pelvis", "root"))

        if mode == "COPY_ROTATION":
            _add_copy_rotation(tgt_pbone, source_arm, src_name, is_root)

        elif mode == "COPY_TRANSFORMS":
            _add_copy_transforms(tgt_pbone, source_arm, src_name)

        elif mode == "CHILD_OF":
            _add_child_of(tgt_pbone, source_arm, src_name)

        else:
            warnings.append(f"Unknown retarget mode '{mode}' for '{tgt_name}' — using Copy Rotation.")
            _add_copy_rotation(tgt_pbone, source_arm, src_name, is_root)

        applied += 1

    return applied, warnings


# ---------------------------------------------------------------------------
# Per-mode helpers
# ---------------------------------------------------------------------------

def _add_copy_rotation(pbone, source_arm, src_name: str, is_root: bool = False) -> None:
    """Copy Rotation in local space; root bone also gets Copy Location."""
    if is_root:
        loc = pbone.constraints.new("COPY_LOCATION")
        loc.name          = CONSTRAINT_PREFIX + "Location"
        loc.target        = source_arm
        loc.subtarget     = src_name
        loc.use_offset    = False

    rot = pbone.constraints.new("COPY_ROTATION")
    rot.name         = CONSTRAINT_PREFIX + "Rotation"
    rot.target       = source_arm
    rot.subtarget    = src_name
    rot.mix_mode     = 'REPLACE'
    rot.owner_space  = 'WORLD'
    rot.target_space = 'WORLD'


def _add_copy_transforms(pbone, source_arm, src_name: str) -> None:
    """Copy Transforms in local space (location + rotation + scale)."""
    ct = pbone.constraints.new("COPY_TRANSFORMS")
    ct.name         = CONSTRAINT_PREFIX + "CopyTransforms"
    ct.target       = source_arm
    ct.subtarget    = src_name
    ct.mix_mode     = 'REPLACE'
    ct.owner_space  = 'LOCAL'
    ct.target_space = 'LOCAL'


def _add_child_of(pbone, source_arm, src_name: str) -> None:
    """
    Child Of constraint with the inverse matrix set automatically.
    Equivalent to clicking 'Set Inverse' in the UI: stores the inverse of
    the source bone's current world matrix so the target bone doesn't jump
    when the constraint activates.
    """
    co = pbone.constraints.new("CHILD_OF")
    co.name           = CONSTRAINT_PREFIX + "ChildOf"
    co.target         = source_arm
    co.subtarget      = src_name
    co.use_location_x = True
    co.use_location_y = True
    co.use_location_z = True
    co.use_rotation_x = True
    co.use_rotation_y = True
    co.use_rotation_z = True
    co.use_scale_x    = False
    co.use_scale_y    = False
    co.use_scale_z    = False

    # Set Inverse: invert the source bone's current world matrix so the
    # target bone stays exactly where it is when the constraint first fires.
    src_pbone = source_arm.pose.bones.get(src_name)
    if src_pbone:
        co.inverse_matrix = (source_arm.matrix_world @ src_pbone.matrix).inverted()
    else:
        co.inverse_matrix = mathutils.Matrix.Identity(4)


def remove_retargeting_constraints(target_arm: bpy.types.Object) -> int:
    """Removes all Kimodo constraints from target armature. Returns count removed."""
    removed = 0
    for pbone in target_arm.pose.bones:
        for c in list(pbone.constraints):
            if c.name.startswith(CONSTRAINT_PREFIX):
                pbone.constraints.remove(c)
                removed += 1
    return removed


# ---------------------------------------------------------------------------
# Baking
# ---------------------------------------------------------------------------

def bake_retargeted_animation(
    target_arm: bpy.types.Object,
    frame_start: int,
    frame_end: int,
) -> bool:
    """
    Bakes the driven (constraint) animation into actual keyframes,
    then removes the retargeting constraints.
    Returns True on success.
    """
    try:
        # Select only target armature
        bpy.ops.object.select_all(action='DESELECT')
        target_arm.select_set(True)
        bpy.context.view_layer.objects.active = target_arm

        # Enter pose mode for baking
        bpy.ops.object.mode_set(mode='POSE')
        bpy.ops.pose.select_all(action='SELECT')

        bpy.ops.nla.bake(
            frame_start=frame_start,
            frame_end=frame_end,
            only_selected=False,
            visual_keying=True,
            clear_constraints=True,
            clear_parents=False,
            use_current_action=True,
            bake_types={'POSE'},
        )

        bpy.ops.object.mode_set(mode='OBJECT')
        return True

    except Exception as e:
        print(f"[Kimodo] Bake error: {e}")
        return False


# ---------------------------------------------------------------------------
# Preset save / load
# ---------------------------------------------------------------------------

def save_preset(prefs, preset_name: str, bone_pairs: list[dict]) -> None:
    """Save bone mapping to addon preferences."""
    import json
    try:
        presets = json.loads(prefs.saved_presets)
    except Exception:
        presets = {}
    presets[preset_name] = bone_pairs
    prefs.saved_presets = json.dumps(presets)


def load_preset(prefs, preset_name: str) -> list[dict] | None:
    """Load bone mapping from addon preferences. Returns None if not found."""
    import json
    try:
        presets = json.loads(prefs.saved_presets)
        return presets.get(preset_name)
    except Exception:
        return None


def list_presets(prefs) -> list[str]:
    """Returns list of saved preset names."""
    import json
    try:
        return list(json.loads(prefs.saved_presets).keys())
    except Exception:
        return []
