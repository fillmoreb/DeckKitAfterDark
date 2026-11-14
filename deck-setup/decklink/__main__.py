"""decklink: talk to the game through DeckLink from the Deck.

    decklink status                   the strip as it is now (no keys sent)
    decklink ping [--size N]          open the link and time a round trip (N bytes, echoed back)
    decklink send CHANNEL [TEXT]      send TEXT (or stdin) to an addon's channel (1-255)
    decklink recv [--count N] [--timeout S]   print frames the game sends (channel<TAB>payload)

Keys are paced (--gap, 1 ms) so the game doesn't freeze: about 55 bytes/s in.
--tool ydotool|xdotool picks how keys go in (default: ydotool when its socket is usable, which
needs root; else xdotool on :1). Reading the strip needs the desktop user's PipeWire session; as
root, use sudo -E or run the reader side as deck.
"""
import argparse, os, sys

from . import codec, keys, link, strip


def main(argv=None):
    p = argparse.ArgumentParser(prog="decklink", description=__doc__.split("\n\n")[0])
    p.add_argument("--tool", choices=["ydotool", "xdotool"])
    p.add_argument("--gap", type=int, default=keys.GAP_MS,
                   help="ms between key presses and releases (default %(default)s; 0 bursts, which freezes the game "
                        "~0.34 ms per bit)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    pg = sub.add_parser("ping")
    pg.add_argument("--size", type=int, default=16)
    sd = sub.add_parser("send")
    sd.add_argument("channel", type=int)
    sd.add_argument("text", nargs="?")
    rv = sub.add_parser("recv")
    rv.add_argument("--count", type=int, default=0, help="stop after N frames (0: keep going)")
    rv.add_argument("--timeout", type=float, default=30)
    a = p.parse_args(argv)
    try:
        if a.cmd == "status":
            s = link.Link(sender=object()).read()
            if s is None:
                print("no strip: the link is closed, or DeckLink isn't loaded")
                return 1
            flags = [n for n, f in (("open", strip.FLAG_OPEN), ("ready", strip.FLAG_READY),
                                    ("bound", strip.FLAG_BOUND), ("text-focus", strip.FLAG_FOCUS)) if s.flags & f]
            print("flags=%s hb=%d rx_seq=%d rx=%d bad=%d out=%s" % (",".join(flags) or "-", s.heartbeat, s.rx_seq,
                  s.rx_count, s.bad, "%d bytes" % len(s.out) if s.out else "-"))
            return 0
        with link.Link(sender=keys.Keys(a.tool, gap_ms=a.gap)) as lk:
            if a.cmd == "ping":
                print("round trip %.0f ms (%d bytes each way)" % (lk.ping(os.urandom(a.size)) * 1000, a.size))
            elif a.cmd == "send":
                if not 1 <= a.channel <= 255:
                    p.error("channel is 1-255")
                data = a.text.encode() if a.text is not None else sys.stdin.buffer.read()
                lk.send(a.channel, data)
            elif a.cmd == "recv":
                n = 0
                while not a.count or n < a.count:
                    got = lk.recv(a.timeout)
                    if got is None:
                        break
                    if got[0] == 0:
                        continue
                    print("%d\t%s" % (got[0], got[1].decode("utf-8", "replace")), flush=True)
                    n += 1
        return 0
    except (link.LinkError, keys.SendError, strip.NoStrip) as e:
        print("decklink: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
