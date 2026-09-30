# pc2xbox360mw2ools

Tools for Call of Duty: Modern Warfare 2 on the Xbox 360 (title update 6), used with
[codxe](https://github.com/slanginbeans/codxe_modified):

- **mw2tex**: swap and animate pictures (emblems, calling card titles, camos, load screens) and
  edit the game's tables
- **mw2ff**: open any fastfile (`.ff`) and change what's in it: numbers, scripts, in-game text
- **mw2port**: convert PC (IW4x) multiplayer maps to the 360, with the teams you pick

## Getting started

Download [`mw2tools/mw2tools.bat`](mw2tools/mw2tools.bat), put it in a folder of its own and
double-click it. It needs [Python](https://www.python.org/downloads/) (tick "Add python.exe to
PATH") and [Git](https://git-scm.com/). It downloads the tools, keeps them up to date every time
you start it, and opens one browser page with a tab per tool. See
[`mw2tools/README.md`](mw2tools/README.md) for the folder layout, and each tool's own README for
how to use it.

Files the tools make go in `_codxe\zone\` on the console, where codxe loads them in place of the
game's own.

## What's where

```
mw2tools/   the launcher and the page that holds every tool
mw2tex/     pictures and tables
mw2ff/      fastfile reader/writer, the fastfile editor and the map converter (port.py)
```

These tools used to live in `tools/` in the codxe repository; their history came along.
