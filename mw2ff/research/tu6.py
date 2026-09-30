"""Where the research scripts find things. The decrypted TU6 default_mp image is not in
the repo; point TU6_PE at it (see the project notes on how it was made)."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(TOOLS, "mw2ff"))
sys.path.insert(0, os.path.join(TOOLS, "mw2tex"))
CACHE = os.path.join(HERE, "cache")
os.makedirs(CACHE, exist_ok=True)


def pe_path():
    for p in (os.environ.get("TU6_PE"), os.path.join(HERE, "default_mp_tu6.pe"),
              "/mnt/project-files/tools/tu6/default_mp_tu6.pe"):
        if p and os.path.exists(p):
            return p
    sys.exit("set TU6_PE to the decrypted TU6 default_mp image (default_mp_tu6.pe)")
