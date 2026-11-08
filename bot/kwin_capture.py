"""Fast screenshots straight from KWin (KDE's compositor), ~50 ms instead of ~800 ms.

`spectacle` starts a whole program for every capture. KWin exposes the same capture
over D-Bus (org.kde.KWin.ScreenShot2), which spectacle itself uses: we pass it one end
of a pipe, and it writes the raw pixels of one monitor into it.
"""
from __future__ import annotations

import os

import numpy as np
from jeepney import DBusAddress, new_method_call
from jeepney.io.blocking import open_dbus_connection

_ADDR = DBusAddress("/org/kde/KWin/ScreenShot2", bus_name="org.kde.KWin",
                    interface="org.kde.KWin.ScreenShot2")
_RGBX8888 = 16   # QImage::Format_RGBX8888: bytes are R, G, B, padding


class KWinCapture:
    def __init__(self, output: str):
        self.output = output   # monitor name, e.g. "HDMI-A-1" (see `kscreen-doctor -o`)
        self.conn = open_dbus_connection(bus="SESSION", enable_fds=True)

    def grab(self) -> np.ndarray:
        """The whole monitor as a BGR array (OpenCV's channel order), at native resolution."""
        r, w = os.pipe()
        try:
            msg = new_method_call(_ADDR, "CaptureScreen", "sa{sv}h",
                                  (self.output, {"native-resolution": ("b", True)}, w))
            reply = self.conn.send_and_get_reply(msg, timeout=5)
        finally:
            os.close(w)        # our copy of the write end; KWin writes through its own
        with os.fdopen(r, "rb") as f:
            data = f.read()
        meta = {k: v[1] for k, v in reply.body[0].items()}
        if meta.get("format") != _RGBX8888:
            raise RuntimeError(f"unexpected KWin image format {meta.get('format')}")
        h, width, stride = meta["height"], meta["width"], meta["stride"]
        img = np.frombuffer(data, np.uint8).reshape(h, stride // 4, 4)[:, :width, :3]
        return img[..., ::-1].copy()   # RGB -> BGR
