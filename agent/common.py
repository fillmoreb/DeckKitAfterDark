"""Where DeckAgent keeps its things, and how it reaches the game."""
import json, os, shlex, subprocess

HOME = os.path.expanduser(os.environ.get("DECKAGENT_HOME", "~/.deckagent"))  # the agent's own folder
DB_PATH = os.path.join(HOME, "deckagent.db")
DATA_DIR = os.path.join(HOME, "data")  # downloads (not ours to redistribute; never committed)


def _deploy_local_deck():
    """DECK=... from the repo's deploy.local (the same address deploy.sh and install.sh use), if any."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deploy.local")
    try:
        with open(path) as f:
            for line in f:
                key, _, value = line.strip().partition("=")
                if key == "DECK":
                    return value.strip().strip("'\"")
    except OSError:
        pass
    return ""


def _on_the_deck():
    return os.path.exists("/home/deck/.local/bin/decklink")


# Where the game is: "" when this runs on the Deck itself, else an SSH target that can run decklink as
# deck (Ben's setup: claude@<deck> with passwordless sudo). DECKAGENT_DECK, else deploy.local's DECK
# (when not on the Deck).
DECK = os.environ.get("DECKAGENT_DECK", "" if _on_the_deck() else _deploy_local_deck())
DECKLINK = os.environ.get("DECKAGENT_DECKLINK", "/home/deck/.local/bin/decklink")


class GameError(Exception):
    pass


def game(*args, timeout=60):
    """Runs `decklink agent <args>` where the game is and returns its JSON reply."""
    inner = [DECKLINK, "agent"] + [str(a) for a in args]
    if DECK:
        remote = "sudo -u deck env XDG_RUNTIME_DIR=/run/user/1000 " + shlex.join(inner)
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", DECK, remote]
    else:
        cmd = inner
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GameError("the game didn't answer in %d s" % timeout)
    line = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
    try:
        reply = json.loads(line)
    except ValueError:
        raise GameError((out.stderr.strip() or out.stdout.strip() or "no reply").splitlines()[-1])
    return reply
