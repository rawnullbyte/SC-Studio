"""Easing curves and tweening for placement animation.

The SC2 movieclip format stores a discrete transform per frame and has nowhere
to record an interpolation, so a curve is an authoring-time choice: applying one
bakes the intermediate matrices as real frames, which is exactly what the game
reads. Nothing here is saved as a curve.
"""

import math

# Cubic control points in CSS cubic-bezier(x1, y1, x2, y2) form, which is the
# vocabulary people already know from web animation.
PRESETS = {
    "Linear": (0.0, 0.0, 1.0, 1.0),
    "Ease in": (0.42, 0.0, 1.0, 1.0),
    "Ease out": (0.0, 0.0, 0.58, 1.0),
    "Ease in-out": (0.42, 0.0, 0.58, 1.0),
    "Smooth": (0.25, 0.1, 0.25, 1.0),
    "Snap in": (0.7, 0.0, 0.84, 0.0),
    "Snap out": (0.16, 1.0, 0.3, 1.0),
    "Overshoot": (0.34, 1.56, 0.64, 1.0),
    "Anticipate": (0.36, -0.56, 0.66, -1.0),
}


def bezier(progress, x1, y1, x2, y2, iterations=24):
    """Evaluate a cubic bezier easing curve at ``progress`` in 0..1.

    The curve is parametric, so the x axis has to be inverted to find the curve
    parameter for a given time. The x component is monotonic for the control
    points an easing curve uses, so a bisection is stable and cheap here.
    """
    if progress <= 0.0:
        return 0.0
    if progress >= 1.0:
        return 1.0
    if (x1, y1, x2, y2) == (0.0, 0.0, 1.0, 1.0):
        return progress

    def curve(t, a, b):
        # Bezier with p0 = 0 and p3 = 1.
        rest = 1.0 - t
        return 3.0 * rest * rest * t * a + 3.0 * rest * t * t * b + t * t * t

    low, high = 0.0, 1.0
    t = progress
    for _ in range(iterations):
        x = curve(t, x1, x2)
        if abs(x - progress) < 1e-6:
            break
        if x < progress:
            low = t
        else:
            high = t
        t = (low + high) / 2.0
    return curve(t, y1, y2)


def curve_for(name):
    """Control points for a preset name, defaulting to linear."""
    return PRESETS.get(name, PRESETS["Linear"])


def sample(name, count):
    """``count`` evenly spaced points along a named curve, for previewing."""
    points = curve_for(name)
    if count < 2:
        return []
    return [bezier(i / (count - 1), *points) for i in range(count)]


# --- transform interpolation ------------------------------------------------

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def decompose(matrix):
    """Split a 2x3 affine matrix into translation, rotation and scale.

    Matrix layout matches the document: [a, b, c, d, tx, ty] where the 2x2 is
    [[a, c], [b, d]]. Interpolating the decomposed parts keeps rotation rigid;
    interpolating a, b, c and d directly would shrink the object through the
    middle of a turn.
    """
    a, b, c, d, tx, ty = [float(v) for v in matrix]
    scale_x = math.hypot(a, b)
    if scale_x < 1e-12:
        # Degenerate: fall back to the other axis so a flat matrix still works.
        scale_x = 0.0
        scale_y = math.hypot(c, d)
        rotation = math.atan2(-c, d)
        shear = 0.0
    else:
        rotation = math.atan2(b, a)
        determinant = a * d - b * c
        scale_y = determinant / scale_x
        shear = (a * c + b * d) / scale_x
    return {
        "tx": tx,
        "ty": ty,
        "rotation": rotation,
        "scale_x": scale_x,
        "scale_y": scale_y,
        "shear": shear,
    }


def compose(parts):
    """Rebuild a 2x3 affine matrix from decomposed parts."""
    cos = math.cos(parts["rotation"])
    sin = math.sin(parts["rotation"])
    a = cos * parts["scale_x"]
    b = sin * parts["scale_x"]
    c = cos * parts.get("shear", 0.0) - sin * parts["scale_y"]
    d = sin * parts.get("shear", 0.0) + cos * parts["scale_y"]
    return [a, b, c, d, parts["tx"], parts["ty"]]


def interpolate_matrix(start, end, t):
    """Blend two affine matrices at ``t``.

    ``t`` runs from 0 to 1 in the ordinary case, but an easing curve is allowed
    to leave that range and this follows it: interpolating is an extrapolation
    past the ends, so an overshooting curve really goes beyond the second key
    and an anticipating one pulls back behind the first. Clamping here instead
    flattened every frame past the peak onto the end value, which read as the
    curve having thrown those frames away.
    """
    if t == 0.0:
        return [float(v) for v in start]
    if t == 1.0:
        return [float(v) for v in end]
    a, b = decompose(start), decompose(end)
    # Take the shorter way round, so a 350 degrees turn goes 20 degrees back
    # rather than almost all the way forward.
    delta = b["rotation"] - a["rotation"]
    while delta > math.pi:
        delta -= 2 * math.pi
    while delta < -math.pi:
        delta += 2 * math.pi
    blended = {
        "tx": a["tx"] + (b["tx"] - a["tx"]) * t,
        "ty": a["ty"] + (b["ty"] - a["ty"]) * t,
        "rotation": a["rotation"] + delta * t,
        "scale_x": a["scale_x"] + (b["scale_x"] - a["scale_x"]) * t,
        "scale_y": a["scale_y"] + (b["scale_y"] - a["scale_y"]) * t,
        "shear": a["shear"] + (b["shear"] - a["shear"]) * t,
    }
    return compose(blended)


def interpolate_color(start, end, t):
    """Blend two colour transforms component-wise."""
    if t <= 0.0:
        return list(start)
    if t >= 1.0:
        return list(end)
    return [round(a + (b - a) * t) for a, b in zip(start, end)]
