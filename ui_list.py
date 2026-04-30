"""
Kimodo Blender Bridge — UIList
Custom UIList for displaying bone mapping pairs.
"""

import bpy
from bpy.types import UIList


_MODE_ICONS = {
    "COPY_ROTATION":   'CON_ROTLIKE',
    "COPY_TRANSFORMS": 'CON_TRANSLIKE',
    "CHILD_OF":        'CON_CHILDOF',
}


class KIMODO_UL_BoneMappings(UIList):
    """Draws each bone mapping row: [✓] SourceBone → TargetBone [mode]"""

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        s = context.scene.kimodo

        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)

            # Checkbox must live outside any disabled sub-row so it stays clickable
            row.prop(item, "enabled", text="", emboss=False,
                     icon='CHECKBOX_HLT' if item.enabled else 'CHECKBOX_DEHLT')

            # Everything else is dimmed when the pair is disabled
            sub = row.row(align=True)
            sub.enabled = item.enabled

            sub.prop_search(item, "source_bone",
                            s.source_armature.data if s.source_armature else bpy.data,
                            "bones" if s.source_armature else "objects",
                            text="", icon='BONE_DATA')

            sub.label(text="", icon='FORWARD')

            sub.prop_search(item, "target_bone",
                            s.target_armature.data if s.target_armature else bpy.data,
                            "bones" if s.target_armature else "objects",
                            text="", icon='BONE_DATA')

            mode_icon = _MODE_ICONS.get(item.retarget_mode, 'CON_ROTLIKE')
            sub.prop(item, "retarget_mode", text="", icon=mode_icon)

        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon='BONE_DATA')

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        flt_flags = []
        flt_neworder = []

        if self.filter_name:
            flt_flags = bpy.types.UI_UL_list.filter_items_by_name(
                self.filter_name, self.bitflag_filter_item, items,
                "source_bone", reverse=self.use_filter_invert
            )

        return flt_flags, flt_neworder


def register():
    bpy.utils.register_class(KIMODO_UL_BoneMappings)


def unregister():
    bpy.utils.unregister_class(KIMODO_UL_BoneMappings)
