"""A black bar over the strip, so the player doesn't see it (lab E7).

gamescope draws "external overlay" windows (on Steam's X display, :0) above the game. That's how
MangoHud's mangoapp shows: a window with GAMESCOPE_EXTERNAL_OVERLAY=1 and GAMESCOPE_NO_FOCUS=1. The
PipeWire stream the strip reader uses leaves overlays out, so the reader still sees the strip under
the bar. The bar takes no input (an empty input shape) and never takes focus.

    python3 -m decklink.cover [--height 4] [--color RRGGBB] [--always]     runs until killed

Like mangoapp, the window is full screen, 32-bit and transparent except for the bottom rows. It's
shown only while the link is open: hosts (link.py) note that in /tmp/decklink-open, which this
checks four times a second. (Watching the strip itself through PipeWire cost 16% of a core.)
--always keeps it up. Verified on the Deck 2026-11-13 (lab E7): it draws above the game,
the reader still decodes the strip under it, and the controller, menus and Steam overlay work as
usual. Runs as the desktop user (deck), with DISPLAY=:0. Plain ctypes against libX11/libXfixes.
"""
import argparse, ctypes, ctypes.util, os, sys, time

from . import link

c_ulong, c_long, c_int, c_uint, c_void_p = ctypes.c_ulong, ctypes.c_long, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p


class XSetWindowAttributes(ctypes.Structure):
    _fields_ = [("background_pixmap", c_ulong), ("background_pixel", c_ulong), ("border_pixmap", c_ulong),
                ("border_pixel", c_ulong), ("bit_gravity", c_int), ("win_gravity", c_int),
                ("backing_store", c_int), ("backing_planes", c_ulong), ("backing_pixel", c_ulong),
                ("save_under", c_int), ("event_mask", c_long), ("do_not_propagate_mask", c_long),
                ("override_redirect", c_int), ("colormap", c_ulong), ("cursor", c_ulong)]


class XVisualInfo(ctypes.Structure):
    _fields_ = [("visual", c_void_p), ("visualid", c_ulong), ("screen", c_int), ("depth", c_int),
                ("c_class", c_int), ("red_mask", c_ulong), ("green_mask", c_ulong), ("blue_mask", c_ulong),
                ("colormap_size", c_int), ("bits_per_rgb", c_int)]


CW_BACK_PIXEL, CW_BORDER_PIXEL, CW_EVENT_MASK, CW_COLORMAP = 1 << 1, 1 << 3, 1 << 11, 1 << 13
EXPOSURE_MASK, STRUCTURE_NOTIFY_MASK = 1 << 15, 1 << 17
EXPOSE, TRUE_COLOR, XA_CARDINAL, SHAPE_INPUT = 12, 4, 6, 2


def load(name):
    lib = ctypes.CDLL(ctypes.util.find_library(name) or "lib%s.so" % name)
    return lib


