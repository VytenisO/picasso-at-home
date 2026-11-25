"""Velocity planning for the plotter's moves: slow down into sharp corners and U-turns, speed back up after.

Without it every leg ran at full speed from a standstill and reversed at full speed, which jerks the carriage
(seen above about 20 mm/s on back-and-forth hatching) and costs position. Here a path (pen down) or a single
travel move starts and ends at rest; in between, the speed at each corner is limited by how sharp the turn is
(junction deviation, as in GRBL), and the speed between corners changes at no more than ACCEL.

Fixed on purpose, not a setting: these are properties of the machine, not of a pen or a job.
"""
import math

ACCEL = 400.0           # mm/s^2: largest change of speed along the path
JUNCTION_DEV = 0.02     # mm: how far the path may cut a corner at speed; smaller = slower corners
RAMP_DT = 0.025         # s: a speed ramp is sent as legs this long (constant speed within each)


def corner_speed(u, w, v_max):
    """Highest speed through a corner from direction u into direction w (unit vectors)."""
    cos_t = -(u[0] * w[0] + u[1] * w[1])          # 1 = a full reversal, -1 = straight on
    if cos_t <= -0.999999:
        return v_max
    sin_half = math.sqrt(max(0.0, (1.0 - cos_t) / 2.0))
    if sin_half <= 1e-9:
        return 0.0
    if sin_half >= 0.999999:
        return v_max
    return min(v_max, math.sqrt(ACCEL * JUNCTION_DEV * sin_half / (1.0 - sin_half)))


def plan(points, v_max):
    """points: [(x, y)] in mm, consecutive duplicates removed. -> speed at each point, starting and ending at 0."""
    n = len(points)
    if n < 2:
        return [0.0] * n
    lens, dirs = [], []
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        d = math.hypot(x1 - x0, y1 - y0)
        lens.append(d)
        dirs.append(((x1 - x0) / d, (y1 - y0) / d))
    v = [0.0] * n
    for i in range(1, n - 1):
        v[i] = corner_speed(dirs[i - 1], dirs[i], v_max)
    for i in range(n - 2, -1, -1):              # backward: able to slow down in time for what comes next
        v[i] = min(v[i], math.sqrt(v[i + 1] ** 2 + 2 * ACCEL * lens[i]))
    for i in range(1, n):                       # forward: able to get up to speed
        v[i] = min(v[i], math.sqrt(v[i - 1] ** 2 + 2 * ACCEL * lens[i - 1]))
    v[0] = v[-1] = 0.0
    return v


def _profile(length, v0, v1, v_max):
    """Trapezoid over one leg -> (peak speed, accel time, cruise time, decel time)."""
    vp = min(v_max, math.sqrt(max(0.0, (2 * ACCEL * length + v0 * v0 + v1 * v1) / 2)))
    vp = max(vp, v0, v1)
    d_acc = (vp * vp - v0 * v0) / (2 * ACCEL)
    d_dec = (vp * vp - v1 * v1) / (2 * ACCEL)
    d_cru = max(0.0, length - d_acc - d_dec)
    return vp, (vp - v0) / ACCEL, (d_cru / vp if vp > 0 else 0.0), (vp - v1) / ACCEL


def leg_time(length, v0, v1, v_max):
    _, ta, tc, td = _profile(length, v0, v1, v_max)
    return ta + tc + td


def pieces(length, v0, v1, v_max):
    """One leg as constant-speed pieces -> [(distance at the piece's end, duration)]. The ramps are cut into
    RAMP_DT pieces; the cruise is one piece (the caller splits long ones)."""
    vp, ta, tc, td = _profile(length, v0, v1, v_max)
    out, s = [], 0.0
    if ta > 0:
        k = max(1, math.ceil(ta / RAMP_DT - 1e-9))
        for j in range(1, k + 1):
            t = ta * j / k
            out.append((v0 * t + ACCEL * t * t / 2, ta / k))
        s = out[-1][0]
    if tc > 0:
        s += vp * tc
        out.append((s, tc))
    if td > 0:
        k = max(1, math.ceil(td / RAMP_DT - 1e-9))
        base = s
        for j in range(1, k + 1):
            t = td * j / k
            out.append((base + vp * t - ACCEL * t * t / 2, td / k))
    if not out:
        return [(length, length / max(v_max, 1e-6))]
    scale = length / out[-1][0] if out[-1][0] > 0 else 1.0       # land exactly on the leg's end
    return [(d * scale, dt) for d, dt in out]


def path_time(points, v_max):
    """Seconds to draw a polyline from rest to rest."""
    pts = [p for i, p in enumerate(points) if i == 0 or p != points[i - 1]]
    v = plan(pts, v_max)
    return sum(leg_time(math.dist(a, b), v[i], v[i + 1], v_max) for i, (a, b) in enumerate(zip(pts, pts[1:])))
