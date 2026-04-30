"""
Kimodo Blender Bridge — Timeline Draw Handler
Paints motion segment bars in the Timeline editor (read-only, purely visual).

Key design decisions:
  • ALL geometry drawn as TRIS — never LINES.  Mixing primitive types between
    batch.draw() calls in Blender 5.x corrupts GPU state and causes subsequent
    segments to disappear.  Borders are drawn as four thin 1px TRIS rectangles.
  • Coordinate conversion uses region_to_view at the VERTICAL CENTRE of the
    region, not y=0.  The DopeSheet Y axis is channel rows; y=0 pixel maps to
    the bottom channel which can be off-screen at normal zoom, making the X
    interpolation scale wrong.
  • One shader object is created per draw call and reused for the whole frame.
"""

import bpy
import blf
import gpu
from gpu_extras.batch import batch_for_shader

_state = {"handle": None}

BAR_HEIGHT     = 20
BAR_Y_FROM_TOP = 28
BAR_PADDING_X  = 4
TEXT_SIZE      = 11
MIN_BAR_PX     = 4
TEXT_MIN_PX    = 45
BORDER_PX      = 1    # thickness of the TRIS border rectangles


def _fill_verts(x1, y1, x2, y2):
    """Two-triangle quad."""
    return [(x1,y1),(x2,y1),(x2,y2), (x1,y1),(x2,y2),(x1,y2)]


def _border_verts(x1, y1, x2, y2, t=BORDER_PX):
    """
    Four thin TRIS rectangles that form a border — NO LINES primitive used.
    t = border thickness in pixels.
    """
    verts = []
    # bottom
    verts += _fill_verts(x1, y1,   x2,   y1+t)
    # top
    verts += _fill_verts(x1, y2-t, x2,   y2)
    # left
    verts += _fill_verts(x1, y1,   x1+t, y2)
    # right
    verts += _fill_verts(x2-t, y1, x2,   y2)
    return verts


def _draw_segments():
    context = bpy.context
    if not context or not context.scene:
        return

    space = context.space_data
    if not space or space.type != 'DOPESHEET_EDITOR':
        return
    if getattr(space, 'mode', None) != 'TIMELINE':
        return

    s = getattr(context.scene, "kimodo", None)
    if not s or not s.motion_segments:
        return

    region = context.region
    if not region or region.width < 10 or region.height < 10:
        return

    view2d = region.view2d
    rw     = region.width
    rh     = region.height

    # Use the vertical CENTRE of the region for region_to_view so we're
    # safely inside the visible channel range at any zoom level.
    cy = rh // 2
    view_left,  _ = view2d.region_to_view(0,  cy)
    view_right, _ = view2d.region_to_view(rw, cy)

    if abs(view_right - view_left) < 1e-6:
        return

    span = view_right - view_left

    def fpx(frame):
        """Frame number to pixel X. May be outside 0..rw for off-screen frames."""
        return (frame - view_left) / span * rw

    y_top    = rh - BAR_Y_FROM_TOP
    y_bottom = y_top - BAR_HEIGHT

    if y_bottom < 0:
        return

    active_idx = s.segment_index

    # One shader for the entire callback — bind before each draw.
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')

    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')

    for i, seg in enumerate(s.motion_segments):
        if not seg.enabled:
            continue

        x1 = fpx(seg.start_frame)
        x2 = fpx(seg.end_frame + 1)

        if x2 - x1 < MIN_BAR_PX:
            continue
        if x2 < 0 or x1 > rw:
            continue

        try:
            r = float(seg.color[0])
            g = float(seg.color[1])
            b = float(seg.color[2])
            a = float(seg.color[3])
        except Exception:
            r, g, b, a = 0.3, 0.6, 1.0, 0.85

        if i != active_idx:
            r, g, b = r * 0.72, g * 0.72, b * 0.72

        # ---- filled bar ----
        batch = batch_for_shader(shader, 'TRIS', {"pos": _fill_verts(x1, y_bottom, x2, y_top)})
        shader.bind()
        shader.uniform_float("color", (r, g, b, a))
        batch.draw(shader)

        # ---- border (all TRIS, no LINES) ----
        bd = batch_for_shader(shader, 'TRIS', {"pos": _border_verts(x1, y_bottom, x2, y_top)})
        shader.bind()
        shader.uniform_float("color", (r * 0.45, g * 0.45, b * 0.45, 1.0))
        bd.draw(shader)

        # ---- active highlight ----
        if i == active_idx:
            hi = batch_for_shader(shader, 'TRIS',
                                  {"pos": _border_verts(x1-1, y_bottom-1, x2+1, y_top+1, t=2)})
            shader.bind()
            shader.uniform_float("color", (1.0, 1.0, 1.0, 0.85))
            hi.draw(shader)

        # ---- label ----
        bar_px = min(x2, rw) - max(x1, 0)
        if bar_px >= TEXT_MIN_PX:
            label = seg.prompt or "(no prompt)"
            blf.size(0, TEXT_SIZE)
            max_ch = max(3, int((bar_px - BAR_PADDING_X * 2) / (TEXT_SIZE * 0.58)))
            if len(label) > max_ch:
                label = label[:max_ch - 1] + "\u2026"

            tx = max(x1, 0) + BAR_PADDING_X
            ty = y_bottom + (BAR_HEIGHT - TEXT_SIZE) // 2

            blf.position(0, tx + 1, ty - 1, 0)
            blf.color(0, 0.0, 0.0, 0.55)
            blf.draw(0, label)

            blf.position(0, tx, ty, 0)
            blf.color(0, 1.0, 1.0, 1.0)
            blf.draw(0, label)

    gpu.state.depth_test_set('NONE')
    gpu.state.blend_set('NONE')


def register():
    if _state["handle"] is not None:
        return
    _state["handle"] = bpy.types.SpaceDopeSheetEditor.draw_handler_add(
        _draw_segments, (), 'WINDOW', 'POST_PIXEL'
    )


def unregister():
    handle = _state.pop("handle", None)
    if handle is not None:
        try:
            bpy.types.SpaceDopeSheetEditor.draw_handler_remove(handle, 'WINDOW')
        except Exception:
            pass
    _state["handle"] = None
