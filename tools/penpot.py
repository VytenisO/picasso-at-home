"""Pen-depth knob: a pot on one of the EBB's analog inputs sets how far the pen goes down.

Stock firmware reads it (AC to switch the channel on, A to read), so no firmware change. machine.json "pen_pot":
  enabled      use the knob; off = servo.pen_down as before
  channel      analog channel (A1 on the side header = 1)
  adc_shallow  reading at the shallow end of the knob: pen-down = pen-up (the pen does not reach the paper)
  adc_deep     reading at the deep end: pen-down = pen-up moved by depth_max toward servo.pen_down
  depth_max    servo units, the deepest the knob can set
Readings in between scale linearly. A shallow end above the deep end is fine (a knob wired the other way round).
Several readings that disagree (a loose wire, a floating input), or one far outside the two ends, mean the knob is
not trusted and servo.pen_down is used instead. Stdlib only.
"""
import re

SERVO_LO, SERVO_HI = 7500, 22000   # same servo limits as plot_svg.py and pen_test.py
SPREAD_MAX = 12      # ADC counts: readings further apart than this mean a loose wire
RANGE_SLACK = 40     # ADC counts past either end before a reading counts as a fault
STEP = 6             # servo units: smaller changes are noise, not a turn of the knob
READS = 3            # readings per look
# How often to look. Each look is a few serial round trips, so the knob is read rarely until it moves, then often
# for a while: quick to follow a turn, nearly free when nobody touches it.
JOB_SLOW = 5.0       # s between looks during a job (at a pen-down)
JOB_FAST = 0.5       # s between looks during a job right after a turn
IDLE_SLOW = 1.0      # s between looks between jobs
FAST_FOR = 10.0      # s of fast looks after the last change


def defaults(servo):
    swing = abs(int(servo["pen_down"]) - int(servo["pen_up"]))
    return {"enabled": False, "channel": 1, "adc_shallow": 0, "adc_deep": 640, "depth_max": round(1.5 * swing) or 1000}


def settings(cfg):
    """-> the full pen_pot block (defaults filled in), enabled or not."""
    return {**defaults(cfg["servo"]), **(cfg.get("pen_pot") or {})}


def active(cfg):
    """-> the pen_pot block when the knob is in use, else None."""
    p = settings(cfg)
    return p if p.get("enabled") else None


def parse(line, channel):
    """'A,0:582,1:330,11:293' -> 330 for channel 1, None when the channel is not in it."""
    for ch, val in re.findall(r"(\d+):(\d+)", line or ""):
        if int(ch) == int(channel):
            return int(val)
    return None


def judge(readings, pot):
    """readings: ADC values from one look. -> (value, None) or (None, why the knob is not trusted)."""
    vals = [r for r in readings if r is not None]
    if len(vals) < len(readings) or not vals:
        return None, "no reading from the knob's channel"
    if max(vals) - min(vals) > SPREAD_MAX:
        return None, f"unsteady readings ({min(vals)} to {max(vals)}): a loose wire?"
    v = round(sum(vals) / len(vals))
    lo, hi = sorted((pot["adc_shallow"], pot["adc_deep"]))
    if v < lo - RANGE_SLACK or v > hi + RANGE_SLACK:
        return None, f"reading {v} is outside the knob's range ({lo} to {hi})"
    return v, None


def pen_down(servo, pot, adc):
    """Servo pen-down value for a reading."""
    sh, dp = pot["adc_shallow"], pot["adc_deep"]
    frac = 0.0 if dp == sh else min(1.0, max(0.0, (adc - sh) / (dp - sh)))
    up = int(servo["pen_up"])
    sign = -1 if int(servo["pen_down"]) < up else 1
    return int(min(SERVO_HI, max(SERVO_LO, round(up + sign * frac * pot["depth_max"]))))
