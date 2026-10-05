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

A PC copy of a stock map (for example PC `mp_rust.ff`) is converted with the stock 360 map of the
same name as its main source when that file is in the work folder: its scripts, sounds and teams.

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

The first conversion reads every stock file it uses, which takes a while, and keeps what it read
in `mw2port_cache` next to them, so later conversions start much sooner. That folder takes about
as much room as the stock files unpacked (a few hundred MB). Deleting it is safe: it's made again.
A stock file that changes, or a tools update that reads files differently, is read again by itself.
It also keeps the names each stock map holds (for finding the map's effects without unpacking
every stock map) and, in `mw2port_cache\pictures`, the slow picture steps (resizing pictures,
normal maps to DXN), so converting a map again, say with other switches, skips them. The
pictures folder is kept under 2 GB (the least recently used go first).

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
**Write imagefile8.pak with each conversion** (in the Convert box, ticked by default) writes a copy of
`imagefile8.pak` next to every converted map. Unticked, maps still take titles and emblems from
`imagefile8.pak` when the box above is ticked, but no new pak is written: use the one already on the console,
or **Build imagefile8.pak**.

**Cancel** (next to Convert) stops a running conversion at the converter's next step, within about 10 seconds,
and skips the maps after it in a batch. The map being converted isn't written; files already in its
`mw2port_out` folder may be from an earlier conversion, so don't copy them.

