#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
#
# Python implementation of the endscopetool (sic!) Android application used for the Vitcoco ear wax remover camera thingy.
#
# Original version released at https://gist.github.com/RaphaelWimmer/5bcb286414e6cd38ed38724f9a6a6129
# under CC0 / Public Domain (0) 2023 Raphael Wimmer.
# Contributions by https://github.com/Aghei2 and https://gist.github.com/jamaggs.
#
# v0.1.0
# reverse-engineered using a packet capture log - this means that I have no idea what all those magic numbers mean
# and whether there are further features that might be supported by the hardware
# usage: first connect to the 'softish-XXXX' wifi, then run this script. Check code for keyboard shortcuts.

import cv2
import sys
import numpy as np
import trio
import argparse
from datetime import datetime
from PIL import Image
from io import BytesIO
from urllib.parse import parse_qs
from transports import AsyncDatagramTransport, UdpDatagramTransport

cv2.setNumThreads(1)

debug = False


class EndscopeConnection:
    def __init__(
        self, meta_channel: AsyncDatagramTransport, vid_channel: AsyncDatagramTransport
    ):
        self.meta = meta_channel
        self.vid = vid_channel

    async def query_battery(self) -> float | None:
        data: bytes = "type=1001\x0a".encode()
        await self.meta.send(data)
        reply = await self.meta.recv()
        received_data: str = reply.decode()
        return get_battery_level(received_data)

    async def set_brightness(self, level: int) -> int | None:
        data = f"type=1003&value={level}\x0a".encode()
        await self.meta.send(data)
        reply = await self.meta.recv()
        if debug:
            print(f"brightness response: {reply.hex(' ')}")
        try:
            params = parse_qs(reply.rstrip(b"\xaa").decode())
            return int(params["value"][0])
        except (KeyError, IndexError, ValueError):
            return None

    async def get_system_info(self) -> str:
        data: bytes = "type=1002\x0a".encode()
        await self.meta.send(data)
        reply = await self.meta.recv()
        return reply.decode()

    async def start_video(self) -> None:
        # three times according to captured traffic, but works fine sent once?
        data = "\x20\x36\x00\x02".encode()
        await self.vid.send(data)

    async def stop_video(self) -> None:
        data = "\x20\x37".encode()
        await self.vid.send(data)

    async def recv_video(self) -> bytes:
        return await self.vid.recv()

    async def aclose(self) -> None:
        await self.meta.aclose()
        await self.vid.aclose()


def get_battery_level(query_string: str) -> float | None:
    """
    Extracts the battery level from a string like 'type=2001&data=23'.
    Returns an float in the range 0-1, or None if not found or invalid.
    """
    try:
        params = parse_qs(query_string)
        return int(params["data"][0]) / 100
    except (KeyError, IndexError, ValueError):
        print(f"failed to extract battery from data: ${query_string}")
        return None


def draw_battery(
    img: cv2.typing.MatLike,
    x: int,
    y: int,
    width: int,
    height: int,
    level: float,
    thickness: int,
) -> None:
    """
    Draw a battery icon at (x, y) with given width, height and charge level (0 to 1).
    """
    # Clamp level to [0, 1]
    level = max(0, min(level, 1.0))

    # Colors
    border_color = (255, 255, 255)
    fill_color = (0, 255, 0) if level > 0.3 else (0, 0, 255)  # Red if low battery

    # Draw battery outline
    cv2.rectangle(img, (x, y), (x + width, y + height), border_color, thickness)

    # Draw battery tip
    tip_width = int(width * 0.08)
    tip_x = x + width
    tip_y = y + int(height * 0.3)
    tip_height = int(height * 0.4)
    cv2.rectangle(
        img, (tip_x, tip_y), (tip_x + tip_width, tip_y + tip_height), border_color, -1
    )

    # Fill battery level
    fill_width = int((width - 4) * level)
    cv2.rectangle(
        img, (x + 2, y + 2), (x + 2 + fill_width, y + height - 2), fill_color, -1
    )


