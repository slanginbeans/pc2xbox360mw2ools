# mw2ff: MW2 fastfiles, asset by asset (Xbox 360, TU6)

mw2ff reads a whole MW2 fastfile (`.ff`) the same way the game does, so every byte in it
belongs to a known field of a known asset. It can list the assets, unpack them into
folders you can read and edit, and pack a folder back into a fastfile.

Edits can change size: scripts and text can be any length, and scripts can be added or
removed. When something grows or shrinks, mw2ff lays the file out again the way the game
will load it and fixes every pointer inside it (see "How it works").

## The editor (easiest way)

Put `mw2ff.bat` in your work folder (the one with your `.ff` files, next to `mw2tex.bat`) and
double-click it. `mw2tex.bat` copies it there for you the first time it updates. It keeps the
tools up to date in the same `mw2tex_app` folder that mw2tex uses, then opens the editor in
your browser.

1. Pick a fastfile on the left (multiplayer files only: names starting with `mp_` or ending
   in `_mp`).
2. Pick an asset. Filter by type (weapon, sound, material...) or search by name.
3. Change the numbers or text you want. Anything that can't fit (a decimal in a whole-number
   field, a value too big for the field) is refused with a message. Text can be any length.
   A script (`rawfile`) opens in a text box: edit it and press **Save script**. **New script**
   adds one, **Remove script** takes one out.
4. Press **Build**. The new file goes in `mw2ff_out`. Copy it to `_codxe\zone\` on the console.

To work on scripts in Notepad or any other editor, press **Export scripts**. Every script and
all in-game text (`localize.txt`, one `KEY = text` line each) go into
`mw2ff_scripts\<file>\`. Edit them, add new script files if you like, then press
**Import scripts** and **Build**.

Your changes are saved in `mw2ff_changes` and come back the next time you open that file.
**Undo all changes** puts that file back the way it was. If `mw2tex_out` has a file with the same name, the console can only use
one of the two, so the editor warns you about it.

## Commands

Run these from the `tools\mw2ff` folder (Python 3, nothing to install):

```
python mw2ff.py list   ui_mp.ff                 every asset in the file, in load order
python mw2ff.py unpack ui_mp.ff ui_mp_files     one folder per asset
python mw2ff.py pack   ui_mp_files ui_mp.ff     build a fastfile from that folder
python mw2ff.py verify ui_mp.ff                 unpack and pack in memory, check nothing changed
```

## What an unpacked folder holds

```
scripts/             every script (rawfile) as a plain file; edit, or add new ones
localize.txt         every in-game text as KEY = text
zone.json            file header, script strings, the asset list (type, name, folder)
layout.bin           where everything sat in the game's memory, so pack can move it
zone.bin             big data that belongs to the zone header part
container.bin        the fastfile's own header, reused by pack
assets/00012_material_mc_flag_red/
    asset.json       every piece of data the asset streams, in order, as named fields
    data.bin         large number arrays (pixels, vertices, sounds), referenced from asset.json
```

Each entry in `asset.json` is one piece of data the game reads, in the order it reads it:

- `"k": "type"` with `"t"` (the C type, see `defs/iw4_assets.h`), `"n"` (how many) and `"v"`
  (the fields). Pointers show as `"follow"` (the data comes next in the file), `null`, or an
  address inside the zone.
- `"k": "string"` with `"s"`, a string the asset points to.
- `"blob": [offset, length]` means the bytes are in `data.bin`.
- `"fix"` lists bytes that are not part of any field (padding and such), kept so the
  file comes back exactly the same.

## How it works

- `defs/iw4_assets.h` and `defs/assets/*.txt` describe the assets and how the game loads them,
  in the format of OpenAssetTools' ZoneCode, with the Xbox 360 differences filled in.
- `cdefs.py` lays the structs out like the 360 compiler does, `schema.py` reads the loading
  rules, `zone.py` walks the zone, and `codec.py` turns each piece into fields and back.
- `relocate.py` handles size changes. The game copies each piece of the zone into a memory
  block at the next free, aligned place, and pointers back to earlier data hold that place.
  After a change, relocate walks the new zone, pairs each piece with where it was before, and
  rewrites every such pointer and the block sizes in the zone header.
- `text.py` turns scripts and in-game text into plain text and back, and adds or removes
  scripts.
- `research/` holds the tools used to check the reader against the game: an emulator that
  runs the TU6 loader code on a zone and records every read, and scripts that compare that
  with `zone.py`. See `research/README.md`.

Checked so far (every byte read in the same order and to the same memory place as the game,
and unpack plus pack gives the same zone back): ui_mp, common_mp, code_post_gfx_mp,
patch_mp (stock TU6 and a modded one), mp_favela, mp_favela_load, mp_nightshift,
mp_underpass, and the single-player airport. Files with longer scripts, longer text, added
and removed scripts were also checked against the game's loader: it reads them exactly as
mw2ff lays them out.

## License note

`defs/` is based on OpenAssetTools (https://github.com/Laupetin/OpenAssetTools), which is
licensed under the GNU GPL v3. See `defs/NOTICE.md`.