Each map converts in a process of its own (a second `python` in Task Manager while it runs). The stock
maps read only for their teams or effects are kept without their drawn geometry and collision (the
converter doesn't use them; about 160 MB each). A big map takes about 1.7 GB while it converts; when it's done that process ends and all of it goes back to Windows,
so the converter page itself stays small between maps and through a batch.

From a command prompt: `python port.py <pc map.ff> <out.ff> --iwd <map.iwd> --ref360
code_post_gfx_mp.ff mp_favela.ff [more stock maps] [--teams ALLIES AXIS]`.
To see where the time goes, tick **Time this conversion** on the converter page: when a map is
done, the log lists the parts that took the longest and the full timings go in
`mw2port_out\<map>\<map>.prof`. From a command prompt, add `--profile`: when it's done it prints the 30 functions that
took the longest (`--profile 50` for more) and saves the full timings as `<out>.prof` next to
the output (`python -m pstats <out>.prof` to look through them). The conversion runs slower
while it's being timed; the file it writes is the same.

What it converts so far: the map itself (geometry, collision, lighting, pictures, reflection
probes, map entities, scripts) and the two teams. Custom models, sounds and effects the PC map
brings with it aren't converted yet. Pictures over 40 MB lose their top mip levels so the map
fits in memory (`--texture-budget` from a command prompt).

### Writer test

**Rewrite stock map (test)** on the converter page (or `python port.py --rewrite-stock STOCK.ff OUT.ff`)
writes a stock 360 map back out through the converter's own writer, converting nothing, into
`mw2port_out\rewrite`. On stock mp_rust the zone comes out byte for byte the same as the stock one
apart from two header sizes, set as they are for converted maps: the temp block (1,200 bytes, stock
1,216) and the callback block (128 KB, stock 27 KB). If the rewritten map shows the same problems on the
console as converted maps, the writer is at fault; if it plays like the stock map, it isn't.

### Fixes and test variants

After each map the converter logs its measures against the 16 stock 360 maps (placed models, world surfaces and triangles, see-through/decal surfaces, collision, materials, pictures and their memory, models within draw range of a spot, and more), with the stock highest and median, and flags any measure past the stock highest with what it costs on the console. It reads the written map back to measure it, which takes 10-20 seconds on a big map: untick **Map measures report** on the converter page to skip it while you're only trying switches (the files are the same either way).

Some conversion steps are fixes for problems seen on the console, and not all of them are proven.
Each can be switched off on the converter page (**Fixes (for testing)**), or from a command prompt
with `--fix-off NAME` (repeatable). All are on by default except `portal_multiply` (HDR portals are hidden instead) and the test switches `stream_pictures`, `hide_foliage`, `draw_distance_cap`, `no_cull_distance`, `room_box_bounds`, `merge_decals`, `skip_lod0`, `one_room`, `plain_pictures`, `stock_world`, `stock_materials`, `stock_pictures`, `merge_duplicates`, `model_box_bounds`, `rebuild_trees`, `huge_tree_boxes`, `ground_lit_flag`, `huge_leaf_boxes`, `huge_inner_boxes`, `tree_box_margin` and `swap_models_test` (`--fix-on NAME`). On the converter page the switches fold away under **Switches**, which says how many differ from the defaults:

| Fix | What it does |
|---|---|
| `texture_budget` | Pictures over the budget lose their top mip levels (out of memory / loading-screen freeze without it) |
| `stock_effects` | Effects a stock 360 map also has come from it (the yellow dust haze); maps have failed to load with it (MT_GetSize ... script usage) |
| `pc_sort_keys` | Each material keeps the PC's sort key (its draw pass: opaque, decal layers, glass, effects), which means the same on the 360 (all 271 materials PC mp_rust shares with stock 360 mp_rust match). Off: taken from a stock material with the same shader set, which moved backlot's decals and blend layers into other passes |
| `normal_maps_dxn` | PC normal maps (DXT5, X in alpha, Y in green) become DXN as every stock 360 normal map is (checked against stock mp_rust's in imagefile1.pak: within 2-3 levels of 255) |
| `stock_scripts` | A script a stock 360 file also has (a stock map's own `maps/mp/<map>.gsc`, effects scripts) comes from it; PC scripts can call functions only later PC patches have (PC mp_rust's `killTrigger`). The teams are still set in it |
| `stock_sounds` | Sound aliases come from a stock 360 file with the same alias, audio and all; others play the silent stock `null` sound (the PC file only names sound files the 360 doesn't have: a converted PC mp_rust froze on the loading screen) |
| `surface_bounds` | Fills in the 360-only culling radius / texture density of every world surface |
| `model_lods` | Model detail levels' partBits and surfs written as stock files have them (0 and empty) |
| `pc_face_culling` | A material the PC draws two-sided (no back-face culling: mp_backlot's market umbrellas, milk cartons, stone blocks) stays two-sided; the stock render state it takes culls back faces, so such models went invisible from one side. Face culling is the same bits on PC and 360 (state word 0, bits 14-15) |
| `stock_material_state` | A material a stock file also has takes that material's culling and draw order |
| `tree_model_bounds` | Culling tree boxes grow to enclose the static models they list (CoD4 ports list models sticking out of their box, which vanish as you turn), and each placed model's box grows to enclose its vertices |
| `model_box_bounds` | Test, off by default: each placed model's culling box grows to enclose its vertices as placed (stock maps' always do; mp_backlot's hanging lights sat 11 units below theirs), for models that vanish depending on the view angle |
| `lighting_origin` | A placed model whose lighting origin is empty (0,0,0) takes its box centre, as stock maps have it; the 360 lights each static model from the light grid at that point, and custom-compiled maps (mp_ancient: all 488) leave it at the world origin |
| `bone_bounds` | On by default: a model whose bone box (XBoneInfo) is broken (negative half-size, misses vertices, or radius squared not its half-diagonal's) gets it rebuilt from its vertices, as all 6,466 stock 360 bone boxes are. mp_ancient's PC file has every model's half-size negative on two axes; its boulders, bushes and grass vanished depending on where they were seen from |
| `dedupe_assets` | On by default: an asset used in several places is written once and pointed at after that (at the pointer that first held it, which the game sets to the asset when it loads it), as in every stock 360 file: no stock map holds two copies of anything. Stock assets copied in (the teams' models, effects) were written again wherever the stock file pointed back at them, each team body picture 120 times over (mp_waw_castle: 3,385 pictures in the file for 955 names, now 987; 1,550 copies left out). Every asset pointer still names the same asset |
| `tree_list_slices` | On by default: every culling tree node's list of static models is a slice of its room's root list, as in every stock 360 map (16,017 of 16,017). The 360 reorders placed models on load and renumbers only the root lists; lists kept apart (mp_ancient 213 of 215, mp_backlot 1,811 of 1,854) kept old numbers on the console and named other models, so models vanished depending on the nodes in view |
| `probe_brightness` | On by default: a map compiled without reflection probes (all probes the same stand-in picture: mp_waw_castle 20, mp_backlot 35) gets them scaled to stock 360 probes' brightness (color level 70; the stand-in converted to 160-190). Shiny surfaces and the player's gun reflect them: sandbags and scopes gleamed and the map looked washed out. Maps with real probes are left as they are |
| `map_effects` | Keep the effects the map's createfx script places (off: left out, its ambient sounds stay; for testing, e.g. the dust haze) |
| `map_fog` | Keep the map's distance fog (off: its `setExpFog` calls are commented out) |
| `hide_foliage` | Test, off by default: foliage static models never show (to test whether too many models in view make the 360 drop some) |
| `draw_distance_cap` | Test, off by default: every static model's draw distance capped at 1,200 units (same test) |
| `no_cull_distance` | Test, off by default: every static model's cull distance becomes 0, which the 360 reads as never hidden by distance (stock mp_terminal has 0 for 206 models the PC gives 2,800-5,250; mp_ancient keeps 2,000 for 206), for models that vanish |
| `room_box_bounds` | Test, off by default: each room's box grows to enclose every static model and surface its culling tree lists; the 360's shadow pass skips rooms whose box is outside the shadow view, so whatever sticks out loses its shadow (mp_backlot: 520 items, up to 537 units; stock mp_terminal: 64, up to 349) |
| `merge_decals` | Test, off by default: blend decals lying on the ground become layers of composite ground materials, as the 360 map compiler builds them (one pass, blended per vertex) instead of separate see-through surfaces (converted mp_rust: 2,061 of 2,291 merged into 122 composites; 121 match stock mp_rust's composites field for field, and the per-vertex layer data matches stock's at 99.9% of shared vertices). See `decals.py` for the format |
| `skip_lod0` | Test, off by default: models with several detail levels never use the closest one (LOD0), to test whether LOD0 is what the 360 fails to draw |
| `stock_models` | Static models a stock 360 file also has are drawn with the stock copy (do the converted models cause a problem?) |
| `stream_pictures` | Test, off by default: the map's own pictures stream from `imagefile9.pak` as stock maps' do (full size, no map memory: converted mp_rust 106 MB -> 82 MB, stock 64 MB). The pak, `mw2port_out\imagefile9.pak`, is shared by every map converted this way and only grows (a picture already in it is reused); copy it to the game folder next to `default_mp.xex`, like `imagefile8.pak` |
| `stock_world` | Test, off by default: for a PC copy of a stock map, the world assets (drawn world, collision, entities, effect placement) come from the stock 360 map of the same name |
| `stock_materials` | Test, off by default: for a PC copy of a stock map, every material the stock 360 map has under the same name comes from it whole (shader set, render state, pictures) |
| `stock_pictures` | Test, off by default: for a PC copy of a stock map, every picture the stock 360 map has under the same name comes from it, streamed from the game's own `imagefile1-4.pak` |
| `rebuild_trees` | Test, off by default: each room's culling tree is built anew from its surfaces and static models (boxes fitted to contents, split in two down to 16 items, every node listing the models below it); rooms and portals stay. mp_ancient's rocks and foliage vanished by view angle with the converted trees and didn't with `one_room` |
| `huge_tree_boxes` | Test, off by default: every culling tree node's box becomes the whole map's box, so the 360 never skips a node as out of view and tests each model against its own box; tells whether node skipping or the per-model test loses mp_ancient's foliage |
| `ground_lit_flag` | Test, off by default: a placed model with a ground colour gets the 360's ground-lit flag (0x02), as in every stock map (35,119 of 35,119); mp_ancient marks foliage ground-lit on the model, so 468 of 474 placements had a colour but no flag |
| `huge_leaf_boxes` | Test, off by default: `huge_tree_boxes` for culling tree nodes with no children only (nodes with children keep their boxes), to find which level of the tree the 360 misjudges |
| `huge_inner_boxes` | Test, off by default: `huge_tree_boxes` for culling tree nodes with children only (nodes with no children keep their boxes) |
| `tree_box_margin` | Test, off by default: every culling tree node's box grows by 64 units on each side; enough to stop the vanishing means the 360 misjudges node boxes by a small margin |
| `swap_models_test` | Test, off by default: placed static models #60 and #487 (on mp_ancient two boulders; #60 vanishes, #487 doesn't) trade numbers while staying where they are, with every list naming them updated; tells whether the vanishing goes with the place or the number |
| `one_room` | Test, off by default: the map is treated as one room (each room's culling tree lists everything, no portals) |
| `plain_pictures` | Test, off by default: the map's own 2D pictures become plain 16x16 ones (white / flat normal / black specular), to test the picture conversion |
| `surface_order` | The world's surfaces laid out as the 360 tools do (solid, then decals / see-through, then shadow casters) with the draw ranges to match; IW4x maps list every surface as opaque |
| `merge_duplicates` | Test, off by default (PC mp_rust stopped partway through loading with it on): same-named assets (shader sets several PC ones turn into, the PC map's own team models next to the stock team) are written once, as stock files have them; the game keeps the first of two anyway, so this only saves memory (PC mp_rust: about 10 MB) |
| `hide_tool_surfaces` | Radiant tool shaders (clip, caulk, ...) draw nothing |
| `portal_multiply` | HDR portal sheets drawn with a multiply shader (off: hidden like tool surfaces) |

**Restore default switches** puts every switch back the way it is normally.

**Game asset room.** The game holds a fixed number of assets of each kind at once (TU6: 3,584
pictures, 4,096 materials, 1,536 models, 768 shader sets, 600 effects, 1,350 loaded sounds, ...),
shared by the map and the files that stay loaded with it (common_mp, code_post_gfx_mp). Over one,
the map stops loading part way. The converter counts them (named references to assets the game
already has take no room, but each full copy of one takes a place of its own), logs the four kinds
closest to their limit, warns above 95% and stops with an error when one is over. Stock maps never
carry a copy of an asset the game already has: silent sounds the converter makes for aliases no
stock file has name the game's `$default` volume curve (65 copies of it stopped PC mp_showdown
with "Exceeded limit of 64 'sndcurve' assets").

**Rust crash tests.** To find which part of a converted PC copy of a stock map (PC mp_rust) makes it
crash or flicker, convert it once per test: **Restore default switches**, then change one switch,
so that one kind of data comes from the stock 360 map instead of being converted (or the other way
round): `stock_world` on, `stock_materials` on, `stock_pictures` on, `stock_models` off (converted
models), `stock_effects` off (converted effects). The test that stops (or starts) the crash names
the part at fault.

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
