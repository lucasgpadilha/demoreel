"""Where things go in the frame, for every aspect ratio. Pure geometry: no drawing, no demo data.

The map is full bleed in every layout. The HUD floats over it and leaves a *focus* rectangle free: that is
the part of the frame the follow camera composes the action in. ``focus_full`` is the (larger) rectangle
the whole radar is fitted in when the camera is "full": there the map may run under the temporary pieces
(nameplate, kill counter). Three layouts are designed, picked from the frame's proportions:

* ``wide``   16:9 and other landscape frames. Spectator-HUD arrangement: scorebug top centre, kill feed top
  right, one roster hanging on each side edge, the player's plate bottom left, the kill counter bottom centre.
* ``tall``   9:16 for social apps. Text stays inside the band those apps leave free (they cover roughly the
  top 11 % and the bottom 18 % with their own UI); the map bleeds under both.
* ``square`` 1:1. Scorebug on top, the two rosters as columns at the bottom.

``s`` is the unit every size is multiplied by: 1.0 at 1280x720, 2.0 at 1080x1920, 1.5 at 1080x1080.
"""

from __future__ import annotations

from dataclasses import dataclass

ROW_PITCH = 32.0  # roster row + gap, in units
ROW_H = 30.0
SAFE_TOP_TALL = 220 / 1920
SAFE_BOTTOM_TALL = 340 / 1920


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h


@dataclass(frozen=True)
class Layout:
    kind: str  # wide | tall | square
    w: int
    h: int
    s: float
    focus: Rect
    focus_full: Rect
    counter_delay: float  # seconds into a clip before the kill counter shows (it shares its place with the plate)
    scorebug_cx: float
    scorebug_y: float  # top of the scorebug
    team_name_w: float  # room for a team name in the scorebug
    feed_x: float  # right edge of the kill feed
    feed_y: float
    feed_rows: int
    feed_name_w: float  # in units: where a long name is cut in the feed
    roster_w: float
    roster_left_x: float  # left edge of the left roster
    roster_right_x: float  # left edge of the right roster
    roster_bottom: float | None  # rosters grow upwards from here; None = centred vertically on roster_cy
    roster_cy: float
    plate_x: float
    plate_bottom: float  # the nameplate sits on this line
    counter_cx: float
    counter_y: float  # top of the kill-counter pips
    caption_y: float  # baseline of the caption
    logo_x: float
    logo_y: float
    logo_h: float
    letterbox: float  # height of each slow-motion bar

    def roster_top(self, rows: int) -> float:
        height = max(1, rows) * ROW_PITCH * self.s - (ROW_PITCH - ROW_H) * self.s
        if self.roster_bottom is not None:
            return self.roster_bottom - height
        return self.roster_cy - height / 2


def layout_kind(width: int, height: int) -> str:
    ratio = width / max(1, height)
    if ratio >= 1.25:
        return "wide"
    if ratio <= 0.7:  # 4:5 feed posts have no app UI over them: they get the square arrangement
        return "tall"
    return "square"


def choose_layout(width: int, height: int) -> Layout:
    kind = layout_kind(width, height)
    w, h = float(width), float(height)
    if kind == "wide":
        s = min(h / 720, w / 960)
        narrow = w / s < 1180  # 4:3-ish: no room for the feed beside the scorebug
        roster_w = 232 * s
        top = 76 * s if not narrow else 96 * s
        return Layout(
            kind, width, height, s,
            focus=Rect(roster_w + 14 * s, top, w - 2 * (roster_w + 14 * s), h - top - 84 * s),
            focus_full=Rect(roster_w + 14 * s, top - 8 * s, w - 2 * (roster_w + 14 * s), h - top - 6 * s),
            counter_delay=0.4, scorebug_cx=w / 2, scorebug_y=14 * s, team_name_w=150,
            feed_x=w - 18 * s, feed_y=(16 if not narrow else 96) * s, feed_rows=5, feed_name_w=170,
            roster_w=roster_w, roster_left_x=0, roster_right_x=w - roster_w, roster_bottom=None,
            roster_cy=h / 2 + 8 * s,
            plate_x=34 * s, plate_bottom=h - 44 * s,
            counter_cx=w / 2, counter_y=h - 50 * s, caption_y=h - 142 * s,
            logo_x=26 * s, logo_y=20 * s, logo_h=40 * s, letterbox=h * 0.06)
    if kind == "tall":
        s = min(w / 540, h / 960)
        safe_top, safe_bottom = h * SAFE_TOP_TALL, h * SAFE_BOTTOM_TALL
        bug_y = safe_top + 4 * s
        roster_bottom = h - safe_bottom - 2 * s
        roster_top = roster_bottom - 5 * ROW_PITCH * s
        focus_top = bug_y + 132 * s  # under the scorebug and the first rows of the kill feed
        return Layout(
            kind, width, height, s,
            focus=Rect(0, focus_top, w, roster_top - 112 * s - focus_top),
            focus_full=Rect(0, bug_y + 68 * s, w, roster_top - 4 * s - (bug_y + 68 * s)),
            counter_delay=3.8, scorebug_cx=w / 2, scorebug_y=bug_y, team_name_w=112,
            feed_x=w - 12 * s, feed_y=bug_y + 80 * s, feed_rows=3, feed_name_w=112,
            roster_w=262 * s, roster_left_x=0, roster_right_x=w - 262 * s, roster_bottom=roster_bottom,
            roster_cy=0,
            plate_x=20 * s, plate_bottom=roster_top - 10 * s,
            counter_cx=w / 2, counter_y=roster_top - 30 * s, caption_y=roster_top - 124 * s,
            logo_x=16 * s, logo_y=bug_y + 80 * s, logo_h=38 * s, letterbox=h * 0.045)
    s = min(w, h) / 720
    roster_bottom = h - 14 * s
    roster_top = roster_bottom - 5 * ROW_PITCH * s
    return Layout(
        kind, width, height, s,
        focus=Rect(8 * s, 90 * s, w - 16 * s, roster_top - 112 * s - 90 * s),
        focus_full=Rect(8 * s, 84 * s, w - 16 * s, roster_top - 6 * s - 84 * s),
        counter_delay=3.8, scorebug_cx=w / 2, scorebug_y=14 * s, team_name_w=128,
        feed_x=w - 14 * s, feed_y=94 * s, feed_rows=4, feed_name_w=150,
        roster_w=248 * s, roster_left_x=0, roster_right_x=w - 248 * s, roster_bottom=roster_bottom, roster_cy=0,
        plate_x=22 * s, plate_bottom=roster_top - 10 * s,
        counter_cx=w / 2, counter_y=roster_top - 30 * s, caption_y=roster_top - 124 * s,
        logo_x=18 * s, logo_y=18 * s, logo_h=36 * s, letterbox=h * 0.05)