def absolute_frame_from_raw(raw_frame: int, latest_abs_frame: int) -> int:
    # Find the multiple of 256 that makes raw_frame closest to latest_abs_frame
    base = (latest_abs_frame // 256) * 256
    candidates = [base - 256 + raw_frame, base + raw_frame, base + 256 + raw_frame]
    # pick the candidate closest to latest_abs_frame
    abs_frame = min(candidates, key=lambda x: abs(x - latest_abs_frame))
    return abs_frame


async def run_app(conn: EndscopeConnection, buffer_size: int) -> None:
    brightness = 100
    win_name = "Video Stream"
    firstframe = True
    global debug

    try:
        # get system info
        received_data = await conn.get_system_info()
        print("Received data:", received_data)

        battery_level: float | None = await conn.query_battery()
        print(f"Battery level: {battery_level}")

        # three times according to captured traffic
        await conn.start_video()

        # set led brightness to 100%
        print("Brightness: ", await conn.set_brightness(100))

        cv2.namedWindow(win_name, flags=cv2.WINDOW_GUI_NORMAL)

        # =====================================================================
        # DIAG EXPERIMENT: window-close detection on macOS Cocoa backend
        # ---------------------------------------------------------------------
        # Context:
        #   - PR #5 merged. Maintainer added `WND_PROP_AUTOSIZE == -1` clause
        #     to close-check to fix Wayland. On macOS that clause fires from
        #     frame one, so the tool quits immediately at startup.
        #   - Previous experiment: setWindowProperty(AUTOSIZE, 0) silently
        #     fails on macOS (value stays -1). So AUTOSIZE is unusable as a
        #     close signal on macOS.
        #   - Current fallback would be `VISIBLE == 0`, but preliminary
        #     observation suggests that trips on *minimize* too, not only on
        #     close. If so, neither upstream clause reliably distinguishes
        #     close from minimize on macOS.
        #
        # Goal of this experiment:
        #   Find a per-frame property signature that uniquely identifies
        #   "user closed the window" (red X) and does NOT fire on minimize,
        #   restore, focus change, or help-window toggle.
        #
        # The script walks through a scripted action matrix. Steps advance on
        # SPACE. Each DIAG event is tagged with the current step number.
        # =====================================================================

        # The scripted steps. Each entry: (short label, user instruction).
        _exp_steps = [
            ("BASELINE",
             "Window should be visible at default size. Observe the first "
             "DIAG event, then focus the preview window and press SPACE."),
            ("MINIMIZE",
             "Minimize the window (macOS: yellow button; Windows: underscore "
             "button). Then restore it and press SPACE with the window focused."),
            ("ALT_TAB_AWAY",
             "Switch focus AWAY from the window (macOS: Cmd-Tab; Windows: "
             "Alt-Tab). Then switch back and press SPACE."),
            ("CLOSE_X",
             "Click the CLOSE button on the window (macOS: red; Windows: X). "
             "NOTE macOS: observed greyed-out / disabled — record if so and "
             "press 'q' to exit. Windows: expect the window to go away; if the "
             "loop survives, press 'q'."),
        ]

        # Findings recorded from 2026-05-10 macOS run (Python 3.12, opencv pip):
        #   post-namedWindow: {'VISIBLE': 1.0, 'AUTOSIZE': -1.0,
        #                      'FULLSCREEN': 0.0, 'ASPECT_RATIO': -1.0,
        #                      'OPENGL': -1.0, 'TOPMOST': 0.0}
        #   setWindowProperty(AUTOSIZE, 0): silently ignored, stays -1.
        #   MINIMIZE: VISIBLE goes 1->0 while hidden, 0->1 on restore.
        #   ALT_TAB_AWAY: no property change (focus != visibility).
        #   CLOSE_X: red button greyed out; no close path from window chrome.
        #   Conclusion for macOS: only 'q'/Esc work; VISIBLE==0 unreliable
        #   (same value for minimize), AUTOSIZE==-1 fires from frame one.
        # Goal on Windows: fill in the same rows and decide whether the
        # upstream VISIBLE==0 / AUTOSIZE==-1 clauses fire on CLOSE_X without
        # also firing on MINIMIZE.

        def _print_banner():
            print("\n" + "=" * 72)
            print("DIAG EXPERIMENT: window-close detection on macOS")
            print("Advance through steps by pressing SPACE with the preview "
                  "window focused.")
            print("All steps:")
            for i, (label, instr) in enumerate(_exp_steps, 1):
                print(f"  {i}. {label}: {instr}")
            print("=" * 72 + "\n")

        _print_banner()

        # DIAG: one-shot probe of properties right after namedWindow
        _probes = [
            ("VISIBLE", cv2.WND_PROP_VISIBLE),
            ("AUTOSIZE", cv2.WND_PROP_AUTOSIZE),
            ("FULLSCREEN", cv2.WND_PROP_FULLSCREEN),
            ("ASPECT_RATIO", cv2.WND_PROP_ASPECT_RATIO),
            ("OPENGL", cv2.WND_PROP_OPENGL),
            ("TOPMOST", cv2.WND_PROP_TOPMOST),
        ]
        print("DIAG post-namedWindow:", {
            name: cv2.getWindowProperty(win_name, prop) for name, prop in _probes
        })
        # Try to initialize AUTOSIZE to 0 — prior experiment showed this is a
        # no-op on macOS Cocoa, but recording the before/after leaves a clear
        # audit trail in the log.
        try:
            cv2.setWindowProperty(win_name, cv2.WND_PROP_AUTOSIZE, 0)
            _after = cv2.getWindowProperty(win_name, cv2.WND_PROP_AUTOSIZE)
            print(f"DIAG after setWindowProperty(AUTOSIZE, 0) AUTOSIZE={_after} "
                  f"(expected 0 if set honored, -1 if ignored)")
        except cv2.error as e:
            print(f"DIAG setWindowProperty raised: {e}")

        # Build help image once
        help_lines = [
            "Keyboard shortcuts:",
            "",
            "  1/2/3/4  Lock rotation 0/90/180/270",
            "  r        Unlock rotation (use sensor)",
            "  +/-      Brightness up/down 10%",
            "  f        Toggle full frame / circle",
            "  w        Save snapshot (timestamped .jpg)",
            "  d        Toggle debug output",
            "  h        Toggle this help",
            "  q / Esc  Quit",
            "",
            "Click either window to toggle this help.",
        ]
        help_font = cv2.FONT_HERSHEY_DUPLEX
        help_scale = 0.7
        help_thickness = 1
        help_padding = 20
        help_line_h = 32
        help_img_h = help_line_h * len(help_lines) + help_padding * 2
        help_img_w = 560
        help_img = np.zeros((help_img_h, help_img_w, 3), dtype=np.uint8)
        for i, line in enumerate(help_lines):
            color = (255, 255, 255) if i == 0 else (180, 180, 180)
            cv2.putText(
                help_img,
                line,
                (help_padding, help_padding + 20 + i * help_line_h),
                help_font,
                help_scale,
                color,
                help_thickness,
                cv2.LINE_AA,
            )
        help_win = "Help"
        help_visible = False

        # Mouse callback: any click toggles help window
        mouse_clicked = [False]

        def on_mouse(event, x, y, flags, param):
            if event == cv2.EVENT_LBUTTONDOWN:
                mouse_clicked[0] = True

        cv2.setMouseCallback(win_name, on_mouse)

        rotation_lock = False
        rotation = 0

        # DIAG: experiment state. _exp_idx points into _exp_steps.
        _diag_last = None
        _diag_event = 0
        _exp_idx = 0

        def _announce_step():
            label, instr = _exp_steps[_exp_idx]
            banner = f">>> STEP {_exp_idx + 1}/{len(_exp_steps)} — {label} <<<"
            print("\n" + banner)
            print(instr)
            print("(SPACE = advance to next step, q = quit)\n")

        _announce_step()
        fullframe = False

        raw_frame = 0
        frame = 0
        part = 0
        pic_buf = b""
        keep_awake_time = trio.current_time()

        # Store received parts per frame
        # frame_number -> {part_number: pic_data}
        frames_dict: dict[int, dict[int, bytes]] = {}
        # number of parts required per frame
        parts_dict: dict[int, int] = {}

        while True:
            # read video stream
            with trio.move_on_after(5.0) as cancel_scope:
                reply = await conn.recv_video()

            if cancel_scope.cancelled_caught:
                print("Video timeout")
                break

            raw_frame = reply[0]
            frame_end: int = reply[1]
            part = reply[2]
            part_end: int = reply[3]
            # misc_data = reply[4:8]
            if not rotation_lock:
                rotation = int.from_bytes(reply[4:6], "big")
            pic_data = reply[8:]

            frame = absolute_frame_from_raw(raw_frame, frame)

            # store the part
            if frame not in frames_dict:
                frames_dict[frame] = {}
            frames_dict[frame][part] = pic_data

            if debug:
                print(
                    f"raw_frame={raw_frame}, frame={frame}, frame_end={frame_end}, part={part}, part_end={part_end}"
                )

            # find number of parts required
            if frame_end == 1:
                parts_dict[frame] = part_end

            if frame in parts_dict:
                num_parts = parts_dict[frame]
                parts = frames_dict[frame]
                if all(p in parts for p in range(num_parts)):
                    pic_buf = b"".join(parts[i] for i in range(num_parts))

                    try:
                        image = Image.open(BytesIO(pic_buf))
                        image_np = np.array(image)
                        image_cv = cv2.cvtColor(image_np, cv2.COLOR_RGB2BGR)
                        num_rows, num_cols = image_cv.shape[:2]

                        if not fullframe:
                            # Case 1: Masked circle. The window will be a square of the SHORTER dimension.
                            square_size = min(num_rows, num_cols)

                            # Create a circular mask on the original image dimensions
                            mask = np.zeros((num_rows, num_cols), np.uint8)
                            cv2.circle(
                                mask,
                                (num_cols // 2, num_rows // 2),
                                square_size // 2,
                                255,
                                -1,
                            )
                            image_masked = cv2.bitwise_and(
                                image_cv, image_cv, mask=mask
                            )

                            # Get rotation matrix for the original image
                            rotation_matrix = cv2.getRotationMatrix2D(
                                (num_cols / 2, num_rows / 2), rotation + 90, 1
                            )
                            # Rotate the masked image within its original frame
                            image_rotated = cv2.warpAffine(
                                image_masked, rotation_matrix, (num_cols, num_rows)
                            )

                            # Crop the center square from the rotated image
                            center_x, center_y = num_cols // 2, num_rows // 2
                            half_size = square_size // 2
                            image_to_show = image_rotated[
                                center_y - half_size : center_y + half_size,
                                center_x - half_size : center_x + half_size,
                            ]

                        else:
                            # Case 2: Full frame, ensuring no corners are ever cropped.
                            # The window will be a square with side length equal to the image diagonal.

                            # Calculate the length of the image diagonal
                            diagonal = np.sqrt(num_cols**2 + num_rows**2)

                            # The new square size is the diagonal, rounded up to the nearest integer
                            square_size = int(np.ceil(diagonal))

                            # Get the rotation matrix centered on the original image
                            rotation_matrix = cv2.getRotationMatrix2D(
                                (num_cols / 2, num_rows / 2), rotation + 90, 1
                            )

                            # Adjust the matrix's translation component to center the image on the new, larger canvas
                            tx = (square_size - num_cols) / 2
                            ty = (square_size - num_rows) / 2
                            rotation_matrix[0, 2] += tx
                            rotation_matrix[1, 2] += ty

                            # Warp the original image onto the new square canvas
                            image_to_show = cv2.warpAffine(
                                image_cv, rotation_matrix, (square_size, square_size)
                            )
                        if debug:
                            print(
                                f"image {num_rows}x{num_cols}, using window {square_size}x{square_size}"
                            )

                        if battery_level is not None:
                            draw_battery(
                                image_to_show,
                                x=square_size // 100,
                                y=square_size // 100,
                                width=square_size // 10,
                                height=square_size // 20,
                                level=battery_level,
                                thickness=square_size // 200,
                            )
                        # Fit image_to_show into the current window size, preserving aspect ratio.
                        # On macOS (WINDOW_GUI_NORMAL), the backend already preserves aspect ratio
                        # on window resize, and getWindowImageRect always returns the native image
                        # size — so this block is a no-op there.
                        # On Windows, the backend stretches the image to fill the window, and
                        # getWindowImageRect reflects the actual stretched display dimensions —
                        # so we resize to fit the smaller dimension and pad the rest with black.
                        # On Linux, at least Wayland, the backend also preserves aspect ratio, but
                        # resizing is async so this block is better off skipped.
                        if sys.platform == "win32":
                            rect = cv2.getWindowImageRect(win_name)
                            win_w, win_h = rect[2], rect[3]
                            if win_w > 0 and win_h > 0:
                                fit = min(win_w, win_h)
                                if fit != square_size:
                                    image_to_show = cv2.resize(
                                        image_to_show,
                                        (fit, fit),
                                        interpolation=cv2.INTER_LINEAR,
                                    )
                                if win_w != win_h:
                                    # Pad the shorter axis with black to fill the window
                                    pad_w = win_w - fit
                                    pad_h = win_h - fit
                                    image_to_show = cv2.copyMakeBorder(
                                        image_to_show,
                                        pad_h // 2,
                                        pad_h - pad_h // 2,
                                        pad_w // 2,
                                        pad_w - pad_w // 2,
                                        cv2.BORDER_CONSTANT,
                                        value=(0, 0, 0),
                                    )
                        cv2.imshow(win_name, image_to_show)
                        if firstframe:
                            cv2.resizeWindow(win_name, square_size, square_size)
                            firstframe = False

                        # delete earlier frame AND current frame data since we processed it
                        frames_dict = {
                            f: frames_dict[f] for f in frames_dict if f > frame
                        }
                        parts_dict = {f: parts_dict[f] for f in parts_dict if f > frame}

                        if trio.current_time() > keep_awake_time:
                            keep_awake_time = trio.current_time() + 10
                            prev_battery_level = battery_level
                            battery_level = await conn.query_battery()
                            if prev_battery_level != battery_level:
                                print(f"Battery level: {battery_level}")

                    except OSError:
                        print("image corrupted")

                    # process UI events (e.g. window closing) and poll for a keypress
                    # we do this only when a frame is completely evaluated to save CPU!
                    key = cv2.pollKey() & 0xFF

                    # DIAG: read ALL probed window properties each frame; print
                    # only when the tuple changes. Each event is tagged with
                    # the current experiment step so cause and effect line up.
                    _vals = tuple(
                        cv2.getWindowProperty(win_name, p) for _, p in _probes
                    )
                    if _vals != _diag_last:
                        _diag_event += 1
                        _step_label = _exp_steps[_exp_idx][0]
                        print(
                            f"DIAG event#{_diag_event} [step {_exp_idx + 1} "
                            f"{_step_label}]: "
                            + ", ".join(
                                f"{name}={v}" for (name, _), v in zip(_probes, _vals)
                            )
                        )
                        _diag_last = _vals

                    # DIAG: SPACE advances to the next experiment step.
                    if key == ord(" "):
                        if _exp_idx + 1 < len(_exp_steps):
                            _exp_idx += 1
                            _announce_step()
                        else:
                            print("DIAG: final step reached. Press q to quit.")

                    # Toggle help window on mouse click
                    if mouse_clicked[0]:
                        mouse_clicked[0] = False
                        if help_visible:
                            cv2.destroyWindow(help_win)
                            help_visible = False
                        else:
                            cv2.namedWindow(help_win, flags=cv2.WINDOW_GUI_NORMAL)
                            cv2.imshow(help_win, help_img)
                            cv2.setMouseCallback(help_win, on_mouse)
                            help_visible = True

                    if key == ord("1"):
                        rotation_lock = True
                        rotation = 0
                    elif key == ord("2"):
                        rotation_lock = True
                        rotation = 90
                    elif key == ord("3"):
                        rotation_lock = True
                        rotation = 180
                    elif key == ord("4"):
                        rotation_lock = True
                        rotation = 270
                    elif key == ord("r"):
                        rotation_lock = False
                    elif (
                        key == ord("q")
                        or key == 27
                        # DIAG EXPERIMENT: both upstream close-clauses disabled
                        # so the loop survives minimize/restore. Exit with 'q'
                        # or Esc. After running the action matrix (including
                        # red-X close, which will leave the window destroyed
                        # but the loop alive), press 'q' to stop cleanly.
                        # or cv2.getWindowProperty(win_name, cv2.WND_PROP_VISIBLE) == 0
                        # or cv2.getWindowProperty(win_name, cv2.WND_PROP_AUTOSIZE) == -1
                    ):
                        print("window closed")
                        break
                    elif key == ord("w"):
                        now = datetime.now()
                        filename = (
                            now.strftime("snapshot_%Y%m%d_%H%M%S_")
                            + f"{now.microsecond // 10000:02d}.jpg"
                        )
                        with open(filename, "wb") as fd:
                            ret = fd.write(pic_buf)
                        print(f"Wrote {ret} bytes to {filename}")
                    elif key == ord("+"):
                        if brightness < 100:
                            brightness += 10
                            print(
                                f"Brightness: {await conn.set_brightness(brightness)}"
                            )
                    elif key == ord("-"):
                        if brightness > 0:
                            brightness -= 10
                            print(
                                f"Brightness: {await conn.set_brightness(brightness)}"
                            )
                    elif key == ord("f"):
                        fullframe = not fullframe
                    elif key == ord("d"):
                        debug = not debug
                    elif key == ord("h"):
                        mouse_clicked[0] = True  # reuse toggle logic

    finally:
        # stop stream and close
        await conn.stop_video()
        await conn.aclose()
        cv2.destroyAllWindows()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fake", action="store_true", help="Use fake endscope device")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    buffer_size = 1500
    target_ip = "192.168.1.1"
    target_port_meta = 61502
    source_port_meta = 50262
    target_port_vid = 61503
    source_port_vid = 51320

    global debug
    debug = args.debug

    async with trio.open_nursery() as nursery:
        if args.fake:
            from fake_endscope import start_fake_device

            meta_chan, vid_chan = start_fake_device(nursery)

            conn = EndscopeConnection(meta_chan, vid_chan)

            await run_app(conn, buffer_size)
            nursery.cancel_scope.cancel()
        else:
            async with (
                UdpDatagramTransport(
                    source_port_meta, target_ip, target_port_meta, buffer_size
                ) as meta_chan,
                UdpDatagramTransport(
                    source_port_vid, target_ip, target_port_vid, buffer_size
                ) as vid_chan,
            ):
                conn = EndscopeConnection(meta_chan, vid_chan)
                await run_app(conn, buffer_size)


def cli_main() -> None:
    trio.run(main)


if __name__ == "__main__":
    cli_main()
