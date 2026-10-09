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
with `--fix-off NAME` (repeatable). All are on by default except `portal_multiply` (HDR portals are hidden instead) and the test switches `stream_pictures`, `stream_full_chains`, `hide_foliage`, `draw_distance_cap`, `no_cull_distance`, `room_box_bounds`, `skip_lod0`, `one_room`, `plain_pictures`, `stock_world`, `stock_materials`, `stock_pictures`, `merge_duplicates`, `model_box_bounds`, `rebuild_trees`, `huge_tree_boxes`, `huge_leaf_boxes`, `huge_inner_boxes`, `tree_box_margin`, `swap_models_test`, `convert_anims`, `split_car_fire`, `list_techsets` and `stock_layout` (`--fix-on NAME`). On the converter page the switches fold away into two lists, each in alphabetical order: **Test switches** (off by default) and **Fixes on by default** (always shown folded), each saying how many differ from the defaults. Next to **Convert**, untick **Make the loading screen** to convert only the map file and keep the `<map>_load.ff` already in `mw2port_out` (`port_map(..., load_screen=False)`):

| Fix | What it does |
|---|---|
| `texture_budget` | Pictures over the budget lose their top mip levels, the largest first and specular (shine) maps before color and normal maps (out of memory / loading-screen freeze without it) |
| `stock_effects` | Effects a stock 360 map also has come from it (the yellow dust haze); maps have failed to load with it (MT_GetSize ... script usage) |
| `pc_sort_keys` | Each material keeps the PC's sort key (its draw pass: opaque, decal layers, glass, effects), which means the same on the 360 (all 271 materials PC mp_rust shares with stock 360 mp_rust match). Off: taken from a stock material with the same shader set, which moved backlot's decals and blend layers into other passes |
| `sort_key_kind` | A PC material keeps its own sort key (draw order) only if stock 360 materials with that key use the same kind of shader set: in every stock file, keys 0-33 go only with shader sets that have the lit technique (9) and 34 and up only with ones that don't. Otherwise it takes its stock template's key. The game sorts materials of one key by their lit technique's shaders without checking the second one has it (TU6 0x82406418): converted mp_bo2cove's `mc/mtl_p6_cas_rock_foliage_cover_blend`, a lit model with the PC key 43 (common_mp's heat distortion's), crashed the console while loading (KMODE_EXCEPTION_NOT_HANDLED at 0x82406530) |
| `normal_maps_dxn` | PC normal maps (DXT5, X in alpha, Y in green) become DXN as every stock 360 normal map is (checked against stock mp_rust's in imagefile1.pak: within 2-3 levels of 255) |
| `drop_pc_effects` | An effect taken from a stock 360 file no longer keeps the PC's converted copy next to it (renamed `~pc/...`), unless the map's other assets point into it; pointers to the copy go to the stock one. Each copy took one of the game's 600 effect places: PC copies of stock maps carried every effect twice (PC mp_quarry: 705 of 600 with common_mp, so it couldn't be converted; now 558, as stock) |
| `drop_pc_models` | Likewise for static models drawn with their stock 360 copy (`stock_models`): the converted copy is left out unless other assets point into it (PC mp_estate: 1,544 of the game's 1,536 models; now 1,472) |
| `share_effect_pictures` | A picture of the map's (from the PC game's files, not the map's own `.iwd`) with the name of one a stock 360 effect it uses brings is taken from the effect (its slot) instead of being kept a second time as `~pc/...`. PC copies of stock maps carried 67-123 pictures twice (mp_estate: 3,514 of the game's 3,584; now 3,443, under stock's 3,449) |
| `stock_scripts` | A script a stock 360 file also has (a stock map's own `maps/mp/<map>.gsc`, effects scripts) comes from it; PC scripts can call functions only later PC patches have (PC mp_rust's `killTrigger`). The teams are still set in it |
| `convert_anims` | Test, off by default: an animation no stock 360 file has is converted to the 360's layout (`xanim.py`) instead of being left out: rotation keys packed as the 360 keeps them (a full rotation in 32 bits: sign and place of the largest component, the other three over it in 10, 10 and 9 bits; a half rotation in 16) and the part types laid out for the 360's two extra. Checked against the 36 animations PC mp_derail, mp_estate, mp_highrise, mp_invasion and mp_quarry share with stock 360 files: 99.35% of 51,051 values the same, the rest by the last unit; PC mp_quarry converted with it reads back with all 22 animations' fields, bone names and notifies as stock. Animations that move the whole object (delta parts) are still left out |
| `stock_sounds` | Sound aliases come from a stock 360 file with the same alias, audio and all; others play the silent stock `null` sound (the PC file only names sound files the 360 doesn't have: a converted PC mp_rust froze on the loading screen) |
| `ambient_tracks` | A map's ambience track (`ambientPlay` in its script) that no stock 360 file has becomes the closest of the 360's own in common_mp (`ambient_mp_desert`, `_urban`, `_snow`, `_rain`, `_rural`, ...), picked by the words in its name and sound file (PC mp_showdown's `ambient_crossfire`, `amb_middle_east1v7_lr.mp3`: `ambient_mp_desert`). The 360 plays ambience only streamed from its disc: converted mp_showdown with its own track encoded stopped with "alias ambient_crossfire ... played as an ambient / music track is not streamed". The track no longer played isn't encoded. |
| `encode_sounds` | On by default (confirmed on the console: converted mp_showdown plays its vehicles' PC sounds): a sound alias no stock 360 file has plays the PC's own audio, encoded as XMA (`xma.py`, the only format the 360 plays) instead of the silent `null` sound: the map's own sounds and the PC game's streamed ones (from the `.iwd` files of the PC game folder given), as loaded sounds; sounds the game already has loaded (common_mp) are named only. Up to 12 MB. Needs numpy, and miniaudio for .mp3 and compressed .wav sounds (CoD4 maps' ambient tracks) (`python -m pip install numpy miniaudio`; mw2tools.bat installs both). PC mp_showdown: all 65 silent aliases get their audio, its 6-minute .mp3 ambience among them (54 sounds, 5.6 MB). numpy is first tried in a short process of its own: one that crashes or hangs loading leaves the sounds silent with a warning instead of stopping the conversion. The log shows the encoding as it goes, and encoded sounds are kept in `mw2port_cache/sounds`, so converting the map again (or its test variants) doesn't encode them again. `xma.py` and `xmatables.py` are LGPL 2.1 or later (derived from FFmpeg's WMA Pro decoder): see `LICENSE.LGPL` |
| `surface_bounds` | Fills in the 360-only culling radius / texture density of every world surface |
| `model_lods` | Model detail levels' partBits and surfs written as stock files have them (0 and empty) |
| `pc_face_culling` | A material the PC draws two-sided (no back-face culling: mp_backlot's market umbrellas, milk cartons, stone blocks) stays two-sided; the stock render state it takes culls back faces, so such models went invisible from one side. Face culling is the same bits on PC and 360 (state word 0, bits 14-15) |
| `stock_material_state` | A material a stock file also has takes that material's culling and draw order |
| `tree_model_bounds` | Culling tree boxes grow to enclose the static models they list (CoD4 ports list models sticking out of their box, which vanish as you turn), and each placed model's box grows to enclose its vertices |
| `model_box_bounds` | Test, off by default: each placed model's culling box grows to enclose its vertices as placed (stock maps' always do; mp_backlot's hanging lights sat 11 units below theirs), for models that vanish depending on the view angle |
| `lighting_origin` | A placed model whose lighting origin is empty (0,0,0) takes its box centre, as stock maps have it; the 360 lights each static model from the light grid at that point, and custom-compiled maps (mp_ancient: all 488) leave it at the world origin |
| `bone_bounds` | On by default: a model whose bone box (XBoneInfo) is broken (negative half-size, misses vertices, or radius squared not its half-diagonal's) gets it rebuilt from its vertices, as all 6,466 stock 360 bone boxes are. mp_ancient's PC file has every model's half-size negative on two axes; its boulders, bushes and grass vanished depending on where they were seen from |
| `stock_techsets` | On by default: shader sets the map's materials use (and, with `merge_decals`, the composite sets its decals can become) that the stock files read for the map lack are looked for in every stock map in the work folder; the one or two carrying the most are read as well, without their world. Before, a material got the nearest set the files read had. Converting PC mp_rust without its own stock file, against stock 360 mp_rust: render state right on 661 of 664 materials (was 658), shader set on 663 (was 661); mp_waw_castle with `merge_decals`: 1,065 decal surfaces merged (was 220), 267 see-through surfaces left (was 1,326; stock highest 740) |
| `material_memory` | On by default: the world's list of the materials its surfaces draw with (materialMemory) is made as every stock 360 map has it (1,866 of 1,866 entries in mp_rust, mp_favela and mp_afghan): every material on a surface, sorted by name, 44 bytes a vertex + 6 a triangle + 40 a surface. The PC counts differently, and `merge_decals`' composite materials were missing from it |
| `dedupe_assets` | On by default: an asset used in several places is written once and pointed at after that (at the pointer that first held it, which the game sets to the asset when it loads it), as in every stock 360 file: no stock map holds two copies of anything. Stock assets copied in (the teams' models, effects) were written again wherever the stock file pointed back at them, each team body picture 120 times over (mp_waw_castle: 3,385 pictures in the file for 955 names, now 987; 1,550 copies left out). Every asset pointer still names the same asset |
| `tree_list_slices` | On by default: every culling tree node's list of static models is a slice of its room's root list, as in every stock 360 map (16,017 of 16,017). The 360 reorders placed models on load and renumbers only the root lists; lists kept apart (mp_ancient 213 of 215, mp_backlot 1,811 of 1,854) kept old numbers on the console and named other models, so models vanished depending on the nodes in view |
| `probe_brightness` | On by default: a map compiled without reflection probes (all probes the same stand-in picture: mp_waw_castle 20, mp_backlot 35) gets them scaled to stock 360 probes' brightness (color level 70; the stand-in converted to 160-190). Shiny surfaces and the player's gun reflect them: sandbags and scopes gleamed and the map looked washed out. Maps with real probes are left as they are |
| `map_effects` | Keep the effects the map's createfx script places (off: left out, its ambient sounds stay; for testing, e.g. the dust haze) |
| `map_fog` | Keep the map's distance fog (off: its `setExpFog` calls are commented out) |
| `hide_foliage` | Test, off by default: foliage static models never show (to test whether too many models in view make the 360 drop some) |
| `draw_distance_cap` | Test, off by default: every static model's draw distance capped at 1,200 units (same test) |
| `no_cull_distance` | Test, off by default: every static model's cull distance becomes 0, which the 360 reads as never hidden by distance (stock mp_terminal has 0 for 206 models the PC gives 2,800-5,250; mp_ancient keeps 2,000 for 206), for models that vanish |
| `room_box_bounds` | Test, off by default: each room's box grows to enclose every static model and surface its culling tree lists; the 360's shadow pass skips rooms whose box is outside the shadow view, so whatever sticks out loses its shadow (mp_backlot: 520 items, up to 537 units; stock mp_terminal: 64, up to 349) |
| `merge_decals` | On by default (confirmed on the console with mp_waw_castle): blend decals lying on the ground become layers of composite ground materials, as the 360 map compiler builds them (one pass, blended per vertex) instead of separate see-through surfaces (converted mp_rust: 2,061 of 2,291 merged into 122 composites; 121 match stock mp_rust's composites field for field, and the per-vertex layer data matches stock's at 99.9% of shared vertices). See `decals.py` for the format. With it, solid surfaces are also sorted so one material's are neighbours (the 360 draws a run of them as one), and the vertex buffer keeps only the vertices surfaces use: converted mp_rust 868 draws (stock 949; 4,646 before) and 192,478 vertices, all used (333,845 before) |
| `skip_lod0` | Test, off by default: models with several detail levels never use the closest one (LOD0), to test whether LOD0 is what the 360 fails to draw |
| `stock_models` | Static models a stock 360 file also has are drawn with the stock copy (do the converted models cause a problem?) |
| `destructible_parts` | On by default (works on the console): the parts a destructible car or prop needs when it breaks (destroyed model, hood, doors, wheels, ...: the names the 360's `common_scripts/_destructible_types.gsc` builds for the map's `destructible_type`s) that neither the map nor common_mp has come from a stock 360 map; a part no stock map has in the car's color is copied in another color and painted with the map's own material for its color (`mc/mtl_80s_econ_red` -> `mc/mtl_80s_econ_silv`). Each stock map's model names are listed once into `mw2port_cache`. mp_backlot (a CoD4 port, whose cars carry CoD4's part names): 27 parts from mp_checkpoint and mp_invasion; blowing a car up showed a stand-in model without them |
| `name_common_assets` | On by default: a picture, material, model, effect or shader set the map carries under a name the always-loaded `common_mp.ff` also has becomes a reference to the loaded one (`,name`), as stock maps name them (none of mp_invasion, mp_rust, mp_afghan, mp_terminal and mp_favela carries a second copy of anything common_mp has). A second copy takes the name over when the map loads: PC mp_backlot's own 128x128 `fire_roar_pm_atlas` replaced common_mp's streamed one, and the console froze (the graphics chip hung) the first time a burning car's flames drew it. Physics presets too, and a top-level raw file (vision, script) common_mp has is left out (the game loads raw files by name; common_mp has IW's own 360 `vision/mp_backlot.vision`). One that other assets point inside stays a copy (logged). mp_backlot: 45 assets named, 2 physics presets, its vision file left out |
| `stock_layout` | Test, off by default: the file laid out as all 16 stock 360 maps are (rules mined by `.claude/skills/mw2-360-map-port/scripts/stock_rules.py`): every shader set an asset list entry of its own (as `list_techsets`); the map's entities written inside the collision map, not as an entry; the one-of-a-kind assets in stock order (com_map, fx_map, lightdef, ..., gfx_map, game_map_mp, col_map_mp); one copy of each picture, material, model geometry, pixel shader and physics preset per name (a copy that differs is replaced by the stock one, else the first); and every asset held or pointed at in more than one place given a slot so the writer writes it once (it wrote a second full copy at the next holder: mp_backlot's cardboard box model in an effect and in the collision map). mp_backlot: 97 copies merged, 720 shared assets written once, no stock rule broken |
| `list_techsets` | Test, off by default: every shader set the map carries in full becomes an asset list entry of its own, just before the first asset that uses it, and materials point at that entry, as stock 360 maps list every one (mp_invasion's car fire shader set `effect_zfeather_falloff_add_eyeoffset` is entry 1142, the fire effect 1160). Converted maps wrote each inside the first material that used it (mp_backlot: 29, inside the world, the collision map's models, effects and models) |
| `split_car_fire` | Test, off by default (a diagnosis, not a fix): the burning car effect `smoke/car_damage_blacksmoke_fire` froze the console on converted mp_backlot although its data matches stock. Each group of its elements moves into an effect set off on its own: shooting a headlight plays its omni light, a brake light its halogen glow (`gfx_flare_halogen_glw_z05`), a side window its heat distortion, the windshield its bright core (`gfx_exp_igc`), the first smoke its white puff; the fire keeps only its flame sprites (`gfx_fire_roar_pm_atlas_z10_godray60_eye20`). The first that freezes names the part at fault |
| `effect_sister_materials` | On by default: an effect material whose shader set common_mp doesn't have (so the map would carry the shader set itself) is replaced by common_mp's own material of the same name without the eye offset (`_eyeN`), when that one draws the same color picture with the same render states. The car fire's flames (`gfx_fire_roar_pm_atlas_z10_godray60_eye20`, shader set `effect_zfeather_falloff_add_eyeoffset`) froze the console on converted mp_backlot (`split_car_fire` named them) although they match stock byte for byte; common_mp's `gfx_fire_roar_pm_atlas_z10_godray60` is what backlot's own fires draw. The flames sit 20 units further from the eye |
| `destructible_sounds` | On by default (needs `stock_sounds`): the sounds the 360's `common_scripts/_destructible_types.gsc` plays for the map's `destructible_type`s (a burning car's `fire_vehicle_med` and `fire_vehicle_flareup_med`, ...) that neither the map nor common_mp has come from a stock 360 map, as every stock map carries them in its own file. Each stock map's alias names are listed once into `mw2port_cache`. mp_backlot (a CoD4 port with no sounds at all): 2 from mp_afghan; its cars burned silently without them |
| `stock_streamed_pictures` | On by default: a picture the map takes from the PC game's files (not its own `.iwd`) that a stock 360 map streams under the same name streams from the 360's own packs (imagefile1-4.pak) at full size, as in that stock map, instead of sitting in the map file. The stock maps' streamed pictures are listed once into `mw2port_cache`. PC mp_showdown: 546 pictures stream, every vehicle's among them; 121 stay in the file and only 2 lose a mip level (444 without it) |
| `stream_pictures` | Test, off by default: the map's own pictures stream from paks as stock maps' do (full size, no map memory: converted mp_waw_castle keeps 11 MB of pictures instead of 42). Every map converted this way shares the paks in `mw2port_out`: `imagefile9.pak` (or the **First pak for streamed pictures** chosen, 9-20; `--pak-start N`) until it holds 1 GB, then the next number, up to 20, the highest the game can open (its table of open pak files has room for 21). New pictures only go on the end of the newest pak and a picture already in one (from the first number on) is reused, so after a new map only the paks its log names need copying again; maps converted earlier keep working. Start a batch on a higher number to keep it apart from the paks already on the console. Copy the paks to the game folder next to `default_mp.xex`, where `imagefile1.pak` to `imagefile4.pak` are, not to `_codxe\zone\` (codxe sends only `imagefile5.pak` there); the converter page lists them apart from the maps. A map whose pak the game can't open stops the console (converted mp_bo2cove without its pak: a kernel crash) |. Every level's recorded data size is its whole mip chain's, as in stock records (each level above the smallest holds only its own top mip, as mw2tex's camos do): sizes of the top mip alone gave "disc is unreadable" on every converted bo2 map |
| `stream_full_chains` | Test, off by default: with `stream_pictures`, every level of a streamed picture holds its whole mip chain in the pak, as stock paks do, instead of only its own top mip (mw2tex found a whole chain gave "disc is unreadable" for its camos). Try it if streamed maps still say "disc is unreadable"; the new chunks go on the end of the pak, so copy the pak again |
| `stock_world` | Test, off by default: for a PC copy of a stock map, the world assets (drawn world, collision, entities, effect placement) come from the stock 360 map of the same name |
| `stock_materials` | Test, off by default: for a PC copy of a stock map, every material the stock 360 map has under the same name comes from it whole (shader set, render state, pictures) |
| `stock_pictures` | Test, off by default: for a PC copy of a stock map, every picture the stock 360 map has under the same name comes from it, streamed from the game's own `imagefile1-4.pak` |
| `rebuild_trees` | Test, off by default: each room's culling tree is built anew from its surfaces and static models (boxes fitted to contents, split in two down to 16 items, every node listing the models below it); rooms and portals stay. mp_ancient's rocks and foliage vanished by view angle with the converted trees and didn't with `one_room` |
| `huge_tree_boxes` | Test, off by default: every culling tree node's box becomes the whole map's box, so the 360 never skips a node as out of view and tests each model against its own box; tells whether node skipping or the per-model test loses mp_ancient's foliage |
| `ground_lit_flag` | On by default: a placed model with a ground colour gets the 360's ground-lit flag (0x02), as in every stock map (35,119 of 35,119, none without a colour); mp_ancient marks foliage ground-lit on the model, so 468 of 474 placements had a colour but no flag; converted mp_backlot had 2029 of 3619 without it, so props in shade were lit from the light grid instead of their baked ground colour and looked too bright |
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

Run these from the `tools\mw2ff` folder (Python 3, nothing to install; numpy only for `encode_sounds`):

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
