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

## Converting PC maps to the 360 (mw2port)

`mw2port.bat` converts a PC (IW4x) multiplayer map to the Xbox 360 (TU6). `mw2tex.bat` and
`mw2ff.bat` put it in your work folder for you, and it updates itself the same way.

Your work folder needs these stock 360 files from the console (copy them over FTP):

- `code_post_gfx_mp.ff` and `common_mp.ff`
- at least one stock map with its loading screen file, for example `mp_favela.ff` and
  `mp_favela_load.ff` (the converter takes 360 shaders and settings from it)
- the stock maps that carry the teams your PC map uses. Each 360 map carries only its own
  two teams (soldiers, flags, crates, icons), so the converter copies them from a stock map
  that has them. `mp_favela` has Task Force 141 (desert) and Militia, `mp_nightshift` (Skidrow)
  has US Army and Spetsnaz, `mp_underpass` has Task Force 141 (forest) and Militia,
  `mp_rust` has OpFor, `mp_derail` has the arctic teams, and `mp_checkpoint` (Karachi) has
  the SEALs.

1. Double-click `mw2port.bat`. The converter opens in your browser.
2. Press **Open mw2port_in** and put each PC map in its own folder there. For an IW4x map,
   copy its whole `usermaps\<map>` folder (`.ff`, `_load.ff`, `.iwd`, `.arena`). Press
   **Refresh**.
3. Pick the map. The teams come from its `.arena` file; change them if you like. A team you
   don't have a stock map for is swapped for one you do, and the log says which map to add.
4. Press **Convert**. It takes a few minutes. The 360 files go in `mw2port_out\<map>\`.
5. Copy both files to `_codxe\zone\` on the console, start a private match and type
   `map <map>` in the console.

Tick several maps to convert them one after another in one go.

**Titles and emblems.** In a match the game draws them from the map's own copy, so converted maps carry
the stock ones. With **Take titles and emblems from imagefile8.pak** ticked, a converted map points them
at fixed places in `imagefile8.pak` (written next to it, filled from `ui_mp.ff`: mw2tex's built one in
`mw2tex_out` when it's there). After you change titles or emblems in mw2tex, its Build writes a new
`imagefile8.pak`: copy that and `ui_mp.ff` to the console, and the maps show them without being converted
or copied again. **Patch stock maps** does the same for every stock `mp_*.ff` in the work folder (only
their picture table changes; the copies go in `mw2port_out\stock`). **Build imagefile8.pak** writes just
the pak to `mw2port_out`, without converting anything. "Fill imagefile8.pak from" picks the source: your
titles and emblems (mw2tex's built `ui_mp.ff`, else the stock one) or always the game's default ones (the
stock `ui_mp.ff`). Not tried on a console yet.

From a command prompt: `python port.py <pc map.ff> <out.ff> --iwd <map.iwd> --ref360
code_post_gfx_mp.ff mp_favela.ff [more stock maps] [--teams ALLIES AXIS]`.

What it converts so far: the map itself (geometry, collision, lighting, pictures, reflection
probes, map entities, scripts) and the two teams. Custom models, sounds and effects the PC map
brings with it aren't converted yet. Pictures over 40 MB lose their top mip levels so the map
fits in memory (`--texture-budget` from a command prompt).

### Fixes and test variants

Some conversion steps are fixes for problems seen on the console, and not all of them are proven.
Each can be switched off on the converter page (**Fixes (for testing)**), or from a command prompt
with `--fix-off NAME` (repeatable). All are on by default:

| Fix | What it does |
|---|---|
| `texture_budget` | Pictures over the budget lose their top mip levels (out of memory / loading-screen freeze without it) |
| `stock_effects` | Effects a stock 360 map also has come from it (the yellow dust haze); maps have failed to load with it (MT_GetSize ... script usage) |
| `surface_bounds` | Fills in the 360-only culling radius / texture density of every world surface |
| `model_lods` | Model detail levels' partBits and surfs written as stock files have them (0 and empty) |
| `stock_material_state` | A material a stock file also has takes that material's culling and draw order |
| `hide_tool_surfaces` | Radiant tool shaders (clip, caulk, ...) draw nothing |
| `portal_multiply` | HDR portal sheets drawn with a multiply shader (off: hidden like tool surfaces) |

**Also build test variants** converts the map once more for every fix that is on, with just that
fix off, into `mw2port_out\<map>\variants\no_<fix>`. Copy them to the console one after another
to find which fix is behind a problem (a few minutes per variant).

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
