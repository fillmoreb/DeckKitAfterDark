"""Deck telemetry into the game: battery, power, temperatures, fan, load. The addon DeckTelemetry
(addons/DeckTelemetry) receives it and offers it to other addons.

    python3 -m decklink.telemetry [--every 5] [--once] [--print]

Fire and forget: one frame on channel 1 every few seconds, by keys only. No hello, no strip, no ack,
so nothing reads the screen and it costs next to nothing. An update that's lost (chat open, a tap
swallowed) fails the CRC or never arrives, and the next one replaces it.

The payload is binary, fields at fixed places, big-endian (addons/DeckTelemetry/Telemetry.lua reads it;
keep the two in step). 17 bytes:
    0      format version (2)
    1-2    field mask: which readings below are real (bit set) rather than unknown (zero)
             bit 0 bat, 1 st, 2 ac, 3 bw, 4 apu, 5 cpu, 6 gpu, 7 mem, 8 tapu, 9 tbat, 10 tssd, 11 fan
    3      bat    battery charge, %
    4      bits 0-1 st (0 discharging, 1 charging, 2 plugged in but not charging, 3 full), bit 2 ac
    5-6    bw     battery power, W x 10, signed: positive draining, negative charging
    7-8    apu    APU power (CPU + GPU), W x 10
    9      cpu    CPU busy since the last update, %
    10     gpu    GPU busy, %
    11     mem    memory in use, %
    12     tapu   APU temperature, C
    13     tbat   battery temperature, C
    14     tssd   SSD temperature, C
    15-16  fan    fan speed, rpm
"""
import argparse, glob, os, struct, sys, time

from . import codec, keys, link

# Telemetry waits while another program has the link open (DeckAgent's link keeper marks it every
# second): its frame takes >1 s of taps and holds the key lock, so a question would wait behind it.
PAUSE_IF_OPEN_WITHIN_S = 3

CHANNEL = 1
FORMAT = 2
FIELDS = ["bat", "st", "ac", "bw", "apu", "cpu", "gpu", "mem", "tapu", "tbat", "tssd", "fan"]  # mask bit order
ST_CODES = {"d": 0, "c": 1, "n": 2, "f": 3}


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _num(path, scale=1.0):
    v = _read(path)
    try:
        return float(v) / scale
    except (TypeError, ValueError):
        return None


def _hwmon(name):
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        if _read(h + "/name") == name:
            return h
    return None


class CPU:
    """CPU busy % between calls, from /proc/stat."""

    def __init__(self):
        self.last = self._sample()

    @staticmethod
    def _sample():
        v = _read("/proc/stat")
        if not v:
            return None
        f = [int(x) for x in v.split("\n", 1)[0].split()[1:]]
        idle = f[3] + (f[4] if len(f) > 4 else 0)
        return sum(f), idle

    def busy(self):
        now = self._sample()
        last, self.last = self.last, now
        if not now or not last or now[0] == last[0]:
            return None
        return 100.0 * (1 - (now[1] - last[1]) / (now[0] - last[0]))


STATES = {"Charging": "c", "Discharging": "d", "Not charging": "n", "Full": "f"}


def collect(cpu):
    """The readings now, as an ordered list of (key, value)."""
    out = [("v", FORMAT)]
    bat = "/sys/class/power_supply/BAT1"
    out.append(("bat", _num(bat + "/capacity")))
    state = _read(bat + "/status")
    out.append(("st", STATES.get(state)))
    ac = _num("/sys/class/power_supply/ACAD/online")
    out.append(("ac", None if ac is None else int(ac)))
    amps, volts = _num(bat + "/current_now", 1e6), _num(bat + "/voltage_now", 1e6)
    if amps is not None and volts is not None:
        watts = amps * volts
        out.append(("bw", -watts if state == "Charging" else watts))
    gpu_mon = _hwmon("amdgpu")
    out.append(("apu", _num(gpu_mon + "/power1_average", 1e6) if gpu_mon else None))
    out.append(("cpu", cpu.busy()))
    out.append(("gpu", _num((glob.glob("/sys/class/drm/card*/device/gpu_busy_percent") or [""])[0])))
    mem = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        mem[k] = v.split()[0] if v.split() else None
    try:
        out.append(("mem", 100.0 * (1 - int(mem["MemAvailable"]) / int(mem["MemTotal"]))))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        pass
    out.append(("tapu", _num(gpu_mon + "/temp1_input", 1000) if gpu_mon else None))
    deck = _hwmon("steamdeck_hwmon") or _hwmon("jupiter")
    out.append(("tbat", _num(deck + "/temp1_input", 1000) if deck else None))
    nvme = _hwmon("nvme")
    out.append(("tssd", _num(nvme + "/temp1_input", 1000) if nvme else None))
    out.append(("fan", _num(deck + "/fan1_input") if deck else None))
    return [(k, v) for k, v in out if v is not None]


def encode(readings):
    """The binary payload (layout above) for collect()'s readings."""
    r = dict(readings)
    mask = sum(1 << b for b, f in enumerate(FIELDS) if r.get(f) is not None)
    u8 = lambda v: max(0, min(255, int(round(v or 0))))
    power = ST_CODES.get(r.get("st"), 0) | (4 if r.get("ac") else 0)
    bw = max(-32768, min(32767, int(round((r.get("bw") or 0) * 10))))
    apu = max(0, min(65535, int(round((r.get("apu") or 0) * 10))))
    fan = max(0, min(65535, int(round(r.get("fan") or 0))))
    return struct.pack(">BHBBhHBBBBBBH", FORMAT, mask, u8(r.get("bat")), power, bw, apu, u8(r.get("cpu")),
                       u8(r.get("gpu")), u8(r.get("mem")), u8(r.get("tapu")), u8(r.get("tbat")),
                       u8(r.get("tssd")), fan)


def main(argv=None):
    p = argparse.ArgumentParser(prog="decklink.telemetry", description=__doc__.split("\n\n")[0])
    p.add_argument("--every", type=float, default=5, help="seconds between updates (default 5)")
    p.add_argument("--once", action="store_true", help="send one update and stop")
    p.add_argument("--print", action="store_true", help="print each payload")
    p.add_argument("--tool", choices=["ydotool", "xdotool"])
    a = p.parse_args(argv)
    sender = keys.Keys(a.tool)
    cpu = CPU()
    time.sleep(0.5)  # a first CPU sample interval
    seq = int(time.time()) % 255 + 1
    while True:
        if not a.once and (time.time() - link.open_since() < PAUSE_IF_OPEN_WITHIN_S or link.held()):
            time.sleep(1)
            continue
        readings = collect(cpu)
        payload = encode(readings)
        seq = seq % 255 + 1
        if a.print:
            print(dict(readings), payload.hex(), flush=True)
        try:
            sender.send(list(codec.bits(codec.build(CHANNEL, seq, payload))))
        except keys.SendError as e:
            print("decklink.telemetry: %s" % e, file=sys.stderr, flush=True)
        if a.once:
            return 0
        time.sleep(a.every)


if __name__ == "__main__":
    sys.exit(main())