def main(argv=None):
    p = argparse.ArgumentParser(prog="decklink.cover")
    p.add_argument("--height", type=int, default=4)
    p.add_argument("--always", action="store_true", help="show it all the time, not only while the link is open")
    p.add_argument("--color", default="000000", help="RRGGBB (default black; e.g. ffff00 to see where it is)")
    a = p.parse_args(argv)

    x = load("X11")
    xf = load("Xfixes")
    x.XOpenDisplay.restype = c_void_p
    x.XOpenDisplay.argtypes = [ctypes.c_char_p]
    for fn in ("XDefaultScreen", "XDisplayWidth", "XDisplayHeight"):
        getattr(x, fn).argtypes = [c_void_p] + ([c_int] if fn != "XDefaultScreen" else [])
    x.XRootWindow.restype = c_ulong
    x.XRootWindow.argtypes = [c_void_p, c_int]
    x.XMatchVisualInfo.argtypes = [c_void_p, c_int, c_int, c_int, ctypes.POINTER(XVisualInfo)]
    x.XCreateColormap.restype = c_ulong
    x.XCreateColormap.argtypes = [c_void_p, c_ulong, c_void_p, c_int]
    x.XCreateWindow.restype = c_ulong
    x.XCreateWindow.argtypes = [c_void_p, c_ulong, c_int, c_int, c_uint, c_uint, c_uint, c_int, c_uint, c_void_p,
                                c_ulong, ctypes.POINTER(XSetWindowAttributes)]
    x.XInternAtom.restype = c_ulong
    x.XInternAtom.argtypes = [c_void_p, ctypes.c_char_p, c_int]
    x.XChangeProperty.argtypes = [c_void_p, c_ulong, c_ulong, c_ulong, c_int, c_int, c_void_p, c_int]
    x.XStoreName.argtypes = [c_void_p, c_ulong, ctypes.c_char_p]
    x.XMapWindow.argtypes = [c_void_p, c_ulong]
    x.XUnmapWindow.argtypes = [c_void_p, c_ulong]
    x.XPending.argtypes = [c_void_p]
    x.XCreateGC.restype = c_void_p
    x.XCreateGC.argtypes = [c_void_p, c_ulong, c_ulong, c_void_p]
    x.XSetForeground.argtypes = [c_void_p, c_void_p, c_ulong]
    x.XFillRectangle.argtypes = [c_void_p, c_ulong, c_void_p, c_int, c_int, c_uint, c_uint]
    x.XFlush.argtypes = [c_void_p]
    x.XNextEvent.argtypes = [c_void_p, c_void_p]
    xf.XFixesCreateRegion.restype = c_ulong
    xf.XFixesCreateRegion.argtypes = [c_void_p, c_void_p, c_int]
    xf.XFixesSetWindowShapeRegion.argtypes = [c_void_p, c_ulong, c_int, c_int, c_int, c_ulong]

    dpy = x.XOpenDisplay(os.environ.get("DISPLAY", ":0").encode())
    if not dpy:
        print("decklink.cover: can't open display %s" % os.environ.get("DISPLAY"), file=sys.stderr)
        return 1
    screen = x.XDefaultScreen(dpy)
    root = x.XRootWindow(dpy, screen)
    width, height = x.XDisplayWidth(dpy, screen), x.XDisplayHeight(dpy, screen)

    attrs = XSetWindowAttributes()
    attrs.border_pixel = 0
    attrs.event_mask = EXPOSURE_MASK | STRUCTURE_NOTIFY_MASK
    mask = CW_BACK_PIXEL | CW_BORDER_PIXEL | CW_EVENT_MASK
    info = XVisualInfo()
    if not x.XMatchVisualInfo(dpy, screen, 32, TRUE_COLOR, ctypes.byref(info)):
        print("decklink.cover: no 32-bit visual", file=sys.stderr)
        return 1
    attrs.background_pixel = 0  # fully transparent
    attrs.colormap = x.XCreateColormap(dpy, root, info.visual, 0)
    mask |= CW_COLORMAP
    geom, depth, visual = (0, 0, width, height), 32, info.visual
    win = x.XCreateWindow(dpy, root, geom[0], geom[1], geom[2], geom[3], 0, depth, 1, visual, mask,
                          ctypes.byref(attrs))
    x.XStoreName(dpy, win, b"decklink cover")
    one = (c_ulong * 1)(1)
    for name in (b"GAMESCOPE_EXTERNAL_OVERLAY", b"GAMESCOPE_NO_FOCUS"):
        x.XChangeProperty(dpy, win, x.XInternAtom(dpy, name, 0), XA_CARDINAL, 32, 0, one, 1)
    xf.XFixesSetWindowShapeRegion(dpy, win, SHAPE_INPUT, 0, 0, xf.XFixesCreateRegion(dpy, None, 0))
    gc = x.XCreateGC(dpy, win, 0, None)
    x.XSetForeground(dpy, gc, 0xFF000000 | int(a.color, 16))

    def paint():
        x.XFillRectangle(dpy, win, gc, 0, height - a.height, width, a.height)
        x.XFlush(dpy)

    shown = False

    def show(on):
        nonlocal shown
        if on == shown:
            return
        (x.XMapWindow if on else x.XUnmapWindow)(dpy, win)
        x.XFlush(dpy)
        shown = on

    show(a.always)
    event = ctypes.create_string_buffer(256)
    while True:
        while x.XPending(dpy):
            x.XNextEvent(dpy, event)
            if ctypes.c_int.from_buffer(event).value == EXPOSE:
                paint()
        if not a.always:
            show(time.time() - link.open_since() < link.OPEN_FOR)
        time.sleep(0.25)

if __name__ == "__main__":
    sys.exit(main())
