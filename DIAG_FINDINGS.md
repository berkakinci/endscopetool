# DIAG findings: window-close detection

Temporary working log. Not intended for commit (don't `git add`).

Branch: `berk-diag-window-behavior` (commit `3eed6cb`)
Experiment: interactive 4-step matrix (BASELINE → MINIMIZE → ALT_TAB_AWAY → CLOSE_X)
Properties probed each frame: VISIBLE, AUTOSIZE, FULLSCREEN, ASPECT_RATIO, OPENGL, TOPMOST.

## Background

After PR #5 merged, the maintainer added an `AUTOSIZE == -1` clause to
the close-check to fix window auto-close on Wayland. On macOS that
clause fires from frame one, so the tool quits immediately at launch.

Upstream close-check as of `a5fa735`:
```python
elif (
    key == ord("q")
    or key == 27
    or cv2.getWindowProperty(win_name, cv2.WND_PROP_VISIBLE) == 0
    or cv2.getWindowProperty(win_name, cv2.WND_PROP_AUTOSIZE) == -1
):
```

## Platform results

### macOS (Tahoe, arm64) — Python 3.12, `opencv-python` via pip

Run date: 2026-05-10

Startup probe:
```
VISIBLE=1.0, AUTOSIZE=-1.0, FULLSCREEN=0.0,
ASPECT_RATIO=-1.0, OPENGL=-1.0, TOPMOST=0.0
```
`setWindowProperty(AUTOSIZE, 0)` — silently ignored, stays `-1.0`.

| Step | VISIBLE | AUTOSIZE | Other probed props | Notes |
|---|---|---|---|---|
| 1 BASELINE | 1.0 | -1.0 | all unchanged | baseline |
| 2 MINIMIZE (hidden) | 0.0 | -1.0 | unchanged | yellow button hides window |
| 2 MINIMIZE (restored) | 1.0 | -1.0 | unchanged | restored via Dock icon |
| 3 ALT_TAB_AWAY | (no event) | (no event) | (no event) | focus != visibility |
| 4 CLOSE_X | — | — | — | **red button is greyed out** — no close path via window chrome |

Conclusion for macOS:
- `VISIBLE == 0` is identical between MINIMIZE and (hypothetical) close.
  Unsafe as close signal — false-positive on minimize.
- `AUTOSIZE == -1` fires from frame one. Unusable as close signal.
- `FULLSCREEN`, `ASPECT_RATIO`, `OPENGL`, `TOPMOST` never change.
- User's only close path is `q` / Esc.

### Windows 11 x86 — Python 3.14, `opencv-python` 4.13.0

Run date: 2026-05-10

Startup probe:
```
VISIBLE=1.0, AUTOSIZE=0.0, FULLSCREEN=0.0,
ASPECT_RATIO=1.0, OPENGL=-1.0, TOPMOST=0.0
```
`setWindowProperty(AUTOSIZE, 0)` — succeeds (value was already 0).

| Step | VISIBLE | AUTOSIZE | Other probed props | Notes |
|---|---|---|---|---|
| 1 BASELINE | 1.0 | 0.0 | ASPECT_RATIO=1.0, rest=0/-1/0 | baseline |
| 2 MINIMIZE (minimized) | **1.0** | 0.0 | ASPECT_RATIO **1.0 → -1.0** | VISIBLE stays 1! Only ASPECT_RATIO flips |
| 2 MINIMIZE (restored) | 1.0 | 0.0 | ASPECT_RATIO -1.0 → 1.0 | back to baseline |
| 3 ALT_TAB_AWAY | (no event) | (no event) | (no event) | focus != visibility |
| 4 CLOSE_X | ? | **raised** | — | cv2.error "NULL window" from `cvGetPropWindowAutoSize_W32`; window destroyed synchronously |

Key Windows observations:
- **MINIMIZE does not drop VISIBLE to 0.** Only `ASPECT_RATIO` moved (1.0 → -1.0 → 1.0). This means `VISIBLE == 0` is safe on Windows — minimize won't trigger it.
- **CLOSE_X destroys the window synchronously.** The next property read raises a NULL-window exception. Our experiment hit this at AUTOSIZE (second probe in tuple). VISIBLE (first probe) returned something without raising — we didn't capture its value because the tuple comprehension died before printing, but the upstream `VISIBLE == 0` clause would short-circuit on VISIBLE's return and never reach AUTOSIZE.
- Implication: upstream `VISIBLE == 0 or AUTOSIZE == -1` works on Windows *because of* Python short-circuit evaluation. If the operands were reordered or replaced with an `any()` over both reads, it would crash.

Conclusion for Windows:
- `VISIBLE == 0` is a safe, sufficient close signal (doesn't trip on MINIMIZE).
- `AUTOSIZE == -1` is irrelevant — stays 0 throughout life and raises after destroy. Including it is harmless only because VISIBLE==0 short-circuits first.

### Linux / Wayland — (not tested here; per maintainer)

From commit `4357052` ("re-fix window auto-closing on wayland"), both
`VISIBLE == 0` and `AUTOSIZE == -1` are needed to catch the close.
Unknown: whether Wayland minimize also trips VISIBLE == 0 (i.e., false
positive on minimize like macOS).

## Revised cross-platform proposal

| Platform | Open | Minimize | Close | `VISIBLE < 1` catches close? | `VISIBLE < 1` false-positive on minimize? |
|---|---|---|---|---|---|
| macOS (Cocoa) | 1 | **0** | (button disabled) | — | **yes** |
| Windows | 1 | 1 (stays) | 0 then raise | yes | no |
| Hyprland / Wayland (per Daniel) | ~1 | unknown | -1 | yes | unknown |

## Cross-platform summary

| Signal | macOS | Windows | Linux/Wayland (per maintainer) |
|---|---|---|---|
| `VISIBLE == 0` on MINIMIZE | **yes (false +)** | no | unknown |
| `VISIBLE == 0` on CLOSE | yes (but close not reachable) | yes | needs AUTOSIZE too |
| `AUTOSIZE == -1` at startup | **yes (false +)** | no | no |
| `AUTOSIZE == -1` on CLOSE | (close not reachable) | raises (post-destroy) | yes (signal) |
| User-reachable close path | none (button greyed) | X button | X button |

## Key findings (tabulated)

| # | Finding | Evidence | Implication |
|---|---|---|---|
| 1 | Windows MINIMIZE does **not** drop VISIBLE to 0. Only `ASPECT_RATIO` changes (`1.0` ↔ `-1.0`). | Windows step 2 rows above: VISIBLE stays 1.0 through minimize + restore. | `VISIBLE == 0` is a clean close-only signal on Windows, unlike macOS where it's ambiguous with minimize. |
| 2 | Windows CLOSE_X destroys the window synchronously; subsequent property reads raise `cv2.error: NULL window`. | Windows step 4: traceback from `cvGetPropWindowAutoSize_W32` via the AUTOSIZE probe, immediately after clicking X. | Upstream `VISIBLE == 0 or AUTOSIZE == -1` works **only because Python short-circuits**: VISIBLE returns 0 → True → AUTOSIZE never evaluated. Any refactor that changes evaluation order (e.g., `any([...])` over both reads) would crash on Windows. |
| 3 | macOS `WND_PROP_AUTOSIZE` is effectively read-only and stuck at `-1.0` for any `WINDOW_GUI_NORMAL` window. | macOS startup probe AUTOSIZE=-1.0; `setWindowProperty(AUTOSIZE, 0)` returned with value still -1.0. | `AUTOSIZE == -1` cannot be used as a close signal on macOS. The close button itself is disabled there, so `q`/Esc is the only viable exit path. |
| 4 | Linux/Wayland needs both clauses per the maintainer's fix (`4357052`), but whether MINIMIZE is distinguished from CLOSE was not tested here. | Maintainer commit message only. | Open question before proposing a cross-platform fix. |
| 5 | Properties `FULLSCREEN`, `OPENGL`, `TOPMOST` never changed on either platform across BASELINE / MINIMIZE / ALT_TAB / (macOS: CLOSE blocked). | Both platforms' per-frame logs: only VISIBLE (macOS on minimize) and ASPECT_RATIO (Windows on minimize) ever moved. | No hidden alternate signal to mine. |

## Proposed fix

Platform-gate the property-based clauses:

```python
elif key == ord("q") or key == 27:
    print("window closed")
    break
elif sys.platform == "win32" and (
    cv2.getWindowProperty(win_name, cv2.WND_PROP_VISIBLE) == 0
):
    print("window closed")
    break
elif sys.platform.startswith("linux") and (
    cv2.getWindowProperty(win_name, cv2.WND_PROP_VISIBLE) == 0
    or cv2.getWindowProperty(win_name, cv2.WND_PROP_AUTOSIZE) == -1
):
    print("window closed")
    break
# macOS (darwin): no property-based close clause.
# VISIBLE == 0 fires on MINIMIZE (false positive);
# AUTOSIZE == -1 fires from frame one; close-X is disabled anyway.
# q/Esc are the only exit path on macOS.
```

## Open questions for the maintainer PR

- Does Wayland's MINIMIZE trip `VISIBLE == 0` too? If so, the Linux
  branch has the same false-positive problem as macOS and needs a
  different predicate (or accepts that minimize = close).

## Addendum (2026-05-16): Proposed fix superseded

The platform-gated approach above is wrong for GTK.  Source inspection
(see "Follow-up" section below) revealed:

- `VISIBLE` is always -1 on GTK (unimplemented) — `== 0` never fires.
- `AUTOSIZE == -1` works only as a "window not found" sentinel, not a
  property change.
- `getWindowImageRect` raises reliably on both GTK and Windows after close.

The actual fix is simpler: call `_is_window_closed()` (which uses
`getWindowImageRect`) **before** `imshow`, since `imshow` silently recreates
destroyed windows on GTK.  No platform-gating needed.
- Would the maintainer accept `sys.platform`-gated clauses, or prefer
  a different structure (e.g., a single `is_window_closed(name)`
  helper)?

## Final summary

All the interesting behavior reduces to one call — `cv2.getWindowProperty(win_name, cv2.WND_PROP_VISIBLE)` — observed across the three window states that matter.

| Platform | Window open | Window minimized | Window closed |
|---|---|---|---|
| macOS (Cocoa) | `1.0` | `0.0` | unreachable (close button disabled with `WINDOW_GUI_NORMAL`) |
| Windows | `1.0` | `1.0` (stays) | raises `cv2.error: (-27) NULL window` |
| Wayland / Hyprland (per Daniel) | ~`1` (inferred) | unknown | `-1` (inferred from the fact that `VISIBLE < 1` worked for him on close) |

Reading the table: the close state is **always either a negative value or a raise**, and minimize is **always a non-negative value**. That's a single-predicate decision:

```python
try:
    vis = cv2.getWindowProperty(win_name, cv2.WND_PROP_VISIBLE)
except cv2.error:
    vis = -1  # Windows raises after destroy; treat as closed
closed = vis < 0
```

Supporting facts that justify dropping `AUTOSIZE` from the check:

- `AUTOSIZE` describes the window's resize mode (`WINDOW_AUTOSIZE` vs `WINDOW_NORMAL`), not a lifecycle signal. Using it to detect close was a side-effect observation on one platform.
- On macOS it is stuck at `-1.0` from frame one and `setWindowProperty` is a no-op, so it carries no information.
- On Windows it is `0.0` in every live state and raises after close, i.e. it adds no signal that `VISIBLE` doesn't already carry.
- On Wayland / Hyprland its `== -1` value post-destroy is coincidental — `VISIBLE < 0` catches the same condition.

Rabbit-hole note: most of the matrix-based investigation was unnecessary. Once we asked "why not wait for a raise on destroy," the `try/except` + `VISIBLE < 0` predicate fell out immediately. The experiment was still worthwhile for producing the evidence table and for invalidating the maintainer's `AUTOSIZE == -1` hypothesis as a *designed* signal.

---

## Follow-up: Source code inspection (2026-05-16)

Maintainer reported window re-opens on close (Hyprland/GTK3).  Inspected
OpenCV 4.x HighGUI source to confirm behavior.

Source refs (all `4.x` branch, stable across 4.10–4.12+):
- [window_gtk.cpp](https://github.com/opencv/opencv/blob/4.x/modules/highgui/src/window_gtk.cpp)
- [window_wayland.cpp](https://github.com/opencv/opencv/blob/4.x/modules/highgui/src/window_wayland.cpp)
- [window.cpp](https://github.com/opencv/opencv/blob/4.x/modules/highgui/src/window.cpp) (legacy C-API dispatcher)

### Findings

**VISIBLE on GTK: hard-coded -1 (unimplemented).**
[`window.cpp` WND_PROP_VISIBLE case](https://github.com/opencv/opencv/blob/4.x/modules/highgui/src/window.cpp):
GTK is not listed among QT/Win32/Cocoa — falls to `#else return -1`.
The maintainer's "VISIBLE is always -1 on Wayland" is really "always -1 on GTK,
any display server."

**AUTOSIZE on GTK: returns -1 when window not found.**
[`cvGetPropWindowAutoSize_GTK`](https://github.com/opencv/opencv/blob/4.x/modules/highgui/src/window_gtk.cpp)
does `icvFindWindowByName` → if null, `return -1`.  This is why the
maintainer's `AUTOSIZE == -1` check worked — it's a "not found" sentinel.

**getWindowImageRect on GTK: raises when window not found.**
[`cvGetWindowRect_GTK`](https://github.com/opencv/opencv/blob/4.x/modules/highgui/src/window_gtk.cpp)
does `icvFindWindowByName` → if null, `CV_Error("NULL window")`.  Same as
Windows (raises after destroy).

**imshow on GTK: recreates destroyed windows.**
[`cvShowImage`](https://github.com/opencv/opencv/blob/4.x/modules/highgui/src/window_gtk.cpp)
does `icvFindWindowByName` → if null, calls `cvNamedWindow` to recreate.
This is the root cause of the re-open bug.

**icvOnClose on GTK: removes window from internal list.**
[`icvOnClose`](https://github.com/opencv/opencv/blob/4.x/modules/highgui/src/window_gtk.cpp)
→ `icvDeleteWindow_` → erases from `g_windows`.

**Native Wayland backend: close is a no-op.**
[`handle_toplevel_close`](https://github.com/opencv/opencv/blob/4.x/modules/highgui/src/window_wayland.cpp)
is `CV_UNUSED(data); CV_UNUSED(surface);` — window stays alive.  Irrelevant
for the maintainer (uses GTK3 via `enableGtk3 = true`).

### Revised table

| Call | GTK alive | GTK after close | Windows alive | Windows after close | macOS |
|---|---|---|---|---|---|
| `getWindowProperty(VISIBLE)` | -1 (always) | -1 (always) | 1.0 | raises | 1.0 / 0.0 on minimize |
| `getWindowProperty(AUTOSIZE)` | 0 (NORMAL) | -1 (not found) | 0.0 | raises | -1 (always) |
| `getWindowImageRect` | returns rect | **raises** | returns rect | **raises** | returns rect / N/A |
| `imshow` | shows image | **recreates** | shows image | **recreates** | shows image |

### Why the try/except didn't catch it

The first cv2 call each iteration is `imshow`, which doesn't raise — it
recreates.  By the time anything else runs, the window is alive again.

### Fix

Check `_is_window_closed()` before `imshow`.  `getWindowImageRect` raises while
the window is still absent → helper returns True → break before recreation.

### Note on _is_window_closed stanzas

- Stanza 1 (`getWindowImageRect` raise): does the actual work on Windows + GTK.
- Stanza 2 (`VISIBLE < 0`): dead code in practice (stanza 1 fires first on
  both platforms; on macOS close is unreachable).  Kept as defensive fallback.
