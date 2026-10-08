"""Convert a PC (IW4x / 2009 PC, version 276) fastfile to Xbox 360 TU6 layout.

    python port.py mp_geometric.ff OUT.ff [--iwd mp_geometric.iwd] [--ref360 FILE.ff ...]
                   [--teams ALLIES AXIS]

The PC file is read as a tree (tree.py). Every struct is carried over to the 360's struct
of the same name field by field (numbers in big-endian, fields matched by name); the pieces
the two platforms store differently are converted by the hooks below:

  - map vertices: normals and tangents repacked (PC 8-bit + scale -> 360 10:10:10)
  - techsets (shaders): PC shaders can't run on the 360, so each techset is swapped for the
    360's own techset of the same name, from a stock 360 file (--ref360) or, when the game
    always has it loaded (code_post_gfx_mp), a reference to it by name (",name")
  - materials: the 360 has fewer techniques per material; render state comes from a 360
    material using the same techset
  - images: pixels from the .iwd (.iwi files) or from the fastfile are tiled for the 360 GPU
    and stored in the fastfile with their mipmaps
  - teams: IW4x loads team assets from its own files, the 360 only from the map's file, so the
    two teams the map's .arena names (soldiers, flags, crates, icons) are copied in from stock
    360 maps given with --ref360 that have them
"""

import argparse
import contextlib
import copy
import io
import math
import os
import re
import struct
import sys
import zipfile
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "mw2tex"))

import codec as codec_mod
import decals
import mw2ff
import schema as schema_mod
import tree
from cdefs import PTR, Compound, Enum, Prim
from tree import Leaf, PtrList, Ref, Str

X_DEFAULT_REFS = ["code_post_gfx_mp.ff", "mp_favela.ff"]
# Unions whose members are separate bytes on both platforms (the "packed" number is only a
# way to copy them): their bytes stay as they are. Checked against stock Favela.
BYTE_UNIONS = {"GfxSurfaceLightingAndFlags"}
# Zones the 360 keeps loaded the whole time: their assets can be used by name.
RESIDENT = ("code_post_gfx_mp", "common_mp")
# The pictures a ported map carries sit in the file's physical memory block (stock maps keep
# most of theirs in the console's image files: 9 to 26 MB). The console ran out of memory with
# 201 MB of them (mp_backlot) and loads 90 MB (mp_waw_castle). Pictures above this budget (MB
# of physical memory) lose their top mip levels, the largest first. 0 keeps them all. At 80
# mp_backlot froze on the loading screen; every test at 40 loaded.
TEXTURE_BUDGET_MB = 40
# The 360 tiles pictures and pads the small mip levels: the physical block comes to about this
# much more than the plain sum of the levels (mp_backlot: 90 MB counted, 115 MB in the block).
TILING_PAD = 1.28
LEVELS = 4      # pak table entries per picture
STREAM_PAK = 9          # first pak for map pictures: mw2tex uses 7 (textures) and 8 (titles and emblems)
# The highest pak number the game can open: TU6 keeps one 72-byte record per pak number
# (0x821E3168, Com_sprintf "imagefile%d") in a table at 0x82CBC640, and the next object
# starts at 0x82CBCC80, so 22 records (0 = inside the map file; stock maps use 1-4). It doesn't
# check the number: a higher one would write over whatever follows.
STREAM_PAK_LAST = 21
PAK_VOLUME_MB = 1024    # a pak full past this is left as it is; the next map pictures go in the next
STREAMABLE = ("DXT1", "DXT3", "DXT5", "DXN")
# Fixes that can be switched off, to find out on the console which one helps or hurts:
# (name, short label, what it does). All on by default.
SWAP_MODELS = (60, 487)          # placed static models swapped by the swap_models_test switch
FIXES = [
    ("texture_budget", "Picture budget",
     "Pictures over the budget (40 MB) lose their top mip levels, so big maps fit in memory "
     "(mp_backlot ran out of memory without it, and froze on the loading screen at 80 MB)."),
    ("stock_effects", "Stock 360 effects",
     "Effects a stock 360 map also has (dust, car glass, fires) come from it instead of being "
     "converted: the converted dust drew a yellow haze. (An early mp_backlot test didn't load "
     "with it: MT_GetSize: max allocation exceeded ... for script usage.)"),
    ("pc_sort_keys", "Keep the PC's draw order (sort keys)",
     "Each material keeps its own sort key (which pass it draws in: opaque, decal layers, glass, "
     "effects), which means the same on the 360: all 271 materials PC mp_rust shares with stock "
     "360 mp_rust have the same one. Off: it comes from a stock material with the same shader set, "
     "which moved backlot's decals and blend layers into other passes (decals to opaque, ...)."),
    ("normal_maps_dxn", "Normal maps as DXN",
     "PC normal maps (DXT5: X in alpha, Y in green) become DXN, two channels, as every stock 360 "
     "normal map is: X is the PC's alpha block as it is, Y its green channel. Off: they stay DXT5, "
     "which the 360's shaders read as if DXN."),
    ("drop_pc_effects", "PC copies of stock effects left out",
     "An effect taken from a stock 360 file (Stock 360 effects) no longer keeps the PC's converted "
     "copy next to it under a ~pc/ name: each took one of the game's 600 effect places, so a PC "
     "copy of a stock map carried every effect twice (PC mp_quarry: 294 effects, with common_mp's "
     "411 past the limit, so it couldn't be converted; stock mp_quarry has 147). Pointers to the PC "
     "copy go to the stock one."),
    ("share_effect_pictures", "Pictures shared with stock effects",
     "A picture of the map's (from the PC game's files, not the map's own .iwd) with the name of one "
     "a stock 360 effect it uses brings is taken from the effect instead of being kept a second "
     "time under a ~pc/ name: each copy took one of the game's 3,584 picture places (PC "
     "mp_estate came to 3,514 with 71 pictures twice)."),
    ("drop_pc_models", "PC copies of stock models left out",
     "A static model drawn with its stock 360 copy (Stock 360 models) no longer keeps the PC's "
     "converted copy in the file under a ~pc/ name, unless other assets point into it: each took "
     "one of the game's 1,536 model places (PC mp_estate came to 1,544 with every model twice)."),
    ("stock_scripts", "Stock 360 scripts",
     "A script a stock 360 file also has (a stock map's own maps/mp/<map>.gsc, its effects "
     "scripts) comes from it. PC scripts can call what only later PC patches have: PC mp_rust's "
     "killTrigger isn't in the 360's scripts. The teams are still set in the map's script."),
    ("stock_anims", "Stock 360 animations",
     "Animations (XAnimParts: swaying foliage, fans, lockers, windsocks) a stock 360 file also has "
     "come from it. The 360 packs rotations differently (a quaternion in 32 bits, the largest "
     "component left out) and has two more part types, so PC animations can't be copied as they "
     "are: PC mp_terminal and mp_afghan didn't convert at all. Animations no stock file has are "
     "left out of the map (it then lacks those movements)."),
    ("convert_anims", "Convert PC animations (test)",
     "An animation no stock 360 file has (a map's own fans, foliage sway, flags) is converted to the "
     "360's layout instead of being left out: its rotation keys packed as the 360 keeps them (32 "
     "bits a full rotation, 16 a half one) and its part types laid out for the 360's two extra. "
     "Checked against the 36 animations PC mp_derail, mp_estate, mp_highrise, mp_invasion and "
     "mp_quarry share with stock 360 files: 99.35% of 51,051 values the same, the rest by the last "
     "unit. Animations that move the whole object (delta parts) are still left out. Off by default "
     "until tried on a console."),
    ("stock_sounds", "Stock 360 sounds",
     "Sound aliases a stock 360 file also has come from it, with its 360 audio. The PC file only "
     "names its sound files (,null.wav: files the PC game loads from disk); the 360 has nothing "
     "under those names, and a converted PC mp_rust froze on the loading screen with them. An "
     "alias no stock file has plays the silent stock \"null\" sound."),
    ("encode_sounds", "Encode PC sounds for the 360 (test)",
     "A sound alias no stock 360 file has gets the PC's own audio, encoded as XMA (the only "
     "sound format the 360 game plays), instead of the silent \"null\" sound: the map's own "
     "sounds, and the PC game's streamed ones (sound/... in the .iwd files of the PC game folder "
     "given). Streamed sounds become loaded ones (the 360 streams only from its own disc). "
     "Needs numpy, and miniaudio for .mp3 sounds (CoD4 maps' ambient tracks). Off by default "
     "until tried on a console."),
    ("surface_bounds", "Surface culling radius",
     "Fill in the 360-only number every world surface carries (its culling radius and texture "
     "density), worked out from stock mp_rust. Converted maps used to leave it 0."),
    ("model_lods", "Model detail levels as stock",
     "Each model detail level's partBits and surfs written as stock 360 files have them (0 and "
     "empty) instead of the PC's values."),
    ("pc_face_culling", "Keep the PC's two-sided materials",
     "A material the PC draws two-sided (no back-face culling: mp_backlot's market umbrellas, "
     "milk cartons, stone blocks) stays two-sided. Its render state comes from a stock material "
     "with the same shader set, which culls back faces, so such models went invisible from one "
     "side. Untick to take the stock state as it is."),
    ("stock_material_state", "Render state from same-named stock materials",
     "A material a stock 360 file also has (same name, same shader set) takes that material's "
     "culling and draw order, instead of another material's with the same shader set."),
    ("tree_model_bounds", "Tree boxes enclose their models",
     "Each culling tree node's box grows to enclose every static model it lists, as in stock 360 "
     "maps (mp_rust: all of them). CoD4 ports (mp_backlot: 471 of 3,619 models, up to 537 units "
     "out) list models that stick out of their node, which the 360 then skips as you turn. Each "
     "placed model's own box also grows to enclose its vertices (mp_backlot's hanging lights sat "
     "11 units below theirs)."),
    ("model_box_bounds", "Model boxes enclose their vertices (test)",
     "Each placed model's culling box (the one the 360 tests the model against when its tree "
     "node is partly in view) grows to enclose the model's vertices as placed. Stock maps' boxes "
     "cover every vertex (mp_rust: 3,356 of 3,356); mp_backlot's hanging fluorescent lights sat 11 "
     "units below theirs. Off by default: for testing models that vanish depending on the "
     "angle you look at them from."),
    ("lighting_origin", "Model lighting origins at their boxes",
     "A placed model whose lighting origin is empty (0,0,0) takes its culling box's centre "
     "instead, as stock 360 maps have it (mp_terminal: 4,032 of 4,032). The 360 lights each "
     "static model from the light grid at that point; custom-compiled maps (mp_ancient: all "
     "488) leave it at the world origin, so every model was lit as if it stood there."),
    ("probe_brightness", "Stand-in reflection probes at stock brightness",
     "A map compiled without reflection probes (every probe the same stand-in picture: "
     "mp_waw_castle's 20, mp_backlot's 35) gets them scaled to the brightness of stock 360 "
     "probes (an average color level of %d; the stand-in converts to about 160-190). Every "
     "shiny surface, and the player's gun, reflects the map's probes: too bright, sandbags and "
     "scopes gleamed and the whole map looked washed out. Maps with real probes are left as "
     "they are." % 70),
    ("dedupe_assets", "Each asset written once",
     "An asset used in several places is written once and pointed at after that, as in every "
     "stock 360 file (no stock map holds two copies of anything). Stock assets copied in "
     "(the teams' models, effects) were written again wherever the stock file pointed back at "
     "them: each team body picture 120 times over, which took the game's room for assets and "
     "its memory."),
    ("stock_techsets", "Shader sets from every stock map",
     "Shader sets the map's materials use (and, with Merge decal layers, the composite sets its "
     "decals can become) that the stock files read for the map lack are looked for in every "
     "stock map given; the one or two carrying the most are read as well (without their world). "
     "Before, a material got the nearest set the files read had: mp_rust without its own stock "
     "file lost a detail map on one material, and most of mp_waw_castle's decals stayed "
     "unmerged because their composite sets were in other stock maps."),
    ("drop_pc_tables", "PC config string tables left out",
     "The PC's config string tables (configstrings_pc_<map>_<mode>.csv, 8 per map) are left out: "
     "the 360 game only ever looks for configStrings_360_<map>_<mode>.csv (a lookup that "
     "shortens network messages, and a checksum host and players compare; without one, both use "
     "the same default), so the PC ones only took memory and string table room."),
    ("material_memory", "World material list as the 360 counts it",
     "The world's list of the materials its surfaces draw with (materialMemory) is made as every "
     "stock 360 map has it (1,866 of 1,866 entries in mp_rust, mp_favela and mp_afghan): every "
     "material on a surface, sorted by name, with 44 bytes a vertex + 6 a triangle + 40 a "
     "surface. The PC counts differently, and the composite materials of Merge decal layers "
     "were missing from it (converted mp_rust: 76 entries, stock 177)."),
    ("tree_list_slices", "Culling tree model lists inside the room's list",
     "Every culling tree node's list of static models becomes a slice of its room's root list, "
     "as in every stock 360 map (16,017 of 16,017 lists): the root list is laid out so each node's "
     "models are next to each other, and each node points into it. When a map loads, the 360 "
     "reorders the placed models and renumbers each room's root list to match; a node list kept "
     "apart from it keeps the old numbers and names other models (mp_ancient on the console: "
     "2,884 of 3,368 entries), so models vanish depending on which nodes are in view. "
     "mp_ancient: 213 of 215 lists were apart; mp_backlot 1,811 of 1,854."),
    ("bone_bounds", "Model bone boxes from their vertices",
     "A model whose bone box (XBoneInfo: centre, half-size, radius squared) is broken gets it "
     "rebuilt from its vertices, as every stock 360 model has it: the box around all its detail "
     "levels' vertices, radius squared its half-diagonal squared (16 stock maps: 6,466 of 6,466). "
     "mp_ancient's PC file has every model's half-size negative on two axes, and its boulders, "
     "bushes and grass vanished depending on where they were seen from. Single-bone models only."),
    ("map_effects", "Map effects",
     "Keep the effects the map's createfx script places (mp_backlot: 32, its blowing dust among "
     "them). Off: they are left out of the script (its ambient sounds stay), to see whether "
     "they are behind a problem such as the yellow haze."),
    ("map_fog", "Map fog",
     "Keep the map's distance fog (setExpFog in its scripts; mp_backlot's is yellow). Off: the "
     "calls are commented out."),
    ("hide_foliage", "Hide foliage (test)",
     "Static models named foliage_* (grass clumps, shrubs, palms: 2,122 of mp_backlot's 3,619) get a "
     "draw distance of 1 unit, so they never show. Off by default. For testing whether the 360 "
     "drops models because too many are in view at once (mp_backlot: about 2,670 in range from a "
     "typical point, stock mp_rust 1,430-1,900)."),
    ("draw_distance_cap", "Cap model draw distance (test)",
     "Every static model's draw distance is capped at %d units (mp_backlot: about 960 models in "
     "range from a typical point instead of 2,670). Off by default; for the same test." % 1200),
    ("no_cull_distance", "No model cull distance (test)",
     "Every static model's cull distance becomes 0, which the 360 reads as never hidden by "
     "distance. Stock mp_terminal has 0 for 206 models whose PC cull distance is 2,800-5,250; "
     "the converter keeps the PC values (mp_ancient: 206 models at 2,000). Off by default: for "
     "testing whether models vanish because of their cull distance."),
    ("merge_decals", "Merge decal layers into the ground",
     "Blend decals lying on the ground (dirt, rust, stains) become layers of the ground's "
     "material, as the 360 map compiler builds them: composite materials drawing the ground and "
     "up to two decals in one pass, blended per vertex. The PC draws each decal as its own "
     "see-through surface over the ground (converted mp_rust: 2,291 of them; stock 101). "
     "Confirmed on the console (mp_waw_castle)."),
    ("room_box_bounds", "Room boxes enclose their contents (test)",
     "Each room's box (GfxCell.bounds) grows to enclose every static model and surface its "
     "culling tree lists. The 360's shadow pass tests each room's box against the shadow view "
     "and skips the whole room when it's outside, so whatever sticks out loses its shadow. "
     "mp_backlot: 520 of 14,568 stick out, up to 537 units (stock mp_terminal: 64, up to 349). "
     "Off by default."),
    ("skip_lod0", "Skip closest detail level (test)",
     "Converted models with more than one detail level never use their closest one (LOD0): its "
     "switch distance becomes 0, so the next level shows from up close. Off by default. For "
     "testing whether LOD0 is what the 360 fails to draw (mp_backlot's cover vanishes within its "
     "LOD0 range, 250-900 units, and shows further away)."),
    ("stock_models", "Stock 360 models where available",
     "Static models a stock 360 file also has (mp_backlot: about 98 kinds, every flickering cover "
     "type among them) are drawn with Infinity Ward's own 360 copy instead of the converted one. "
     "Untick to draw the converted models instead (if they then flicker and the stock ones "
     "didn't, the converted models are at fault)."),
    ("destructible_parts", "Breakable car parts (test)",
     "The parts a destructible car or prop needs when it breaks (its destroyed model, hood, "
     "doors, wheels, ...: the names the 360's own destructible script asks for) that neither the "
     "map nor the always-loaded files have come from a stock 360 map that has them; a part no "
     "stock map has in the car's color is copied in another color and painted with the map's own "
     "material for its color. CoD4 ports carry other names for these (mp_backlot's silver and "
     "yellow sedans and brown wagons), so blowing one up showed a stand-in model. mp_backlot: 27 "
     "parts from mp_checkpoint and mp_invasion. Off by default until tried on a console."),
    ("name_common_assets", "Name what common_mp has",
     "A picture, material, model, effect or shader set the map carries under a name the "
     "always-loaded common_mp.ff also has is named instead (a reference to the loaded one), as "
     "stock maps do: none of 5 stock maps checked carries a second copy of anything common_mp "
     "has. A second copy takes the name over when the map loads: PC mp_backlot's own "
     "128x128 fire_roar_pm_atlas replaced common_mp's streamed one. Physics presets too, and "
     "a raw file (vision, script) common_mp has is left out (the game loads raw files by name; "
     "common_mp has IW's own 360 vision/mp_backlot.vision). mp_backlot: 24 copies (14 "
     "pictures, 7 materials, 2 models, 1 effect), 2 physics presets, its vision file."),
    ("stock_layout", "Stock file layout (test)",
     "The file laid out as every one of the 16 stock 360 maps is (mined with the skill's "
     "stock_rules.py): every shader set an asset list entry of its own (as Shader sets listed "
     "as assets); the map's entities inside the collision map, not an entry of their own; the "
     "map's one-of-a-kind assets in stock order (com_map, fx_map, lightdef, then gfx_map, "
     "game_map_mp, col_map_mp); and one copy of each picture, material, model geometry, pixel "
     "shader and physics preset per name (mp_backlot carried 65 names twice, from the team "
     "models and stock shader sets copied from different maps; stock files never do). Off by "
     "default until tried on a console."),
    ("list_techsets", "Shader sets listed as assets, as stock (test)",
     "Every shader set the map carries in full becomes an entry of its own in the file's asset "
     "list, just before the first asset that uses it, and materials point at that entry: stock "
     "360 maps list every one that way (mp_invasion's car fire shader set, "
     "effect_zfeather_falloff_add_eyeoffset, is entry 1142, the fire effect 1160). Converted "
     "maps wrote each inside the first material that used it (mp_backlot: 63, inside the world, "
     "the collision map's models, effects and models). Off by default until tried on a console."),
    ("split_car_fire", "Split the car fire across the car's effects (test)",
     "A diagnosis switch, not a fix. The burning car effect (smoke/car_damage_blacksmoke_fire) "
     "froze the console on converted mp_backlot, and its data matches stock. This moves each "
     "group of its elements into an effect you can set off on its own: shooting a headlight "
     "plays its omni light, a brake light its halogen glow, a side window its heat distortion, "
     "the windshield its bright core, the first smoke its white puff; the fire keeps only its "
     "flame sprites. The first one that freezes names the part at fault."),
    ("effect_sister_materials", "Stock flame materials from common_mp",
     "An effect material whose shader set common_mp doesn't have (so the map would carry the "
     "shader set itself) is replaced by common_mp's own material of the same name without the "
     "eye offset (_eyeN), when that one draws the same color picture with the same render "
     "states. The car fire's flames (gfx_fire_roar_pm_atlas_z10_godray60_eye20, shader set "
     "effect_zfeather_falloff_add_eyeoffset) froze the console on converted mp_backlot although "
     "they match stock byte for byte; common_mp's gfx_fire_roar_pm_atlas_z10_godray60 is what "
     "backlot's own fires draw. The flames then sit 20 units further from the eye."),
    ("destructible_sounds", "Destructible sounds",
     "The sounds the 360's destructible script plays for the map's destructible cars and props "
     "(a burning car's fire_vehicle_med and fire_vehicle_flareup_med, ...) that neither the map "
     "nor the always-loaded files have come from a stock 360 map, as every stock map carries "
     "them in its own file. PC mp_backlot (a CoD4 port) has no sounds at all, so its cars "
     "burned silently."),
    ("stock_streamed_pictures", "Pictures the 360 already has, streamed (test)",
     "A picture the map takes from the PC game's files (not its own .iwd) that a stock 360 map "
     "streams under the same name streams from the 360's own picture packs (imagefile1-4.pak, "
     "always on the disc) at full size, as in that stock map, instead of sitting in the map file. "
     "PC mp_showdown: 539 of its 639 pictures (142 of 173 MB), every vehicle among them, so the "
     "rest fit the picture budget without losing any detail. Off by default until tried on a "
     "console. Reads every stock map given once to list their pictures (kept in mw2port_cache)."),
    ("stream_pictures", "Stream pictures from imagefile9.pak and on (test)",
     "The map's own pictures stream from a pak as stock maps' do, instead of sitting in the map "
     "file: they come in at full size and take no map memory (converted mp_waw_castle: 11 MB of "
     "pictures in the map instead of 42). Every map converted this way shares the paks in "
     "mw2port_out: imagefile9.pak (or the first pak number chosen) until it holds %d MB, then "
     "imagefile10.pak, up to 21. New pictures only go on the end of the newest pak, so after a "
     "new map only the paks its log names need copying to the game folder (next to "
     "default_mp.xex, beside imagefile8.pak, not over it); maps converted earlier keep working. "
     "Off by default until tried on a console." % PAK_VOLUME_MB),
    ("stock_world", "Stock 360 world (test)",
     "For a PC copy of a stock map (PC mp_rust): the world assets (drawn world, collision, map "
     "entities, effects placement, game world) come from the stock 360 map of the same name, "
     "everything else stays converted. Off by default. If the converted map stops crashing or "
     "flickering with it, the world conversion is at fault; if not, the rest (models, materials, "
     "pictures, effects, sounds) is."),
    ("stock_materials", "Stock 360 materials (test)",
     "For a PC copy of a stock map (PC mp_rust): every material the stock 360 map has under the "
     "same name comes from it whole (its shader set, render state and pictures) instead of being "
     "converted. Off by default. If the crash or flicker stops with it, the material conversion "
     "is at fault."),
    ("stock_pictures", "Stock 360 pictures (test)",
     "For a PC copy of a stock map: every picture the stock 360 map has under the same name comes "
     "from it (streamed from the game's own imagefile1-4.pak, as there) instead of being "
     "converted. Off by default. If the crash or flicker stops with it, the picture conversion is "
     "at fault."),
    ("rebuild_trees", "Rebuild culling trees (test)",
     "Each room's culling tree is built anew from what it holds, the way stock trees are: boxes "
     "fitted to their contents, split in two along the longest side until a node holds 16 or "
     "fewer surfaces and models, every node listing the models below it. Off by default. "
     "mp_ancient's rocks and foliage vanished depending on the view angle with the converted "
     "trees and didn't with Room visibility off; this keeps rooms and portals as they are."),
    ("huge_tree_boxes", "Huge culling tree boxes (test)",
     "Every culling tree node's box becomes the whole map's box, so the 360 never skips a node "
     "as out of view; it still tests each model against its own box at the bottom of the tree. "
     "Off by default. If models stop vanishing with it, the 360 is wrongly skipping tree nodes; "
     "if not, the loss is in each model's own test or in how the tree marks models visible."),
    ("huge_leaf_boxes", "Huge boxes on end nodes only (test)",
     "Like Huge culling tree boxes, but only culling tree nodes with no children (the ones whose "
     "models and surfaces the 360 tests one by one) get the whole map's box; nodes with children "
     "keep theirs. Off by default. Together with Huge boxes on nodes with children, tells which "
     "level of the tree the 360 misjudges (mp_ancient: huge boxes on every node stopped the "
     "vanishing)."),
    ("huge_inner_boxes", "Huge boxes on nodes with children only (test)",
     "Like Huge culling tree boxes, but only culling tree nodes with children get the whole "
     "map's box; nodes with no children keep theirs. Off by default."),
    ("tree_box_margin", "Grow culling tree boxes by %d units (test)" % 64,
     "Every culling tree node's box grows by %d units on each side. Off by default. If that is "
     "enough to stop the vanishing, the 360 misjudges node boxes by a small margin; if not, by a "
     "lot (as if it read another box)." % 64),
    ("swap_models_test", "Swap placed models #%d and #%d (test)" % SWAP_MODELS,
     "Placed static models #%d and #%d trade numbers: each keeps its place, model, box and "
     "lighting, and every list that names them by number (culling tree nodes, shadow lists) "
     "follows. mp_ancient: #60 is a boulder that vanishes, #487 a boulder that doesn't. "
     "If the vanishing stays at #60's spot, it goes with the place; if it moves to #487's spot, "
     "with the number. Off by default."
     % SWAP_MODELS),
    ("ground_lit_flag", "Ground-lit flag from ground colour",
     "A placed model with a ground colour gets the 360's ground-lit flag (0x02), as in every "
     "stock 360 map (16 maps: 35,119 of 35,119 placements with a ground colour have it, none "
     "without). mp_ancient's PC file marks its foliage ground-lit on the model rather than on "
     "each placement, so 468 of its 474 foliage placements had a ground colour but no flag; "
     "converted mp_backlot: 2029 of 3619, so props in shade were lit from the light grid "
     "instead of their baked ground colour and looked too bright."),
    ("one_room", "Room visibility off (test)",
     "The map is treated as one room: every room's culling tree becomes a single node listing "
     "every surface and static model, and the portals between rooms go. Off by default. If the "
     "flicker stops, the rooms and portals (which no stock map we have could be compared with) "
     "are at fault."),
    ("plain_pictures", "Plain pictures (test)",
     "Every picture the map brings (2D ones, not lightmaps, reflection probes or loading screens) "
     "becomes a plain 16x16 one instead of being converted: white for colors, flat for normal "
     "maps, black for specular. Off by default. If flicker or missing textures stop, the picture "
     "conversion (tiling, mip levels, headers) is at fault."),
    ("surface_order", "Surfaces in 360 draw order",
     "Lay the world's surfaces out as the 360 tools do: solid ones (sort key below 6) first, then "
     "decals and see-through ones, then shadow casters, with the opaque / transparent / shadow "
     "caster ranges set to match (stock mp_rust: 5,230 / 101 / 1). IW4x maps list every surface "
     "as opaque (mp_backlot: about 2,400 decals among them), which can make decals flicker and "
     "hide things behind them."),
    ("merge_duplicates", "One copy of each asset (test)",
     "Assets with the same type and name are written once, as in stock 360 files: several PC "
     "shader sets become the same 360 one, and PC copies of stock maps carry their own team "
     "models next to the stock team copied in (PC mp_rust: 217 copies, about 10 MB). The game "
     "itself keeps the first of two same-named assets in a map, so this only saves memory. "
     "Off by default until a converted map with it has loaded on a console (PC mp_rust stopped "
     "partway through loading with it on; not yet known whether it is the cause)."),
    ("hide_tool_surfaces", "Hide tool surfaces",
     "Radiant tool shaders (clip, caulk, ...) get a see-through stand-in so they draw nothing."),
    ("portal_multiply", "HDR portals as multiply",
     "The white HDR portal sheets in doorways and windows (wc_unlit_distfalloff_*) use a "
     "multiply shader that leaves the picture as it is. Off (the default): they are hidden like "
     "tool surfaces."),
]
# Off unless switched on: the test switches, and portal_multiply (HDR portals are hidden instead).
DEFAULT_OFF = {"hide_foliage", "draw_distance_cap", "no_cull_distance", "room_box_bounds", "skip_lod0", "one_room", "plain_pictures",
               "stock_world", "portal_multiply", "stream_pictures", "stock_materials", "stock_pictures",
               "merge_duplicates", "model_box_bounds", "rebuild_trees", "huge_tree_boxes",
               "huge_leaf_boxes", "huge_inner_boxes", "tree_box_margin",
               "swap_models_test", "encode_sounds", "stock_streamed_pictures", "destructible_parts",
               "convert_anims", "split_car_fire", "list_techsets", "stock_layout"}
TREE_BOX_MARGIN = 64            # units, for the tree_box_margin test switch
# MB of XMA the encode_sounds switch makes at most (stock maps carry 4 to 8 MB of sounds).
ENCODED_SOUND_BUDGET = 12
DRAW_DISTANCE_CAP = 1200        # units, for the draw_distance_cap test switch
DEFAULT_FIXES = {k: k not in DEFAULT_OFF for k, _, _ in FIXES}


def fix_set(fixes=None, off=()):
    """The default fixes (all on but DEFAULT_OFF), as given in fixes ({name: bool}), with those
    named in off switched off."""
    out = dict(DEFAULT_FIXES)
    for k, v in (fixes or {}).items():
        if k not in out:
            raise PortError("unknown fix %r (known: %s)" % (k, ", ".join(out)))
        out[k] = bool(v)
    for k in off:
        if k not in out:
            raise PortError("unknown fix %r (known: %s)" % (k, ", ".join(out)))
        out[k] = False
    return out
COLOR_MAP, COLOR_MAP1 = 0xA0AB1041, 0xB60D1850     # texture table name hashes

# Per team (mp/factionTable.csv + the character scripts in common_mp): the models and
# icons a map carries for it. The 360 keeps these in each map's own file.
TEAM_ASSETS = {
    'us_army': {
        "models": ['head_allies_us_army_sniper', 'head_us_army_a', 'head_us_army_b', 'head_us_army_c', 'head_us_army_d', 'head_us_army_e', 'head_us_army_f', 'mp_body_army_sniper', 'mp_body_us_army_assault_a', 'mp_body_us_army_assault_b', 'mp_body_us_army_assault_c', 'mp_body_us_army_lmg', 'mp_body_us_army_lmg_b', 'mp_body_us_army_lmg_c', 'mp_body_us_army_riot', 'mp_body_us_army_shotgun', 'mp_body_us_army_shotgun_b', 'mp_body_us_army_shotgun_c', 'mp_body_us_army_smg', 'mp_body_us_army_smg_b', 'mp_body_us_army_smg_c', 'viewhands_sniper_us_army', 'viewhands_us_army', 'prop_flag_ranger', 'prop_flag_ranger_carry', 'com_plasticcase_rangers'],
        "materials": ['faction_128_rangers', 'faction_128_rangers_fade', 'objpoint_flag_rangers', 'headicon_rangers'],
    },
    'opforce_composite': {
        "models": ['head_op_arab_sniper', 'head_opforce_arab_a', 'head_opforce_arab_b', 'head_opforce_arab_c', 'head_opforce_arab_d_hat', 'head_opforce_arab_e', 'head_riot_op_arab', 'mp_body_op_arab_sniper', 'mp_body_opforce_arab_assault_a', 'mp_body_opforce_arab_lmg_a', 'mp_body_opforce_arab_shotgun_a', 'mp_body_opforce_arab_smg_a', 'mp_body_riot_op_arab', 'viewhands_militia', 'viewhands_sniper_op_arab', 'prop_flag_opforce', 'prop_flag_opforce_carry', 'com_plasticcase_arab'],
        "materials": ['faction_128_arab', 'faction_128_arab_fade', 'objpoint_flag_arab', 'headicon_arab'],
    },
    'opforce_arctic': {
        "models": ['head_op_arctic_sniper', 'head_opforce_arctic_a', 'head_opforce_arctic_b', 'head_opforce_arctic_c', 'head_opforce_arctic_d', 'head_riot_op_arctic', 'mp_body_op_arctic_sniper', 'mp_body_opforce_arctic_assault_a', 'mp_body_opforce_arctic_assault_b', 'mp_body_opforce_arctic_assault_c', 'mp_body_opforce_arctic_lmg', 'mp_body_opforce_arctic_lmg_b', 'mp_body_opforce_arctic_lmg_c', 'mp_body_opforce_arctic_shotgun', 'mp_body_opforce_arctic_shotgun_b', 'mp_body_opforce_arctic_shotgun_c', 'mp_body_opforce_arctic_smg', 'mp_body_opforce_arctic_smg_b', 'mp_body_opforce_arctic_smg_c', 'mp_body_riot_op_arctic', 'viewhands_arctic_opforce', 'viewhands_sniper_op_arctic', 'prop_flag_speznas', 'prop_flag_speznas_carry', 'com_plasticcase_ussr'],
        "materials": ['faction_128_ussr', 'faction_128_ussr_fade', 'objpoint_flag_ussr', 'headicon_ussr'],
    },
    'opforce_airborne': {
        "models": ['head_airborne_a', 'head_airborne_b', 'head_airborne_c', 'head_airborne_d', 'head_airborne_e', 'head_op_airborne_sniper', 'head_riot_op_airborne', 'mp_body_airborne_assault_a', 'mp_body_airborne_assault_b', 'mp_body_airborne_assault_c', 'mp_body_airborne_lmg', 'mp_body_airborne_lmg_b', 'mp_body_airborne_lmg_c', 'mp_body_airborne_shotgun', 'mp_body_airborne_shotgun_b', 'mp_body_airborne_shotgun_c', 'mp_body_airborne_smg', 'mp_body_airborne_smg_b', 'mp_body_airborne_smg_c', 'mp_body_op_airborne_sniper', 'mp_body_riot_op_airborne', 'viewhands_russian_airborne', 'viewhands_sniper_op_airborne', 'prop_flag_speznas', 'prop_flag_speznas_carry', 'com_plasticcase_ussr'],
        "materials": ['faction_128_ussr', 'faction_128_ussr_fade', 'objpoint_flag_ussr', 'headicon_ussr'],
    },
    'militia': {
        "models": ['head_militia_a_wht', 'head_militia_ba_blk', 'head_militia_bb_blk_hat', 'head_militia_bc_blk', 'head_militia_bd_blk', 'head_op_militia_sniper', 'head_riot_op_militia', 'mp_body_militia_assault_aa_blk', 'mp_body_militia_assault_aa_wht', 'mp_body_militia_assault_ab_blk', 'mp_body_militia_assault_ac_blk', 'mp_body_militia_lmg_aa_blk', 'mp_body_militia_lmg_ab_blk', 'mp_body_militia_lmg_ac_blk', 'mp_body_militia_smg_aa_blk', 'mp_body_militia_smg_aa_wht', 'mp_body_militia_smg_ab_blk', 'mp_body_militia_smg_ac_blk', 'mp_body_op_miltia_sniper', 'mp_body_riot_op_militia', 'viewhands_militia', 'prop_flag_militia', 'prop_flag_militia_carry', 'com_plasticcase_militia'],
        "materials": ['faction_128_militia', 'faction_128_militia_fade', 'objpoint_flag_militia', 'headicon_militia'],
    },
    'socom_141': {
        "models": ['head_seal_soccom_a', 'head_seal_soccom_ba', 'head_seal_soccom_ca', 'head_seal_soccom_da', 'mp_body_seal_soccom_assault_a', 'mp_body_seal_soccom_assault_b', 'mp_body_seal_soccom_assault_b_blk', 'mp_body_seal_soccom_assault_c', 'mp_body_seal_soccom_assault_c_blk', 'mp_body_seal_soccom_assault_d', 'viewhands_us_army', 'prop_flag_tf141', 'prop_flag_tf141_carry', 'com_plasticcase_taskforce141'],
        "materials": ['faction_128_taskforce141', 'faction_128_taskforce141_fade', 'objpoint_flag_taskforce', 'headicon_taskforce141'],
    },
    'socom_141_arctic': {
        "models": ['head_allies_tf141_arctic_sniper', 'head_riot_tf141_arctic', 'head_tf141_arctic_a', 'head_tf141_arctic_b', 'head_tf141_arctic_c', 'head_tf141_arctic_d', 'mp_body_riot_tf141_arctic', 'mp_body_tf141_arctic_sniper', 'mp_body_tf141_assault_a', 'mp_body_tf141_assault_b', 'mp_body_tf141_lmg', 'mp_body_tf141_shotgun', 'mp_body_tf141_smg', 'viewhands_arctic', 'viewhands_sniper_tf141_arctic', 'viewhands_tf141', 'prop_flag_tf141', 'prop_flag_tf141_carry', 'com_plasticcase_taskforce141'],
        "materials": ['faction_128_taskforce141', 'faction_128_taskforce141_fade', 'objpoint_flag_taskforce', 'headicon_taskforce141'],
    },
    'socom_141_desert': {
        "models": ['head_allies_tf141_desert_sniper', 'head_riot_tf141_desert', 'head_tf141_desert_a', 'head_tf141_desert_b', 'head_tf141_desert_c', 'head_tf141_desert_d', 'mp_body_desert_tf141_assault_a', 'mp_body_desert_tf141_assault_b', 'mp_body_desert_tf141_lmg', 'mp_body_desert_tf141_shotgun', 'mp_body_desert_tf141_smg', 'mp_body_riot_tf141_desert', 'mp_body_tf141_desert_sniper', 'viewhands_sniper_tf141_desert', 'viewhands_tf141', 'prop_flag_tf141', 'prop_flag_tf141_carry', 'com_plasticcase_taskforce141'],
        "materials": ['faction_128_taskforce141', 'faction_128_taskforce141_fade', 'objpoint_flag_taskforce', 'headicon_taskforce141'],
    },
    'socom_141_forest': {
        "models": ['head_allies_tf141_forest_sniper', 'head_riot_tf141_forest', 'head_tf141_forest_a', 'head_tf141_forest_b', 'head_tf141_forest_c', 'head_tf141_forest_d', 'mp_body_forest_tf141_assault_a', 'mp_body_forest_tf141_assault_b', 'mp_body_forest_tf141_lmg', 'mp_body_forest_tf141_shotgun', 'mp_body_forest_tf141_smg', 'mp_body_riot_tf141_forest', 'mp_body_tf141_forest_sniper', 'viewhands_sniper_tf141_forest', 'viewhands_tf141', 'prop_flag_tf141', 'prop_flag_tf141_carry', 'com_plasticcase_taskforce141'],
        "materials": ['faction_128_taskforce141', 'faction_128_taskforce141_fade', 'objpoint_flag_taskforce', 'headicon_taskforce141'],
    },
    'seals_udt': {
        "models": ['head_allies_seal_udt_sniper', 'head_riot_udt', 'head_seal_udt_a', 'head_seal_udt_c', 'head_seal_udt_d', 'head_seal_udt_e', 'mp_body_riot_udt', 'mp_body_seal_udt_assault_a', 'mp_body_seal_udt_assault_b', 'mp_body_seal_udt_lmg', 'mp_body_seal_udt_smg', 'mp_body_seal_udt_sniper', 'viewhands_sniper_udt', 'viewhands_udt', 'prop_flag_seal', 'prop_flag_seal_carry', 'com_plasticcase_seals'],
        "materials": ['faction_128_seals', 'faction_128_seals_fade', 'objpoint_flag_seals', 'headicon_seals'],
    },
}
SIDE_TEAMS = {
    "allies": ("us_army", "seals_udt", "socom_141", "socom_141_desert", "socom_141_forest",
               "socom_141_arctic"),
    "axis": ("opforce_composite", "opforce_airborne", "opforce_arctic", "militia"),
}
# Stock maps whose file carries each team (mp/basemaps.arena in code_post_gfx_mp).
STOCK_MAP_TEAMS_BY_TEAM = {
    'militia': ['mp_favela', 'mp_quarry', 'mp_underpass', 'mp_rundown'],
    'opforce_airborne': ['mp_highrise', 'mp_nightshift', 'mp_brecourt', 'mp_estate', 'mp_terminal'],
    'opforce_arctic': ['mp_derail', 'mp_subbase'],
    'opforce_composite': ['mp_invasion', 'mp_checkpoint', 'mp_boneyard', 'mp_afghan', 'mp_rust'],
    'seals_udt': ['mp_checkpoint', 'mp_subbase'],
    'socom_141_arctic': ['mp_derail'],
    'socom_141_desert': ['mp_favela', 'mp_quarry', 'mp_rundown', 'mp_boneyard', 'mp_afghan', 'mp_rust'],
    'socom_141_forest': ['mp_brecourt', 'mp_underpass', 'mp_estate'],
    'us_army': ['mp_invasion', 'mp_highrise', 'mp_nightshift', 'mp_terminal'],
}


class PortError(Exception):
    pass


def _swap_words(raw, size):
    if size <= 1:
        return bytes(raw)
    b = bytearray(len(raw))
    for i in range(size):
        b[i::size] = raw[size - 1 - i::size]
    return bytes(b)


def _uniform(m):
    """Element size when member m is made only of numbers (or pointers) of one size, else None."""
    if PTR in m.mods:
        return 4
    t = m.type
    if isinstance(t, (Prim, Enum)):
        return t.size if m.bits is None else None
    if isinstance(t, Compound):
        sizes = set()
        for x in t.members:
            u = _uniform(x)
            if u is None:
                return None
            sizes.add(u)
        return sizes.pop() if len(sizes) == 1 else None
    return None


def _has_ptr(m):
    if PTR in m.mods:
        return True
    return isinstance(m.type, Compound) and any(_has_ptr(x) for x in m.type.members)


def _ptr_array_value(r):
    """If alias r points at one pointer of an array of pointers inside an element of a struct
    array (not at the array's start), what that pointer holds, else None."""
    t, tgt, rel = r.t, r.target, r.rel
    if not (isinstance(tgt, list) and isinstance(t, Compound) and t.kind == "struct" and rel % t.size):
        return None
    el = tgt[rel // t.size] if rel // t.size < len(tgt) else None
    off = rel % t.size
    for m in t.members:
        if m.mods and m.mods[-1] == PTR and len(m.mods) > 1 and m.offset < off < m.offset + m.size \
                and isinstance(el, dict):
            lst = el.get("@", {}).get((tree.mkey(t, m), ()))
            k = (off - m.offset) // 4
            if isinstance(lst, list) and k < len(lst) and lst[k] is not None:
                return lst[k]
    return None


def _asset_in_slot(r):
    """If alias r points at a pointer member (inside a struct or array of structs) that holds
    an asset, that asset (or Ref), else None."""
    t, tgt, rel = r.t, r.target, r.rel
    if isinstance(tgt, tree.AssetEntry) and rel == 4:
        return tgt[1]
    if isinstance(tgt, tree.InsertSlot):
        return tgt.asset
    if isinstance(tgt, PtrList) and rel % 4 == 0 and rel // 4 < len(tgt):
        # An element of another asset's pointer array (XModel.materialHandles).
        c = tgt[rel // 4]
        if (isinstance(c, dict) and "_asset" in c) or isinstance(c, Ref):
            return c
    if not isinstance(t, Compound) or t.kind not in ("struct", "union"):
        return None
    if isinstance(tgt, list):
        i, rel = divmod(rel, t.size)
        if i >= len(tgt):
            return None
        tgt = tgt[i]
    return _asset_in_member(t, tgt, rel)


def _asset_in_member(t, tgt, rel):
    if not isinstance(tgt, dict):
        return None
    for m in t.members:
        if m.offset == rel and m.mods and m.mods[0] == PTR:
            c = tgt.get("@", {}).get((tree.mkey(t, m), ()))
            if (isinstance(c, dict) and "_asset" in c) or isinstance(c, Ref):
                return c
        elif m.mods and m.mods[-1] == PTR and len(m.mods) > 1 and m.offset <= rel < m.offset + m.size \
                and (rel - m.offset) % 4 == 0:
            # One pointer of an array of them (FxElemMarkVisuals.materials[2], which effects
            # share: "the same material as that element's").
            lst = tgt.get("@", {}).get((tree.mkey(t, m), ()))
            k = (rel - m.offset) // 4
            c = lst[k] if isinstance(lst, list) and k < len(lst) else None
            if (isinstance(c, dict) and "_asset" in c) or isinstance(c, Ref):
                return c
        elif (not m.mods and isinstance(m.type, Compound)
              and m.offset <= rel < m.offset + m.type.size):
            # A pointer inside an inline struct or union (MaterialTextureDef.u.image).
            c = _asset_in_member(m.type, tgt.get(m.name), rel - m.offset)
            if c is not None:
                return c
    return None


def deref(c):
    """An asset child or the asset an alias refers to."""
    if isinstance(c, Ref):
        a = _asset_in_slot(c)
        if a is not None:
            return deref(a)
        if c.rel == 0 and isinstance(c.target, dict):
            return c.target
        return None
    return c


def asset_name(d):
    """The name string of an asset dict (Str), or None."""
    ch = d.get("@", {})
    c = ch.get(("name", ()))
    if c is None and isinstance(d.get("info"), dict):
        c = d["info"].get("@", {}).get(("name", ()))
    return c.b if isinstance(c, Str) else None


def state_words(table):
    """[(loadBits[0], loadBits[1])] of a material's stateBitsTable (raw or decoded; or shared
    with another material through a pointer, as PC files share identical ones)."""
    if isinstance(table, Ref) and table.rel == 0:
        table = table.target
    if isinstance(table, Leaf):
        return [struct.unpack_from(table.E + "2I", table.raw, 8 * i) for i in range(len(table.raw) // 8)]
    if isinstance(table, list):
        return [tuple(x["loadBits"]) if isinstance(x, dict) else tuple(x) for x in table]
    return None


def with_cull(table, cull):
    """A copy of stateBitsTable with every entry's face culling (loadBits[0] bits 14-15, the
    same on PC and 360: 1 none, 2 back, 3 front) set to cull."""
    def w0(v):
        return (v & ~0xC000) | (cull << 14)
    if isinstance(table, Leaf):
        raw = bytearray(table.raw)
        for i in range(len(raw) // 8):
            a, b = struct.unpack_from(table.E + "2I", raw, 8 * i)
            struct.pack_into(table.E + "2I", raw, 8 * i, w0(a), b)
        return Leaf(table.t, table.n, raw, table.E)
    out = []
    for x in table:
        if isinstance(x, dict):
            y = dict(x)
            y["loadBits"] = [w0(x["loadBits"][0])] + list(x["loadBits"][1:])
            out.append(y)
        else:
            out.append([w0(x[0])] + list(x[1:]))
    return out


class _Kinds(dict):
    """What iter_objects does with an object of a type: 0 skip it (None, names, pointers,
    numbers), 1 a dict, 2 a list, 3 a Leaf, 4 anything else (yielded, not looked into)."""
    def __missing__(self, t):
        k = self[t] = (0 if t is type(None) or issubclass(t, (Str, Ref, str, int, float)) else
                       1 if issubclass(t, dict) else 2 if issubclass(t, list) else
                       3 if issubclass(t, Leaf) else 4)
        return k


_KINDS = _Kinds()


def iter_objects(root):
    """Every dict/list/Leaf in a tree, once."""
    kinds = _KINDS
    seen = set()
    stack = [root] if kinds[type(root)] else []
    pop, push, extend, mark = stack.pop, stack.append, stack.extend, seen.add
    while stack:
        o = pop()
        i = id(o)
        if i in seen:
            continue
        mark(i)
        yield o
        k = kinds[type(o)]
        if k == 1:
            for key, v in o.items():
                if key == "@":
                    extend([x for x in v.values() if kinds[type(x)]])
                elif 0 < kinds[type(v)] < 4:
                    push(v)
        elif k == 2:
            extend([x for x in o if kinds[type(x)]])


def alias_name(d):
    """A sound alias list's name (aliasName, not name), or None."""
    c = d.get("@", {}).get(("aliasName", ()))
    if isinstance(c, Ref) and isinstance(c.target, Str) and c.rel == 0:
        c = c.target
    return c.b if isinstance(c, Str) else None


def asset_indexes(root):
    """({(struct name, asset name bytes): dict} for every asset in a tree, top-level or inline,
    {alias name: snd_alias_list_t dict} for every sound alias list), in one walk of the tree.
    Stock files name some assets through a string another asset holds (a pointer to it): those
    count too (without them every world asset, and many models and pictures, went unfound)."""
    assets, aliases = {}, {}
    for o in iter_objects(root):
        if isinstance(o, dict) and "_asset" in o:
            n = asset_name(o) or _name(o)
            if n is not None:
                assets.setdefault((o["_asset"], n), o)
            if o["_asset"] == "snd_alias_list_t":
                n = alias_name(o)
                if n is not None:
                    aliases.setdefault(n, o)
    return assets, aliases


# ================================================================ vertices

def _pc_unit(b):
    s = (b[3] + 192) / 32385.0
    return [(b[i] - 127) * s for i in range(3)]


def _dec3n(v):
    u = 0
    for k in range(3):
        x = int(round(max(-1.0, min(1.0, v[k])) * 511))
        u |= (x & 0x3FF) << (10 * k)
    return u


# PC reflection probes are stored with a brightness multiplier in alpha (the largest color
# channel is always 255); the 360 stores plain color. color = rgb * alpha / 255 * PROBE_SCALE
# matches stock 360 probes' brightness (and turns the PC's "no probe" red into the 360's).
PROBE_SCALE = 2.5
# Stock 360 probes' average color level (mp_rust, mp_terminal, mp_afghan, mp_estate, mp_quarry:
# 67, 70, 78, 71, 66), for maps whose probes are all one stand-in picture (probe_brightness).
PROBE_STOCK_LEVEL = 70


def decode_probe(bgra):
    """One probe mip (B, G, R, A bytes) to plain color; alpha is set later."""
    out = bytearray(bgra)
    lut = {}
    for o in range(0, len(bgra), 4):
        a = bgra[o + 3]
        t = lut.get(a)
        if t is None:
            k = a / 255.0 * PROBE_SCALE
            t = lut[a] = bytes(min(255, int(v * k + 0.5)) for v in range(256))
        out[o] = t[bgra[o]]
        out[o + 1] = t[bgra[o + 1]]
        out[o + 2] = t[bgra[o + 2]]
    return bytes(out)


HIMIP_RADIUS = 1238     # the median of stock Favela's models


def convert_model_vertices(leaf):
    """GfxPackedVertex: floats to big-endian, color and packed texture coordinates as the
    same 32-bit value, normal and tangent repacked as 10:10:10 signed (checked against stock
    mil_tntbomb_mp, whose vertices match exactly)."""
    raw = leaf.raw
    out = bytearray(len(raw))
    cache = {}
    for o in range(0, len(raw), 32):
        struct.pack_into(">4f", out, o, *struct.unpack_from("<4f", raw, o))
        out[o + 16:o + 20] = raw[o + 16:o + 20][::-1]
        out[o + 20:o + 24] = raw[o + 20:o + 24][::-1]
        for k in (24, 28):
            key = raw[o + k:o + k + 4]
            u = cache.get(key)
            if u is None:
                u = cache[key] = _dec3n(_pc_unit(key))
            struct.pack_into(">I", out, o + k, u)
    return Leaf(leaf.t, leaf.n, out, ">")


def convert_world_vertices(leaf):
    """GfxWorldVertex: floats to big-endian, color as the same 32-bit value, normal and
    tangent repacked as 10:10:10 signed (checked against stock Favela: 99% within 0.02)."""
    raw = leaf.raw
    out = bytearray(len(raw))
    cache = {}
    for o in range(0, len(raw), 44):
        f = struct.unpack_from("<4f", raw, o)
        t = struct.unpack_from("<4f", raw, o + 20)
        struct.pack_into(">4f", out, o, *f)
        out[o + 16:o + 20] = raw[o + 16:o + 20][::-1]
        struct.pack_into(">4f", out, o + 20, *t)
        for k in (36, 40):
            key = raw[o + k:o + k + 4]
            u = cache.get(key)
            if u is None:
                u = cache[key] = _dec3n(_pc_unit(key))
            struct.pack_into(">I", out, o + k, u)
    return Leaf(leaf.t, leaf.n, out, ">")


# ================================================================ images

# PC D3DFORMAT / fourcc -> (mw2tex gpu format, 360 image format word from a stock image)
IWI_FORMATS = {0x0B: "DXT1", 0x0C: "DXT3", 0x0D: "DXT5", 0x01: "ARGB8"}


IWI_NOMIPMAPS, IWI_CUBE = 0x2, 0x10000


def iwi_header(data):
    """(flags as IW4 has them, format, width, height, header size) of an .iwi.
    Version 8 is IW4's own; 6 is CoD4 / World at War's (maps ported from those can carry
    them). Other versions are read when their header is laid out as one of those two and
    its file size field matches the file."""
    if data[:3] != b"IWi" or len(data) < 32:
        raise PortError("not an .iwi picture")
    ver = data[3]

    def v8():
        flags, = struct.unpack_from("<I", data, 4)
        w, h, _ = struct.unpack_from("<3H", data, 10)
        return flags, data[8], w, h, 32, struct.unpack_from("<I", data, 16)[0]

    def v6():
        f6 = data[5]
        # CoD4 flags: 2 no mipmaps, 4 cube map.
        flags = (IWI_NOMIPMAPS if f6 & 2 else 0) | (IWI_CUBE if f6 & 4 else 0)
        w, h, _ = struct.unpack_from("<3H", data, 6)
        return flags, data[4], w, h, 28, struct.unpack_from("<I", data, 12)[0]

    if ver == 8:
        return v8()[:5]
    if ver == 6:
        return v6()[:5]
    for layout in (v8, v6):
        flags, fmt, w, h, head, size = layout()
        if size == len(data) and w and h and (fmt in IWI_FORMATS or fmt in IWI_EXPAND
                                              or fmt in IWI_WAVELET):
            return flags, fmt, w, h, head
    raise PortError(".iwi version %d isn't supported" % ver)


def read_iwi(data):
    """.iwi picture: returns (format name, width, height, mips largest first, is cube).
    For a cube map, mips holds the six faces' top levels instead."""
    flags, fmt, w, h, head = iwi_header(data)
    if fmt in IWI_WAVELET:
        return read_iwi_wavelet(data, flags, fmt, w, h, head)
    name = IWI_FORMATS.get(fmt)
    expand = IWI_EXPAND.get(fmt)
    if expand is not None:
        name = "ARGB8"      # the few uncompressed kinds without a 360 twin become 32-bit
    if name is None:
        raise PortError("image format %d isn't supported yet" % fmt)
    bw, bpb = (1, 4) if name == "ARGB8" else (4, 8 if name == "DXT1" else 16)
    if expand is not None:
        bpb = expand[0]
    sizes = []
    lw, lh = w, h
    while True:
        sizes.append(max(1, (lw + bw - 1) // bw) * max(1, (lh + bw - 1) // bw) * bpb)
        if (lw == 1 and lh == 1) or flags & IWI_NOMIPMAPS:
            break
        lw, lh = max(1, lw >> 1), max(1, lh >> 1)
    body = data[head:]
    cube = bool(flags & IWI_CUBE)
    faces = 6 if cube else 1
    if len(body) < sum(sizes) * faces:
        sizes = sizes[:1]
    # Smallest mip first in the file (each mip holds all faces of a cube map).
    levels, pos = [None] * len(sizes), len(body) - sum(sizes) * faces
    for i in range(len(sizes) - 1, -1, -1):
        levels[i] = body[pos:pos + sizes[i] * faces]
        pos += sizes[i] * faces
    if expand is not None:
        levels = [expand[1](lv) for lv in levels]
        sizes = [n // expand[0] * 4 for n in sizes]
    if cube:
        return name, w, h, [levels[0][f * sizes[0]:(f + 1) * sizes[0]] for f in range(6)], True
    return name, w, h, levels, False


def _rgb24_to_argb8(b):
    """B,G,R -> B,G,R,A (as in a DDS / format 1 .iwi)."""
    out = bytearray(len(b) // 3 * 4)
    out[0::4], out[1::4], out[2::4] = b[0::3], b[1::3], b[2::3]
    out[3::4] = b"\xff" * (len(b) // 3)
    return bytes(out)


def _la16_to_argb8(b):
    """Luminance, alpha -> gray with that alpha."""
    out = bytearray(len(b) * 2)
    lum, alpha = b[0::2], b[1::2]
    out[0::4], out[1::4], out[2::4], out[3::4] = lum, lum, lum, alpha
    return bytes(out)


def _a8_to_argb8(b):
    """Alpha only -> black with that alpha (what the PC's A8 samples as)."""
    out = bytearray(len(b) * 4)
    out[3::4] = b
    return bytes(out)


# .iwi kinds turned into 32-bit pixels: format -> (bytes a pixel, converter)
IWI_EXPAND = {0x02: (3, _rgb24_to_argb8), 0x03: (2, _la16_to_argb8), 0x04: (1, _a8_to_argb8)}


def _l8_to_argb8(b):
    """Luminance -> opaque gray."""
    out = bytearray(len(b) * 4)
    out[0::4] = out[1::4] = out[2::4] = b
    out[3::4] = b"\xff" * len(b)
    return bytes(out)


# Wavelet-compressed .iwi kinds (see wavelet.py): format -> (channels in the stream, converter
# to 32-bit pixels or None when the stream already is B,G,R,A).
IWI_WAVELET = {0x06: (4, None), 0x07: (3, _rgb24_to_argb8), 0x08: (2, _la16_to_argb8),
               0x09: (1, _l8_to_argb8), 0x0A: (1, _a8_to_argb8)}


def read_iwi_wavelet(data, flags, fmt, w, h, head):
    """read_iwi for the wavelet kinds: every mip level is decoded, then made 32-bit pixels."""
    import wavelet
    if flags & IWI_CUBE:
        raise PortError("wavelet cube map pictures aren't supported yet")
    if flags & IWI_NOMIPMAPS:
        raise PortError("wavelet pictures without mipmaps aren't supported")
    channels, convert = IWI_WAVELET[fmt]
    try:
        levels, _ = wavelet.decode(data[head:], w, h, channels)
    except wavelet.WaveletError as e:
        raise PortError("wavelet picture: %s" % e)
    return "ARGB8", w, h, [lv if convert is None else convert(lv) for lv in levels], False


def _bc4_block(vals):
    """16 values (0-255) -> one 8-byte BC4 block (8-step ramp between the extremes)."""
    hi, lo = max(vals), min(vals)
    if hi == lo:
        return bytes([hi, lo]) + bytes(6)
    pal = [hi, lo] + [((7 - i) * hi + i * lo) // 7 for i in range(1, 7)]
    bits = 0
    for k, v in enumerate(vals):
        bits |= min(range(8), key=lambda i: abs(pal[i] - v)) << (3 * k)
    return bytes([hi, lo]) + bits.to_bytes(6, "little")


def dxt5_normal_to_dxn(mip):
    """A PC DXT5 normal map level (X in alpha, Y in green) as 360 DXN: the alpha block as it
    is, then green as a second block. Stock 360 normal maps are laid out this way: their first
    block is the PC's alpha block (byte for byte in most), the second the PC's green channel."""
    out = bytearray(len(mip))
    done = _DXN_GREEN       # green block -> its DXN block (normal maps repeat many blocks)
    if len(done) > 1 << 20:
        done.clear()
    for o in range(0, len(mip) - 15, 16):
        key = mip[o + 8:o + 16]
        block = done.get(key)
        if block is None:
            c0, c1, bits = struct.unpack_from("<HHI", key)
            g0, g1 = ((c0 >> 5) & 63) * 255 // 63, ((c1 >> 5) & 63) * 255 // 63
            pal = [g0, g1, (2 * g0 + g1) // 3, (g0 + 2 * g1) // 3] if c0 > c1 else [g0, g1, (g0 + g1) // 2, 0]
            block = done[key] = _bc4_block([pal[(bits >> (2 * k)) & 3] for k in range(16)])
        out[o:o + 8] = mip[o:o + 8]
        out[o + 8:o + 16] = block
    return bytes(out)


_DXN_GREEN = {}


def normal_maps_to_dxn(mips):
    return [dxt5_normal_to_dxn(m) for m in mips]


def picture_size(w, h, limit=2048):
    """The size fit_picture gives a picture: each side down to a power of two, at most limit."""
    def p2(v):
        r = 1
        while r * 2 <= v:
            r *= 2
        return min(r, limit)
    return p2(w), p2(h)


def fit_picture(fmt_name, w, h, mips, limit=2048):
    """Pictures the 360 can't take as they are (sides not a power of two, or too big) are
    resized: decoded, scaled down to powers of two, and compressed again (top level only)."""
    nw, nh = picture_size(w, h, limit)
    if (nw, nh) == (w, h):
        return w, h, mips
    import io
    import mw2tex
    from PIL import Image
    gpu = {v[0]: k for k, v in mw2tex.FORMATS.items()}[fmt_name]
    pic = Image.open(io.BytesIO(mw2tex.dds_bytes(w, h, gpu, mips[:1]))).convert("RGBA")
    pic = pic.resize((nw, nh), Image.LANCZOS)
    return nw, nh, [mw2tex._encode(pic.tobytes(), nw, nh, gpu)]


class PictureCache:
    """Results of the slow picture steps (resizing a picture, normal maps to DXN) kept in
    mw2port_cache/pictures between conversions: the same picture always comes out the same,
    and converting a map again with other switches is the usual case. The least recently used
    go first once the folder holds more than LIMIT bytes. Never fails a conversion: anything
    wrong with it and the step is done again."""
    LIMIT = 2 << 30

    def __init__(self, path):
        self.path = path
        self.hits = self.misses = 0

    def get(self, fn, *args):
        """fn(*args), from the cache when it has it. args: numbers, names and lists of bytes."""
        import hashlib
        import pickle
        h = hashlib.sha1(_picture_tag())
        h.update(fn.__name__.encode())
        for a in args:
            for m in (a if isinstance(a, list) else [repr(a).encode()]):
                h.update(struct.pack("<Q", len(m)))
                h.update(m)
        path = os.path.join(self.path, h.hexdigest() + ".pickle")
        try:
            with open(path, "rb") as fh:
                out = pickle.load(fh)
            os.utime(path)
            self.hits += 1
            return out
        except Exception:  # noqa: BLE001 - not cached (or unreadable): do the step
            pass
        out = fn(*args)
        self.misses += 1
        try:
            os.makedirs(self.path, exist_ok=True)
            with open(path + ".tmp", "wb") as fh:
                pickle.dump(out, fh, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(path + ".tmp", path)
        except OSError:
            pass
        return out

    def prune(self):
        try:
            files = []
            for n in os.listdir(self.path):
                st = os.stat(os.path.join(self.path, n))
                files.append((st.st_mtime, st.st_size, n))
            total = sum(f[1] for f in files)
            for _, size, n in sorted(files):
                if total <= self.LIMIT:
                    break
                os.remove(os.path.join(self.path, n))
                total -= size
        except OSError:
            pass


_picture_tag_value = None


def _picture_tag():
    """Changes whenever the code behind the cached picture steps changes."""
    global _picture_tag_value
    if _picture_tag_value is None:
        import hashlib
        import inspect
        import mw2tex
        h = hashlib.sha1(b"%d.%d" % sys.version_info[:2])
        for f in (picture_size, fit_picture, normal_maps_to_dxn, dxt5_normal_to_dxn, _bc4_block):
            h.update(inspect.getsource(f).encode())
        with open(mw2tex.__file__, "rb") as fh:
            h.update(fh.read())
        _picture_tag_value = h.digest()
    return _picture_tag_value




class PakWriter:
    """Rolling paks for stream_pictures: imagefile<start>.pak, then <start+1> and on, in one
    folder (mw2port_out), shared by every map converted this way. New pictures only ever go on
    the end of the newest one; when it would grow past the volume (PAK_VOLUME_MB) the next
    number starts, and the full one never changes again, so only the newest pak needs copying
    to the console after a new map. A picture already in any of them is reused. Each pak keeps
    an index of its chunks next to it (imagefile<n>.json). A picture already in one of the paks
    from the start number on is reused."""

    HEADER = b"IWffu100\0\0\x01\x0d"

    def __init__(self, folder, start=STREAM_PAK, volume_mb=PAK_VOLUME_MB):
        import json
        if not STREAM_PAK <= start <= STREAM_PAK_LAST:
            raise PortError("the first pak for streamed pictures must be %d to %d (7 and 8 are "
                            "mw2tex's, 1-4 the game's own)" % (STREAM_PAK, STREAM_PAK_LAST))
        self.folder, self.start, self.volume = folder, start, volume_mb * 1048576
        self.size, self.index, self.pending, self.added = {}, {}, {}, {}
        for n in range(STREAM_PAK, STREAM_PAK_LAST + 1):
            path = self.path_of(n)
            if not os.path.exists(path):
                continue
            self.size[n] = os.path.getsize(path)
            try:
                with open(os.path.splitext(path)[0] + ".json") as fh:
                    idx = json.load(fh)
                if idx.get("size") == self.size[n]:
                    self.index[n] = idx.get("chunks", {})
            except (OSError, ValueError):
                pass
        # The pak new pictures go in: the newest one from start on.
        self.current = max([n for n in self.size if n >= start] or [start])
        # Pictures already in a pak are reused, from the start number on only: a batch started
        # on a higher number depends on no pak before it.
        self.seen = {k: (n, v[0], v[1]) for n, idx in sorted(self.index.items()) if n >= start
                     for k, v in idx.items()}
        self.used = set()       # the paks the map's pictures are in

    def path_of(self, n):
        return os.path.join(self.folder, "imagefile%d.pak" % n)

    def add(self, blob):
        """(pak, start, end) of the zlib chunk holding blob."""
        import hashlib
        import zlib
        key = hashlib.sha1(blob).hexdigest()
        if key in self.seen:
            self.used.add(self.seen[key][0])
            return self.seen[key]
        chunk = zlib.compress(blob, 9)
        n = self.current
        size = self.size.get(n, len(self.HEADER))
        if size + len(chunk) > self.volume and size > len(self.HEADER):
            n = self.current = n + 1
            if n > STREAM_PAK_LAST:
                raise PortError("every pak for streamed pictures (imagefile%d-%d.pak) is full; the "
                                "game can't open a higher number" % (self.start, STREAM_PAK_LAST))
            size = self.size.get(n, len(self.HEADER))
        start = size
        self.pending.setdefault(n, []).append(chunk)
        self.size[n] = size + len(chunk)
        self.index.setdefault(n, {})[key] = [start, self.size[n]]
        self.added[n] = self.added.get(n, 0) + len(chunk)
        self.seen[key] = (n, start, self.size[n])
        self.used.add(n)
        return self.seen[key]

    def save(self):
        """Put the new chunks on the end of their paks. Returns [(pak path, MB added, MB in
        all)] for the paks that changed."""
        import json
        os.makedirs(self.folder, exist_ok=True)
        out = []
        for n, chunks in sorted(self.pending.items()):
            path = self.path_of(n)
            # (Appended only now, all at once: a conversion stopped part way leaves every pak as
            # it was.)
            with open(path, "ab") as fh:
                if fh.tell() == 0:
                    fh.write(self.HEADER)
                for c in chunks:
                    fh.write(c)
            with open(os.path.splitext(path)[0] + ".json.tmp", "w") as fh:
                json.dump({"size": self.size[n], "chunks": self.index[n]}, fh)
            os.replace(os.path.splitext(path)[0] + ".json.tmp", os.path.splitext(path)[0] + ".json")
            out.append((path, self.added[n] / 1048576.0, self.size[n] / 1048576.0))
        self.pending = {}
        return out


def stream_levels(width, height, has_mips):
    """The sizes a streamed picture comes in, smallest first, as stock pictures do: up to four,
    each twice the one before (stock 1024x512: 128x64, 256x128, 512x256, 1024x512)."""
    if not has_mips:
        return [(width, height)]
    n = 1
    while n < 4 and max(width, height) >> n >= 32 and min(width, height) >> n >= 16:
        n += 1
    return [(max(1, width >> k), max(1, height >> k)) for k in reversed(range(n))]


class ImageMaker:
    """Builds 360 GfxImage dicts with their pixels in the fastfile."""

    def __init__(self, xcodec, templates):
        import mw2tex
        mw2tex.FORMATS.setdefault(0x02, ("L8", 1, 1, ()))
        mw2tex.FORMATS.setdefault(0x3A, ("DXT3A", 4, 8, ()))
        self.tex = mw2tex
        self.xc = xcodec
        self.t = xcodec.type_by_name("GfxImage")
        self.templates = templates      # gpu format -> stock 360 image dict

    def build(self, d, fmt_name, width, height, mips, cube=False, tpl=None):
        """Fill image dict d (in place) as a 360 image with these linear little-endian mips.
        tpl: the stock image to copy settings from (default: any of this format)."""
        tex = self.tex
        gpu = {v[0]: k for k, v in tex.FORMATS.items()}[fmt_name]
        if tpl is None:
            tpl = self.templates.get((gpu, cube))
        make_cube = False
        if tpl is None and gpu == 0x31 and self.templates.get((0x14, cube)) is not None:
            # Stock DXN pictures (normal maps) all stream from the disc, so no file carries one
            # to copy: a DXT5 one with the format changed (in the fetch constant and in
            # textureFormat, as stock DXN pictures' 0x1a200171 against DXT5's 0x1a200154).
            tpl = dict(self.templates[(0x14, cube)])
            hdr = bytearray(bytes.fromhex(tpl["texture"]))
            dw1 = struct.unpack_from("<I", hdr, 32)[0]
            struct.pack_into("<I", hdr, 32, (dw1 & ~0x3F) | 0x31)
            tpl["texture"] = hdr.hex()
            tpl["textureFormat"] = (tpl.get("textureFormat", 0) & ~0x3F) | 0x31
            self.templates[(0x31, cube)] = tpl
        if tpl is None and cube:
            # No stock cube map of this format inside a file: take a flat one and make it a cube.
            tpl = self.templates.get((gpu, False))
            make_cube = True
        if tpl is None:
            raise PortError("no stock 360 %s%s image to copy settings from" % (fmt_name, " cube" if cube else ""))
        if cube:
            # Six faces, each tiled on its own. mips: per face, its top mip or its whole chain.
            # With mips: every face's top level, then each further mip slice for all six faces
            # (as in stock reflection probes).
            chains = [m if isinstance(m, list) else [m] for m in mips]
            levels = min(len(c) for c in chains)
            if levels == 1:
                pixels = b"".join(tex.tile(c[:1], width, height, gpu, single=True) for c in chains)
            else:
                plan, total = tex._plan(width, height, gpu)
                cuts = sorted(set(p[1] for p in plan[:levels])) + [total]
                tiled = [tex.tile(c[:levels], width, height, gpu) for c in chains]
                pixels = b"".join(t[a:b] for a, b in zip(cuts, cuts[1:]) for t in tiled)
        else:
            levels = len(mips)
            pixels = tex.tile(mips, width, height, gpu, single=(levels == 1))
        if fmt_name == "L8":
            pixels = tex._swap(pixels, gpu)     # 8-bit data isn't byte-swapped on the 360
        hdr = bytearray(bytes.fromhex(tpl["texture"]))
        dw = list(struct.unpack_from("<6I", hdr, 28))
        bw = tex.FORMATS[gpu][1]
        pitch = tex._align(width, 128 if bw == 4 else 32) // 32
        dw[0] = (dw[0] & ~(0x1FF << 22)) | (pitch << 22)
        dw[2] = (dw[2] & ~0x3FFFFFF) | (width - 1) | ((height - 1) << 13)
        dw[4] = (dw[4] & ~(0xF << 6)) | ((levels - 1) << 6)
        mip_addr = 0
        if levels > 1:
            mip_addr = tex._layout(width, height, 0, gpu)[3] * (6 if cube else 1) >> 12
        dw[5] = (dw[5] & 0xFFF & ~(1 << 11)) | (mip_addr << 12) | ((1 << 11) if levels > 1 else 0)
        if make_cube:
            dw[2] = (dw[2] & 0x3FFFFFF) | (5 << 26)          # six faces
            dw[5] = (dw[5] & ~(3 << 9)) | (3 << 9)            # dimension: cube
        struct.pack_into("<6I", hdr, 28, *dw)
        name = d.get("@", {}).get(("name", ()))
        new = dict(tpl)
        new.pop("_fix", None)
        new.update(texture=hdr.hex(), cardMemory=len(pixels), width=width, height=height, depth=1,
                   levelCount=levels, streaming=0, pixels="follow", name="follow",
                   streams=[{"width": 0, "height": 0, "info": 0} for _ in range(4)])
        for k in ("mapType", "semantic", "category"):
            if k in d:
                new[k] = d[k]
        if cube:
            new["mapType"] = 5
        new["@"] = {("name", ()): name, ("pixels", ()): Leaf(self.xc.type_by_name("unsigned char"), len(pixels),
                                                              pixels, ">")}
        new["_asset"] = "GfxImage"
        keep = {k: d[k] for k in ("_slot", "_forward") if k in d}
        d.clear()
        d.update(new)
        d.update(keep)

    def build_streamed(self, d, fmt_name, width, height, mips, pak):
        """Fill image dict d (in place) as a streamed 360 picture, as stock ones are: a 1x1
        record listing up to four levels (streams: size, and mip count << 26 | data size), each
        one zlib chunk in pak. The smallest level holds its whole mip chain, each bigger one only
        its own top mip (a whole chain there overflows the game's buffer, as mw2tex found).
        False when it can't be (mips missing): the caller keeps the picture in the file."""
        import math
        tex = self.tex
        gpu = {v[0]: k for k, v in tex.FORMATS.items()}[fmt_name]
        tpl = self.templates.get((gpu, False))
        if tpl is None:
            self.build(d, fmt_name, 4, 4, [b"\0" * tex.FORMATS[gpu][2]], tpl=None)   # makes the DXN one
            tpl = self.templates.get((gpu, False))
        if tpl is None:
            return False
        full = int(math.log2(max(width, height))) + 1
        has_mips = len(mips) >= full
        levels = stream_levels(width, height, has_mips)
        streams, paks = [], []
        for k, (w, h) in enumerate(levels):
            first = int(round(math.log2(width / w))) if w else 0
            if k == 0:
                chain = mips[first:] if has_mips else mips[:1]
                blob = tex.tile(chain, w, h, gpu, single=not has_mips)
                count = int(math.log2(max(w, h))) + 1 if has_mips else 1
            else:
                blob = tex.tile(mips[first:first + 1], w, h, gpu, single=True)
                count = int(math.log2(max(w, h))) + 1
            streams.append({"width": w, "height": h, "info": (count << 26) | len(blob)})
            paks.append(pak.add(blob))
        while len(streams) < 4:
            streams.append({"width": 0, "height": 0, "info": 0})
            paks.append((0, 0, 0))
        name = d.get("@", {}).get(("name", ()))
        new = {"texture": "00" * 52, "textureFormat": tpl.get("textureFormat"), "mapType": 3,
               "semantic": d.get("semantic", 0), "category": d.get("category", 3), "useSrgbReads": 0,
               "cardMemory": 0, "width": 1, "height": 1, "depth": 1, "levelCount": 1, "streaming": 1,
               "pixels": None, "streams": streams, "name": "follow", "@": {("name", ()): name},
               "_asset": "GfxImage", "_pak": paks}
        keep = {k: d[k] for k in ("_slot", "_forward") if k in d}
        d.clear()
        d.update(new)
        d.update(keep)
        return True


def encode_dxt3a(lum, width, height):
    """8-bit single channel -> DXT3A blocks (4 bits a pixel, DXT3's alpha block), linear."""
    bw, bh = max(1, width // 4), max(1, height // 4)
    out = bytearray(bw * bh * 8)
    q = bytes(min(15, (v * 15 + 127) // 255) for v in range(256))
    for by in range(bh):
        for bx in range(bw):
            o = (by * bw + bx) * 8
            for y in range(4):
                row = lum[(by * 4 + y) * width + bx * 4:(by * 4 + y) * width + bx * 4 + 4]
                out[o + 2 * y] = q[row[0]] | (q[row[1]] << 4)
                out[o + 2 * y + 1] = q[row[2]] | (q[row[3]] << 4)
    return bytes(out)


# ================================================================ converter

class Porter:
    def __init__(self, pc_root, x_refs, iwd=None, log=print, game_iwds=(), texture_budget=0,
                 fixes=None, pak=None):
        self.root = pc_root
        self.pak = pak                  # PakWriter for stream_pictures, else None
        self.streamed = 0
        self.log = log
        self.fixes = fix_set(fixes)
        self.test_counts = {}           # static models hide_foliage / draw_distance_cap changed
        self.texture_budget = texture_budget    # MB; 0: no limit
        self.mip_drop = {}                      # picture name -> top mip levels left out
        self.P = schema_mod.load("pc")
        self.X = schema_mod.load("xbox")
        self.pc = codec_mod.Codec(self.P)
        self.xc = codec_mod.Codec(self.X)
        self.done = set()
        # The map's own .iwd files: pictures it brings. The game looks names up without
        # caring about case (Windows), so the index doesn't either.
        self.iwds = [] if iwd is None else list(iwd) if isinstance(iwd, (list, tuple)) else [iwd]
        self.map_pictures = picture_index(self.iwds)
        self.missing_images = []        # (name, stand-in picture or None for magenta)
        self.unreadable = []            # pictures whose .iwi couldn't be read, and why
        # The PC game's own .iwd files (iw_00.iwd ...): pictures a map borrows from the game.
        # Later files win, as in the game.
        self.game_pictures = picture_index(game_iwds)
        self.game_iwds = list(game_iwds)
        self.from_game = 0
        self.techset_swaps = {}
        self.magenta_materials = []     # materials given a magenta picture for one they lack
        # Tool shader sets (wc_tools: clip, caulk, ... in Radiant) draw nothing in game. Their
        # materials get an alpha tested shader set and a see-through picture instead.
        self.invisible_ts = set()       # id() of techset dicts swapped in for a tool one
        self.invisible_materials = []
        # Stock 360 assets: resident ones (use by ",name") and others to copy in.
        self.resident = {}
        self.library = {}
        # Assets stock maps only name (",name"): the game has them loaded from its always-loaded
        # files (common_mp, ...) whenever a map loads, so a ported map can name them too.
        self.named = set()
        self.loaded = set()     # (type, lower-case name) of every asset the always-loaded files have
        self.common_materials = {}      # common_mp's materials: render state templates only
        self.stock_fx = {}              # effect name -> a stock map's effect (stock_effects)
        self.stock_fx_used = []
        self.stock_models = {}          # model name -> (a stock map's model, its file) (stock_models)
        self.model_swap = {}            # id() of a converted model -> the stock model's list entry
        self.stock_copied = Counter()   # materials / pictures copied whole (stock_materials, stock_pictures)
        self.stock_streamed = {}        # picture name -> a stock map's streamed one (stock_streamed_pictures)
        self.breakables = []            # destructible_plan: models to copy in (destructible_parts)
        self.destructible_aliases = []  # destructible_sound_plan: aliases to add (destructible_sounds)
        self.swapped_rows = set()       # light grid row data already put in 360 byte order
        self.pc_sort = {}               # id() of a material -> the PC's sort key (pc_sort_keys)
        self.pc_cull = {}               # id() of a two-sided PC material -> its culling (pc_face_culling)
        self.two_sided = 0
        self.stock_glass = None         # glass material name -> a stock glass type's 360-only numbers
        self.stock_sort_keys = None     # every sort key a stock material uses (pc_sort_keys)
        self.x_refs = x_refs
        self.common_names = set()       # (type, lower-case name) common_mp.ff has (name_common_assets)
        self.stock_aliases = {}         # sound alias name -> (stock alias list, resident?) (stock_sounds)
        for name, root in x_refs:
            idx, aliases = asset_indexes(root)
            base = os.path.splitext(os.path.basename(name))[0]
            if base in RESIDENT:
                for t, names in pool_names(root, _file_key(name)).items():
                    self.loaded.update((t, n) for n in names)
                    if base == "common_mp":
                        self.common_names.update((t, n.lower()) for n in names)
            order = {id(e[1]): i for i, e in enumerate(root["assets"])}
            for k, v in aliases.items():
                if k.startswith(b","):
                    continue
                old = self.stock_aliases.get(k)
                if old is None or (old[1] and base not in RESIDENT):
                    self.stock_aliases[k] = (v, base in RESIDENT, root, order.get(id(v), -1))
            if base not in RESIDENT:
                for k, v in effect_index(root).items():
                    self.stock_fx.setdefault(k, (v, root))
                for k, v in idx.items():
                    if k[0] == "XModel" and not k[1].startswith(b","):
                        self.stock_models.setdefault(k[1], (v, root))
            for k, v in idx.items():
                if k[1].startswith(b","):
                    self.named.add((k[0], k[1][1:]))
                    continue
                if base == "common_mp":
                    # Not used by name: only what a map's own materials (heat distortion, ...)
                    # need a stock material of the same shader set for.
                    if k[0] == "Material":
                        self.common_materials.setdefault(k, v)
                    continue
                if base in RESIDENT:
                    self.resident.setdefault(k, v)
                else:
                    self.library.setdefault(k, v)
        self.templates = {}
        for (typ, n), v in list(self.resident.items()) + list(self.library.items()):
            if typ == "GfxImage" and v.get("pixels") == "follow" and v.get("streaming") == 0:
                fmt = struct.unpack_from("<I", bytes.fromhex(v["texture"]), 32)[0] & 0x3F
                self.templates.setdefault((fmt, v.get("mapType") == 5), v)
        self.images = ImageMaker(self.xc, self.templates)
        # Stock lightmaps: their channel order (swizzle) differs from other pictures'.
        self.lightmap_templates = {}
        for (typ, n), v in list(self.library.items()):
            if typ == "GfxImage" and n.startswith(b"*lightmap") and v.get("pixels") == "follow":
                self.lightmap_templates.setdefault(n.rsplit(b"_", 1)[-1], v)
        # Stock 360 materials by the techset they use (render state templates).
        self.material_templates = {}
        for (typ, n), v in (list(self.library.items()) + list(self.resident.items())
                            + list(self.common_materials.items())):
            if typ != "Material":
                continue
            ts = deref(v.get("@", {}).get(("techniqueSet", ())))
            tn = asset_name(ts) if isinstance(ts, dict) else None
            if tn is not None:
                self.material_templates.setdefault(tn.lstrip(b","), v)
        self.warnings = []
        self.extra_assets = []          # asset list entries made while converting, listed before the world
        self.moved_images = []
        self.picked = {}        # stock file -> its asset list entries to copy in
        self.owners = {}        # stock file -> {id() of an asset dict: its entry} (_pick)
        self.picture_cache = None       # PictureCache, set by port()
        self.stock_entry_ids = set()    # id() of asset list entries copied whole from stock (teams)
        self.remapped = set()
        self.report = set()     # (type, "dropped" | "defaulted", member) seen while converting

    def warn(self, msg):
        if msg not in self.warnings:
            self.warnings.append(msg)
            self.log("  note: " + msg)

    # ------------------------------------------------------------ generic

    def zero(self, t):
        raw = bytes(t.size)
        return self.xc._decode_one(t, raw, 0, t.size)

    def conv_value(self, v, tp, tx, dims_p=(), dims_x=(), track=True):
        """An embedded value (not a pointer target) of PC type tp to 360 type tx."""
        if dims_p or dims_x:
            if not isinstance(v, list):
                return v
            n = dims_x[0] if dims_x else len(v)
            out = [self.conv_value(v[i], tp, tx, dims_p[1:], dims_x[1:], track) if i < len(v) else
                   self._zero_dims(tx, dims_x[1:]) for i in range(n)]
            return out
        if isinstance(v, Leaf):
            return self.conv_leaf(v, tx)
        if isinstance(tx, Compound) and isinstance(v, dict):
            return self.conv_dict(v, tp, tx, track)
        if isinstance(v, str) and isinstance(tx, Prim) and tx.size == 1:
            return v
        return v

    def _zero_dims(self, t, dims):
        if not dims:
            return self.zero(t) if isinstance(t, Compound) else (0 if not (isinstance(t, Prim) and t.fmt == "f") else 0.0)
        return [self._zero_dims(t, dims[1:]) for _ in range(dims[0])]

    def conv_union(self, d, tp, tx, track=True):
        """A union's bytes: converted as the member that is in use. That is the member the load
        followed pointers in (expanded into d), else the only member without pointers, else
        (members all plain numbers of one size) any of them."""
        h = bytes.fromhex(d["union"])
        size = len(h)
        out = dict(d)
        um = d.get("_um")
        if tx.name in BYTE_UNIONS:
            self.report.add((tx.name, "union", "bytes"))
            return out
        if tx.name == "FxGlassGeometryData":
            # What a file holds here is a glass piece's vertices (two 16-bit numbers) and fan
            # indexes (16-bit numbers); the hole and crack headers only exist at run time.
            self.report.add((tx.name, "union", "words of 2"))
            out["union"] = _swap_words(h, 2).hex()
            return out
        if um:
            names = um
        else:
            words = set(_uniform(m) for m in tx.members)
            if len(words) == 1 and None not in words:
                w = words.pop()
                self.report.add((tx.name, "union", "words of %d" % w))
                out["union"] = _swap_words(h, w).hex()
                return out
            full = [m for m in tx.members if not m.mods and m.bits is None and
                    isinstance(m.type, (Prim, Enum)) and m.type.size == size]
            if full:
                # A "packed" member holding the whole thing as one number: keep the number.
                self.report.add((tx.name, "union", "packed %d" % size))
                out["union"] = _swap_words(h, size).hex()
                return out
            plain = [tree.mkey(tx, m) for m in tx.members if not _has_ptr(m)]
            if len(plain) != 1:
                raise PortError("don't know which member of union %s is in use" % tx.name)
            names = plain
        b = bytearray(tx.size)      # the 360's union can be larger (StreamedSound has fileIndex)
        pm = {tree.mkey(tp, m): m for m in tp.members}
        for i, mx in enumerate(tx.members):
            k = tree.mkey(tx, mx)
            if k not in names:
                continue
            mp = pm[k]
            v = d[k] if k in d else self.pc._decode_member(mp, h, 0)
            v = self.conv_value(v, mp.type, mx.type, [q for q in mp.mods if q != PTR],
                                [q for q in mx.mods if q != PTR], track) if PTR not in mx.mods else v
            if k in d:
                out[k] = v
            self.xc._encode_member(mx, v, b, 0)
        out["union"] = b.hex()
        return out

    def conv_dict(self, d, tp, tx, track=True):
        """Convert struct dict d in place (keeps its identity for pointers that refer to it).
        track=False for throwaway dicts (their ids get reused)."""
        if track:
            if id(d) in self.done:
                return d
            self.done.add(id(d))
        # A typedef'd struct (GfxCellTree128 = GfxCellTree) is stored as the struct itself.
        while tp.kind == "typedef" and isinstance(tp.members[0].type, Compound) and not tp.members[0].mods:
            tp = tp.members[0].type
        while tx.kind == "typedef" and isinstance(tx.members[0].type, Compound) and not tx.members[0].mods:
            tx = tx.members[0].type
        hook = getattr(self, "pre_" + tx.name, None)
        if hook is not None and hook(d, tp, tx) is not None:
            return d
        if tx.kind == "union":
            new = self.conv_union(d, tp, tx, track)
            ch = d.get("@")
            d.clear()
            d.update(new)
            if ch:
                d["@"] = self.conv_children(ch, tp, tx)
            return d
        if tx.kind == "typedef":
            return d
        pm = {}
        for i, m in enumerate(tp.members):
            pm[tree.mkey(tp, m)] = m
        xk = set(tree.mkey(tx, m) for m in tx.members)
        for k in pm:
            if k not in xk and k in d:
                self.report.add((tx.name, "dropped", k))
        new = {}
        ch = d.get("@", {})
        for i, mx in enumerate(tx.members):
            kx = tree.mkey(tx, mx)
            mp = pm.get(kx)
            if mp is None and kx in d:
                # Set by a pre_ hook in the 360 layout already.
                new[kx] = d[kx]
                continue
            if mp is None or kx not in d or (PTR in mp.mods) != (PTR in mx.mods):
                new[kx] = self._default_member(mx)
                self.report.add((tx.name, "defaulted", kx))
                continue
            v = d[kx]
            if PTR in mx.mods:
                new[kx] = v
                continue
            dims_p = [q for q in mp.mods if q != PTR]
            dims_x = [q for q in mx.mods if q != PTR]
            if mx.bits is not None:
                new[kx] = v
            elif isinstance(v, (Leaf, list, dict)):
                new[kx] = self.conv_value(v, mp.type, mx.type, dims_p, dims_x, track)
            else:
                new[kx] = v
        new["@"] = self.conv_children(ch, tp, tx)
        for k in ("_asset", "_slot", "_template", "_forward", "_invisible"):
            if k in d:
                new[k] = d[k]
        d.clear()
        d.update(new)
        post = getattr(self, "post_" + tx.name, None)
        if post is not None:
            post(d, tx)
        return d

    def _default_member(self, m):
        if m.mods and m.mods[0] == PTR:
            return None
        dims = [q for q in m.mods if q != PTR]
        if PTR in m.mods:
            n = 1
            for q in dims:
                n *= q
            return [None] * n
        if isinstance(m.type, Prim) and m.type.size == 1 and len(dims) == 1:
            return "00" * dims[0]
        if m.bits is not None:
            return 0
        return self._zero_dims(m.type, dims)

    def conv_children(self, ch, tp, tx):
        out = {}
        pm = {tree.mkey(tp, m): m for m in tp.members}
        xm = {tree.mkey(tx, m): m for m in tx.members}
        for (k, idx), c in ch.items():
            mx, mp = xm.get(k), pm.get(k)
            if mx is None:
                continue
            if mp is None:
                mp = mx     # put there by a pre_ hook; already in 360 terms or plain data
            out[(k, idx)] = self.conv_child(c, mp.type, mx.type)
        return out

    def conv_child(self, c, tp, tx):
        if c is None or isinstance(c, (Str, Ref)):
            return c
        if isinstance(c, PtrList):
            for i, x in enumerate(c):
                c[i] = self.conv_child(x, tp, tx)
            return c
        if isinstance(c, list):
            for x in c:
                self.conv_dict(x, tp, tx)
            return c
        if isinstance(c, dict):
            if "_asset" in c:
                return self.conv_asset(c)
            return self.conv_dict(c, tp, tx)
        if isinstance(c, Leaf):
            return self.conv_leaf(c, tx)
        return c

    def conv_leaf(self, lf, tx):
        """Convert plain data in place (pointers may refer to it)."""
        if lf.E == ">":
            return lf
        new = self._conv_leaf(lf, tx)
        lf.t, lf.n, lf.raw, lf.E = new.t, new.n, new.raw, new.E
        return lf

    def _conv_leaf(self, lf, tx):
        hook = getattr(self, "leaf_" + getattr(tx, "name", ""), None)
        if hook is not None:
            return hook(lf, tx)
        tp = lf.t
        if isinstance(tp, (Prim, Enum)):
            if tp.size != tx.size:
                raise PortError("%s changes size" % tp.name)
            return Leaf(tx, lf.n, _swap_words(lf.raw, tp.size), ">")
        if not isinstance(tp, Compound):
            raise PortError("can't convert %r" % tp)
        vals = [self.pc._decode_one(tp, lf.raw, i * tp.size) for i in range(lf.n)]
        out = bytearray(tx.size * lf.n)
        for i, v in enumerate(vals):
            if isinstance(v, dict):
                v = self.conv_dict(v, tp, tx, track=False)
            self.xc._encode_one(tx, v, out, i * tx.size)
        return Leaf(tx, lf.n, out, ">")

    # ------------------------------------------------------------ assets

    def conv_asset(self, d):
        if id(d) in self.done:
            return d
        name = d["_asset"]
        tp = self.P.infos[name].ctype
        if name not in self.X.infos:
            raise PortError("the 360 has no %s assets" % name)
        tx = self.X.infos[name].ctype
        return self.conv_dict(d, tp, tx)

    def plan_texture_budget(self):
        """Decide which pictures lose top mip levels so the map's pictures fit the budget:
        the largest picture (shine maps counted double) goes down a level at a time. Lightmaps,
        reflection probes and loading screens stay as they are."""
        import heapq
        budget = int(self.texture_budget * 1048576 / TILING_PAD)
        fixed, items = 0, {}
        for o in iter_objects(self.root["assets"]):
            if not (isinstance(o, dict) and o.get("_asset") == "GfxImage"):
                continue
            name = asset_name(o)
            if not name or name.startswith(b",") or name.startswith(b"loadscreen"):
                continue
            if ("GfxImage", name) in self.resident and (name.startswith(b"$") or not self.in_iwd(name)):
                continue
            if (self.fixes["stock_pictures"] and ("GfxImage", name) in self.library) \
                    or self.streams_from_stock(name):
                continue        # copied from stock, streamed
            tex = o.get("texture", {})
            ld = tex.get("@", {}).get(("loadDef", ())) if isinstance(tex, dict) else None
            if isinstance(ld, dict) and ld.get("resourceSize"):
                fixed += ld["resourceSize"]
                continue
            low = name.decode().lower()
            found = self.map_pictures.get(low) or self.game_pictures.get(low)
            if found is None:
                continue
            try:
                fmt, w, h, mips, cube = read_iwi(found[0].read(found[1]))
            except (PortError, struct.error, ValueError, KeyError):
                continue
            if self.pak is not None and not cube and fmt in STREAMABLE and not name.startswith(b"loadscreen"):
                continue        # streamed from the pak: takes no map memory
            if cube or len(mips) < 2:
                fixed += sum(len(m) for m in mips)
            else:
                items[name] = ([len(m) for m in mips], w, h)
        before = total = fixed + sum(sum(s) for s, w, h in items.values())
        # Specular (shine) maps count double: they go down before color and normal maps of the
        # same size, whose loss shows far more (IW's combined ones are named ~..spc..&..cos..).
        def weight(n, size):
            low = n.lower()
            spec = low.startswith(b"~") or any(k in low for k in (b"_spc", b"_spec", b"_cos", b"_gloss"))
            return -size * (2 if spec else 1)
        heap = [(weight(n, sum(s)), n) for n, (s, w, h) in items.items()]
        heapq.heapify(heap)
        while total > budget and heap:
            _, n = heapq.heappop(heap)
            sizes, w, h = items[n]
            k = self.mip_drop.get(n, 0)
            if k + 1 >= len(sizes) or min(w, h) >> (k + 1) < 32:
                continue
            total -= sizes[k]
            self.mip_drop[n] = k + 1
            heapq.heappush(heap, (weight(n, sum(sizes[k + 1:])), n))
        if self.mip_drop:
            self.log("  pictures: about %.0f MB, over the %d MB budget, so %d pictures lose their "
                     "top mip levels (about %.0f MB left)"
                     % (before * TILING_PAD / 1048576, self.texture_budget, len(self.mip_drop),
                        total * TILING_PAD / 1048576))
            if total > budget:
                self.warn("the pictures still come to about %.0f MB (budget %d MB): the rest "
                          "are lightmaps, probes and pictures that are already small"
                          % (total * TILING_PAD / 1048576, self.texture_budget))

    def convert(self):
        ents = self.root["assets"]
        if self.texture_budget:
            self.plan_texture_budget()
        # IW4x ZoneBuilder signs a file with a rawfile named after the zone: its text stored as
        # is, but marked compressed (compressedLen 42, len 0). The 360 would take it for a
        # zlib stream. Stock files have the same rawfile empty (0, 0, one zero byte).
        self.tests_found = {"map_effects": 0, "map_fog": 0}
        stock_scripts = []
        for e in ents:
            if e[0] == "rawfile" and isinstance(e[1], dict) and _is_builder_signature(e[1]):
                _set_rawfile_text(e[1], b"")
                continue
            src = self.library.get(("RawFile", _name(e[1]) or b"")) \
                if e[0] == "rawfile" and isinstance(e[1], dict) and self.fixes["stock_scripts"] else None
            if src is not None:
                # The stock 360 map's own script: PC scripts can call what only later PC
                # patches have (PC mp_rust's killTrigger, missing from the 360's _utility).
                try:
                    text = _rawfile_text(src)
                except (KeyError, ValueError, zlib_error()):
                    text = None
                if text is not None and text != _rawfile_text(e[1]):
                    _set_rawfile_text(e[1], text)
                    stock_scripts.append(_name(e[1]).decode("latin-1"))
            elif e[0] == "rawfile" and isinstance(e[1], dict) and \
                    (not self.fixes["map_effects"] or not self.fixes["map_fog"]):
                self.edit_script(e[1])
        if stock_scripts:
            self.log("  %d script%s from the stock 360 files: %s" % (
                len(stock_scripts), "s" if len(stock_scripts) > 1 else "", ", ".join(stock_scripts)))
        gfx = next((e[1] for e in ents if e[0] == "gfx_map" and isinstance(e[1], dict)), {})
        self.map_name = re.sub(rb"^maps/mp/|\.d3dbsp$", b"", _name(gfx) or b"")
        if gfx:
            # The map itself (not its loading screen file): say when a test switch had nothing
            # to take out, so every map in a batch shows what the switch did to it.
            for k, what in (("map_effects", "effects in a createfx script"),
                            ("map_fog", "setExpFog call in its scripts")):
                if not self.fixes[k] and not self.tests_found[k]:
                    self.log("  test: %s is off, but this map has no %s: nothing to take out"
                             % (k, what))
        self.world_checksum = next((e[1].get("checksum", 0) for e in ents
                                    if e[0] == "gfx_map" and isinstance(e[1], dict)), 0)
        # Some PC files (a whole zone saved out: mp_raid) list the pieces of a model or shader set
        # as assets of their own. Stock 360 files never do: the PC shaders go (the shader sets
        # that used them are swapped for stock ones) and model surfaces are written where the
        # model that uses them points at them, as the orphans below are.
        gone = set(id(e[1]) for e in ents if e[0] in ("pixelshader", "vertexshader", "vertexdecl")
                   and isinstance(e[1], dict))
        # Pointers to a model surfaces asset's entry in the list become pointers to its own slot.
        slots = {}
        for e in ents:
            if e[0] == "xmodelsurfs" and isinstance(e[1], dict):
                slots[id(e)] = e[1].setdefault("_slot", tree.InsertSlot(e[1]))
        for o in iter_objects(ents):
            if isinstance(o, dict):
                for c in o.get("@", {}).values():
                    for x in (c if isinstance(c, list) else [c]):
                        if isinstance(x, Ref) and id(x.target) in slots and x.rel == 4:
                            x.target, x.rel, x.t = slots[id(x.target)], 0, None
        ents[:] = [e for e in ents if e[0] not in ("pixelshader", "vertexshader", "vertexdecl", "xmodelsurfs")]
        self.stock_effects(ents)
        if self.fixes["stock_sounds"]:
            self.add_destructible_aliases(ents)
            self.stock_sounds(ents)
        self.stock_anims(ents)
        if self.fixes["stock_models"]:
            self.stock_model_swap(ents)
        if self.fixes["name_common_assets"]:
            self.name_common_assets(ents)
        if self.fixes["effect_sister_materials"]:
            self.effect_sister_materials(ents)
        if self.fixes["split_car_fire"]:
            self.split_car_fire(ents)
        # Materials and pictures a left-out asset brought in first are only pointed at from
        # then on: the writer puts each where the first remaining pointer to it is.
        reached = set(id(o) for o in iter_objects(ents))
        orphans = {}
        for o in iter_objects(ents):
            if not isinstance(o, dict):
                continue
            for c in o.get("@", {}).values():
                for x in (c if isinstance(c, list) else [c]):
                    if isinstance(x, Ref) and isinstance(x.target, tree.InsertSlot):
                        a = x.target.asset
                        if isinstance(a, dict) and id(a) not in reached and id(a) not in gone:
                            orphans[id(a)] = a
        inner = set()
        for a in orphans.values():
            inner.update(id(o) for o in iter_objects(a) if o is not a)
        orphans = [a for k, a in orphans.items() if k not in inner]
        for a in orphans:
            a["_forward"] = True
        for e in ents:
            if e[0] in ("vertexshader", "vertexdecl"):
                raise PortError("top-level %s assets can't be converted" % e[0])
            if isinstance(e[1], dict):
                self.conv_asset(e[1])
        for a in orphans:
            self.conv_asset(a)
            a["_forward"] = True
        if self.extra_assets:
            # Assets made here (merge_decals' composite materials): before the world, whose
            # pointers to them must point back.
            at = next((i for i, e in enumerate(ents) if e[0] == "gfx_map"), len(ents))
            # (Shader sets first: the materials point at them.)
            ents[at:at] = sorted(self.extra_assets, key=lambda e: e[0] != "techset")
            self.pictures_before_composites(ents, at, at + len(self.extra_assets))
        if self.breakables:
            self.add_breakables(ents)
        if self.from_game:
            self.log("  %d pictures come from the PC game's own .iwd files" % self.from_game)
        if self.two_sided:
            self.log("  %d materials drawn two-sided as on the PC (the stock render state they take culls back faces)" % self.two_sided)
        if getattr(self, "dxn_count", 0):
            self.log("  %d normal maps converted to DXN, as the 360 keeps them" % self.dxn_count)
        for k in ("materials", "pictures"):
            if self.fixes["stock_" + k]:
                self.log("  test: %d %s copied from the stock 360 files" % (self.stock_copied[k], k))
        if self.fixes["stock_streamed_pictures"]:
            self.log("  test: %d pictures stream from the 360's own picture packs, as stock maps have "
                     "them (%d stock streamed pictures known)" % (self.stock_copied["streamed pictures"],
                                                                 len(self.stock_streamed)))
        if self.fixes["stock_world"]:
            self.stock_world_swap(ents)
        self.own_reference_copies(ents)
        if self.fixes["merge_duplicates"]:
            self.merge_same_named(ents)
        for u in self.unreadable[:5]:
            self.warn("picture %s can't be read, so it's treated as missing" % u)
        if len(self.unreadable) > 5:
            self.warn("... and %d more pictures that can't be read" % (len(self.unreadable) - 5))
        if self.missing_images:
            magenta = [n for n, stand in self.missing_images if stand is None]
            flat = [n for n, stand in self.missing_images if stand is not None]
            where = "the map's .iwd%s" % (" or the PC game's files" if self.game_pictures else "")
            if magenta:
                self.warn("%d picture%s %s in %s, so magenta stands in (%s%s)"
                          % (len(magenta), "s" if len(magenta) > 1 else "",
                             "aren't" if len(magenta) > 1 else "isn't", where,
                             ", ".join(magenta[:5]), ", ..." if len(magenta) > 5 else ""))
            if flat:
                self.warn("%d normal/specular map%s %s in %s, so flat / no-shine ones stand in "
                          "(%s%s)" % (len(flat), "s" if len(flat) > 1 else "",
                                      "aren't" if len(flat) > 1 else "isn't", where,
                                      ", ".join(flat[:5]), ", ..." if len(flat) > 5 else ""))
        if self.magenta_materials:
            n = len(self.magenta_materials)
            self.warn("%d material%s lack%s a picture the shader set used needs, so %s magenta "
                      "(%s%s)" % (n, "s" if n > 1 else "", "" if n > 1 else "s", "they show" if n > 1 else "it shows",
                                               ", ".join(self.magenta_materials[:5]),
                                               ", ..." if len(self.magenta_materials) > 5 else ""))
        if self.invisible_materials:
            n = len(self.invisible_materials)
            self.warn("%d material%s with a tool or distance-fade shader set draw%s nothing (%s%s)"
                      % (n, "s" if n > 1 else "", "" if n > 1 else "s",
                         ", ".join(self.invisible_materials[:5]),
                         ", ..." if n > 5 else ""))
        for a, b in sorted(self.techset_swaps.items()):
            if self.is_tools_techset(a):
                if self.hides(a, b):
                    continue
                self.warn("shader set %s isn't in the stock files given and no alpha tested one is "
                          "either, so %s is used and its surfaces show" % (a.decode(), b.decode()))
                continue
            self.warn("shader set %s isn't in the stock files given, so %s is used"
                      % (a.decode(), b.decode()))
        self.rename_clashes()
        if self.test_counts.get("plain"):
            self.log("  test: %d pictures made plain" % self.test_counts["plain"])
        if self.test_counts.get("skip_lod0"):
            self.log("  test: %d models skip their closest detail level" % self.test_counts["skip_lod0"])
        if self.test_counts.get("foliage"):
            self.log("  test: %d foliage models hidden" % self.test_counts["foliage"])
        if self.test_counts.get("no_cull"):
            self.log("  test: %d static models' cull distance set to 0 (never hidden by distance)"
                     % self.test_counts["no_cull"])
        if self.test_counts.get("bone_bounds"):
            self.log("  %d models' bone boxes rebuilt from their vertices (as stock 360 models have them)"
                     % self.test_counts["bone_bounds"])
        if self.test_counts.get("ground_lit"):
            self.log("  test: %d placed models with a ground colour given the ground-lit flag"
                     % self.test_counts["ground_lit"])
        if self.test_counts.get("capped"):
            self.log("  test: %d static models' draw distance capped at %d" % (self.test_counts["capped"],
                                                                              DRAW_DISTANCE_CAP))
        ss = self.root.get("script_strings")
        self.root["platform"] = "xbox"
        return self.root

    def edit_script(self, d):
        """Test switches that take things out of the map's scripts: the effects its createfx
        script places (map_effects off) and its distance fog (map_fog off)."""
        name = (_name(d) or b"").decode("latin-1")
        if not name.endswith(".gsc"):
            return
        text = _rawfile_text(d).decode("latin-1")
        new = text
        if not self.fixes["map_effects"] and "createfx/" in name:
            new, n = strip_effects(new)
            self.tests_found["map_effects"] += n
            if n:
                self.log("  test: %d effects left out of %s" % (n, name))
        if not self.fixes["map_fog"]:
            new, n = strip_fog(new)
            self.tests_found["map_fog"] += n
            if n:
                self.log("  test: fog switched off in %s" % name)
        if new != text:
            _set_rawfile_text(d, new.encode("latin-1"))

    def stock_model_swap(self, ents):
        """Test switch stock_models: static models a stock 360 file also has are drawn with the
        stock copy. The stock model goes in first in the asset list; each placed copy
        (GfxStaticModelDrawInst) points at it (pre_GfxStaticModelDrawInst). The converted model
        stays, renamed and listed on its own, as other assets can point into it (its materials);
        a model anything but placed copies holds is left as it is."""
        top = set(id(e[1]) for e in ents if isinstance(e[1], dict))
        holders = {}
        for o in iter_objects(ents):
            if not isinstance(o, dict):
                continue
            for c in o.get("@", {}).values():
                for x in (c if isinstance(c, list) else [c]):
                    t = x if isinstance(x, dict) else deref(x) if isinstance(x, Ref) else None
                    if isinstance(t, dict) and t.get("_asset") == "XModel":
                        holders.setdefault(id(t), set()).add("placement" in o)
        front, moved, names, swapped_models = [], [], [], []
        for o in list(iter_objects(ents)):
            if not (isinstance(o, dict) and o.get("_asset") == "XModel") or id(o) in self.done:
                continue
            name = _name(o)
            if not name or name.startswith(b",") or name not in self.stock_models:
                continue
            if holders.get(id(o), {False}) != {True}:
                continue        # held by something other than placed copies (or nothing)
            src, root = self.stock_models[name]
            m = self.copy_in(src)
            self.remap_bones(m, root["script_strings"])
            self.done.add(id(m))
            entry = tree.AssetEntry(["xmodel", m])
            front.append(entry)
            self.model_swap[id(o)] = entry
            o["@"][("name", ())] = Str(b"~pc/" + name)
            if id(o) not in top:
                moved.append(tree.AssetEntry(["xmodel", o]))
            swapped_models.append(o)
            names.append(name.decode("latin-1"))
        if self.fixes["drop_pc_models"] and swapped_models:
            # The converted copy goes too when nothing but placed copies points at it (they now
            # point at the stock one): each took one of the game's 1,536 model places (PC
            # mp_estate, a PC copy of a stock map, came to 1,544 with every model twice).
            inside, own = {}, set()
            for k, m in enumerate(swapped_models):
                for x in iter_objects(m):
                    own.add(id(x))
                    if x is not m:
                        inside[id(x)] = k
                        if isinstance(x, dict) and "_slot" in x:
                            inside[id(x["_slot"])] = k
            keep = set()
            for x in iter_objects(ents):
                if id(x) in own:
                    continue
                for c in (x.get("@", {}).values() if isinstance(x, dict) else
                          (x if isinstance(x, PtrList) else ())):
                    for y in (c if isinstance(c, list) else [c]):
                        if isinstance(y, Ref) and id(y.target) in inside:
                            keep.add(inside[id(y.target)])
            gone = set(id(m) for k, m in enumerate(swapped_models) if k not in keep)
            moved = [e for e in moved if id(e[1]) not in gone]
            ents[:] = [e for e in ents if not (isinstance(e[1], dict) and id(e[1]) in gone)]
            self.log("  %d converted copies of those models left out (%d kept: other assets point "
                     "into them)" % (len(gone), len(keep)))
        ents[:0] = front + moved
        self.log("  test: %d kinds of static model drawn with the stock 360 copy" % len(names))

    def rebuild_trees(self, world):
        """Test switch rebuild_trees: every room's culling tree (GfxAabbTree array) built anew
        from the surfaces (its root's sortedSurfIndex range) and static models (its root's list)
        it holds: boxes fitted to their contents, split in two at the median along the longest
        side down to 16 items, each node listing every model below it and the range of surfaces
        below it (the room's slice of sortedSurfIndex laid out again in leaf order), children
        stored together after their parent. The 360 walks these (TU6 0x8240DDA0): a box out of
        view skips the node, one wholly in view takes its lists, one partly in view goes down
        to its children and tests a leaf's items one by one."""
        def tgt(c):
            return c.target if isinstance(c, Ref) else c
        dpvs = world.get("dpvs") or {}
        ch = dpvs.get("@", {})
        insts = tgt(ch.get(("smodelInsts", ())))
        sbounds = tgt(ch.get(("surfacesBounds", ())))
        order = tgt(ch.get(("sortedSurfIndex", ())))
        trees = tgt(world.get("@", {}).get(("aabbTrees", ())))
        counts = tgt(world.get("@", {}).get(("aabbTreeCounts", ())))
        if not (isinstance(insts, Leaf) and isinstance(sbounds, Leaf) and isinstance(order, Leaf)
                and isinstance(trees, list) and isinstance(counts, Leaf)):
            self.warn("culling trees couldn't be rebuilt (data not found)")
            return
        mboxes = [struct.unpack_from(insts.E + "6f", insts.raw, 36 * i) for i in range(len(insts.raw) // 36)]
        ssize = sbounds.t.size
        sboxes = [struct.unpack_from(sbounds.E + "6f", sbounds.raw, ssize * i) for i in range(len(sbounds.raw) // ssize)]
        sorted_idx = list(struct.unpack(order.E + "%dH" % (len(order.raw) // 2), order.raw))
        idx_t = idx_e = None
        for ct in trees:
            for nd in tgt(ct.get("@", {}).get(("aabbTree", ()))) or []:
                c = tgt(nd.get("@", {}).get(("smodelIndexes", ())))
                if isinstance(c, Leaf):
                    idx_t, idx_e = c.t, c.E
                    break
            if idx_t is not None:
                break

        def models_of(nd):
            c = nd.get("@", {}).get(("smodelIndexes", ()))
            cnt = nd.get("smodelIndexCount") or 0
            off = 0
            if isinstance(c, Ref):
                off, c = c.rel, c.target
            if not cnt or not isinstance(c, Leaf):
                return []
            return list(struct.unpack_from(c.E + "%dH" % cnt, c.raw, off))

        def union(items):
            lo = [min(b[k] - b[3 + k] for _, _, b in items) for k in range(3)]
            hi = [max(b[k] + b[3 + k] for _, _, b in items) for k in range(3)]
            return lo, hi

        def split(items, depth):
            lo, hi = union(items)
            node = {"lo": lo, "hi": hi, "items": items, "kids": []}
            if len(items) <= 16 or depth >= 16:
                return node
            cen = [[b[k] for _, _, b in items] for k in range(3)]
            ext = [max(c) - min(c) for c in cen]
            ax = ext.index(max(ext))
            if ext[ax] <= 0:
                return node
            items = sorted(items, key=lambda it: it[2][ax])
            h = len(items) // 2
            node["kids"] = [split(items[:h], depth + 1), split(items[h:], depth + 1)]
            return node

        rebuilt = total = 0
        new_counts = list(struct.unpack(counts.E + "%di" % counts.n, counts.raw))
        for ci, ct in enumerate(trees):
            nodes = tgt(ct.get("@", {}).get(("aabbTree", ()))) if isinstance(ct, dict) else None
            if not nodes:
                continue
            root = nodes[0]
            start, scount = root.get("startSurfIndex", 0), root.get("surfaceCount", 0)
            surfs = sorted_idx[start:start + scount]
            models = models_of(root)
            if len(surfs) != scount or any(s >= len(sboxes) for s in surfs) or any(m >= len(mboxes) for m in models):
                self.warn("room %d's culling tree kept (its lists don't add up)" % ci)
                continue
            items = [("s", s, sboxes[s]) for s in surfs] + [("m", m, mboxes[m]) for m in models]
            if not items:
                continue
            if models and idx_t is None:
                self.warn("room %d's culling tree kept (no model list to copy the type of)" % ci)
                continue
            tree_ = split(items, 0)
            # Surfaces in leaf order (depth first): every subtree's surfaces are then one run.
            run = []

            def lay(nd):
                if nd["kids"]:
                    first = len(run)
                    ms = []
                    for k in nd["kids"]:
                        lay(k)
                        ms += k["models"]
                    nd["start"], nd["count"], nd["models"] = first, len(run) - first, ms
                else:
                    nd["start"] = len(run)
                    run.extend(i for t, i, _ in nd["items"] if t == "s")
                    nd["count"] = len(run) - nd["start"]
                    nd["models"] = [i for t, i, _ in nd["items"] if t == "m"]
            lay(tree_)
            # Breadth first: each node's children next to each other, after it.
            flat, queue = [], [tree_]
            while queue:
                nd = queue.pop(0)
                nd["at"] = len(flat)
                flat.append(nd)
                queue.extend(nd["kids"])
            out = []
            for nd in flat:
                lo, hi = nd["lo"], nd["hi"]
                o = {"bounds": {"midPoint": {"union": struct.pack(">3f", *[(lo[k] + hi[k]) / 2 for k in range(3)]).hex()},
                                "halfSize": {"union": struct.pack(">3f", *[(hi[k] - lo[k]) / 2 for k in range(3)]).hex()}},
                     "childCount": len(nd["kids"]), "surfaceCount": nd["count"], "startSurfIndex": start + nd["start"],
                     "smodelIndexCount": len(nd["models"]), "smodelIndexes": "follow" if nd["models"] else None,
                     "childrenOffset": (nd["kids"][0]["at"] - nd["at"]) * 40 if nd["kids"] else 0, "@": {}}
                if nd["models"]:
                    o["@"][("smodelIndexes", ())] = Leaf(idx_t, len(nd["models"]),
                                                         struct.pack(idx_e + "%dH" % len(nd["models"]), *nd["models"]), idx_e)
                out.append(o)
            sorted_idx[start:start + scount] = run
            ct.setdefault("@", {})[("aabbTree", ())] = out
            ct["aabbTree"] = "follow"
            if ci < len(new_counts):
                new_counts[ci] = len(out)
            rebuilt += 1
            total += len(out)
        order.raw = struct.pack(order.E + "%dH" % len(sorted_idx), *sorted_idx)
        counts.raw = struct.pack(counts.E + "%di" % counts.n, *new_counts)
        self.log("  test: culling trees rebuilt (%d rooms, %d nodes)" % (rebuilt, total))

    def huge_tree_boxes(self, world, leaves=True, inner=True):
        """Test switch huge_tree_boxes: every culling tree node's box becomes the world's box, so
        the 360's walk (TU6 0x8240DDA0) never judges a node out of view or wholly in view. It
        goes down every node partly in view and tests a leaf's models and surfaces one by one
        against their own boxes, as with Room visibility off, but through the real tree."""
        def tgt(c):
            return c.target if isinstance(c, Ref) else c
        trees = tgt(world.get("@", {}).get(("aabbTrees", ())))
        if not isinstance(trees, list) or not isinstance(world.get("bounds"), dict):
            self.warn("culling tree boxes couldn't be grown (data not found)")
            return
        n = 0
        for ct in trees:
            for nd in tgt(ct.get("@", {}).get(("aabbTree", ()))) or []:
                if isinstance(nd, dict) and "bounds" in nd and (inner if nd.get("childCount") else leaves):
                    nd["bounds"] = {k: dict(v) for k, v in world["bounds"].items()}
                    n += 1
        which = "" if leaves and inner else " (end nodes only)" if leaves else " (nodes with children only)"
        self.log("  test: %d culling tree node boxes set to the whole map's box%s" % (n, which))

    def grow_tree_boxes_by(self, world, margin):
        """Test switch tree_box_margin: every culling tree node's box grows by margin units on
        each side (its half-size grows by margin, its centre stays)."""
        def tgt(c):
            return c.target if isinstance(c, Ref) else c
        trees = tgt(world.get("@", {}).get(("aabbTrees", ())))
        if not isinstance(trees, list):
            self.warn("culling tree boxes couldn't be grown (data not found)")
            return
        n = 0
        for ct in trees:
            for nd in tgt(ct.get("@", {}).get(("aabbTree", ()))) or []:
                hs = nd.get("bounds", {}).get("halfSize") if isinstance(nd, dict) else None
                if isinstance(hs, dict) and isinstance(hs.get("union"), str) and len(hs["union"]) == 24:
                    h = struct.unpack(">3f", bytes.fromhex(hs["union"]))
                    if all(math.isfinite(v) and v >= 0 for v in h):
                        nd["bounds"]["halfSize"] = {"union": struct.pack(">3f", *[v + margin for v in h]).hex()}
                        n += 1
        self.log("  test: %d culling tree node boxes grown by %d units" % (n, margin))

    def dim_standin_probes(self, world):
        """probe_brightness: when every reflection probe is the same picture (the map was
        compiled without probes), scale their color to stock 360 probes' average level."""
        def tgt(c):
            return c.target if isinstance(c, Ref) else c
        draw = world.get("draw") or {}
        probes = tgt(draw.get("@", {}).get(("reflectionProbes", ()))) or []
        leaves = []
        for c in probes:
            im = tgt(c)
            if isinstance(im, tree.InsertSlot):
                im = im.asset
            px = tgt(im.get("@", {}).get(("pixels", ()))) if isinstance(im, dict) else None
            if not isinstance(px, Leaf) or not px.raw or len(px.raw) % 4:
                return
            leaves.append(px)
        if len(leaves) < 2 or any(l.raw != leaves[0].raw for l in leaves[1:]):
            return
        raw = leaves[0].raw          # A8R8G8B8: alpha first, then the color
        n = len(raw) // 4
        level = (sum(raw[1::4]) + sum(raw[2::4]) + sum(raw[3::4])) / (3.0 * n)
        if level <= PROBE_STOCK_LEVEL:
            return
        k = PROBE_STOCK_LEVEL / level
        lut = bytes(min(255, int(v * k + 0.5)) for v in range(256))
        out = bytearray(raw)
        for i in (1, 2, 3):
            out[i::4] = raw[i::4].translate(lut)
        out = bytes(out)
        for l in leaves:
            l.raw = out
        self.log("  %d stand-in reflection probes (the map has no real ones) dimmed to stock "
                 "brightness (color level %.0f -> %d)" % (len(leaves), level, PROBE_STOCK_LEVEL))

    def slice_tree_lists(self, world):
        """tree_list_slices: each room's culling tree keeps its nodes and the set of static models
        each node lists, but the lists are laid out as stock 360 maps have them: the root's list
        holds every model of the room in depth-first order (a node's own models, then each
        child's in turn), and every other node's list is the run of it that is its subtree.
        The 360 renumbers only the root lists after it reorders the placed models on load."""
        def tgt(c):
            return c.target if isinstance(c, Ref) else c

        def read(nd):
            c = nd.get("@", {}).get(("smodelIndexes", ()))
            off = 0
            if isinstance(c, Ref):
                off, c = c.rel, c.target
            n = nd.get("smodelIndexCount") or 0
            if not n:
                return []
            if not isinstance(c, Leaf) or off + 2 * n > len(c.raw):
                return None
            return list(struct.unpack_from(c.E + "%dH" % n, c.raw, off))

        rooms = done = 0
        for ct in tgt(world.get("@", {}).get(("aabbTrees", ()))) or []:
            nodes = tgt(ct.get("@", {}).get(("aabbTree", ())))
            if not isinstance(nodes, list) or not nodes:
                continue
            lists = [read(nd) for nd in nodes]
            if any(l is None for l in lists) or not lists[0]:
                continue
            kids = []
            for i, nd in enumerate(nodes):
                f = i + (nd.get("childrenOffset") or 0) // 40
                kids.append(list(range(f, f + nd["childCount"])) if nd.get("childCount") else [])
            sub = {}

            def subtree(i, depth=0):
                if depth > 64:
                    raise ValueError("tree too deep")
                if i not in sub:
                    s_ = set(lists[i])
                    for k in kids[i]:
                        s_ |= subtree(k, depth + 1)
                    sub[i] = s_
                return sub[i]
            try:
                subtree(0)
            except (ValueError, IndexError, RecursionError):
                self.warn("a culling tree's model lists couldn't be laid out (tree not as expected)")
                continue
            # Only when each node lists exactly its subtree and no model sits under two children
            # does every node get one run of the root list.
            ok = all(set(lists[i]) == sub[i] for i in sub)
            for i in sub:
                seen = set()
                for k in kids[i]:
                    if sub[k] & seen:
                        ok = False
                    seen |= sub[k]
            if not ok or len(lists[0]) != len(sub[0]):
                self.warn("a culling tree's model lists weren't laid out (a model listed twice, or a "
                          "node not listing what is below it)")
                continue
            order, span = [], {}

            def lay(i):
                start = len(order)
                below = set()
                for k in kids[i]:
                    below |= sub[k]
                order.extend(sorted(m for m in lists[i] if m not in below))
                for k in kids[i]:
                    lay(k)
                span[i] = (start, len(order) - start)
            lay(0)
            c0 = nodes[0].get("@", {}).get(("smodelIndexes", ()))
            base = tgt(c0)
            root = Leaf(base.t, len(order), struct.pack(base.E + "%dH" % len(order), *order), base.E)
            for i, nd in sorted(span.items()):
                nd_ = nodes[i]
                start, n = nd
                nd_["smodelIndexCount"] = n
                if i == 0:
                    nd_["@"][("smodelIndexes", ())] = root
                    nd_["smodelIndexes"] = "follow"
                elif n:
                    r = tree.Ref(1)     # non-null until the writer knows where the root list lands
                    r.target, r.rel, r.t = root, 2 * start, root.t
                    nd_.setdefault("@", {})[("smodelIndexes", ())] = r
                    nd_["smodelIndexes"] = "0x00000001"     # any non-null value: an alias, set on writing
                else:
                    nd_.get("@", {}).pop(("smodelIndexes", ()), None)
                    nd_["smodelIndexes"] = None
            rooms += 1
            done += len(span)
        if rooms:
            self.log("  culling tree model lists laid out inside each room's list (%d rooms, %d nodes)"
                     % (rooms, done))

    def swap_models(self, world, a, b):
        """Test switch swap_models_test: placed static models a and b trade numbers. Their
        smodelDrawInsts and smodelInsts records swap places, and every index list naming them
        (culling tree nodes' smodelIndexes, shadowGeom smodelIndex) swaps a and b, so each model
        still draws where it stood (its record, model included, moves with it); only its number
        changes."""
        def tgt(c):
            return c.target if isinstance(c, Ref) else c
        dpvs = world.get("dpvs") or {}
        ch = dpvs.get("@", {})
        draws = tgt(ch.get(("smodelDrawInsts", ())))
        insts = tgt(ch.get(("smodelInsts", ())))
        if not (isinstance(draws, list) and isinstance(insts, Leaf) and max(a, b) < len(draws)
                and 36 * (max(a, b) + 1) <= len(insts.raw)):
            self.warn("placed models #%d and #%d couldn't be swapped (not in this map)" % (a, b))
            return
        name = lambda i: (_name(deref(draws[i].get("@", {}).get(("model", ())))) or b"?").lstrip(b",").decode(errors="replace")
        draws[a], draws[b] = draws[b], draws[a]
        raw = bytearray(insts.raw)
        raw[36 * a:36 * a + 36], raw[36 * b:36 * b + 36] = insts.raw[36 * b:36 * b + 36], insts.raw[36 * a:36 * a + 36]
        insts.raw = bytes(raw)
        lists, seen = [], set()
        for ct in tgt(world.get("@", {}).get(("aabbTrees", ()))) or []:
            for nd in tgt(ct.get("@", {}).get(("aabbTree", ()))) or []:
                lists.append(tgt(nd.get("@", {}).get(("smodelIndexes", ()))) if isinstance(nd, dict) else None)
        for sg in tgt(world.get("@", {}).get(("shadowGeom", ()))) or []:
            lists.append(tgt(sg.get("@", {}).get(("smodelIndex", ()))) if isinstance(sg, dict) else None)
        changed = 0
        for L in lists:
            if not isinstance(L, Leaf) or id(L) in seen or not L.raw:
                continue
            seen.add(id(L))
            v = list(struct.unpack(L.E + "%dH" % (len(L.raw) // 2), L.raw))
            w = [b if x == a else a if x == b else x for x in v]
            if w != v:
                L.raw = struct.pack(L.E + "%dH" % len(w), *w)
                changed += 1
        self.log("  test: placed models #%d (now %s) and #%d (now %s) swapped numbers (%d index lists updated)"
                 % (a, name(a), b, name(b), changed))

    def one_room(self, world):
        """Test switch one_room: every room's culling tree becomes one node listing every surface
        (all of sortedSurfIndex) and static model, and the portals go, so whichever room the
        camera is in, everything in view is drawn."""
        def tgt(c):
            return c.target if isinstance(c, Ref) else c
        dpvs = world.get("dpvs") or {}
        trees = tgt(world.get("@", {}).get(("aabbTrees", ())))
        cells = tgt(world.get("@", {}).get(("cells", ())))
        counts = tgt(world.get("@", {}).get(("aabbTreeCounts", ())))
        if not isinstance(trees, list) or not isinstance(cells, list) or not isinstance(counts, Leaf):
            self.warn("room visibility couldn't be switched off (data not found)")
            return
        nsurf, nsm = dpvs.get("staticSurfaceCount", 0), dpvs.get("smodelCount", 0)
        if nsurf > 0xFFFF or nsm > 0xFFFF:
            self.warn("room visibility couldn't be switched off (too many surfaces or models)")
            return
        idx_t = None
        for ct in trees:
            for nd in tgt(ct.get("@", {}).get(("aabbTree", ()))) or []:
                c = tgt(nd.get("@", {}).get(("smodelIndexes", ())))
                if isinstance(c, Leaf):
                    idx_t, E = c.t, c.E
                    break
            if idx_t:
                break
        if idx_t is None and nsm:
            self.warn("room visibility couldn't be switched off (no model list to copy the type of)")
            return
        for ct in trees:
            node = {"bounds": {k: dict(v) for k, v in world["bounds"].items()}, "childCount": 0,
                    "surfaceCount": nsurf, "startSurfIndex": 0, "smodelIndexCount": nsm,
                    "smodelIndexes": "follow" if nsm else None, "childrenOffset": 0, "@": {}}
            if nsm:
                node["@"][("smodelIndexes", ())] = Leaf(idx_t, nsm, struct.pack(E + "%dH" % nsm, *range(nsm)), E)
            ct.setdefault("@", {})[("aabbTree", ())] = [node]
            ct["aabbTree"] = "follow"
        counts.raw = struct.pack(counts.E + "%di" % counts.n, *[1] * counts.n)
        for c in cells:
            c["portalCount"] = 0
            c["portals"] = None
            c.get("@", {}).pop(("portals", ()), None)
        self.log("  test: room visibility off (%d rooms each list all %d surfaces and %d static models)"
                 % (len(cells), nsurf, nsm))

    def stock_effects(self, ents):
        """Effects a stock 360 file given has under the same name come from there (with their
        materials) instead of being converted: converted ones can draw far stronger than on
        the PC (mp_backlot's dust_wind_* filled the map with a yellow haze). Other assets and
        the map's scripts name effects rather than point at them, so the stock one goes in under
        the name and the converted one is renamed out of the way, then left out
        (drop_pc_effects) unless the map's other assets point into it (materials it brings)."""
        if not self.fixes["stock_effects"]:
            return
        new, names, swapped = [], [], {}
        for e in ents:
            if e[0] != "fx" or not isinstance(e[1], dict):
                continue
            name = _name(e[1])
            if name not in self.stock_fx:
                continue
            src, src_root = self.stock_fx[name]
            e[1]["@"][("name", ())] = Str(b"~pc/" + name)
            fx = self.copy_in(src)
            self.remap_bones(fx, src_root["script_strings"])
            self.done.add(id(fx))
            new.append(tree.AssetEntry([e[0], fx]))
            swapped[id(e)] = (e, new[-1])
            self.stock_fx_used.append(fx)
            names.append(name.decode("latin-1"))
        if swapped and self.fixes["drop_pc_effects"]:
            self.drop_swapped_effects(ents, swapped)
        # First in the list: the shader sets they bring are the stock ones the map's own
        # materials of those sets point at, and have to be written before them.
        ents[:0] = new
        if names:
            self.log("  %d effect%s from the stock 360 files: %s" % (
                len(names), "s" if len(names) > 1 else "", ", ".join(names)))

    def drop_swapped_effects(self, ents, swapped):
        """The PC copies of effects stock_effects took from a stock 360 file are left out:
        each took a place in the game's room for 600 effects next to the stock one (PC
        mp_quarry: 294 effects with common_mp's 411, stock mp_quarry 147). A pointer to one
        (another effect's child, a breakable prop's destroy effect) goes to the stock copy;
        the materials and pictures it brought are written where they are pointed at next (the
        convert step's left-out assets). swapped: {id() of the PC entry: (it, the stock entry)}."""
        # Only one nothing kept points into: a material, shader set or picture a dropped copy
        # holds can be pointed at by the map's other effects before it would be written
        # (mp_backlot: a shader set of one, pointed at by another effect's material).
        swapped = dict(swapped)
        kept = len(swapped)
        inside = {}
        for k, (e, stock) in swapped.items():
            for o in iter_objects(e[1]):
                if o is not e[1]:
                    inside[id(o)] = k
                    if isinstance(o, dict) and "_slot" in o:
                        inside[id(o["_slot"])] = k
        while True:
            keep = set()
            for e in ents:
                if id(e) in swapped:
                    continue
                for o in iter_objects(e[1]):
                    for c in (o.get("@", {}).values() if isinstance(o, dict) else
                              (o if isinstance(o, PtrList) else ())):
                        for x in (c if isinstance(c, list) else [c]):
                            if isinstance(x, Ref) and id(x.target) in inside:
                                keep.add(inside[id(x.target)])
            keep &= set(swapped)
            if not keep:
                break
            for k in keep:
                swapped.pop(k)
            inside = {i: k for i, k in inside.items() if k in swapped}
        kept -= len(swapped)
        if kept:
            self.log("  %d PC copies of stock effects kept (renamed ~pc/...): the map's other assets "
                     "point into them" % kept)
        if not swapped:
            return
        by_obj = {}
        for e, stock in swapped.values():
            by_obj[id(e)] = stock
            by_obj[id(e[1])] = stock
            if "_slot" in e[1]:
                by_obj[id(e[1]["_slot"])] = stock
        moved = 0
        for o in iter_objects(ents):
            items = o.get("@", {}).items() if isinstance(o, dict) else (
                enumerate(o) if isinstance(o, PtrList) else ())
            for k, c in list(items):
                for i, x in enumerate(c if isinstance(c, list) and not isinstance(o, PtrList) else [c]):
                    if isinstance(x, Ref) and id(x.target) in by_obj and (x.rel in (0, 4)):
                        stock = by_obj[id(x.target)]
                        x.target, x.rel, x.t = stock, 4, None
                        moved += 1
                    elif isinstance(x, dict) and id(x) in by_obj:
                        r = Ref(1)
                        r.target, r.rel = by_obj[id(x)], 4
                        if isinstance(c, list) and not isinstance(o, PtrList):
                            c[i] = r
                        elif isinstance(o, dict):
                            o["@"][k] = r
                            if isinstance(o.get(k[0]), str):
                                o[k[0]] = "0x00000001"
                        else:
                            o[k] = r
                        moved += 1
        ents[:] = [e for e in ents if id(e) not in swapped]
        self.log("  %d PC copies of those effects left out (%d pointers to them now name the "
                 "stock copy)" % (len(swapped), moved))

    WORLD_ASSETS = {"gfx_map": "GfxWorld", "col_map_mp": "clipMap_t", "com_map": "ComWorld",
                    "game_map_mp": "GameWorldMp", "fx_map": "FxWorld", "map_ents": "MapEnts"}

    def stock_world_swap(self, ents):
        """Test switch stock_world: every world asset the stock 360 file of the same map has
        (same name) is copied in from there in place of the converted one, with what it points
        at (its own materials, pictures, ...)."""
        names = []
        for i, e in enumerate(ents):
            typ = self.WORLD_ASSETS.get(e[0])
            if typ is None or not isinstance(e[1], dict):
                continue
            name = (asset_name(e[1]) or _name(e[1]) or b"").lstrip(b",")
            src = self.library.get((typ, name))
            if src is None:
                continue
            new = self.copy_in(src)
            self.done.add(id(new))
            for o in iter_objects(new):
                if isinstance(o, dict):
                    self.done.add(id(o))
            slot = e[1].get("_slot")
            if slot is not None:
                new["_slot"] = slot
                slot.asset = new
            ents[i] = tree.AssetEntry([e[0], new])
            names.append(e[0])
        if names:
            self.log("  test: world from the stock 360 file (%s)" % ", ".join(names))
        else:
            self.warn("stock_world is on, but no stock 360 file given has this map's world")

    def merge_same_named(self, ents):
        """One asset list entry per (type, name), as stock 360 files have: the others' pointers
        go to it and they are left out. PC copies of stock maps carry their own team models next
        to the stock team copied in (PC mp_rust: 37 models twice, 7 MB), and several PC shader
        sets become the same 360 one (mc_l_sm_t0c0 five times, 4 MB of shader sets in all)."""
        groups = {}
        for i, e in enumerate(ents):
            if isinstance(e, tree.AssetEntry) and isinstance(e[1], dict):
                name = asset_name(e[1]) or _name(e[1])
                if name:
                    groups.setdefault((e[0], name), []).append(i)
        keep = {}       # id() of a dropped asset -> the entry kept in its place
        for (typ, name), idx in groups.items():
            if len(idx) < 2:
                continue
            first = next((i for i in idx if id(ents[i]) in self.stock_entry_ids), idx[0])
            for i in idx:
                if i != first:
                    keep[id(ents[i][1])] = ents[first]
        if not keep:
            return
        dropped = set(keep)
        pos = {id(e): i for i, e in enumerate(ents)}

        def ref_to(entry):
            r = Ref(0)
            r.target, r.rel = entry, 4
            return r

        def swap(x):
            if isinstance(x, dict) and id(x) in keep:
                return ref_to(keep[id(x)])
            if isinstance(x, Ref) and isinstance(x.target, dict) and id(x.target) in keep \
                    and keep[id(x.target)][1].get("@") is x.target.get("@"):
                # Inside the asset itself (a techset's techniques alias each other): copies of
                # one stock asset share their insides, so the kept one has the same layout.
                r = Ref(x.val)
                r.target, r.rel, r.t = keep[id(x.target)][1], x.rel, x.t
                return r
            if isinstance(x, Ref):
                t = x.target[1] if isinstance(x.target, tree.AssetEntry) else None
                a = t if isinstance(t, dict) and x.rel == 4 else _asset_in_slot(x)
                if isinstance(a, dict) and id(a) in keep:
                    return ref_to(keep[id(a)])
            return None
        early = 0
        for i, e in enumerate(ents):
            if not isinstance(e[1], dict) or id(e[1]) in dropped:
                continue
            for o in iter_objects(e[1]):
                items = o.get("@", {}).items() if isinstance(o, dict) else (
                    enumerate(o) if isinstance(o, PtrList) else ())
                for k, c in list(items):
                    if isinstance(c, list) and not isinstance(c, PtrList):
                        for j, x in enumerate(c):
                            v = swap(x)
                            if v is not None:
                                c[j] = v
                                early += pos.get(id(v.target), -1) > i
                        continue
                    v = swap(c)
                    if v is not None:
                        if isinstance(o, dict):
                            o["@"][k] = v
                        else:
                            o[k] = v
                        early += pos.get(id(v.target), -1) > i
        types = Counter(e[0] for e in ents if isinstance(e[1], dict) and id(e[1]) in dropped)
        ents[:] = [e for e in ents if not (isinstance(e[1], dict) and id(e[1]) in dropped)]
        # What a kept asset shares with a dropped one (PC models share their surfaces: PC
        # mp_rust's ghillie sniper head uses the TF141 sniper head's) is written at its first
        # pointer instead.
        reached = set(id(o) for o in iter_objects(ents))
        for o in list(iter_objects(ents)):
            if not isinstance(o, dict):
                continue
            for c in o.get("@", {}).values():
                for x in (c if isinstance(c, list) else [c]):
                    if isinstance(x, Ref) and isinstance(x.target, tree.InsertSlot) and \
                            isinstance(x.target.asset, dict) and id(x.target.asset) not in reached:
                        x.target.asset["_forward"] = True
        self.log("  %d duplicate assets left out, their pointers going to the one kept (%s)" % (
            len(dropped), ", ".join("%s %d" % kv for kv in types.most_common())))
        if early:
            self.warn("%d pointers to a merged asset come before it in the file" % early)

    def pictures_before_composites(self, ents, start, end):
        """merge_decals' composite materials (ents[start:end], before the world) point at the
        pictures of the ground and decal materials. A picture first written further on (inside
        the world, mp_waw_castle's berlin_floors_wood_dirty_white1_n) would be pointed at before
        it is written: it is written at the composite's pointer instead (its first), and the
        places that held it later point back at it."""
        before = set(id(o) for e in ents[:start] for o in iter_objects([e]))
        late = {}
        for e in ents[start:end]:
            for o in iter_objects([e]):
                if not isinstance(o, dict):
                    continue
                for c in o.get("@", {}).values():
                    for x in (c if isinstance(c, list) else [c]):
                        a = x.target.asset if isinstance(x, Ref) and isinstance(x.target, tree.InsertSlot) else None
                        if isinstance(a, dict) and a.get("_asset") == "GfxImage" and id(a) not in before:
                            late[id(a)] = a
        if not late:
            return
        moved = 0
        for o in list(iter_objects(ents[end:])):
            if not isinstance(o, dict):
                continue
            ch = o.get("@", {})
            for k, v in list(ch.items()):
                if isinstance(v, dict) and id(v) in late and "_slot" in v:
                    r = tree.Ref(1)     # (not null until the writer places the picture)
                    r.target, r.rel = v["_slot"], 0
                    ch[k] = r
                    if o.get("union") in ("ffffffff", "fffffffe"):
                        o["union"] = "00000001"
                    elif o.get(k[0]) in ("follow", "insert"):
                        o[k[0]] = "0x00000001"
                    moved += 1
        for a in late.values():
            a["_forward"] = True
        # Every pointer to such a picture points at the picture itself, not its slot: the first
        # one writes it there, and a slot only gets a place when the picture is written at it.
        for o in iter_objects(ents):
            if not isinstance(o, dict):
                continue
            for c in o.get("@", {}).values():
                for x in (c if isinstance(c, list) else [c]):
                    if isinstance(x, Ref) and isinstance(x.target, tree.InsertSlot) and id(x.target.asset) in late:
                        x.target, x.rel, x.t = x.target.asset, 0, None
        self.log("  decal layers: %d pictures written with the composite materials that use "
                 "them (first written further on before)" % len(late))

    def own_reference_copies(self, ents):
        """A pointer to the slot of a picture that is only a name (",$white": the game has it
        loaded) gets its own copy of that name, as stock materials repeat such names inline. The
        360 orders some structs' members differently from the PC (GfxWorld), so the pointer
        can come before the asset it points into: PC mp_tundra_depot's world stopped the
        writer ("a pointer refers to ... before it is written")."""
        count = 0
        self.ref_copy_types = {}
        # A pointer into another element's pointer array (effects share a mark material:
        # FxElemMarkVisuals.materials[1] "the same as that one's"): the writer would take it for
        # the element. Point it at the material itself (its slot), or copy a name-only one.
        shared = 0
        for o in list(iter_objects(ents)):
            items = o.get("@", {}).items() if isinstance(o, dict) else (
                enumerate(o) if isinstance(o, PtrList) else ())
            for k, c in list(items):
                v = _ptr_array_value(c) if isinstance(c, Ref) else None
                if v is None:
                    continue
                if isinstance(v, dict) and "_slot" in v:
                    v = Ref(0)
                    v.target, v.rel = _ptr_array_value(c)["_slot"], 0
                elif isinstance(v, dict) and (_name(v) or b"").startswith(b","):
                    v = {kk: vv for kk, vv in v.items() if kk not in ("_slot", "_forward")}
                    self.done.add(id(v))
                if isinstance(o, dict):
                    o["@"][k] = v
                else:
                    o[k] = v
                shared += 1
        if shared:
            self.log("  %d pointers into another element's material list pointed at the material" % shared)
        objs = list(iter_objects(ents))
        # Name-only assets of other kinds too once nothing holds them any more: the PC wrote
        # each where its first pointer was, and that one can be gone (a decal material Merge
        # decal layers drew into a composite: PC mp_backlot's ",wc_l_sm_b0c0", the materials
        # listed after it pointing at its slot stopped the writer).
        held = {id(o) for o in objs}
        for o in objs:
            if not isinstance(o, dict):
                continue
            ch = o.get("@", {})
            for k, c in list(ch.items()):
                if not (isinstance(c, Ref) and isinstance(c.target, tree.InsertSlot) and c.rel == 0):
                    continue
                a = c.target.asset
                if not (isinstance(a, dict) and (_name(a) or b"").startswith(b",")
                        and (a.get("_asset") == "GfxImage" or id(a) not in held)):
                    continue
                new = {kk: vv for kk, vv in a.items() if kk not in ("_slot", "_forward")}
                new["@"] = dict(a.get("@", {}))
                if isinstance(a.get("info"), dict):
                    new["info"] = dict(a["info"])
                ch[k] = new
                if "union" in o and len(k[1]) == 0:
                    o["union"] = "ffffffff"
                elif isinstance(o.get(k[0]), str):
                    o[k[0]] = "follow"
                self.done.add(id(new))
                count += 1
                kind = a.get("_asset") or "other"
                self.ref_copy_types[kind] = self.ref_copy_types.get(kind, 0) + 1
        if count:
            self.log("  %d pointers to a name-only asset given their own copy of the name (%s)" % (
                count, ", ".join("%s %d" % kv for kv in sorted(self.ref_copy_types.items()))))

    def stock_anims(self, ents):
        """Animations (XAnimParts) can't be converted yet: the 360 packs rotations differently
        (one 32-bit value: sign and index of the largest component, the other three over it in
        9, 10 and 10 bits) and has two more part types. Same-named ones come from a stock 360
        file (stock_anims); the rest are left out, as nothing points at an animation (scripts
        and map entities name them)."""
        took, dropped, converted, reasons = [], [], [], set()
        keep = []
        for e in ents:
            if e[0] != "xanim" or not isinstance(e[1], dict):
                keep.append(e)
                continue
            name = (_name(e[1]) or b"")
            if name.startswith(b","):
                keep.append(e)
                continue
            if not hasattr(self, "_anim_root"):
                # By resolved name: stock files name animations through a shared string.
                self._anim_root, self._stock_anims = {}, {}
                for base, r in self.x_refs:
                    resident = os.path.splitext(os.path.basename(base))[0] in RESIDENT
                    for o in iter_objects(r["assets"]):
                        if isinstance(o, dict) and o.get("_asset") == "XAnimParts":
                            n = (_name(o) or b"")
                            if n and not n.startswith(b",") and (n not in self._stock_anims or not resident):
                                self._stock_anims.setdefault(n, o)
                                self._anim_root.setdefault(id(o), r)
            src = self._stock_anims.get(name) if self.fixes["stock_anims"] else None
            if src is None:
                why = self.convert_anim(e[1]) if self.fixes["convert_anims"] else "convert switched off"
                if why is None:
                    keep.append(e)
                    converted.append(name.decode("latin-1"))
                else:
                    dropped.append(name.decode("latin-1"))
                    reasons.add(why)
                continue
            root = self._anim_root[id(src)]
            new = self.copy_in(src)
            self.remap_bones(new, root["script_strings"])
            self._replace(e[1], new)
            for o in iter_objects(e[1]):
                if isinstance(o, dict):
                    self.done.add(id(o))
            keep.append(e)
            took.append(name.decode("latin-1"))
        ents[:] = keep
        if took:
            self.log("  %d animations from the stock 360 files" % len(took))
        if converted:
            self.log("  test: %d PC animations converted to the 360's layout: %s" % (
                len(converted), ", ".join(converted[:8]) + (" ..." if len(converted) > 8 else "")))
        if dropped:
            self.warn("%d animations left out (no stock 360 copy; %s): %s" % (
                len(dropped), "; ".join(sorted(reasons)),
                ", ".join(dropped[:8]) + (" ..." if len(dropped) > 8 else "")))

    def convert_anim(self, d):
        """PC animation d in the 360's layout, in place (xanim.py: rotations packed, two more part
        types). Returns None, or why it can't be converted."""
        import xanim
        ch = d.get("@", {})

        def leaf(k):
            v = ch.get((k, ()))
            return v.target if isinstance(v, Ref) else v

        def values(k, fmt):
            lf = leaf(k)
            if not isinstance(lf, Leaf):
                return []
            n = len(lf.raw) // struct.calcsize(fmt)
            return list(struct.unpack(lf.E + "%d%s" % (n, fmt), lf.raw[:n * struct.calcsize(fmt)]))

        delta = leaf("deltaPart")
        if isinstance(delta, dict) and any(delta.get(k) for k in ("trans", "quat2", "quat")):
            return "PC animations that move the whole object (delta parts) can't be converted yet"
        try:
            bc, ds, di, rs, ri = xanim.convert(
                list(bytes.fromhex(d["boneCount"])), d["numframes"], values("dataShort", "h"),
                values("dataInt", "i"), values("randomDataShort", "h"))
        except (xanim.AnimError, KeyError, ValueError, TypeError) as e:
            return "PC animation not in the layout expected (%s)" % e
        new = {k: v for k, v in d.items() if k not in ("@", "_slot", "_forward")}
        nch = {}
        for k, v in ch.items():
            lf = v.target if isinstance(v, Ref) else v
            if isinstance(lf, Leaf):
                xt = self.xc.type_by_name(getattr(lf.t, "name", "unsigned char"))
                nch[k] = Leaf(xt, lf.n, _swap_words(lf.raw, lf.t.size if hasattr(lf.t, "size") else 1), ">")
            else:
                nch[k] = v
        idx = d.get("indices")
        if isinstance(idx, dict):
            new["indices"] = dict(idx)
            if "@" in idx:
                new["indices"]["@"] = {}
                for k, v in idx["@"].items():
                    lf = v.target if isinstance(v, Ref) else v
                    new["indices"]["@"][k] = Leaf(self.xc.type_by_name(lf.t.name), lf.n,
                                                  _swap_words(lf.raw, lf.t.size), ">") \
                        if isinstance(lf, Leaf) else v
        for k, vals, fmt, tname in (("dataShort", ds, "H", "int16_t"), ("dataInt", di, "I", "int"),
                                     ("randomDataShort", rs, "H", "int16_t"),
                                     ("randomDataInt", ri, "I", "int")):
            if vals:
                nch[(k, ())] = Leaf(self.xc.type_by_name(tname), len(vals),
                                    struct.pack(">%d%s" % (len(vals), fmt), *vals), ">")
                new[k] = "follow"
            else:
                nch.pop((k, ()), None)
                new[k] = None
        new.update(boneCount=bc.hex(), dataShortCount=len(ds), dataIntCount=len(di),
                   randomDataShortCount=len(rs), randomDataIntCount=len(ri))
        new["@"] = nch
        self._replace(d, new)
        for o in iter_objects(d):
            if isinstance(o, dict):
                self.done.add(id(o))
        return None

    def name_loaded_curves(self, alias):
        """Volume and other curves (SndCurve) in an alias list made here that the game already
        has loaded ($default) only name them (",$default"), as stock maps do. A copy of its own
        in each took one of the game's 64 curve slots apiece: PC mp_showdown's 65 silent aliases
        stopped the load ("Exceeded limit of 64 'sndcurve' assets")."""
        for o in list(iter_objects(alias)):
            if not isinstance(o, dict):
                continue
            ch = o.get("@", {})
            for k, c in list(ch.items()):
                held = c if isinstance(c, list) else [c]     # (a pointer array holds it in a list)
                for i, x in enumerate(held):
                    if isinstance(x, dict) and x.get("_asset") == "SndCurve":
                        n = _pool_name(x) or b""
                        if n and not n.startswith(b",") and ("SndCurve", n.lower()) in self.loaded:
                            ref = self.reference("SndCurve", n)
                            self.done.add(id(ref))
                            if held is c:
                                c[i] = ref
                            else:
                                ch[k] = ref

    # Alias head members the PC's alias keeps (the stock "null" alias's head gives the rest).
    HEAD_FIELDS = ("sequence", "volMin", "volMax", "pitchMin", "pitchMax", "distMin", "distMax",
                   "velocityMin", "flags", "probability", "lfePercentage", "centerPercentage",
                   "startDelay", "envelopMin", "envelopMax", "envelopPercentage")

    def sound_files(self):
        """{lowercase path under sound/: (zipfile, path)} in the PC game's .iwd files and then
        the map's (later ones win)."""
        if getattr(self, "_sound_files", None) is None:
            found = {}
            for zf in self.game_iwds + self.iwds:
                for n in zf.namelist():
                    low = n.replace("\\", "/").lower()
                    if low.startswith("sound/"):
                        found[low[6:]] = (zf, n)
            self._sound_files = found
        return self._sound_files

    def pc_audio(self, d, ents):
        """[(PC alias head, XMA-encoded sound, its name)] for the heads of PC alias list d
        whose audio is here: the map's loaded sounds, and streamed ones found in the .iwd files.
        Each sound is encoded once."""
        if not hasattr(self, "_encoded"):
            self._encoded, self._encoded_bytes, self._no_audio = {}, 0, set()
            self._pc_loaded = {}
            for e in ents:
                if e[0] == "loaded_sound" and isinstance(e[1], dict):
                    n = _pool_name(e[1])
                    if n and not n.startswith(b","):
                        self._pc_loaded.setdefault(n.lower(), e[1])
        hp = d.get("@", {}).get(("head", ()))
        hp = hp.target if isinstance(hp, Ref) else hp
        out = []
        for h in hp if isinstance(hp, list) else [hp]:
            sf = h.get("@", {}).get(("soundFile", ())) if isinstance(h, dict) else None
            if not isinstance(sf, dict):
                continue
            u = sf.get("u") if isinstance(sf.get("u"), dict) else {}
            got, key = None, None
            if sf.get("type") == 1:
                ls = u.get("@", {}).get(("loadSnd", ()))
                if isinstance(ls, Ref):
                    ls = ls.target.asset if isinstance(ls.target, tree.InsertSlot) else ls.target
                if isinstance(ls, dict):
                    key = (_pool_name(ls) or b"").lstrip(b",").lower()
                    if not _sound_data(ls):
                        ls = self._pc_loaded.get(key, ls)
                    raw = _sound_data(ls)
                    if raw:
                        got = lambda raw=raw, info=ls.get("sound", {}).get("info"): wav_pcm(raw, info)
            elif sf.get("type") == 2:
                ss = u.get("streamSnd")
                c = ss.get("@", {}) if isinstance(ss, dict) else {}
                dr, nm = c.get(("dir", ())), c.get(("name", ()))
                if isinstance(nm, Str):
                    key = ((dr.b + b"/") if isinstance(dr, Str) and dr.b else b"") + nm.b
                    key = key.replace(b"\\", b"/").lower()
                    found = self.sound_files().get(key.decode("latin-1"))
                    if found:
                        got = lambda found=found: wav_pcm(found[0].read(found[1]))
            if not key:
                continue
            if key not in self._encoded and ("LoadedSound", _sound_name(key)) in self.loaded:
                # The game has it loaded already (common_mp): named only, as stock maps do.
                self._encoded[key] = [RESIDENT_SOUND, None]
            if key not in self._encoded:
                enc, pcm = None, got() if got else None
                if pcm and self._encoded_bytes < ENCODED_SOUND_BUDGET * 1024 * 1024:
                    enc = self.xma.encode(*pcm)
                    self._encoded_bytes += len(enc.data)
                elif not pcm:
                    self._no_audio.add(key)
                self._encoded[key] = [enc, None]      # (the 360 sound made from it, once made)
            if self._encoded[key][0] is not None:
                out.append((h, key))
        return out

    def encoded_heads(self, new, audio):
        """Alias list new (a copy of the stock "null" alias) gets a head for each (PC head, sound)
        in audio: the null head with the PC's numbers, playing its own 360 sound (a loaded one,
        as stock aliases' are). A sound used again points at the first copy."""
        hp = new["@"][("head", ())]
        hp = hp.target if isinstance(hp, Ref) else hp
        tmpl = hp[0] if isinstance(hp, list) else hp
        heads = []
        for pc, key in audio:
            h = copy.deepcopy(tmpl)
            for k in self.HEAD_FIELDS:
                if k in pc and k in h:
                    h[k] = pc[k]
            # Bit 7: a loaded sound, bit 8: streamed (stock: 1,684 and 3,802 of 5,486 heads).
            h["flags"] = (h["flags"] | 0x80) & ~0x100
            for k in ("secondaryAliasName", "chainAliasName"):
                s = pc.get("@", {}).get((k, ()))
                if isinstance(s, Str) and k in h:
                    h[k] = "follow"
                    h.setdefault("@", {})[(k, ())] = Str(s.b)
            u = h["@"][("soundFile", ())]["u"]
            enc, first = self._encoded[key]
            if enc is RESIDENT_SOUND:
                ref = self.reference("LoadedSound", _sound_name(key))
                self.done.add(id(ref))
                u["@"][("loadSnd", ())] = ref
            elif first is not None:
                r = tree.Ref(1)
                r.target, r.rel = first["_slot"], 0
                u["@"][("loadSnd", ())] = r
            else:
                ls = u["@"][("loadSnd", ())]
                self._encoded[key][1] = ls
                ls.setdefault("_slot", tree.InsertSlot(ls))
                ls["_slot"].asset = ls
                ls["@"][("name", ())] = Str(_sound_name(key))
                s = ls["sound"]
                info = s["info"]
                lf = info["@"][("data", ())]
                info["@"][("data", ())] = Leaf(lf.t, len(enc.data), enc.data, lf.E)
                info["dataSize"] = len(enc.data)
                # Where the audio starts and ends (in bits), and which quarter of its last
                # frame it ends in (3: the whole frame plays).
                info["unknown"] = [0xFFFFFFFF, 32, enc.end_bit, (3 << 24) | (3 << 16)] \
                    + [0] * (len(info["unknown"]) - 4)
                # XMA, one stream, its rate, one channel, its length in ms.
                su = [0] * len(s["unknown"])
                su[0], su[1], su[2], su[3], su[-1] = 0x04000000, 0x01000000, enc.rate, \
                    0x01000000, enc.ms
                s["unknown"] = su
                st = s["seekTable"]
                lf = st["@"][("data", ())]
                words = [1, len(enc.seek)] + enc.seek
                st["size"] = len(words)
                st["@"][("data", ())] = Leaf(lf.t, len(words), struct.pack(lf.E + "%dI" % len(words),
                                                                           *words), lf.E)
            heads.append(h)
        if isinstance(hp, list):
            hp[:] = heads
        else:
            new["@"][("head", ())] = heads
        new["count"] = len(heads)

    def stock_sounds(self, ents):
        """Sound alias lists come from a stock 360 file with the same alias (stock mp_rust has
        all 143 of PC mp_rust's), audio and all, as stock maps carry them. The PC file only
        names its sound files (",null.wav", read from the PC game's own files), which the 360
        has nothing under. An alias no stock file has gets the stock "null" alias's silent
        sound under its own name. The PC's sound file assets then go."""
        swapped, silent, missing, encoded = 0, [], [], []
        null = self.stock_aliases.get(b"null")
        self.xma = None
        if self.fixes["encode_sounds"]:
            try:
                import xma
                self.xma = xma
            except ImportError:
                self.warn("Encode PC sounds is on, but numpy isn't installed (pip install numpy, or "
                          "start mw2tools.bat again): the sounds stay silent")
        ours = [o for o in iter_objects(ents) if isinstance(o, dict)
                and o.get("_asset") == "snd_alias_list_t" and id(o) not in self.done]
        # Stock aliases share sound files (one alias's points into another's): those taken from
        # one file are made self-contained together, so they keep sharing, and go in that
        # file's order, so what is shared is written before what points at it.
        take = {}
        for d in ours:
            n = alias_name(d)
            if n and not n.startswith(b",") and n in self.stock_aliases:
                take[id(d)] = self.stock_aliases[n]
        by_root = {}
        for src in take.values():
            by_root.setdefault(id(src[2]), {})[id(src[0])] = src[0]
        for group in by_root.values():
            self.localize(list(group.values()))
        rank = {}
        for d in ours:
            src = take.get(id(d))
            if src is None:
                continue
            new = dict(src[0])
            new.pop("_slot", None)
            self._replace(d, new)
            rank[id(d)] = (id(src[2]), src[3])
            swapped += 1
        at = [i for i, e in enumerate(ents) if isinstance(e[1], dict) and id(e[1]) in rank]
        for i, e in zip(at, sorted((ents[i] for i in at), key=lambda e: rank[id(e[1])])):
            ents[i] = e
        # Silent ones share the first one's sound file (in the order they are written).
        pos = {id(e[1]): i for i, e in enumerate(ents)}
        ours.sort(key=lambda d: pos.get(id(d), len(ents)))
        null_sf = None
        for d in ours:
            if id(d) in rank:
                continue
            name = alias_name(d)
            if not name or name.startswith(b","):
                continue
            if null is None:
                missing.append(name)
                continue
            new = copy.deepcopy(self.copy_in(null[0]))
            audio = self.pc_audio(d, ents) if self.xma else None
            if audio:
                self.encoded_heads(new, audio)
            self.name_loaded_curves(new)
            new["@"][("aliasName", ())] = Str(name)
            head = new["@"].get(("head", ()))
            head = head.target if isinstance(head, Ref) else head
            for h in head if isinstance(head, list) else [head]:
                if isinstance(h, dict):
                    h.setdefault("@", {})[("aliasName", ())] = Str(name)
                    h["aliasName"] = "follow"
                    sf = h["@"].get(("soundFile", ()))
                    if not isinstance(sf, dict) or audio:
                        continue
                    if null_sf is None:
                        null_sf = sf
                        continue
                    # Each keeps its own sound file record (inside its own alias list) and
                    # shares the first one's silent sound through that sound's slot. Pointing
                    # at the first one's record pointed into another asset's temporary data,
                    # which the 360 reuses once that asset is loaded (converted mp_rust: two
                    # ambient emitters pointed at whatever was loaded there next).
                    first = (null_sf.get("u") or {}).get("@", {}).get(("loadSnd", ()))
                    u = sf.get("u")
                    if isinstance(first, dict) and "_slot" in first and isinstance(u, dict) \
                            and isinstance(u.get("@", {}).get(("loadSnd", ())), dict):
                        r = tree.Ref(1)
                        r.target, r.rel = first["_slot"], 0
                        u["@"][("loadSnd", ())] = r
            self._replace(d, new)
            (encoded if audio else silent).append(name)
        # The PC's sound files (each only a name) are pointed at by nothing now.
        held = set()
        for e in ents:
            if e[0] == "loaded_sound":
                continue
            for o in iter_objects(e[1]):
                if isinstance(o, dict):
                    for c in o.get("@", {}).values():
                        for x in (c if isinstance(c, list) else [c]):
                            if isinstance(x, Ref):
                                t = x.target.asset if isinstance(x.target, tree.InsertSlot) else x.target
                                held.add(id(t))
                            elif isinstance(x, dict):
                                held.add(id(x))
        before = len(ents)
        ents[:] = [e for e in ents if not (e[0] == "loaded_sound" and isinstance(e[1], dict)
                                           and id(e[1]) not in held)]
        self.log("  sounds: %d aliases from the stock 360 files, %d silent (no stock copy), "
                 "%d PC sound files left out" % (swapped, len(silent), before - len(ents)))
        if encoded:
            sounds = [v[0] for v in self._encoded.values() if hasattr(v[0], "data")]
            resident = sum(1 for v in self._encoded.values() if v[0] is RESIDENT_SOUND)
            self.log("  test: %d aliases play the PC's audio, encoded for the 360 (%d sounds, %.1f MB; "
                     "%d more the game has loaded)" % (len(encoded), len(sounds),
                                                       sum(len(e.data) for e in sounds) / 1048576.0, resident))
        if self.xma and getattr(self, "_encoded_bytes", 0) >= ENCODED_SOUND_BUDGET * 1048576:
            self.warn("encoded sounds reached %d MB: the rest stay silent" % ENCODED_SOUND_BUDGET)
        if self.xma and compressed_pcm.missing:
            self.warn("some sounds are .mp3 or compressed .wav files, which need miniaudio "
                      "(pip install miniaudio, or start mw2tools.bat again): they stay silent")
        if self.xma and getattr(self, "_no_audio", None):
            self.log("    no audio found for %d sound files%s" % (
                len(self._no_audio), "" if self.game_iwds else
                " (give the PC game folder for its streamed sounds)"))
        if silent:
            self.log("    silent: " + ", ".join(n.decode("latin-1") for n in silent[:20])
                     + (" ..." if len(silent) > 20 else ""))
        if missing:
            self.warn("%d sound aliases have no stock copy and no stock \"null\" alias was found "
                      "to stand in: add a stock map, they stay as the PC has them" % len(missing))

    def rename_clashes(self):
        """The map's own materials and pictures named as one a stock effect brings get a name
        of their own: the game keeps one asset per name, and the stock effect has to get its
        own (the converted ones drew the haze)."""
        ours = set()
        names = set()
        for fx in self.stock_fx_used:
            for o in iter_objects(fx):
                ours.add(id(o))
                # (",name": a reference to one the game has loaded, not an asset of its own)
                if isinstance(o, dict) and o.get("_asset") in ("Material", "GfxImage") and \
                        not (_name(o) or b",").startswith(b","):
                    names.add((o["_asset"], _name(o)))
        if self.fixes["share_effect_pictures"]:
            self.share_effect_pictures(ours)
        count = 0
        for o in iter_objects(self.root["assets"]):
            if isinstance(o, dict) and id(o) not in ours and o.get("_asset") in ("Material", "GfxImage") \
                    and (o["_asset"], _name(o)) in names:
                _set_name(o, b"~pc/" + _name(o))
                count += 1
        return count

    def share_effect_pictures(self, ours):
        """A picture of the map's own with the name of one a stock effect brings (ours: id() of
        everything in the stock effects) is the same picture when it comes from the PC game's
        files, not the map's own .iwd: the map's materials point at the stock effect's copy
        (its slot, which the game sets to it), and the map's own copy is left out. Renamed and
        kept (rename_clashes), it took a second place in the game's room for 3,584 pictures
        (PC mp_quarry: 123 such pictures, mp_estate 71, which came to 3,514)."""
        stock = {}
        for fx in self.stock_fx_used:
            for o in iter_objects(fx):
                if isinstance(o, dict) and o.get("_asset") == "GfxImage":
                    n = _name(o) or b","
                    if not n.startswith(b","):
                        stock.setdefault(n, o)
        if not stock:
            return
        swap = {}
        for o in iter_objects(self.root["assets"]):
            if isinstance(o, dict) and id(o) not in ours and o.get("_asset") == "GfxImage":
                n = _name(o) or b""
                if n in stock and n.decode("latin-1").lower() not in self.map_pictures:
                    swap[id(o)] = stock[n]
                    if "_slot" in o:
                        swap[id(o["_slot"])] = stock[n]
        if not swap:
            return
        for v in swap.values():
            # Stock effects hold their pictures inline: written with a slot of its own at its
            # first pointer (in the effect, listed first), which the map's pointers then name.
            if "_slot" not in v:
                v["_slot"] = tree.InsertSlot(v)
            v["_forward"] = True
        moved = 0
        for o in iter_objects(self.root["assets"]):
            if not isinstance(o, dict) or id(o) in ours:
                continue
            ch = o.get("@", {})
            for k, c in list(ch.items()):
                if isinstance(c, dict) and id(c) in swap:
                    target = swap[id(c)]
                elif isinstance(c, Ref) and id(c.target) in swap and c.rel == 0:
                    target = swap[id(c.target)]
                else:
                    continue
                r = Ref(1)      # (not null until the writer places the picture)
                r.target, r.rel = target["_slot"], 0
                ch[k] = r
                if o.get("union") in ("ffffffff", "fffffffe"):
                    o["union"] = "00000001"
                elif o.get(k[0]) in ("follow", "insert"):
                    o[k[0]] = "0x00000001"
                moved += 1
        gone = set(k for k, v in swap.items())
        self.root["assets"][:] = [e for e in self.root["assets"]
                                  if not (isinstance(e[1], dict) and id(e[1]) in gone)]
        self.log("  %d pictures the map shares with stock effects it uses taken from them (%d "
                 "pointers), not kept twice" % (len(set(id(v) for v in swap.values())), moved))

    # ------------------------------------------------------------ hooks

    def pre_GfxWorldDpvsDynamic(self, d, tp, tx):
        """PC: dynEntVisData[2][3]; 360: six separate pointers dynEntVisData0_0 .. 1_2."""
        v = d.pop("dynEntVisData", None)
        ch = d.get("@", {})
        for i in range(2):
            pl = ch.pop(("dynEntVisData", (i,)), None)
            for j in range(3):
                k = "dynEntVisData%d_%d" % (i, j)
                d[k] = v[i * 3 + j] if v else None
                if pl is not None and pl[j] is not None:
                    ch[(k, ())] = pl[j]
        return None

    def post_GfxAabbTree(self, d, tx):
        """childrenOffset is a byte distance to the node's first child: nodes are 44 bytes on
        the PC and 40 on the 360."""
        tp = self.P.defs.types["GfxAabbTree"]
        d["childrenOffset"] = d["childrenOffset"] // tp.size * tx.size

    def post_GfxWorldDpvsStatic(self, d, tx):
        """The PC sorts surfaces twice (all, then without decals); the 360 keeps one list."""
        lf = d["@"].get(("sortedSurfIndex", ()))
        n = d["staticSurfaceCount"]
        if isinstance(lf, Leaf) and lf.n > n:
            lf.raw, lf.n = lf.raw[:n * lf.t.size], n

    def post_GfxWorldDraw(self, d, tx):
        """IW4x ZoneBuilder fills the lightmap override pointers with the sky and $outdoor
        pictures. Stock 360 maps leave them empty; set, the game would light every surface
        with them (a cube map where a flat lightmap belongs)."""
        for k in ("lightmapOverridePrimary", "lightmapOverrideSecondary"):
            c = d["@"].pop((k, ()), None)
            d[k] = None
            if isinstance(c, dict):
                # The picture was written here first; later pointers to it now get it instead.
                self.moved_images.append(c)

    def world_material_memory(self, world):
        """The world's materialMemory list as every stock 360 map has it (mp_rust, mp_favela,
        mp_afghan: 1,866 of 1,866 entries): one entry for every material its surfaces draw with,
        sorted by name, each with 44 bytes a vertex of its vertex runs + 6 a triangle + 40 a
        surface. The PC's list counts differently, and the composite materials Merge decal
        layers makes were left out of it (converted mp_rust: 76 entries, stock 177)."""
        ch = world.get("@", {})
        lst = ch.get(("materialMemory", ()))
        lst = lst.target if isinstance(lst, Ref) else lst
        surfs = world["dpvs"]["@"].get(("surfaces", ()))
        surfs = surfs.target if isinstance(surfs, Ref) else surfs
        if not isinstance(lst, list) or not lst or not isinstance(lst[0], dict) or not isinstance(surfs, list):
            return
        tmpl = lst[0]
        # The PC's own entries are kept (re-ordered, with the 360's count): the list is
        # written before the surfaces, so it may hold a material itself, written there first,
        # with the surfaces pointing back at it. Materials it lacks (decals' composite ones)
        # are pointed at through the asset list, where they're written before the world.
        listed = {}
        for ent in lst:
            c = ent.get("@", {}).get(("material", ())) if isinstance(ent, dict) else None
            m = c if isinstance(c, dict) else deref(c) if c is not None else None
            if isinstance(m, dict):
                listed.setdefault(id(m), ent)
        per = {}
        for sf in surfs[:world.get("surfaceCount") or len(surfs)]:
            c = sf.get("@", {}).get(("material", ())) if isinstance(sf, dict) else None
            m = deref(c) if c is not None else None
            if not isinstance(m, dict) or not isinstance(c, Ref):
                return      # (a surface whose material isn't pointed at as usual: list kept)
            name = asset_name(m) or _name(m) or b""
            e = per.setdefault(id(m), {"name": name, "ref": c, "runs": set(), "tris": 0, "surfs": 0})
            t = sf["tris"]
            e["runs"].add((t["firstVertex"], t["vertexCount"]))
            e["tris"] += t["triCount"]
            e["surfs"] += 1
        new = []
        for mid, e in sorted(per.items(), key=lambda kv: kv[1]["name"]):
            old = listed.get(mid)
            if old is not None:
                ent = dict(old)
            elif isinstance(e["ref"].target, tree.AssetEntry):
                r = Ref(1)
                r.target, r.rel, r.t = e["ref"].target, e["ref"].rel, e["ref"].t
                ent = {k: v for k, v in tmpl.items() if k != "@"}
                ent["@"] = {("material", ()): r}
                ent["material"] = "0x00000001"      # (an alias: a non-null placeholder)
            else:
                self.warn("world material list left as the PC file has it (material %s isn't "
                          "pointed at as expected)" % e["name"].decode("latin-1"))
                return
            ent["memory"] = 44 * sum(v for _, v in e["runs"]) + 6 * e["tris"] + 40 * e["surfs"]
            new.append(ent)
        before = len(lst)
        lst[:] = new
        world["materialMemoryCount"] = len(new)
        if len(new) != before:
            self.log("  world material list: %d materials as the 360 counts them (the PC file "
                     "listed %d)" % (len(new), before))

    def post_GfxWorld(self, d, tx):
        if self.fixes["merge_decals"]:
            decals.merge_decal_layers(self, d, deref, asset_name)
        if self.fixes["surface_order"]:
            self.order_surfaces(d)
        if self.fixes["merge_decals"]:
            decals.relay_surfaces(self, d, deref, asset_name)
            decals.compact_vertices(self, d, deref, asset_name)
        if self.fixes["surface_bounds"]:
            self.fill_surface_bounds(d)
        if self.fixes["model_box_bounds"]:
            self.grow_model_boxes(d)
        if self.fixes["tree_model_bounds"]:
            self.grow_tree_bounds(d)
        if self.fixes["lighting_origin"]:
            self.fill_lighting_origins(d)
        if self.fixes["rebuild_trees"]:
            self.rebuild_trees(d)
        if self.fixes["huge_tree_boxes"] or self.fixes["huge_leaf_boxes"] or self.fixes["huge_inner_boxes"]:
            self.huge_tree_boxes(d, leaves=self.fixes["huge_tree_boxes"] or self.fixes["huge_leaf_boxes"],
                                 inner=self.fixes["huge_tree_boxes"] or self.fixes["huge_inner_boxes"])
        if self.fixes["tree_box_margin"]:
            self.grow_tree_boxes_by(d, TREE_BOX_MARGIN)
        if self.fixes["one_room"]:
            self.one_room(d)
        if self.fixes["room_box_bounds"]:
            self.grow_room_boxes(d)
        if self.fixes["swap_models_test"]:
            self.swap_models(d, *SWAP_MODELS)
        if self.fixes["tree_list_slices"]:
            self.slice_tree_lists(d)
        if self.fixes["probe_brightness"]:
            self.dim_standin_probes(d)
        if self.fixes["material_memory"]:
            self.world_material_memory(d)
        ch = d["@"]
        # One bit per room ("cell has sun-lit surfaces"), 32 to a word. Stock 360 maps always
        # carry it (all zero in every one checked: mp_rust 1 word, mp_favela 2) and the renderer
        # reads it every frame; the original PC mp_rust has none (null), and its conversion
        # crashed in the renderer as the first frame drew.
        cells = (d.get("dpvsPlanes") or {}).get("cellCount") or 0
        if cells and ch.get(("cellHasSunLitSurfsBits", ())) is None:
            caster = ch.get(("cellCasterBits", ()))
            caster = caster.target if isinstance(caster, Ref) else caster
            m = next(m for m in tx.members if m.name == "cellHasSunLitSurfsBits")
            n = (cells + 31) // 32
            ch[("cellHasSunLitSurfsBits", ())] = Leaf(caster.t if isinstance(caster, Leaf) else m.type,
                                                        n, bytes(4 * n), ">")
            d["cellHasSunLitSurfsBits"] = "follow"
            self.log("  world: room sun-light bits filled in (the PC file has none)")
        for key, c in list(ch.items()):
            t = deref(c) if isinstance(c, Ref) else None
            if t is not None and any(t is m for m in self.moved_images):
                ch[key] = t
                d[key[0]] = "insert"
                self.moved_images = [m for m in self.moved_images if m is not t]
        if self.moved_images:
            raise PortError("a picture from the lightmap override is still needed elsewhere")

    def solid_runs(self, surfs, order, groups, group):
        """With merge_decals: the solid surfaces sorted so those with one material (and lighting)
        are neighbours, which the 360 draws as one (0x823FABB0); solid surfaces depth-test, so
        their order doesn't change the picture. Decals and see-through ones keep their order.
        A run takes at most 65,535 vertices (16-bit indices): bigger ones are cut in chunks,
        and chunks of one material are kept apart by other materials' surfaces (a sort key
        with no other material keeps its surfaces as they were). Converted mp_rust: 4,646
        draws after merging decals (stock 949)."""
        solid = [i for i in order if groups[i] == 0]
        rest = [i for i in order if groups[i] != 0]

        def key(i):
            s = surfs[i]
            m = deref(s.get("@", {}).get(("material", ())))
            return id(m), (s.get("laf") or {}).get("union")

        by_sort = {}
        for i in solid:
            by_sort.setdefault(group(surfs[i])[1], []).append(i)
        out = []
        for sk in sorted(by_sort):
            idx = by_sort[sk]
            keys = {}
            for i in idx:
                keys.setdefault(key(i), []).append(i)
            chunks = []             # (chunk number, first position, surfaces)
            for k, members in keys.items():
                blocks, total, cur, c = set(), 0, [], 0
                for i in members:
                    t = surfs[i]["tris"]
                    b = (t["firstVertex"], t["vertexCount"])
                    add = 0 if b in blocks else b[1]
                    if cur and total + add > 0xFFFF:
                        chunks.append((c, idx.index(members[0]), cur))
                        blocks, total, cur, c = set(), 0, [], c + 1
                        add = b[1]
                    blocks.add(b)
                    total += add
                    cur.append(i)
                chunks.append((c, idx.index(members[0]), cur))
            if len(keys) == 1 and len(chunks) > 1:
                out += idx      # nothing to keep its chunks apart: as it was
                continue
            # Chunk 0 of every material (in the order they first appear), then chunk 1 of those
            # that have one, ...; a round doesn't start with the material the one before ended
            # with (it's rotated), so no two chunks of one material meet.
            rounds = {}
            for c, first, members in chunks:
                rounds.setdefault(c, []).append((first, members))
            flat = []
            for c in sorted(rounds):
                part = [m for _, m in sorted(rounds[c])]
                if flat and key(part[0][0]) == key(flat[-1][0]):
                    part = part[1:] + part[:1]
                flat += part
            if any(key(flat[j][0]) == key(flat[j + 1][0]) for j in range(len(flat) - 1)):
                out += idx      # (only one material left with chunks: as it was)
                continue
            for members in flat:
                out += members
        return out + rest

    def order_surfaces(self, world):
        """Test switch surface_order: the static surfaces in the 360 tools' order (solid, sort key
        below 6; then decals and see-through; then shadow casters; each group keeping its order)
        and the dpvs ranges to match. Every per-surface array moves with them (bounds, draw
        surface, sun shadow bit) and every surface number is renumbered (sortedSurfIndex, the
        shadow geometry lists). Culling tree nodes index sortedSurfIndex, which keeps its order,
        so they stay valid. Brush model surfaces (after the static ones) don't move."""
        def tgt(c):
            return c.target if isinstance(c, Ref) else c
        dpvs = world.get("dpvs") or {}
        ch = dpvs.get("@", {})
        surfs = tgt(ch.get(("surfaces", ())))
        n = dpvs.get("staticSurfaceCount", 0)
        if not isinstance(surfs, list) or not n or len(surfs) < n:
            self.warn("surfaces couldn't be put in 360 order (data not found)")
            return

        def group(s):
            m = deref(s.get("@", {}).get(("material", ()))) if isinstance(s, dict) else None
            if not isinstance(m, dict):
                return 0, 0
            ts = deref(m.get("@", {}).get(("techniqueSet", ())))
            if isinstance(ts, dict) and b"shadowcaster" in (asset_name(ts) or b""):
                return 2, 0
            key = (m.get("info") or {}).get("sortKey") or 0
            return (0 if key < 6 else 1), key

        groups = [group(s)[0] for s in surfs[:n]]
        order = sorted(range(n), key=lambda i: groups[i])     # stable: each group keeps its order
        if self.fixes["merge_decals"]:
            order = self.solid_runs(surfs, order, groups, group)
        if order == list(range(n)):
            new_of = None
        else:
            new_of = [0] * n
            for new, old in enumerate(order):
                new_of[old] = new
            surfs[:n] = [surfs[i] for i in order]
            for key in ("surfacesBounds", "surfaceMaterials"):
                lf = tgt(ch.get((key, ())))
                if isinstance(lf, Leaf) and lf.n >= n:
                    size = lf.t.size
                    raw = lf.raw
                    lf.raw = b"".join(raw[i * size:(i + 1) * size] for i in order) + raw[n * size:]
            bits = tgt(ch.get(("surfaceCastsSunShadow", ())))
            if isinstance(bits, Leaf) and bits.raw:
                words = list(struct.unpack(bits.E + "%dI" % (len(bits.raw) // 4), bits.raw))
                out = list(words)
                for w in range(min(len(out), (n + 31) // 32)):
                    out[w] &= ~(0xFFFFFFFF if (w + 1) * 32 <= n else (1 << (n - w * 32)) - 1) & 0xFFFFFFFF
                for old in range(n):
                    if old >> 5 < len(words) and words[old >> 5] >> (old & 31) & 1:
                        new = new_of[old]
                        out[new >> 5] |= 1 << (new & 31)
                bits.raw = struct.pack(bits.E + "%dI" % len(out), *out)

            def renumber(lf, count=None):
                if isinstance(lf, Leaf) and lf.n:
                    k = lf.n if count is None else count
                    v = list(struct.unpack_from(lf.E + "%dH" % k, lf.raw))
                    lf.raw = struct.pack(lf.E + "%dH" % k, *[new_of[x] if x < n else x for x in v]) + lf.raw[2 * k:]
            renumber(tgt(ch.get(("sortedSurfIndex", ()))))
            geoms = tgt(world.get("@", {}).get(("shadowGeom", ())))
            for gm in geoms if isinstance(geoms, list) else []:
                if isinstance(gm, dict) and gm.get("surfaceCount"):
                    renumber(tgt(gm.get("@", {}).get(("sortedSurfIndex", ()))), gm["surfaceCount"])
        a = groups.count(0)
        b = a + groups.count(1)
        c = b + groups.count(2)
        dpvs.update(litOpaqueSurfsBegin=0, litOpaqueSurfsEnd=a, litTransSurfsBegin=a, litTransSurfsEnd=b,
                    shadowCasterSurfsBegin=b, shadowCasterSurfsEnd=c, emissiveSurfsBegin=c, emissiveSurfsEnd=c)
        self.log("  test: surfaces in 360 order: %d solid, %d decals / see-through, %d shadow casters%s"
                 % (a, b - a, c - b, "" if new_of else " (already in order)"))

    def fill_lighting_origins(self, world):
        """GfxStaticModelInst is (box centre, box half-size, lighting origin). The 360 samples
        the light grid at the lighting origin for the model's lighting (TU6 0x823F2958); stock
        maps set it, custom-compiled ones can leave it (0,0,0). Those take the box centre."""
        insts = (world.get("dpvs") or {}).get("@", {}).get(("smodelInsts", ()))
        insts = insts.target if isinstance(insts, Ref) else insts
        if not isinstance(insts, Leaf):
            return
        raw = bytearray(insts.raw)
        filled = 0
        for i in range(len(raw) // 36):
            if struct.unpack_from(insts.E + "3f", raw, 36 * i + 24) == (0.0, 0.0, 0.0):
                raw[36 * i + 24:36 * i + 36] = raw[36 * i:36 * i + 12]
                filled += 1
        if filled:
            insts.raw = bytes(raw)
            self.log("  static model lighting origins set to their box centres: %d of %d"
                     % (filled, len(raw) // 36))

    def grow_model_boxes(self, world):
        """Grow each placed model's box (GfxStaticModelInst.bounds) to enclose the model's
        vertices as placed. The 360 tests a model against it whenever its culling tree leaf is
        partly in view; stock maps' boxes cover every vertex (mp_rust: 3,356 of 3,356), maps
        ported from CoD4 don't always (mp_backlot's hanging fluorescent lights: 11 units low)."""
        def tgt(c):
            return c.target if isinstance(c, Ref) else c
        dpvs = world.get("dpvs") or {}
        ch = dpvs.get("@", {})
        insts = tgt(ch.get(("smodelInsts", ())))
        draws = tgt(ch.get(("smodelDrawInsts", ())))
        if not isinstance(insts, Leaf) or not isinstance(draws, list):
            return
        E = insts.E
        raw = bytearray(insts.raw)
        cache = {}

        def model_points(m):
            if id(m) in cache:
                return cache[id(m)]
            pts = []
            for lod in (m.get("lodInfo") or [])[:m.get("numLods") or 0]:
                ms = deref(lod.get("@", {}).get(("modelSurfs", ()))) if isinstance(lod, dict) else None
                if isinstance(ms, tree.InsertSlot):
                    ms = ms.asset
                c = lod.get("@", {}).get(("modelSurfs", ())) if isinstance(lod, dict) else None
                if isinstance(c, Ref) and isinstance(c.target, tree.InsertSlot):
                    ms = c.target.asset
                surfs = tgt(ms.get("@", {}).get(("surfs", ()))) if isinstance(ms, dict) else None
                for sf in surfs if isinstance(surfs, list) else []:
                    v = tgt(sf.get("@", {}).get(("verts0", ()))) if isinstance(sf, dict) else None
                    if isinstance(v, Leaf) and v.raw:
                        size = getattr(v.t, "size", 32) or 32
                        pts += [struct.unpack_from(v.E + "3f", v.raw, size * i) for i in range(len(v.raw) // size)]
            cache[id(m)] = pts
            return pts

        grown, far = 0, 0.0
        for i, d in enumerate(draws):
            if 36 * (i + 1) > len(raw) or not isinstance(d, dict) or not d.get("packedAxis"):
                continue
            c = d.get("@", {}).get(("model", ()))
            m = deref(c) if isinstance(c, Ref) else c
            pts = model_points(m) if isinstance(m, dict) else []
            if not pts:
                continue
            ax = []
            for v in d["packedAxis"][:3]:
                ax.append([(((v >> (10 * k)) & 0x3FF) - (0x400 if (v >> (10 * k)) & 0x200 else 0)) / 511.0
                           for k in range(3)])
            scale = struct.unpack("<f", struct.pack("<I", d["packedAxis"][3]))[0]
            o = d["origin"]
            lo, hi = [1e30] * 3, [-1e30] * 3
            for p in pts:
                for k in range(3):
                    w = o[k] + scale * (p[0] * ax[0][k] + p[1] * ax[1][k] + p[2] * ax[2][k])
                    lo[k] = min(lo[k], w)
                    hi[k] = max(hi[k], w)
            box = struct.unpack_from(E + "6f", raw, 36 * i)
            blo = [box[k] - box[3 + k] for k in range(3)]
            bhi = [box[k] + box[3 + k] for k in range(3)]
            out = max(max(blo[k] - lo[k], hi[k] - bhi[k]) for k in range(3))
            if out <= 0.5:
                continue
            far = max(far, out)
            lo = [min(lo[k], blo[k]) for k in range(3)]
            hi = [max(hi[k], bhi[k]) for k in range(3)]
            struct.pack_into(E + "6f", raw, 36 * i, *([(lo[k] + hi[k]) / 2 for k in range(3)]
                                                      + [(hi[k] - lo[k]) / 2 for k in range(3)]))
            grown += 1
        insts.raw = bytes(raw)
        if grown:
            self.log("  %d placed model boxes grown to enclose their models (up to %.0f units)" % (grown, far))

    def grow_tree_bounds(self, world):
        """Grow every culling tree node's box (GfxAabbTree.bounds) to enclose the static models
        it lists. Stock 360 maps always have them inside (mp_rust: 28,821 of 28,821 listings);
        maps ported from CoD4 don't (mp_backlot: 471 models, up to 537 units out), and the 360
        skips a node's models when its box is off screen: they vanish as you turn. A bigger box
        only means drawing more, never less."""
        def tgt(c):
            return c.target if isinstance(c, Ref) else c
        dpvs = world.get("dpvs") or {}
        insts = tgt(dpvs.get("@", {}).get(("smodelInsts", ())))
        trees = tgt(world.get("@", {}).get(("aabbTrees", ())))
        if not isinstance(insts, Leaf) or not isinstance(trees, list):
            return
        E = insts.E
        boxes = [struct.unpack_from(E + "6f", insts.raw, 36 * i) for i in range(len(insts.raw) // 36)]

        def get(u):
            h = bytes.fromhex(u["union"])
            return list(struct.unpack(">%df" % (len(h) // 4), h))

        grown = 0
        far = 0.0
        for ct in trees:
            nodes = tgt(ct.get("@", {}).get(("aabbTree", ()))) if isinstance(ct, dict) else None
            for nd in nodes or []:
                cnt = nd.get("smodelIndexCount") or 0
                c = nd.get("@", {}).get(("smodelIndexes", ()))
                off = 0
                if isinstance(c, Ref):
                    off, c = c.rel, c.target
                if not cnt or not isinstance(c, Leaf):
                    continue
                mid, half = get(nd["bounds"]["midPoint"]), get(nd["bounds"]["halfSize"])
                lo = [mid[k] - half[k] for k in range(3)]
                hi = [mid[k] + half[k] for k in range(3)]
                out = False
                for i in struct.unpack_from(c.E + "%dH" % cnt, c.raw, off):
                    if i >= len(boxes):
                        continue
                    b = boxes[i]
                    for k in range(3):
                        if b[k] - b[3 + k] < lo[k] or b[k] + b[3 + k] > hi[k]:
                            far = max(far, lo[k] - (b[k] - b[3 + k]), (b[k] + b[3 + k]) - hi[k])
                            lo[k] = min(lo[k], b[k] - b[3 + k])
                            hi[k] = max(hi[k], b[k] + b[3 + k])
                            out = True
                if out:
                    grown += 1
                    nd["bounds"]["midPoint"]["union"] = struct.pack(
                        ">3f", *[(lo[k] + hi[k]) / 2 for k in range(3)]).hex()
                    nd["bounds"]["halfSize"]["union"] = struct.pack(
                        ">3f", *[(hi[k] - lo[k]) / 2 for k in range(3)]).hex()
        if grown:
            self.log("  culling tree: %d node boxes grown to enclose their static models (up to %.0f "
                     "units)" % (grown, far))

    def grow_room_boxes(self, world):
        """Grow each room's box (GfxCell.bounds) to enclose the static models and surfaces its
        culling tree's root lists. The shadow pass (TU6 0x823D9080) tests room boxes against
        the shadow view and walks only the rooms inside it."""
        def tgt(c):
            return c.target if isinstance(c, Ref) else c
        ch = world.get("@", {})
        dch = (world.get("dpvs") or {}).get("@", {})
        cells, trees = tgt(ch.get(("cells", ()))), tgt(ch.get(("aabbTrees", ())))
        insts, sbounds = tgt(dch.get(("smodelInsts", ()))), tgt(dch.get(("surfacesBounds", ())))
        order = tgt(dch.get(("sortedSurfIndex", ())))
        if not (isinstance(cells, list) and isinstance(trees, list) and isinstance(insts, Leaf)
                and isinstance(sbounds, Leaf) and isinstance(order, Leaf)):
            self.warn("room boxes couldn't be grown (data not found)")
            return
        sorted_idx = struct.unpack(order.E + "%dH" % (len(order.raw) // 2), order.raw)
        ssize = sbounds.t.size

        def get(u):
            h = bytes.fromhex(u["union"])
            return list(struct.unpack(">3f", h[:12]))

        grown, far = 0, 0.0
        for cell, ct in zip(cells, trees):
            nodes = tgt(ct.get("@", {}).get(("aabbTree", ()))) if isinstance(ct, dict) else None
            b = cell.get("bounds") if isinstance(cell, dict) else None
            if not nodes or not isinstance(b, dict) or not all(
                    isinstance(b.get(k), dict) and isinstance(b[k].get("union"), str)
                    for k in ("midPoint", "halfSize")):
                continue
            root = nodes[0]
            boxes = []
            cnt = root.get("smodelIndexCount") or 0
            c = root.get("@", {}).get(("smodelIndexes", ()))
            off = 0
            if isinstance(c, Ref):
                off, c = c.rel, c.target
            if cnt and isinstance(c, Leaf):
                for i in struct.unpack_from(c.E + "%dH" % cnt, c.raw, off):
                    if 36 * (i + 1) <= len(insts.raw):
                        boxes.append(struct.unpack_from(insts.E + "6f", insts.raw, 36 * i))
            start = root.get("startSurfIndex") or 0
            for k in range(root.get("surfaceCount") or 0):
                if start + k < len(sorted_idx):
                    s = sorted_idx[start + k]
                    if ssize * (s + 1) <= len(sbounds.raw):
                        boxes.append(struct.unpack_from(sbounds.E + "6f", sbounds.raw, ssize * s))
            mid, half = get(b["midPoint"]), get(b["halfSize"])
            lo = [mid[k] - half[k] for k in range(3)]
            hi = [mid[k] + half[k] for k in range(3)]
            out = False
            for bx in boxes:
                for k in range(3):
                    if bx[k] - bx[3 + k] < lo[k] or bx[k] + bx[3 + k] > hi[k]:
                        far = max(far, lo[k] - (bx[k] - bx[3 + k]), (bx[k] + bx[3 + k]) - hi[k])
                        lo[k] = min(lo[k], bx[k] - bx[3 + k])
                        hi[k] = max(hi[k], bx[k] + bx[3 + k])
                        out = True
            if out:
                grown += 1
                b["midPoint"]["union"] = struct.pack(">3f", *[(lo[k] + hi[k]) / 2 for k in range(3)]).hex()
                b["halfSize"]["union"] = struct.pack(">3f", *[(hi[k] - lo[k]) / 2 for k in range(3)]).hex()
        self.log("  test: %d of %d room boxes grown to enclose their contents (up to %.0f units)"
                 % (grown, len(cells), far))

    def fill_surface_bounds(self, world):
        """GfxSurfaceBounds has two more words on the 360 (the PC has none of it): the first
        is radius << 16 | colorMap density << 8 | colorMap1 density. Radius: the distance from
        the bounds' middle to the surface's farthest vertex, rounded up (65535 at most). Density:
        26 x picture width / (world units per texture repeat), the second for a blend
        material's second color map, 0 without one. Worked out from stock mp_rust (radius exact
        on 4803 of 5332 surfaces, 1 more than stock on the rest). Left 0, the game
        took every surface for a point at its middle."""
        def leaf(c):
            if isinstance(c, Ref) and c.rel == 0:
                c = c.target
            return c if isinstance(c, Leaf) else None
        draw, dpvs = world.get("draw"), world.get("dpvs")
        if not (isinstance(draw, dict) and isinstance(dpvs, dict)):
            return
        vd = draw.get("vd")
        verts = leaf(vd.get("@", {}).get(("vertices", ()))) if isinstance(vd, dict) else None
        idx = leaf(draw.get("@", {}).get(("indices", ())))
        surfs = dpvs.get("@", {}).get(("surfaces", ()))
        surfs = surfs.target if isinstance(surfs, Ref) else surfs
        bounds = leaf(dpvs.get("@", {}).get(("surfacesBounds", ())))
        if verts is None or idx is None or not isinstance(surfs, list) or bounds is None:
            self.warn("the world's surfaces get no culling radius (data not found)")
            return
        V, ve = verts.raw, verts.E
        I = struct.unpack(idx.E + "%dH" % idx.n, idx.raw)
        size = bounds.t.size
        out = bytearray(bounds.raw)
        widths = {}

        def width(mat, hash_):
            mat = deref(mat) if isinstance(mat, Ref) else mat
            if not isinstance(mat, dict):
                return 0
            key = (id(mat), hash_)
            if key not in widths:
                w = 0
                tt = mat.get("@", {}).get(("textureTable", ()))
                for t in (tt if isinstance(tt, list) else []):
                    if isinstance(t, dict) and t.get("nameHash") == hash_ and isinstance(t.get("u"), dict):
                        img = deref(t["u"].get("@", {}).get(("image", ())))
                        if isinstance(img, dict):
                            w = img.get("width") or 0
                            st = img.get("streams")
                            if isinstance(st, list):
                                w = max([w] + [x.get("width") or 0 for x in st if isinstance(x, dict)])
                            # A picture the game has loaded (",name"): its size isn't here.
                            if w <= 1:
                                w = 512
                widths[key] = w
            return widths[key]

        for k, s in enumerate(surfs[:bounds.n]):
            if not isinstance(s, dict) or not isinstance(s.get("tris"), dict):
                continue
            t = s["tris"]
            mid = struct.unpack_from(bounds.E + "3f", out, k * size)
            base, fv = t["baseIndex"], t["firstVertex"]
            tri = I[base:base + 3 * t["triCount"]]
            r2 = 0.0
            world_area = uv_area = 0.0
            for j in range(0, len(tri) - 2, 3):
                p, uv = [], []
                for v in tri[j:j + 3]:
                    o = (fv + v) * 44
                    x = struct.unpack_from(ve + "3f", V, o)
                    p.append(x)
                    uv.append(struct.unpack_from(ve + "2f", V, o + 20))
                    r2 = max(r2, sum((x[q] - mid[q]) ** 2 for q in range(3)))
                a = [p[1][q] - p[0][q] for q in range(3)]
                b = [p[2][q] - p[0][q] for q in range(3)]
                cr = (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
                world_area += math.sqrt(sum(c * c for c in cr))
                uv_area += abs((uv[1][0] - uv[0][0]) * (uv[2][1] - uv[0][1])
                               - (uv[1][1] - uv[0][1]) * (uv[2][0] - uv[0][0]))
            radius = min(65535, math.ceil(math.sqrt(r2)))
            dens = []
            for h in (COLOR_MAP, COLOR_MAP1):
                w = width(s.get("@", {}).get(("material", ())), h)
                if not w or not world_area or not uv_area or not all(map(math.isfinite, (world_area, uv_area))):
                    dens.append(0)
                    continue
                dens.append(max(1, min(255, round(26 * w / math.sqrt(world_area / uv_area)))))
            struct.pack_into(bounds.E + "I", out, k * size + 24, radius << 16 | dens[0] << 8 | dens[1])
        bounds.raw = bytes(out)

    def post_GfxLightGrid(self, d, tx):
        """rawRowData is plain bytes in the definitions but holds one row per rowDataStart entry
        (at 4 * that, in bytes): colStart, colCount, zStart, zCount (16-bit), firstEntry (32-bit),
        then a byte lookup table. The row headers need the 360's byte order: copied as they were,
        every model lit from the grid read garbage rows. With them swapped, PC mp_rust's row data
        matches stock 360 mp_rust's exactly (21,356 bytes, 1,096 rows)."""
        ch = d.get("@", {})
        starts, raw = ch.get(("rowDataStart", ())), ch.get(("rawRowData", ()))
        starts = starts.target if isinstance(starts, Ref) else starts
        raw = raw.target if isinstance(raw, Ref) else raw
        if not (isinstance(starts, Leaf) and isinstance(raw, Leaf)) or id(raw) in self.swapped_rows:
            return
        self.swapped_rows.add(id(raw))
        rows = struct.unpack(starts.E + "%dH" % starts.n, starts.raw[:2 * starts.n])
        src = raw.raw
        out = bytearray(src)
        for s in sorted(set(rows)):
            o = 4 * s
            if o + 12 <= len(src):
                struct.pack_into(">4HI", out, o, *struct.unpack_from("<4HI", src, o))
        raw.raw = bytes(out)

    def post_FxElemVisualState(self, d, tx):
        """An effect element's color: the PC keeps it as a D3DCOLOR (bytes B, G, R, A), the 360
        as A, R, G, B (stock mp_rust's misc/glow_stick_glow_pile_orange: 80 e8 31 15, where the
        converted file had 15 31 e8 80)."""
        c = d.get("color")
        if isinstance(c, list):
            d["color"] = c[::-1]
        elif isinstance(c, (bytes, bytearray)):
            d["color"] = bytes(c[::-1])
        elif isinstance(c, str) and len(c) == 8:
            d["color"] = bytes.fromhex(c)[::-1].hex()

    def post_FxGlassDef(self, d, tx):
        """Two 360-only numbers per glass type (unknown[2], floats). Stock maps always hold 0.4077
        in the second; the first follows the material (com_glass_clear: always 0.001593,
        glass_clear mostly 0.012741). Converted ones were left 0. Taken from a stock glass type
        with the same material, else com_glass_clear's (the most common)."""
        if any(d.get("unknown") or [0]):
            return
        m = deref(d.get("@", {}).get(("material", ())))
        name = ((_name(m) or b"") if isinstance(m, dict) else b"").lstrip(b",")
        if self.stock_glass is None:
            self.stock_glass = {}
            for _, root in self.x_refs:
                for o in iter_objects(root["assets"]):
                    if isinstance(o, dict) and "halfThickness" in o and "texVecs" in o and any(o.get("unknown") or [0]):
                        sm = deref(o.get("@", {}).get(("material", ())))
                        if isinstance(sm, dict):
                            mn = (_name(sm) or b"").lstrip(b",")
                            self.stock_glass.setdefault(mn, list(o["unknown"]))
                            # The first number also follows the texture scale (mp_terminal's
                            # la_glass_banister01: 0.0127 to 0.0255 by its texVecs).
                            self.stock_glass.setdefault((mn, _glass_key(o)), list(o["unknown"]))
        d["unknown"] = list(self.stock_glass.get((name, _glass_key(d))) or self.stock_glass.get(name)
                            or [986759600, 1053868464])   # 0.001593, 0.4077

    def post_FxGlassSystem(self, d, tx):
        """firstFreePiece is really a 16-bit number (then padding): 0xFFFF, "no free piece",
        reads 0x0000FFFF on the PC and 0xFFFF0000 on the 360."""
        v = d.get("firstFreePiece")
        if isinstance(v, int) and v <= 0xFFFF:
            d["firstFreePiece"] = v << 16

    def post_clipMap_t(self, d, tx):
        """IW4x ZoneBuilder leaves the collision checksum 0 but writes 0xDEADBEEF into the
        world's: stock maps have the same number in both, so use the world's."""
        if not d.get("checksum"):
            d["checksum"] = self.world_checksum

    def post_MapEnts(self, d, tx):
        """Single-player actors (IW4x maps often keep one) have no spawn code in
        multiplayer and name models this file doesn't carry: leave them out."""
        lf = d["@"].get(("entityString", ()))
        if not isinstance(lf, Leaf):
            return
        text = lf.raw.rstrip(b"\0").decode("latin-1")
        kept, dropped = [], 0
        for ent in re.findall(r"\{[^{}]*\}", text):
            m = re.search(r'"classname"\s+"([^"]*)"', ent)
            if m and m.group(1).startswith("actor_"):
                dropped += 1
                continue
            kept.append(ent)
        if not dropped:
            return
        self.warn("left out %d single-player actor entities" % dropped)
        raw = ("\n".join(kept) + "\n").encode("latin-1") + b"\0"
        lf.raw, lf.n = raw, len(raw)
        d["numEntityChars"] = len(raw)

    def leaf_GfxWorldVertex(self, lf, tx):
        return convert_world_vertices(lf)

    def leaf_GfxPackedVertex(self, lf, tx):
        return convert_model_vertices(lf)

    def post_XSurface(self, d, tx):
        # The PC's buffer handle means nothing here; the 360 builds its vertex and index
        # buffers from verts0 / triIndices when the file loads (both zero in stock files).
        d["zoneHandle"] = 0
        d["unknown"] = 0
        d["vertexBuffer"] = [0] * 8
        d["indexBuffer"] = [0] * 8

    def post_XModel(self, d, tx):
        """The 360 keeps a number per surface for texture streaming (himipRadii); the game
        reads it for every model when the file loads. Stock models use about 1240."""
        m = next(m for m in tx.members if m.name == "himipRadii")
        n = d["numsurfs"]
        d["himipRadii"] = "follow" if n else None
        if n:
            d["@"][("himipRadii", ())] = Leaf(m.type, n, struct.pack(">%dH" % n, *[HIMIP_RADIUS] * n), ">")
        # Every one of the 8,241 models in the stock 360 files has lodRampType 0; the PC marks
        # skinned ones (characters, view hands) 1.
        if d.get("lodRampType"):
            d["lodRampType"] = 0
        if self.fixes["bone_bounds"]:
            self.fix_bone_bounds(d)
        if self.fixes["skip_lod0"] and (d.get("numLods") or 0) > 1 and d.get("lodInfo"):
            # The game takes the first detail level whose distance is beyond the camera's.
            d["lodInfo"][0]["dist"] = 0.0
            self.test_counts["skip_lod0"] = self.test_counts.get("skip_lod0", 0) + 1
        # Each detail level's partBits and surfs: stock 360 models always hold 0 and null here
        # (the game fills them in when it loads the model); the PC file has them set.
        for li in (d.get("lodInfo") or []) if self.fixes["model_lods"] else []:
            if not isinstance(li, dict):
                continue
            li["partBits"] = [0] * len(li.get("partBits") or [0] * 6)
            c = li.get("@", {}).get(("surfs", ()))
            if c is None or isinstance(c, Ref):
                li.get("@", {}).pop(("surfs", ()), None)
                li["surfs"] = None

    def fix_bone_bounds(self, d):
        """bone_bounds: a single-bone model's XBoneInfo rebuilt from its vertices when it is
        broken (a negative half-size, a box that misses vertices, or a radius squared that isn't
        the half-diagonal's), as stock 360 models have it."""
        if d.get("numBones") != 1:
            return
        L = d.get("@", {}).get(("boneInfo", ()))
        L = L.target if isinstance(L, Ref) else L
        if not isinstance(L, Leaf) or len(L.raw) < 28:
            return
        pts = []
        for lod in (d.get("lodInfo") or [])[:d.get("numLods") or 0]:
            c = lod.get("@", {}).get(("modelSurfs", ())) if isinstance(lod, dict) else None
            ms = c.target if isinstance(c, Ref) else c
            if isinstance(ms, tree.InsertSlot):
                ms = ms.asset
            surfs = ms.get("@", {}).get(("surfs", ())) if isinstance(ms, dict) else None
            surfs = surfs.target if isinstance(surfs, Ref) else surfs
            for sf in surfs if isinstance(surfs, list) else []:
                v = sf.get("@", {}).get(("verts0", ())) if isinstance(sf, dict) else None
                v = v.target if isinstance(v, Ref) else v
                if isinstance(v, Leaf) and v.raw:
                    size = getattr(v.t, "size", 32) or 32
                    pts += [struct.unpack_from(v.E + "3f", v.raw, size * i) for i in range(len(v.raw) // size)]
        if not pts:
            return
        lo = [min(p[k] for p in pts) for k in range(3)]
        hi = [max(p[k] for p in pts) for k in range(3)]
        mid = [(lo[k] + hi[k]) / 2 for k in range(3)]
        half = [(hi[k] - lo[k]) / 2 for k in range(3)]
        old = struct.unpack_from(L.E + "7f", L.raw, 0)
        oh = old[3:6]
        broken = (any(not math.isfinite(x) for x in old) or any(h < 0 for h in oh)
                  or any(old[k] - oh[k] > lo[k] + 0.5 or old[k] + oh[k] < hi[k] - 0.5 for k in range(3))
                  or abs(sum(h * h for h in oh) - old[6]) > max(1.0, 0.01 * old[6]))
        if not broken:
            return
        L.raw = struct.pack(L.E + "7f", *(mid + half + [sum(h * h for h in half)])) + L.raw[28:]
        self.test_counts["bone_bounds"] = self.test_counts.get("bone_bounds", 0) + 1

    def streams_from_stock(self, name):
        """Whether picture name comes from a stock map's streamed copy (stock_streamed_pictures):
        one a stock map streams, that the map doesn't bring in its own .iwd (the PC game's
        pictures are the same pictures as the 360's; a map's own may not be)."""
        low = name.lower()
        return bool(self.stock_streamed) and low in self.stock_streamed \
            and low.decode("latin-1") not in self.map_pictures

    def stock_copy(self, src):
        """A copy of stock asset src (with what it points at), left as it is (stock_materials,
        stock_pictures)."""
        new = self.copy_in(src)
        for o in iter_objects(new):
            if isinstance(o, dict):
                self.done.add(id(o))
        return new

    def _replace(self, d, new):
        slot = d.get("_slot")
        fwd = d.get("_forward")
        d.clear()
        d.update(new)
        if slot is not None:
            d["_slot"] = slot
        if fwd:
            d["_forward"] = fwd
        self.done.add(id(d))
        return True

    def reference(self, typ, name):
        """A 360 asset that only names an asset the game already has loaded."""
        tx = self.X.infos[typ].ctype
        d = self.zero(tx)
        if typ == "Material":
            d["info"]["name"] = "follow"
            d["info"]["@"] = {("name", ()): Str(b"," + name)}
        else:
            # (Its name member: "name", or "filename" for a few kinds such as SndCurve.)
            m = "name" if any(x.name == "name" for x in tx.members) else "filename"
            d[m] = "follow"
            d["@"] = {(m, ()): Str(b"," + name)}
        d["_asset"] = typ
        return d

    def have_techset(self, cand):
        key = ("MaterialTechniqueSet", cand)
        return cand in self.material_templates and (key in self.resident or key in self.library
                                                    or key in self.named)

    def nearest_techset(self, name):
        """A stock techset to use in place of name, or None. Lit world and model shaders keep
        their kind and blend (wc_l_sm_t0c0s0: lit, alpha tested, color + specular) and drop or
        add inputs (-> wc_l_sm_t0c0); inputs the material lacks are filled in (fill_textures).
        Others: the one sharing the most leading name parts; a wc_/mc_ one sharing only that
        gets the plain lit shader."""
        if name in self.techset_swaps:
            return self.techset_swaps[name]
        if b"_distfalloff" in name and self.fixes["portal_multiply"] and \
                self.have_techset(b"wc_unlit_multiply_lin"):
            # HDR portals (wc_unlit_distfalloff_replace, a plain white picture, in doorways and
            # windows) are next to invisible. The 360 files have no such set. Drawn opaque
            # (wc_unlit_replace_lin) or alpha tested they still wrote depth: invisible walls
            # that hid the models behind them, popping as the view turned. Multiplied by their
            # white picture they change nothing on screen and write no depth.
            self.techset_swaps[name] = b"wc_unlit_multiply_lin"
            return self.techset_swaps[name]
        best = self.invisible_techset(name) if self.is_tools_techset(name) and \
            self.fixes["hide_tool_surfaces"] else None
        if best is not None:
            self.techset_swaps[name] = best
            return best
        best = self.similar_techset(name)
        if best is not None:
            self.techset_swaps[name] = best
            return best
        want = name.split(b"_")
        best, score = None, (0, 0)
        for cand in self.material_templates:
            key = ("MaterialTechniqueSet", cand)
            if cand is None or not (key in self.resident or key in self.library or key in self.named):
                continue
            parts = cand.split(b"_")
            lead = 0
            while lead < min(len(want), len(parts)) and want[lead] == parts[lead]:
                lead += 1
            sc = (lead, len(set(want) & set(parts)) - len(set(parts) - set(want)))
            if lead and sc > score:
                best, score = cand, sc
        if name.startswith((b"wc_", b"mc_")) and score[0] < 2 and self.have_techset(name[:3] + b"l_sm_r0c0"):
            # Only "wc"/"mc" in common (wc_tools -> wc_water draws nothing): plain lit instead.
            best = name[:3] + b"l_sm_r0c0"
        if best is not None:
            self.techset_swaps[name] = best
        return best

    def is_tools_techset(self, name):
        """wc_tools, mc_tools: Radiant's tool shaders, which draw nothing in game (and the HDR
        portal sets when they aren't drawn as multiply)."""
        return name.split(b"_")[1:2] == [b"tools"] or (
            b"_distfalloff" in name and not self.fixes["portal_multiply"])

    def invisible_techset(self, name):
        """A stock alpha tested shader set of the same kind (wc_/mc_) with a color picture and
        as few other inputs as there are: given a see-through picture, it draws nothing (no
        shadow either, which reads the same picture)."""
        best, score = None, None
        for cand in self.material_templates:
            got = self.techset_codes(cand) if cand else None
            if got is None or not got[0].startswith(name[:3]) or got[2] \
                    or not got[1][0].startswith(b"t") or b"c0" not in got[1] \
                    or not self.have_techset(cand):
                continue
            sc = (-len(got[1]), got[0] == name[:3] + b"l_sm_", cand)
            if score is None or sc > score:
                best, score = cand, sc
        return best

    def hides(self, name, near):
        """Whether shader set near, used for tool shader set name, draws nothing."""
        return self.is_tools_techset(name) and near == self.invisible_techset(name)

    @staticmethod
    def techset_codes(name):
        """wc_l_sm_t0c0s0_nocast -> (b"wc_l_sm_", [b"t0", b"c0", b"s0"], b"_nocast"), or None."""
        m = re.match(rb"^((?:wc|mc)_l_(?:sm_)?(?:ua_)?)((?:[a-z]\d)+)(_.*)?$", name)
        if not m:
            return None
        return m.group(1), re.findall(rb"[a-z]\d", m.group(2)), m.group(3) or b""

    def similar_techset(self, name):
        """Same kind, blend and extras as name, with the fewest inputs added or dropped
        (dropping preferred: a picture the material has but the shader doesn't use is harmless)."""
        want = self.techset_codes(name)
        if want is None:
            return None
        best, score = None, None
        for cand in self.material_templates:
            got = self.techset_codes(cand) if cand else None
            if got is None or got[0] != want[0] or got[2] != want[2] or got[1][0] != want[1][0] \
                    or not self.have_techset(cand):
                continue
            extra = len(set(got[1]) - set(want[1]))
            sc = (-extra, len(set(got[1]) & set(want[1])), cand)
            if score is None or sc > score:
                best, score = cand, sc
        return best

    def pre_MaterialTechniqueSet(self, d, tp, tx):
        name = asset_name(d)
        if name.startswith(b","):          # the PC file only names it too
            name = name[1:]
        if ("MaterialTechniqueSet", name) in self.resident or ("MaterialTechniqueSet", name) in self.named:
            return self._replace(d, self.reference("MaterialTechniqueSet", name))
        src = self.library.get(("MaterialTechniqueSet", name))
        if src is None:
            near = self.nearest_techset(name)
            if near is not None and near != name:
                if self.hides(name, near):
                    self.invisible_ts.add(id(d))
                d["@"][("name", ())] = Str(near)
                return self.pre_MaterialTechniqueSet(d, tp, tx)
        if src is None:
            raise PortError("no 360 techset %s in the stock files given" % name.decode())
        return self._replace(d, self.copy_in(src))

    def in_iwd(self, name):
        return name.decode().lower() in self.map_pictures

    def pre_GfxStaticModelDrawInst(self, d, tp, tx):
        """PC: placement {origin, axis[3][3], scale}; 360: origin and packedAxis[4], each axis
        three signed 10-bit numbers (x 511, rounded; x, y, z from the low bits) and the scale as
        a float (worked out from stock maps, whose clip map holds the same models with plain
        axes: exact for 19,530 of 19,533 axes). Left zero, every static model drew nowhere
        while its collision stayed."""
        if self.model_swap:
            c = d.get("@", {}).get(("model", ()))
            m = c if isinstance(c, dict) else deref(c) if isinstance(c, Ref) else None
            entry = self.model_swap.get(id(m)) if isinstance(m, dict) else None
            if entry is not None:
                r = tree.Ref(0)
                r.target, r.rel = entry, 4      # the stock model, through its asset list entry
                d["@"][("model", ())] = r
                d["model"] = "0x00000000"
        pl = d.pop("placement", None)
        if not isinstance(pl, dict):
            return None
        d["origin"] = list(pl["origin"])
        packed = []
        for axis in pl["axis"]:
            v = 0
            for k, c in enumerate(axis):
                v |= (int(math.floor(max(-1.0, min(1.0, c)) * 511 + 0.5)) & 0x3FF) << (10 * k)
            packed.append(v)
        packed.append(struct.unpack("<I", struct.pack("<f", pl["scale"]))[0])
        d["packedAxis"] = packed
        # Ground-lit models (grass): the PC marks them 0x20, stock 360 rust marks the same 299
        # models 0x02, and the 360 holds their ground color with its bytes the other way round.
        # Placed-model flags (from PC mp_rust, mp_terminal and mp_afghan against stock 360 ones,
        # 15,000 models): the PC's 0x20 (ground lighting) is the 360's 0x02, the PC's 0x10 the
        # 360's 0x01, and the PC's low bits (0x01, 0x02, 0x04: afghan's poppies, pines and
        # boulders) have no 360 counterpart (stock holds 0 for them). The ground color is kept
        # with its bytes the other way round on the 360.
        f = d.get("flags") or 0
        d["flags"] = (0x02 if f & 0x20 else 0) | (0x01 if f & 0x10 else 0)
        gl = d.get("groundLighting")
        if (self.fixes["ground_lit_flag"] and not d["flags"] & 0x02 and isinstance(gl, dict)
                and isinstance(gl.get("union"), str) and int(gl["union"] or "0", 16)):
            d["flags"] |= 0x02
            self.test_counts["ground_lit"] = self.test_counts.get("ground_lit", 0) + 1
        if isinstance(gl, dict) and isinstance(gl.get("union"), str):
            gl["union"] = bytes.fromhex(gl["union"])[::-1].hex()
        if self.fixes["hide_foliage"] or self.fixes["draw_distance_cap"]:
            m = deref(d.get("@", {}).get(("model", ())))
            name = (_name(m) or b"").lstrip(b",") if isinstance(m, dict) else b""
            if self.fixes["hide_foliage"] and name.startswith(b"foliage"):
                d["cullDist"] = 1
                self.test_counts["foliage"] = self.test_counts.get("foliage", 0) + 1
            elif self.fixes["draw_distance_cap"] and not 0 < d.get("cullDist", 0) <= DRAW_DISTANCE_CAP:
                d["cullDist"] = DRAW_DISTANCE_CAP
                self.test_counts["capped"] = self.test_counts.get("capped", 0) + 1
        if self.fixes["no_cull_distance"] and d.get("cullDist"):
            d["cullDist"] = 0
            self.test_counts["no_cull"] = self.test_counts.get("no_cull", 0) + 1
        return None

    def pre_water_t(self, d, tp, tx):
        """The PC keeps a water surface's starting waves as one array of complex numbers (H0);
        the 360 keeps the real and imaginary parts as two arrays (H0X, H0Y). The game reads
        them every frame: left empty, the console turned off as backlot's water came into view."""
        ch = d.setdefault("@", {})
        h0 = ch.pop(("H0", ()), None)
        if isinstance(h0, Ref):
            h0 = h0.target
        d.pop("H0", None)
        d["H0X"] = d["H0Y"] = None
        if isinstance(h0, Leaf):
            n = h0.n
            vals = struct.unpack("<%df" % (2 * n), h0.raw[:8 * n])
            ft = self.pc.type_by_name("float")
            for k, part in (("H0X", vals[0::2]), ("H0Y", vals[1::2])):
                ch[(k, ())] = Leaf(ft, n, struct.pack("<%df" % n, *part), "<")
                d[k] = "follow"
        return None

    def water_image(self, d, name):
        """A copy of a stock water picture (the game draws the waves into it at run time; the
        PC file has no pixels for it) under name, or None."""
        best = None
        for (typ, n), v in self.library.items():
            if typ != "GfxImage" or v.get("category") != 5 or v.get("pixels") != "follow":
                continue
            if best is None or (v.get("width"), v.get("height")) == (d.get("width"), d.get("height")):
                best = v
        if best is None:
            return None
        new = self.copy_in(best)
        new["@"] = dict(new["@"])
        new["@"][("name", ())] = Str(name)
        lf = new["@"].get(("pixels", ()))
        if isinstance(lf, Leaf):
            new["@"][("pixels", ())] = Leaf(lf.t, lf.n, lf.raw, lf.E)
        return new

    def pre_GfxImage(self, d, tp, tx):
        name = asset_name(d) or _name(d) or b""     # (_name: a name shared with another asset)
        if d.get("category") == 5 and not name.startswith(b","):
            # Water (IMG_CATEGORY_WATER): a stand-in picture of another size would have the
            # game write the waves past its end.
            new = self.water_image(d, name)
            if new is None:
                raise PortError("water picture %s: no stock 360 water picture in the stock files "
                                "given to copy (mp_favela has some)" % name.decode())
            return self._replace(d, new)
        if name.startswith(b","):
            # The PC file only names it (the PC game has it loaded already). Name it on the 360
            # too when the 360 has it loaded; else it becomes a picture of the map's own.
            name = name[1:]
            if ("GfxImage", name) in self.resident or ("GfxImage", name) in self.named:
                return self._replace(d, self.reference("GfxImage", name))
            d.setdefault("@", {})[("name", ())] = Str(name)
        # Built-in pictures ($identitynormalmap, ...) stay the game's own even when an IW4x
        # .iwd carries a copy: a second asset with the name would replace the resident one.
        if ("GfxImage", name) in self.resident and (name.startswith(b"$") or not self.in_iwd(name)):
            return self._replace(d, self.reference("GfxImage", name))
        if self.streams_from_stock(name):
            self.stock_copied["streamed pictures"] += 1
            return self._replace(d, self.stock_copy(copy.deepcopy(self.stock_streamed[name.lower()])))
        if self.fixes["stock_pictures"] and ("GfxImage", name) in self.library:
            self.stock_copied["pictures"] += 1
            return self._replace(d, self.stock_copy(self.library[("GfxImage", name)]))
        if self.fixes["plain_pictures"] and d.get("mapType") == 3 and \
                not name.startswith((b"*", b"$", b"loadscreen")):
            self.plain_picture(d, name)
            self.done.add(id(d))
            return True
        tex = d.get("texture", {})
        ld = tex.get("@", {}).get(("loadDef", ())) if isinstance(tex, dict) else None
        if isinstance(ld, dict) and ld.get("resourceSize"):
            self.image_from_loaddef(d, ld)
        elif not self.picture_from_files(d, name):
            # Not in the map's .iwd (none given) nor the PC game's files given, or no copy
            # there can be read: a plain built-in picture stands in so the map still loads.
            stand = self.stand_in(d, name)
            self.missing_images.append((name.decode(), stand))
            if stand is None:
                self.magenta(d)
            else:
                return self._replace(d, self.reference("GfxImage", stand))
        self.done.add(id(d))
        return True

    def picture_from_files(self, d, name):
        """Fill image d from the map's .iwd, else the PC game's; False when neither has a
        copy that can be read (an unreadable one is noted, not fatal)."""
        low = name.decode().lower()
        for where, found in (("map", self.map_pictures), ("game", self.game_pictures)):
            if low not in found:
                continue
            zf, path = found[low]
            try:
                self.image_from_iwd(d, name, zf, path)
            except (PortError, struct.error, ValueError) as e:
                self.unreadable.append("%s (%s: %s)" % (name.decode(), os.path.basename(zf.filename or "?"), e))
                continue
            if where == "game":
                self.from_game += 1
            return True
        return False

    # PC technique slots that draw the material: from 4 (vertex lit, then the lit ones) up to the
    # wireframe and debug views at 44 (0-3: depth and shadow map passes, which a material such
    # as wc/shadowcaster draws two-sided while drawing itself one-sided).
    PC_DRAW_TECHNIQUES = range(4, 44)

    def pc_cull_of(self, d):
        """The face culling a PC material draws itself with (its most common one over its
        drawing passes), or None."""
        entry = d.get("stateBitsEntry")
        entry = bytes.fromhex(entry) if isinstance(entry, str) else bytes(entry or b"")
        words = state_words(d.get("@", {}).get(("stateBitsTable", ())))
        if not words:
            return None
        votes = Counter((words[e][0] >> 14) & 3 for t, e in enumerate(entry)
                        if t in self.PC_DRAW_TECHNIQUES and e < len(words))
        return votes.most_common(1)[0][0] if votes else None

    def pre_Material(self, d, tp, tx):
        name = asset_name(d) or _name(d) or b""     # (_name: a name shared with another asset)
        if name and name.startswith(b","):
            # The PC file only names it (the game has it loaded): so does the 360 file (stock
            # 360 mp_rust names ,mc/lambert1 the same way).
            return self._replace(d, self.reference("Material", name[1:]))
        tt = d.get("@", {}).get(("textureTable", ()))
        images = [deref(t.get("@", {}).get(("image", ()))) for t in (tt if isinstance(tt, list) else [])
                  if isinstance(t, dict) and isinstance(t.get("u"), dict)]
        images += [deref(t["u"].get("@", {}).get(("image", ()))) for t in (tt if isinstance(tt, list) else [])
                   if isinstance(t, dict) and isinstance(t.get("u"), dict)]
        own = any(isinstance(i, dict) and self.in_iwd(asset_name(i) or b"") for i in images)
        if ("Material", name) in self.resident and not own:
            # The game has this material loaded already and the map brings no pictures of its
            # own for it (a map's $levelbriefing does, so it gets its own copy).
            return self._replace(d, self.reference("Material", name))
        if self.fixes["stock_materials"] and ("Material", name) in self.library:
            self.stock_copied["materials"] += 1
            return self._replace(d, self.stock_copy(self.library[("Material", name)]))
        ts = deref(d.get("@", {}).get(("techniqueSet", ())))
        tsname = asset_name(ts) if isinstance(ts, dict) else None
        if tsname and tsname.startswith(b","):     # already converted to a reference
            tsname = tsname[1:]
        orig_ts = tsname
        tpl = self.material_templates.get(tsname)
        # A stock material of the same name and shader set: its own render state (culling,
        # draw order), not that of another material that happens to share the shader set.
        same = self.library.get(("Material", name)) or self.common_materials.get(("Material", name))
        if isinstance(same, dict) and tsname and self.fixes["stock_material_state"]:
            sts = deref(same.get("@", {}).get(("techniqueSet", ())))
            if isinstance(sts, dict) and (asset_name(sts) or b"").lstrip(b",") == tsname:
                tpl = same
        if tpl is None and tsname:
            # No stock file given has this shader set: use the closest one that is there.
            near = self.nearest_techset(tsname)
            if near is not None:
                if self.hides(tsname, near):
                    self.invisible_ts.add(id(ts))
                ts["@"][("name", ())] = Str(near)
                tsname, tpl = near, self.material_templates[near]
        if isinstance(ts, dict) and id(ts) in self.invisible_ts:
            d["_invisible"] = True
        if tpl is None:
            raise PortError("material %s: no stock 360 material uses techset %s to copy render "
                            "settings from" % (name.decode(), tsname))
        # PC render state (D3D9) means nothing to the 360: it comes from the template instead,
        # all but its face culling (the same bits on both): a two-sided PC material stays so.
        if self.fixes["pc_face_culling"]:
            cull = self.pc_cull_of(d)
            if cull in (1, 3):
                self.pc_cull[id(d)] = cull
        d["@"].pop(("stateBitsTable", ()), None)
        d["_template"] = tpl
        # The PC's own draw order (sort key) means the same on the 360: all 271 materials PC
        # mp_rust shares with stock 360 mp_rust have the same one. Kept unless the shader set
        # was swapped for one that hides it (tool surfaces, HDR portals).
        if self.stock_sort_keys is None:
            self.stock_sort_keys = set(
                (v.get("info") or {}).get("sortKey") for (t, _), v in
                list(self.library.items()) + list(self.resident.items()) + list(self.common_materials.items())
                if t == "Material" and isinstance(v, dict))
        sort = (d.get("info") or {}).get("sortKey")
        # (A sort key no stock material uses, PC mp_raid's 35, 49, 52, 54: the template's.)
        if self.fixes["pc_sort_keys"] and not d.get("_invisible") and not (b"_distfalloff" in (orig_ts or b"")) \
                and sort in self.stock_sort_keys:
            self.pc_sort[id(d)] = sort
        return None

    def post_Material(self, d, tx):
        tpl = d.pop("_template")
        for k in ("stateBitsEntry", "stateBitsCount", "stateFlags", "cameraRegion", "stateBitsTable", "unknown"):
            d[k] = tpl[k]
        sort = self.pc_sort.pop(id(d), None)
        d["info"]["sortKey"] = sort if isinstance(sort, int) else tpl["info"]["sortKey"]
        # The game builds drawSurf itself when it registers the material; stock 360 files
        # always hold zero here (the PC keeps its own bit layout).
        d["info"]["drawSurf"] = tpl["info"]["drawSurf"]
        c = tpl.get("@", {}).get(("stateBitsTable", ()))
        if isinstance(c, Ref):
            c = c.target if c.rel == 0 else None
        if c is None:
            raise PortError("template material's render state can't be copied")
        cull = self.pc_cull.pop(id(d), None)
        words = state_words(c) or []
        entry = d.get("stateBitsEntry")
        entry = bytes.fromhex(entry) if isinstance(entry, str) else bytes(entry or b"")
        main = Counter((words[e][0] >> 14) & 3 for e in entry if e < len(words)).most_common(1)
        # (Only where the template draws mostly otherwise: a same-named stock material that
        # already draws two-sided keeps its own state, as PC mp_rust's 271 do.)
        if cull is not None and main and main[0][0] != cull:
            c = with_cull(c, cull)
            self.two_sided += 1
        d["@"][("stateBitsTable", ())] = c
        d["stateBitsTable"] = "follow"
        invisible = d.pop("_invisible", False)
        if invisible:
            # Its own pictures go: every one the shader set reads is a stand-in, the color one
            # see-through.
            d["@"][("textureTable", ())] = []
            d["textureTable"] = "follow"
            d["textureCount"] = 0
            iname = (asset_name(d) or b"").decode()
            if iname not in self.invisible_materials:
                self.invisible_materials.append(iname)
        self.fill_textures(d, tpl, asset_name(d) or b"", invisible)

    def fill_textures(self, d, tpl, name, invisible=False):
        """Give material d every picture its shader set reads (as stock material tpl, which uses
        the same set, has them) that it lacks: a swapped-in shader set can want one the PC
        material never had, and a shader without its picture draws nothing. Stand-ins: flat
        normal map, no shine, magenta color."""
        table = d.get("@", {}).get(("textureTable", ()))
        table = table if isinstance(table, list) else []
        have = set(e.get("nameHash") for e in table if isinstance(e, dict))
        added = []
        for e in tpl.get("@", {}).get(("textureTable", ()), []):
            if not isinstance(e, dict) or e.get("nameHash") in have:
                continue
            img = deref(e["u"].get("@", {}).get(("image", ())))
            iname = (asset_name(img) or b"").lstrip(b",") if isinstance(img, dict) else b""
            sem = e.get("semantic")
            if sem == 5:
                img = self.reference("GfxImage", b"$identitynormalmap")
            elif sem == 8:
                img = self.reference("GfxImage", b"$black")
            elif invisible:
                img = self.see_through(self.zero(self.X.infos["GfxImage"].ctype),
                                       b"~invisible_%d" % len(self.invisible_materials))
                img["_asset"] = "GfxImage"
            elif iname.startswith(b"$") and (("GfxImage", iname) in self.resident or
                                               ("GfxImage", iname) in self.named):
                img = self.reference("GfxImage", iname)
            else:
                img = self.magenta(self.zero(self.X.infos["GfxImage"].ctype),
                                   b"~magenta_%d" % len(self.magenta_materials))
                img["_asset"] = "GfxImage"
                if name.decode() not in self.magenta_materials:
                    self.magenta_materials.append(name.decode())
            new = {k: (dict(v) if isinstance(v, dict) else v) for k, v in e.items() if k != "u"}
            new["u"] = {"union": "ffffffff", "@": {("image", ()): img}}
            added.append(new)
        if not added:
            return
        table = sorted(table + added, key=lambda e: e["nameHash"])
        d["@"][("textureTable", ())] = table
        d["textureTable"] = "follow"
        d["textureCount"] = len(table)

    # ------------------------------------------------------------ images

    @staticmethod
    def stand_in(d, name):
        """The built-in picture used for one the map doesn't bring: flat for normal maps,
        black (no shine) for specular maps; None for the rest, which get a magenta one
        (magenta()) so what's missing is easy to see."""
        semantic = d.get("semantic")
        if semantic == 5 or name.endswith((b"_nml", b"_n")):
            return b"$identitynormalmap"
        if semantic == 8 or name.endswith((b"_spc", b"_s")):
            return b"$black"
        return None

    def plain_picture(self, d, name):
        """Test switch plain_pictures: image dict d becomes a plain 16x16 picture of its kind
        (white color, flat normal map, black specular) instead of the map's own."""
        kind = self.stand_in(d, name)
        color = {b"$identitynormalmap": 0x841F, b"$black": 0x0000}.get(kind, 0xFFFF)  # RGB565
        block = struct.pack("<HHI", color, color, 0)        # DXT1, one color
        self.images.build(d, "DXT1", 16, 16, [block * 16])
        d["category"] = 3
        self.test_counts["plain"] = self.test_counts.get("plain", 0) + 1

    def magenta(self, d, name=None):
        """Make image dict d a small plain magenta picture (placeholder)."""
        if name is not None:
            d.setdefault("@", {})[("name", ())] = Str(name)
        block = struct.pack("<HHI", 0xF81F, 0xF81F, 0)      # DXT1, both colors magenta
        self.images.build(d, "DXT1", 16, 16, [block * 16])
        d["category"] = 3
        self.done.add(id(d))
        return d

    def see_through(self, d, name):
        """Make image dict d a small fully see-through picture (alpha tested away)."""
        d.setdefault("@", {})[("name", ())] = Str(name)
        block = struct.pack("<HHI", 0, 0, 0xFFFFFFFF)       # DXT1, every texel transparent
        self.images.build(d, "DXT1", 16, 16, [block * 16])
        d["category"] = 3
        self.done.add(id(d))
        return d

    def picture_step(self, fn, *args):
        """fn(*args), a slow picture step, through the picture cache when there is one."""
        return self.picture_cache.get(fn, *args) if self.picture_cache else fn(*args)

    def fit_picture(self, fmt, w, h, mips, limit):
        if picture_size(w, h, limit) == (w, h):
            return w, h, mips
        return self.picture_step(fit_picture, fmt, w, h, mips, limit)

    def image_from_iwd(self, d, name, iwd, path):
        try:
            data = iwd.read(path)
        except KeyError:
            raise PortError("image %s isn't in the .iwd" % name.decode())
        fmt, w, h, mips, cube = read_iwi(data)
        limit = 1024 if name.startswith(b"loadscreen") else 2048
        if self.pak is not None and not cube and fmt in STREAMABLE and not name.startswith(b"loadscreen"):
            w, h, mips = self.fit_picture(fmt, w, h, mips, limit)
            if fmt == "DXT5" and (d.get("semantic") == 5 or name.endswith(b"_nml")) \
                    and self.fixes["normal_maps_dxn"]:
                mips = self.picture_step(normal_maps_to_dxn, mips)
                fmt = "DXN"
                self.dxn_count = getattr(self, "dxn_count", 0) + 1
            d["category"] = 3
            if self.images.build_streamed(d, fmt, w, h, mips, self.pak):
                self.streamed += 1
                self.done.add(id(d))
                return d
        if not cube:
            w, h, mips = self.fit_picture(fmt, w, h, mips, limit)
            k = self.mip_drop.get(name, 0)
            if k and len(mips) > k:
                mips, w, h = mips[k:], max(1, w >> k), max(1, h >> k)
        if name.startswith(b"loadscreen"):
            # Stock loading screens have no mipmaps. With them (1024x1024 DXT1, 704 KB) the
            # console stopped loading mp_waw_castle with "MT_GetSize: max allocation exceeded
            # ... for script usage"; the top level alone (512 KB) loads.
            mips = mips[:1]
        # (semantic 5, or named _nml: mp_afghan's wavy_nml, semantic 3, is DXN in stock too)
        if fmt == "DXT5" and not cube and (d.get("semantic") == 5 or name.endswith(b"_nml")) \
                and self.fixes["normal_maps_dxn"]:
            mips = self.picture_step(normal_maps_to_dxn, mips)
            fmt = "DXN"
            self.dxn_count = getattr(self, "dxn_count", 0) + 1
        self.images.build(d, fmt, w, h, mips, cube)
        # Pictures from files are "load from file" on the 360 (the PC leaves them unknown);
        # stock loading screens are plain 2D pictures.
        d["category"] = 3
        if name.startswith(b"loadscreen"):
            d["semantic"] = 0

    def image_from_loaddef(self, d, ld):
        """Images the PC keeps in the fastfile: lightmaps, reflection probes, $outdoor."""
        name = asset_name(d)
        fmt = ld["format"]
        data = ld["data"].raw if isinstance(ld.get("data"), Leaf) else b""
        w, h = d["width"], d["height"]
        cube = bool(ld["flags"] & 4) or d.get("mapType") == 5
        if fmt == 21:        # A8R8G8B8: same byte order as a DDS, which is what mw2tex tiles
            bpp, out_fmt = 4, "ARGB8"
        elif fmt == 50:      # L8
            bpp = 1
            # Lightmaps: the 360 stores the primary lightmap as DXT3A (4-bit single channel,
            # like the stock maps); other single-channel images as plain 8-bit.
            out_fmt = "DXT3A" if name.startswith(b"*lightmap") else "L8"
        else:
            raise PortError("image %s: PC format %d isn't supported yet" % (name.decode(), fmt))
        faces = 6 if cube else 1
        face = len(data) // faces
        top = w * h * bpp
        mips = [data[i * face:i * face + top] for i in range(faces)]
        if cube and out_fmt == "ARGB8":
            # Reflection probes: keep every face's mips. The smaller mips are blurrier copies
            # the game uses for less glossy surfaces; with the top mip alone every gun and
            # shiny surface reflects the sharp picture (too shiny).
            chains = []
            for i in range(faces):
                chain, off, mw, mh = [], i * face, w, h
                while off + mw * mh * bpp <= (i + 1) * face:
                    chain.append(data[off:off + mw * mh * bpp])
                    off += mw * mh * bpp
                    if mw == 1 and mh == 1:
                        break
                    mw, mh = max(1, mw // 2), max(1, mh // 2)
                chains.append(chain)
            if name.startswith(b"*reflection_probe"):
                chains = [[decode_probe(m) for m in c] for c in chains]
            mips = chains
        if out_fmt == "DXT3A":
            mips = [encode_dxt3a(m, w, h) for m in mips]
        tpl = None
        if name.startswith(b"*lightmap"):
            tpl = self.lightmap_templates.get(name.rsplit(b"_", 1)[-1])
        self.images.build(d, out_fmt, w, h, mips if cube else mips[:1], cube, tpl)
        if name.startswith(b"*reflection_probe"):
            self.probe_alpha(d)

    def probe_alpha(self, d):
        """Stock 360 probes carry the same fixed pattern in alpha in every probe of a map;
        give ours a stock one (same size, so the same place in the tiled pixels)."""
        px = d["@"][("pixels", ())]
        for n, v in self.library.items():
            if n[0] == "GfxImage" and n[1].startswith(b"*reflection_probe") and isinstance(
                    v.get("@", {}).get(("pixels", ())), Leaf) and v["@"][("pixels", ())].n == px.n \
                    and v.get("width") == d["width"]:
                raw = bytearray(px.raw)
                raw[0::4] = v["@"][("pixels", ())].raw[0::4]
                px.raw = bytes(raw)
                return

    # ------------------------------------------------------------ copying 360 assets in

    def copy_in(self, src):
        """A copy of a stock 360 asset subtree, safe to write into another file: pointers back
        to things outside the subtree are replaced by the things themselves."""
        self.localize(src)
        new = dict(src)
        new.pop("_slot", None)
        return new

    def localize(self, src):
        """Make src (an asset, or a list of asset list entries) self-contained, in place."""
        inside = set(id(o) for o in iter_objects(src))
        # The slot a temp-block asset reserves counts as part of it (later assets point there).
        inside |= set(id(o["_slot"]) for o in iter_objects(src) if isinstance(o, dict) and "_slot" in o)
        memo = {}

        def cp(o):
            if isinstance(o, Ref):
                if o.target is not None and id(o.target) in inside:
                    # A pointer into a pointer array of an element (FxElemMarkVisuals.materials[1]:
                    # "the same as that one") is replaced by what it points at even inside: the
                    # writer would take it for the element itself.
                    v = _ptr_array_value(o)
                    if v is not None:
                        return v
                    # Likewise an asset pointer that means "the asset that member holds"
                    # (MaterialTextureDef.u.image of another material: mp_afghan's effect
                    # models): this file's order can put that member later than the pointer.
                    a = _asset_in_slot(o)
                    if isinstance(a, dict) and "_slot" not in a:
                        return a
                    return o
                asset = _asset_in_slot(o)
                if asset is not None:
                    # An asset pointer that points at another struct's pointer to the asset
                    # (how zones refer to an asset loaded earlier): use the asset itself.
                    if isinstance(asset, Ref):
                        return cp(asset)
                    if id(asset) not in inside:
                        for y in iter_objects(asset):
                            inside.add(id(y))
                    return asset
                if isinstance(o.target, list) and isinstance(o.t, Compound) and o.rel % o.t.size == 0:
                    # Points at a later element of another array (stock files share the tail of
                    # one argument list with another): give it its own copy of that tail.
                    tail = tree.Tail(o.target[o.rel // o.t.size:])
                    for x in tail:
                        for y in iter_objects(x):
                            inside.add(id(y))
                    inside.add(id(tail))
                    return tail
                if isinstance(o.target, list) and isinstance(o.t, Compound) and o.rel % o.t.size:
                    # Points at a pointer field of another array's element (stock sound aliases
                    # share a sound file that way): the thing that field points at.
                    el = o.target[o.rel // o.t.size]
                    off = o.rel % o.t.size
                    # (or one pointer of an array of them: FxElemMarkVisuals.materials[2], which
                    # stock effects share the second of)
                    m = next((m for m in o.t.members if m.mods and m.mods[-1] == "*"
                              and m.offset <= off < m.offset + m.size and (off - m.offset) % 4 == 0), None)
                    key = None
                    if m is not None:
                        dims = [d for d in m.mods[:-1] if isinstance(d, int)]
                        flat, idx = (off - m.offset) // 4, []
                        for dim in reversed(dims):
                            idx.insert(0, flat % dim)
                            flat //= dim
                        key = (m.name, tuple(idx))
                    if key is not None and isinstance(el, dict):
                        c = el.get("@", {}).get(key)
                        if c is None and key[1]:
                            # (arrays of pointers are kept as one list under the member's name)
                            lst = el.get("@", {}).get((m.name, ()))
                            flat = (off - m.offset) // 4
                            c = lst[flat] if isinstance(lst, list) and flat < len(lst) else None
                        if isinstance(c, Ref):
                            return cp(c)
                        if isinstance(c, dict):
                            for x in iter_objects(c):
                                inside.add(id(x))
                            return c
                if o.target is not None and o.rel == 0:
                    inside.add(id(o.target))
                    for x in iter_objects(o.target):
                        inside.add(id(x))
                    return o.target
                raise PortError("stock asset points outside itself (%r -> %s of %s rel %d)" % (o, type(o.target).__name__, getattr(o.t, "name", o.t), o.rel))
            return o

        def marker(n):
            return "insert" if isinstance(n, dict) and "_slot" in n else "follow"

        def fix(o):
            if not isinstance(o, dict):
                return
            ch = o.get("@")
            if not ch:
                return
            for (name, idx), c in list(ch.items()):
                if isinstance(c, Ref):
                    n = cp(c)
                    if n is not c:
                        ch[(name, idx)] = n
                        if idx == () and not isinstance(o.get(name), list):
                            o[name] = marker(n)
                        elif idx:
                            o[name][idx[0]] = marker(n)
                elif isinstance(c, PtrList):
                    for i, x in enumerate(c):
                        if isinstance(x, Ref):
                            n = cp(x)
                            if n is not x:
                                c[i] = n
                                if c.raw is not None:
                                    c.raw[i] = marker(n)
                                if isinstance(o.get(name), list):
                                    o[name][i] = marker(n)

        seen = set()
        stack = [src]
        while stack:
            o = stack.pop()
            if id(o) in seen:
                continue
            seen.add(id(o))
            fix(o)
            if isinstance(o, dict):
                for k, v in o.items():
                    if k == "@":
                        stack.extend(x for x in v.values() if isinstance(x, (dict, list)))
                    elif isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(o, list):
                stack.extend(x for x in o if isinstance(x, (dict, list)))

    # ------------------------------------------------------------ teams

    def add_teams(self, x_refs, teams):
        """Copy what the two teams need (soldier bodies, heads and arms, flag and crate models,
        team icons) from stock 360 maps that have them, and make the map's script use those
        teams. IW4x loads team assets from its own files; on the 360 each map carries them.
        teams: (allies, axis) as wanted (the map's .arena); a team no stock file given has is
        swapped for one that is there. Returns the teams used."""
        arena = None
        for n, r in x_refs:
            for e in r["assets"]:
                if e[0] == "rawfile" and _name(e[1]) == b"mp/basemaps.arena":
                    arena = arena or _rawfile_text(e[1])
        donors = []         # (file name, root, team)
        for n, r in x_refs:
            have = set((e[0], _name(e[1])) for e in r["assets"] if isinstance(e[1], dict))
            for team, need in TEAM_ASSETS.items():
                if all(("xmodel", m.encode()) in have for m in need["models"]) and \
                        all(("material", m.encode()) in have for m in need["materials"]):
                    donors.append((n, r, team))
        used = []
        for side, want in zip(("allies", "axis"), teams):
            want = (want or "").lower()
            pick = next((d for d in donors if d[2] == want), None)
            if pick is None:
                pick = next((d for d in donors if d[2] in SIDE_TEAMS[side] and d[2] not in
                             [u[2] for u in used]), None) or next(
                    (d for d in donors if d[2] not in [u[2] for u in used]), None)
                if pick is None:
                    raise PortError("no stock map given (--ref360) has the %s team" % want)
                self.warn("the map wants the %s team for %s; using %s instead (for %s, add one of "
                          "these stock 360 maps: %s)" % (want or "(none)", side, pick[2], want,
                                                         ", ".join(_team_maps(arena, want)) or "?"))
            used.append(pick)
        self.pick_card_pictures(x_refs)
        for n, r, team in used:
            self._pick_team(r, team)
        for n, r, team in used:
            self._copy_picked(r)
        for n, r in x_refs:         # titles and emblems from a map no team came from
            self._copy_picked(r)
        # The 360 only knows the teams of its own maps (mp/basemaps.arena); set them in the
        # map's script so the game uses the assets copied in.
        gsc = b"maps/mp/%s.gsc" % self.map_name
        for e in self.root["assets"]:
            if e[0] == "rawfile" and _name(e[1]) == gsc:
                text = _rawfile_text(e[1]).decode("latin-1")
                line = '\tgame[ "allies" ] = "%s";\n\tgame[ "axis" ] = "%s";\n' % (used[0][2], used[1][2])
                text, n = re.subn(r"(main\s*\(\s*\)\s*\{[^\n]*\n)", lambda m: m.group(1) + line, text, 1)
                if n:
                    _set_rawfile_text(e[1], text.encode("latin-1"))
                    break
        else:
            self.warn("couldn't set the teams in %s" % gsc.decode())
        self.log("  teams: %s (from %s) vs %s (from %s)" % (
            used[0][2], os.path.basename(used[0][0]), used[1][2], os.path.basename(used[1][0])))
        return used[0][2], used[1][2]

    def _pick_team(self, src, team):
        need = TEAM_ASSETS[team]
        names = set(("xmodel", m.encode()) for m in need["models"]) | \
            set(("material", m.encode()) for m in need["materials"])
        self._pick(src, names)

    def pick_card_pictures(self, x_refs):
        """Pick the calling card titles and emblems (cardtitle_*, cardicon_* materials) a stock
        map carries: in a match the game draws them from the map's own copy, not ui_mp's. They
        come as stock maps have them, their pixels streamed from the console's imagefile*.pak, so
        they cost the map next to no memory, and mw2tex's Build updates them in converted maps
        as it does in stock ones (custom titles and emblems, without converting again)."""
        have = set((e[0], _name(e[1])) for e in self.root["assets"] if isinstance(e[1], dict))
        for n, r in x_refs:
            base = os.path.splitext(os.path.basename(n))[0].lower()
            if not base.startswith("mp_") or base.endswith("_load"):
                continue
            names = set(("material", _name(e[1])) for e in r["assets"]
                        if e[0] == "material" and isinstance(e[1], dict)
                        and (_name(e[1]) or b"").startswith((b"cardtitle_", b"cardicon_"))) - have
            if names:
                self._pick(r, names)
                self.log("  titles and emblems: %d from %s" % (len(names), os.path.basename(n)))
                return
        self.warn("no stock map given carries the calling card titles and emblems; they show as "
                  "missing in matches")

    def cards_to_pak(self):
        """Point the copied titles and emblems at their fixed slots in imagefile8.pak (mw2tex's
        cardslots.json), so changing titles and emblems later only means writing that pak anew
        (mw2tex Build does), never this map. Returns how many pictures use it."""
        import mw2tex
        by = {(s["name"], s["level"]): s for s in mw2tex.load_card_slots()}
        count = 0
        for o in iter_objects(self.root["assets"]):
            if not (isinstance(o, dict) and o.get("_asset") == "GfxImage" and "_pak" in o):
                continue
            name = (asset_name(o) or b"").decode("latin-1").lower()
            if not mw2tex.is_card(name):
                continue
            hit = False
            o["_pak"] = list(o["_pak"])     # its own list: the stock tree's stays as it was
            for k, st in enumerate(o.get("streams") or []):
                s = by.get((name, k))
                if s and st["width"] and (s["width"], s["height"]) == (st["width"], st["height"]):
                    o["_pak"][k] = (mw2tex.CARD_PAK, s["start"], s["end"])
                    hit = True
            count += hit
        self.log("  titles and emblems: %d come from imagefile%d.pak" % (count, mw2tex.CARD_PAK))
        return count

    def _pick(self, src, names):
        """Pick src's asset list entries named (type, name), with every entry they point into."""
        ents = src["assets"]
        pos = {id(e): i for i, e in enumerate(ents)}
        owner = self.owners.get(id(src))
        if owner is None:
            # (Kept for the next pick from src: both teams and the titles can come from one file.)
            owner = self.owners[id(src)] = {}
            for i, e in enumerate(ents):
                for o in iter_objects(e[1]):
                    if isinstance(o, dict) and "_asset" in o:
                        owner.setdefault(id(o), i)
        sel = set(i for i, e in enumerate(ents) if isinstance(e[1], dict) and (e[0], _name(e[1])) in names)
        todo = list(sel)
        while todo:
            for o in iter_objects(ents[todo.pop()][1]):
                if not isinstance(o, dict):
                    continue
                for c in o.get("@", {}).values():
                    for x in (c if isinstance(c, list) else [c]):
                        if not isinstance(x, Ref):
                            continue
                        a = x.target if isinstance(x.target, tree.AssetEntry) else _asset_in_slot(x)
                        j = None
                        if isinstance(a, tree.AssetEntry):
                            j = pos.get(id(a))      # (None: in a world drop_world let go)
                        elif isinstance(a, dict):
                            j = owner.get(id(a))
                        if j is not None and j not in sel:
                            sel.add(j)
                            todo.append(j)
        self.picked.setdefault(id(src), set()).update(sel)

    ONE_COPY = ("GfxImage", "Material", "XModelSurfs", "MaterialPixelShader", "PhysPreset")
    GLOBAL_ORDER = ("com_map", "fx_map", "lightdef")
    WORLD_ORDER = ("gfx_map", "game_map_mp", "col_map_mp")

    @staticmethod
    def _signature(o, depth=0):
        """Content of an asset for comparing two same-named copies (pointers by name)."""
        if depth > 12:
            return "deep"
        if isinstance(o, Ref):
            t = o.target
            t = t.asset if isinstance(t, tree.InsertSlot) else (t[1] if isinstance(t, tree.AssetEntry) else t)
            if isinstance(t, dict) and t.get("_asset"):
                return ("ref", t["_asset"], _name(t) or asset_name(t))
            return ("ref", o.rel)
        if isinstance(o, dict):
            if depth and o.get("_asset"):
                return ("asset", o["_asset"], _name(o) or asset_name(o))
            items = []
            for k, v in sorted(o.items(), key=lambda kv: str(kv[0])):
                if str(k).startswith("_"):
                    continue
                if k == "@":
                    for kk, vv in sorted(v.items(), key=lambda kv: str(kv[0])):
                        items.append((str(kk), Porter._signature(vv, depth + 1)))
                elif isinstance(v, (dict, list)):
                    items.append((k, Porter._signature(v, depth + 1)))
                elif not (isinstance(v, str) and (v.startswith("0x") or v in ("follow", "insert"))):
                    items.append((k, v))
            return tuple(items)
        if isinstance(o, list):
            return tuple(Porter._signature(x, depth + 1) for x in o)
        if isinstance(o, Leaf):
            return ("leaf", o.n, hash(o.raw))
        if isinstance(o, Str):
            return ("str", o.b)
        return repr(o)

    @classmethod
    def _held(cls, ents):
        """ids of what is written in place somewhere (an entry, or inside something written),
        not only pointed at."""
        held = set(id(e[1]) for e in ents)
        for o in cls._reach(ents):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k == "@":
                        for c in v.values():
                            for x in (c if isinstance(c, list) else [c]):
                                if isinstance(x, (dict, list, Leaf)):
                                    held.add(id(x))
                    elif isinstance(v, (dict, list)):
                        held.add(id(v))
            elif isinstance(o, list):
                held.update(id(x) for x in o if isinstance(x, (dict, list, Leaf)))
        return held

    @staticmethod
    def _reach(ents):
        """Every dict/list/Leaf the writer will write: those the asset list holds and those
        reached only through a pointer (an effect's model in a stock file), each once."""
        seen = set()
        stack = [e[1] for e in ents]
        out = []
        while stack:
            o = stack.pop()
            if id(o) in seen:
                continue
            seen.add(id(o))
            out.append(o)
            if isinstance(o, dict):
                for k, v in o.items():
                    if k == "@":
                        for c in v.values():
                            for x in (c if isinstance(c, list) else [c]):
                                if isinstance(x, Ref):
                                    t = x.target
                                    t = t.asset if isinstance(t, tree.InsertSlot) else (
                                        t[1] if isinstance(t, tree.AssetEntry) else t)
                                    if isinstance(t, (dict, list)):
                                        stack.append(t)
                                elif isinstance(x, (dict, list, Leaf)):
                                    stack.append(x)
                            if isinstance(c, PtrList):
                                stack.append(c)
                    elif isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(o, list):
                for x in o:
                    if isinstance(x, Ref):
                        t = x.target
                        t = t.asset if isinstance(t, tree.InsertSlot) else (
                            t[1] if isinstance(t, tree.AssetEntry) else t)
                        if isinstance(t, (dict, list)):
                            stack.append(t)
                    elif isinstance(x, (dict, list, Leaf)):
                        stack.append(x)
        return out

    def stock_layout(self, ents):
        """The test switch stock_layout (see FIXES); list_techsets has run already."""
        notes = []
        # The map's entities inside the collision map, as an inline (temporary) asset.
        cm = next((e[1] for e in ents if e[0] == "col_map_mp" and isinstance(e[1], dict)), None)
        me_entry = next((e for e in ents if e[0] == "map_ents"), None)
        c = cm.get("@", {}).get(("mapEnts", ())) if cm else None
        if me_entry is not None and isinstance(c, Ref) and c.target is me_entry:
            me = me_entry[1]
            slot = me.setdefault("_slot", tree.InsertSlot(me))
            for o in self._reach(ents):
                if isinstance(o, dict):
                    for k, x in list(o.get("@", {}).items()):
                        if isinstance(x, Ref) and x.target is me_entry and o is not cm:
                            x.target, x.rel, x.t = slot, 0, None
            cm["@"][("mapEnts", ())] = me
            cm["mapEnts"] = "insert"
            ents[:] = [e for e in ents if e is not me_entry]
            notes.append("entities inside the collision map")
        # One copy per name of the kinds stock files never repeat.
        groups = {}
        order = {}
        for i, e in enumerate(ents):
            for o in self._reach([e]):
                if isinstance(o, dict) and o.get("_asset") in self.ONE_COPY:
                    n = _name(o) or asset_name(o)
                    if n and not n.startswith(b","):
                        if id(o) not in order:
                            order[id(o)] = i
                            groups.setdefault((o["_asset"], n.lower()), []).append(o)
        swap = {}           # id(copy) -> kept asset
        differed = Counter()
        for key, objs in groups.items():
            if len(objs) < 2:
                continue
            # The stock copy (a streamed picture: what stock maps use) wins, else the first.
            keep = next((o for o in objs if o.get("streaming") == 1), objs[0])
            sig = self._signature(keep)
            for o in objs:
                if o is not keep:
                    swap[id(o)] = keep
                    if self._signature(o) != sig:
                        differed[key[0]] += 1
        if swap:
            inside = {}
            for o in self._reach(ents):
                if isinstance(o, dict) and id(o) in swap:
                    for x in iter_objects(o):
                        if x is not o:
                            inside[id(x)] = id(o)
            pointed = set()
            for o in self._reach(ents):
                if isinstance(o, dict) and id(o) not in inside:
                    for v in o.get("@", {}).values():
                        for x in (v if isinstance(v, list) else [v]):
                            if isinstance(x, Ref):
                                t = x.target.asset if isinstance(x.target, tree.InsertSlot) else x.target
                                if id(t) in inside and (x.rel or not (isinstance(t, dict) and t.get("_asset"))):
                                    pointed.add(inside[id(t)])
            # Pointers into an identical copy go to the same place in the kept one.
            same = {}
            def pair(a, b, depth=0):
                if depth > 14 or type(a) is not type(b) or id(a) in same:
                    return
                same[id(a)] = b
                if isinstance(a, dict):
                    for k, v in a.items():
                        if k == "@" and isinstance(b.get("@"), dict):
                            for kk, vv in v.items():
                                if kk in b["@"]:
                                    pair(vv, b["@"][kk], depth + 1)
                        elif isinstance(v, (dict, list)) and k in b and not str(k).startswith("_"):
                            pair(v, b[k], depth + 1)
                        elif isinstance(v, tree.InsertSlot) and isinstance(b.get(k), tree.InsertSlot):
                            same[id(v)] = b[k]
                elif isinstance(a, list) and len(a) == len(b):
                    for x, y in zip(a, b):
                        pair(x, y, depth + 1)
            identical = set()
            for o in self._reach(ents):
                if isinstance(o, dict) and id(o) in swap and id(o) in pointed \
                        and self._signature(o) == self._signature(swap[id(o)]):
                    pair(o, swap[id(o)])
                    identical.add(id(o))
            for k in pointed - identical:
                swap.pop(k, None)
            if same:
                for o in self._reach(ents):
                    if not isinstance(o, dict) or id(o) in inside:
                        continue
                    for v in o.get("@", {}).values():
                        for x in (v if isinstance(v, list) else [v]):
                            if isinstance(x, Ref) and id(x.target) in same and inside.get(id(x.target)) in identical:
                                x.target = same[id(x.target)]

            def ref_to(a):
                r = Ref(1)
                r.target, r.rel = a.setdefault("_slot", tree.InsertSlot(a)), 0
                a["_forward"] = True
                return r
            slots = {id(o["_slot"]): o for o in self._reach(ents)
                     if isinstance(o, dict) and id(o) in swap and isinstance(o.get("_slot"), tree.InsertSlot)}
            merged = Counter()
            for o in list(self._reach(ents)):
                if not isinstance(o, dict) or id(o) in swap:
                    continue
                ch = o.get("@", {})
                for k, v in list(ch.items()):
                    if isinstance(v, dict) and id(v) in swap:
                        ch[k] = ref_to(swap[id(v)])
                        if isinstance(o.get(k[0]), str):
                            o[k[0]] = "0x00000001"
                        merged[v["_asset"]] += 1
                    elif isinstance(v, list):
                        for j, x in enumerate(v):
                            if isinstance(x, dict) and id(x) in swap:
                                v[j] = ref_to(swap[id(x)])
                                merged[x["_asset"]] += 1
                            elif isinstance(x, Ref) and (id(x.target) in swap or id(x.target) in slots):
                                a = swap.get(id(x.target)) or swap[id(slots[id(x.target)])]
                                v[j] = ref_to(a)
                    elif isinstance(v, Ref) and (id(v.target) in swap or id(v.target) in slots):
                        a = swap.get(id(v.target)) or swap[id(slots[id(v.target)])]
                        ch[k] = ref_to(a)
            # What only a dropped copy held is now only pointed at: a name-only asset gets
            # a copy of its name at each pointer, anything else is written at its first use.
            held = self._held(ents)
            for o in list(self._reach(ents)):
                if not isinstance(o, dict):
                    continue
                ch = o.get("@", {})
                for k, v in list(ch.items()):
                    for j, x in enumerate(v if isinstance(v, list) else [v]):
                        if not isinstance(x, Ref) or x.rel:
                            continue
                        t = x.target.asset if isinstance(x.target, tree.InsertSlot) else x.target
                        if not isinstance(t, dict) or not t.get("_asset") or id(t) in held:
                            continue
                        if (_name(t) or asset_name(t) or b"").startswith(b","):
                            c = {kk: vv for kk, vv in t.items() if kk not in ("_slot", "_forward")}
                            if isinstance(t.get("@"), dict):
                                c["@"] = dict(t["@"])
                            self.done.add(id(c))
                            if isinstance(v, list):
                                v[j] = c
                            else:
                                ch[k] = c
                                if isinstance(o.get(k[0]), str):
                                    o[k[0]] = "follow"
                        else:
                            t.setdefault("_slot", tree.InsertSlot(t))
                            t["_forward"] = True
            if merged:
                notes.append("one copy per name (%s%s)" % (
                    ", ".join("%s %d" % kv for kv in sorted(merged.items())),
                    "; differing copies replaced by the stock or first one: %s" % ", ".join(
                        "%s %d" % kv for kv in sorted(differed.items())) if differed else ""))
        # An asset pointed at from somewhere besides where it is held keeps a slot that the
        # other pointers name: written inline without one, the writer wrote it again at the
        # next pointer (mp_backlot: the cardboard box material in an effect and again in the
        # collision map's dynamic entities).
        pointed_at = set()
        for o in self._reach(ents):
            if isinstance(o, dict):
                for v in o.get("@", {}).values():
                    for x in (v if isinstance(v, list) else [v]):
                        if isinstance(x, Ref) and not x.rel:
                            t = x.target.asset if isinstance(x.target, tree.InsertSlot) else x.target
                            if isinstance(t, dict) and t.get("_asset"):
                                pointed_at.add(id(t))
        held_by = Counter()
        for o in self._reach(ents):
            if isinstance(o, dict):
                for v in o.get("@", {}).values():
                    for x in (v if isinstance(v, list) else [v]):
                        if isinstance(x, dict) and x.get("_asset"):
                            held_by[id(x)] += 1
            elif isinstance(o, PtrList):
                for x in o:
                    if isinstance(x, dict) and x.get("_asset"):
                        held_by[id(x)] += 1
        pointed_at.update(k for k, n in held_by.items() if n > 1)
        shared = 0
        for o in self._reach(ents):
            if isinstance(o, dict) and id(o) in pointed_at and not o.get("_forward") \
                    and not (_name(o) or asset_name(o) or b"").startswith(b","):
                o.setdefault("_slot", tree.InsertSlot(o))
                o["_forward"] = True
                shared += 1
        if shared:
            notes.append("%d shared assets given a slot (written once)" % shared)
        # The one-of-a-kind assets in stock order.
        idx = {e[0]: i for i, e in enumerate(ents) if e[0] in self.GLOBAL_ORDER + self.WORLD_ORDER}
        if all(t in idx for t in self.WORLD_ORDER):
            singles = {t: ents[i] for t, i in idx.items()}
            g_at = min(idx.values())
            w_at = max(idx[t] for t in self.WORLD_ORDER)
            rest = [(i, e) for i, e in enumerate(ents) if e[0] not in idx]
            new = []
            placed_g = placed_w = False
            for i, e in enumerate(ents):
                if i == g_at and not placed_g:
                    new.extend(singles[t] for t in self.GLOBAL_ORDER if t in singles)
                    placed_g = True
                if i == w_at and not placed_w:
                    new.extend(singles[t] for t in self.WORLD_ORDER)
                    placed_w = True
                if e[0] not in idx:
                    new.append(e)
            ents[:] = new
            notes.append("one-of-a-kind assets in stock order")
            moved = self._forward_early_uses(ents)
            if moved:
                notes.append("%d assets written at their first use" % moved)
        self.log("  test: stock file layout: " + "; ".join(notes))

    @staticmethod
    def _forward_early_uses(ents):
        """An asset used through a pointer before the entry that holds it is written at its
        first use instead (_forward, as shared pictures are); what it points at counts as
        used there too, so this repeats until nothing more moves. Returns how many moved."""
        home = {}
        for i, e in enumerate(ents):
            for o in iter_objects(e[1]):
                if isinstance(o, dict) and o.get("_asset"):
                    home.setdefault(id(o), i)
        eff = dict(home)
        changed = True
        while changed:
            changed = False
            for i, e in enumerate(ents):
                stack = [(e[1], i)]
                seen = set()
                while stack:
                    o, at = stack.pop()
                    if id(o) in seen:
                        continue
                    seen.add(id(o))
                    if isinstance(o, dict):
                        if o.get("_asset") and id(o) in eff:
                            at = min(at, eff[id(o)])
                        for k, v in o.items():
                            if k == "@":
                                for c in v.values():
                                    for x in (c if isinstance(c, list) else [c]):
                                        if isinstance(x, Ref):
                                            t = x.target.asset if isinstance(x.target, tree.InsertSlot) else x.target
                                            if isinstance(t, dict) and id(t) in eff and eff[id(t)] > at:
                                                eff[id(t)] = at
                                                changed = True
                                        elif isinstance(x, (dict, list)):
                                            stack.append((x, at))
                            elif isinstance(v, (dict, list)):
                                stack.append((v, at))
                    elif isinstance(o, list):
                        stack.extend((x, at) for x in o if isinstance(x, (dict, list)))
        moved = 0
        for e in ents:
            for o in iter_objects(e[1]):
                if isinstance(o, dict) and id(o) in eff and eff[id(o)] < home[id(o)] and not o.get("_forward"):
                    o.setdefault("_slot", tree.InsertSlot(o))
                    o["_forward"] = True
                    moved += 1
        return moved

    def list_techsets(self, ents):
        """The test switch list_techsets (see FIXES): each full shader set written inside a
        material becomes an asset list entry of its own before the first entry that held it;
        pointers to it point at the entry (as stock files point from one asset at another)."""
        found = {}          # id(techset) -> [techset, first entry index, [(holder dict, key)]]
        for i, e in enumerate(ents):
            if e[0] == "techset":
                continue
            for o in iter_objects(e[1]):
                if not isinstance(o, dict):
                    continue
                for k, c in o.get("@", {}).items():
                    if isinstance(c, dict) and c.get("_asset") == "MaterialTechniqueSet" \
                            and not (_name(c) or b",").startswith(b","):
                        f = found.setdefault(id(c), [c, i, []])
                        f[2].append((o, k))
        if not found:
            return
        entries = {}
        for key, (ts, at, holders) in found.items():
            entries[key] = tree.AssetEntry(["techset", ts])
        targets = {}
        for key, (ts, at, holders) in found.items():
            targets[id(ts)] = entries[key]
            if isinstance(ts.get("_slot"), tree.InsertSlot):
                targets[id(ts["_slot"])] = entries[key]
        for o in iter_objects(ents):
            if not isinstance(o, dict):
                continue
            for k, c in list(o.get("@", {}).items()):
                for j, x in enumerate(c if isinstance(c, list) else [c]):
                    if isinstance(x, Ref) and not x.rel and id(x.target) in targets:
                        x.target, x.rel, x.t = targets[id(x.target)], 4, None
        for key, (ts, at, holders) in found.items():
            for o, k in holders:
                r = Ref(1)
                r.target, r.rel = entries[key], 4
                o["@"][k] = r
                if isinstance(o.get(k[0]), str):
                    o[k[0]] = "0x00000001"      # (an alias: a non-null placeholder)
            ts.pop("_slot", None)
            ts.pop("_forward", None)
        by_at = {}
        for key, (ts, at, holders) in found.items():
            by_at.setdefault(at, []).append(entries[key])
        new = []
        for i, e in enumerate(ents):
            new.extend(by_at.get(i, ()))
            new.append(e)
        ents[:] = new
        self.log("  test: %d shader sets listed as assets of their own, as stock files list them"
                 % len(found))

    # split_car_fire: (effect that gets them, element numbers of the car fire)
    FIRE_SPLIT = ((b"props/car_glass_headlight", (0,)), (b"props/car_glass_brakelight", (1, 8)),
                  (b"props/car_glass_med", (2,)), (b"props/car_glass_large", (3,)),
                  (b"smoke/car_damage_whitesmoke", (10,)),
                  (b"smoke/car_damage_blacksmoke_fire", (4, 5, 6, 7, 9)))

    def split_car_fire(self, ents):
        """The test switch split_car_fire (see FIXES): each FIRE_SPLIT effect becomes the car
        fire with only the elements listed, sharing the fire's materials."""
        fx = {(asset_name(e[1]) or b"").lower(): e[1] for e in ents
              if e[0] == "fx" and isinstance(e[1], dict)}
        fire = fx.get(b"smoke/car_damage_blacksmoke_fire")
        els = deref(fire["@"].get(("elemDefs", ()))) if fire else None
        if not any(n in fx for n, _ in self.FIRE_SPLIT):
            return
        if not isinstance(els, list) or len(els) != 11 or any(n not in fx for n, _ in self.FIRE_SPLIT):
            self.warn("Split the car fire: the map lacks the car fire or the car's glass and smoke "
                      "effects as expected; left as they were")
            return
        looping = fire["elemDefCountLooping"]
        shared = {}
        for o in iter_objects(fire):
            if isinstance(o, dict) and o.get("_asset") not in (None, "FxEffectDef"):
                for x in iter_objects(o):
                    shared[id(x)] = x
                    if isinstance(x, dict) and "_slot" in x:
                        shared[id(x["_slot"])] = x["_slot"]
        made = {}
        for name, keep in self.FIRE_SPLIT:
            new = {k: v for k, v in fire.items() if k not in ("@", "_slot", "_forward")}
            new["@"] = {k: v for k, v in fire["@"].items() if k != ("elemDefs", ())}
            new["@"][("elemDefs", ())] = [copy.deepcopy(els[i], dict(shared)) for i in keep]
            new["elemDefCountLooping"] = sum(1 for i in keep if i < looping)
            new["elemDefCountOneShot"] = sum(1 for i in keep if i >= looping)
            new["elemDefCountEmission"] = 0
            if not new["elemDefCountLooping"]:
                new["msecLoopingLife"] = 0
            made[name] = new
        for name, new in made.items():
            d = fx[name]
            self._replace(d, new)
            _set_name(d, asset_name(fire) if name == b"smoke/car_damage_blacksmoke_fire" else name)
            for o in iter_objects(d):
                self.done.add(id(o))
        self.log("  test: car fire split across the headlight (light), brake light (glow), side "
                 "window (distortion), windshield (bright core), first smoke (white puff) and fire "
                 "(flames)")

    COLOR_MAP = 2695565377         # nameHash of a material's colorMap texture

    def _color_image(self, m):
        for td in deref(m.get("@", {}).get(("textureTable", ()))) or []:
            if isinstance(td, dict) and td.get("nameHash") == self.COLOR_MAP:
                im = deref((td.get("u") or {}).get("@", {}).get(("image", ())))
                return (_name(im) or b"").lstrip(b",").lower() if isinstance(im, dict) else None
        return None

    def effect_sister_materials(self, ents):
        """The fix effect_sister_materials (see FIXES)."""
        common = {n.lower(): v for (t, n), v in self.common_materials.items() if t == "Material"}
        common_ts = set(n for t, n in self.common_names if t == "MaterialTechniqueSet")
        swap = {}
        for o in self._reach([e for e in ents if e[0] == "fx"]):
            if not (isinstance(o, dict) and o.get("_asset") == "Material") or id(o) in swap:
                continue
            name = asset_name(o) or b""
            sister = re.sub(rb"_eye\d+$", b"", name)
            if name.startswith(b",") or sister == name or sister.lower() not in common:
                continue
            ts = deref(o.get("@", {}).get(("techniqueSet", ())))
            tsn = (_name(ts) or b"") if isinstance(ts, dict) else b""
            if not tsn or tsn.startswith(b",") or tsn.lower() in common_ts:
                continue
            s = common[sister.lower()]
            if s.get("stateBitsEntry") != o.get("stateBitsEntry") or \
                    self._color_image(s) != self._color_image(o):
                continue
            swap[id(o)] = sister
        if not swap:
            return
        count = 0
        for o in self._reach([e for e in ents if e[0] == "fx"]):
            if not isinstance(o, dict):
                continue
            ch = o.get("@", {})
            for k, v in list(ch.items()):
                m = deref(v) if isinstance(v, Ref) else v
                if isinstance(m, dict) and id(m) in swap:
                    ch[k] = self.reference("Material", swap[id(m)])
                    if isinstance(o.get(k[0]), str):
                        o[k[0]] = "follow"
                    count += 1
        self.log("  %d effect material pointers drawn with common_mp's own material (%s)" % (
            count, ", ".join(sorted(set(n.decode("latin-1") for n in swap.values())))))

    COMMON_NAMED = ("GfxImage", "Material", "XModel", "FxEffectDef", "MaterialTechniqueSet", "PhysPreset")

    def name_common_assets(self, ents):
        """A full asset under a name common_mp.ff has (always loaded) becomes a reference to
        the loaded one, as stock maps name them (see name_common_assets in FIXES). One that
        other assets point into (not at it, but at a part inside it) stays, as the parts
        would be written nowhere."""
        # A raw file (vision, script) common_mp has is left out: the game loads raw files by
        # name, and stock maps carry none of common_mp's (PC mp_backlot's vision/mp_backlot.vision:
        # common_mp has IW's own 360 one).
        dropped = [e for e in ents if e[0] == "rawfile" and isinstance(e[1], dict)
                   and ("RawFile", (_name(e[1]) or b"").lower()) in self.common_names]
        if dropped:
            ents[:] = [e for e in ents if not any(e is d for d in dropped)]
            self.log("  %d raw files common_mp has left out (%s)" % (
                len(dropped), ", ".join(_name(e[1]).decode("latin-1") for e in dropped[:4])))
        found = [o for o in iter_objects(ents) if isinstance(o, dict) and id(o) not in self.done
                 and o.get("_asset") in self.COMMON_NAMED
                 and not (asset_name(o) or b",").startswith(b",")
                 and (o["_asset"], asset_name(o).lower()) in self.common_names]
        if not found:
            return
        inside = {}
        for o in found:
            for x in iter_objects(o):
                if x is not o:
                    inside[id(x)] = id(o)
            for v in o.values():
                if isinstance(v, tree.InsertSlot):
                    inside.pop(id(v), None)
        held = set(id(o) for o in found)
        pointed_into = set()
        for o in iter_objects(ents):
            if not isinstance(o, dict) or id(o) in inside or id(o) in held:
                continue
            for c in o.get("@", {}).values():
                for x in (c if isinstance(c, (list, PtrList)) else [c]):
                    if isinstance(x, Ref):
                        t = x.target.asset if isinstance(x.target, tree.InsertSlot) else x.target
                        if id(t) in inside and (x.rel or not isinstance(t, dict)
                                                or t.get("_asset") is None):
                            pointed_into.add(inside[id(t)])
        named, kept = Counter(), []
        for o in found:
            if id(o) in pointed_into:
                kept.append(asset_name(o))
                continue
            named[o["_asset"]] += 1
            self._replace(o, self.reference(o["_asset"], asset_name(o)))
        if named:
            self.log("  %d assets common_mp has named instead of copied (%s)" % (
                sum(named.values()), ", ".join("%s %d" % kv for kv in sorted(named.items()))))
        if kept:
            self.log("  note: %d kept as copies (other assets point inside them: %s)" % (
                len(kept), ", ".join(n.decode("latin-1") for n in kept[:5])))

    def add_destructible_aliases(self, ents):
        """An alias list entry for each sound the map's destructibles need and lack
        (self.destructible_aliases): only its name, which stock_sounds then fills from the
        stock 360 file that has it, as it does the map's own aliases."""
        have = set((alias_name(o) or b"").lower() for o in iter_objects(ents)
                   if isinstance(o, dict) and o.get("_asset") == "snd_alias_list_t")
        made = []
        for name in self.destructible_aliases:
            if name.lower() in have or name not in self.stock_aliases:
                continue
            d = {"_asset": "snd_alias_list_t", "@": {("aliasName", ()): Str(name)}}
            made.append(tree.AssetEntry(["sound", d]))
            have.add(name.lower())
        ents[0:0] = made
        if made:
            self.log("  %d destructible sounds added from the stock 360 files (%s)" % (
                len(made), ", ".join(alias_name(e[1]).decode("latin-1") for e in made)))

    def add_breakables(self, ents):
        """Copy in the models the map's destructible entities need (self.breakables, from
        destructible_plan), after the map's own assets: each named as the 360's script asks for
        it, a part taken in another color painted with the map's material for its own color
        (mc/mtl_80s_econ_red -> mc/mtl_80s_econ_silv), and a material the map has under the same
        name taken from the map. Copies of one stock model share its materials and geometry."""
        ours = {}
        for e in ents:
            for o in iter_objects(e[1]):
                if isinstance(o, dict) and o.get("_asset") == "Material":
                    n = asset_name(o)
                    if n and not n.startswith(b","):
                        ours.setdefault(n.lower(), o)
        roots = dict(self.x_refs)
        made, painted = [], 0
        for need, src, path, swap in self.breakables:
            root = roots.get(path)
            hit = None
            for e in (root or {}).get("assets", ()):
                if e[0] == "xmodel" and isinstance(e[1], dict) and (_name(e[1]) or b"").lower() == src.encode():
                    hit = e[1]
                    break
            if hit is None:
                continue
            base = self.copy_in(hit)
            # A material named through another model's list ("the same as its second")
            # becomes the material itself, so the copy stands alone.
            ml = base.get("@", {}).get(("materialHandles", ()))
            for i, x in enumerate(ml if isinstance(ml, list) else ()):
                while isinstance(x, Ref) and isinstance(x.target, PtrList):
                    x = _asset_in_slot(x)
                if isinstance(x, dict):
                    ml[i] = x
            shared = {}
            for o in iter_objects(base):
                if isinstance(o, dict) and o.get("_asset") not in (None, "XModel"):
                    for x in iter_objects(o):
                        shared[id(x)] = x
                        if isinstance(x, dict) and "_slot" in x:
                            shared[id(x["_slot"])] = x["_slot"]
            m = copy.deepcopy(base, shared)
            m.pop("_slot", None)
            self.remap_bones(m, root["script_strings"])
            _set_name(m, need.encode())
            painted += self.paint_breakable(m, swap, ours)
            self.done.add(id(m))
            made.append(tree.AssetEntry(["xmodel", m]))
        at = next((i for i, e in enumerate(ents) if e[0] == "gfx_map"), len(ents))
        ents[at:at] = made
        if made:
            self.log("  test: %d breakable parts copied in (%d paint materials the map's own)"
                     % (len(made), painted))

    def paint_breakable(self, model, swap, ours):
        """Point model's materials at the map's own: (swap: (stock color, map color)) the one
        with the map's color, else the same-named one. Returns how many changed."""
        ml = model.get("@", {}).get(("materialHandles", ()))
        ml = ml.target if isinstance(ml, Ref) else ml
        if not isinstance(ml, list):
            return 0
        count = 0
        for i, x in enumerate(ml):
            m = x
            if isinstance(m, Ref):
                m = _ptr_array_value(m) or (m.target if isinstance(m.target, tree.AssetEntry)
                                            else _asset_in_slot(m))
                m = m[1] if isinstance(m, tree.AssetEntry) else m
            n = (asset_name(m) or b"").lower() if isinstance(m, dict) else b""
            if not n:
                continue
            mine = None
            if swap:
                a, b = swap
                mine = ours.get(re.sub(rb"_%s(?=_|$)" % a.encode(), b"_" + b.encode(), n))
            mine = mine or ours.get(n)
            if mine is not None and mine is not m:
                ml[i] = mine
                count += 1
        return count

    def _copy_picked(self, src):
        """Copy the entries picked from src in their stock order (both teams of one file
        together: a later model can point into an earlier one's parts)."""
        sel = self.picked.pop(id(src), None)
        if not sel:
            return
        self.owners.pop(id(src), None)     # copying changes the entries' pointers
        ents = src["assets"]
        new = [ents[i] for i in sorted(sel)]
        self.stock_entry_ids.update(id(e) for e in new)
        self.localize(new)
        self.remap_bones(new, src["script_strings"])
        self.root["assets"][:0] = new

    def remap_bones(self, new, theirs):
        """Bone names are indexes into the file's script string list (theirs: the stock file's):
        move them to ours. Left as they were, they named whatever string had that number
        here, or one past the end of the list (the game then failed with "MT_GetSize: max
        allocation exceeded ... for script usage")."""
        ss = self.root.get("script_strings")
        if ss is None:
            ss = self.root["script_strings"] = [None]
        where = {(s.b if isinstance(s, Str) else None): k for k, s in enumerate(ss)}

        def ours(v):
            b = theirs[v].b if isinstance(theirs[v], Str) else None
            if b not in where:
                where[b] = len(ss)
                ss.append(Str(b))
            return where[b]
        for o in iter_objects(new):
            if not isinstance(o, dict):
                continue
            # Model bone names; an animation's bone names and its notifies' names (XAnimParts).
            key = {"XModel": "boneNames", "XAnimParts": "names"}.get(o.get("_asset"))
            if key is None:
                if "time" in o and isinstance(o.get("name"), int) and id(o) not in self.remapped:
                    # XAnimNotifyInfo: {name (script string), time}
                    self.remapped.add(id(o))
                    o["name"] = ours(o["name"])
                continue
            lf = o.get("@", {}).get((key, ()))
            if isinstance(lf, Ref):
                lf = lf.target
            if not isinstance(lf, Leaf) or id(lf) in self.remapped:
                continue
            self.remapped.add(id(lf))
            out = [ours(v) for v in struct.unpack(lf.E + "%dH" % lf.n, lf.raw)]
            lf.raw = struct.pack(lf.E + "%dH" % lf.n, *out)


def _glass_key(d):
    """A glass type's texture vectors, rounded (the key its 360-only numbers follow)."""
    tv = d.get("texVecs") or []
    return tuple(round(abs(float(x)), 6) for row in tv for x in (row if isinstance(row, list) else [row]))


def _name(d):
    c = d.get("@", {}).get(("name", ()))
    if c is None and isinstance(d.get("info"), dict):
        c = d["info"].get("@", {}).get(("name", ()))
    if isinstance(c, Ref) and isinstance(c.target, Str) and c.rel == 0:
        c = c.target
    return c.b if isinstance(c, Str) else None


def _set_name(d, b):
    ch = d.get("@", {})
    if ("name", ()) not in ch and isinstance(d.get("info"), dict):
        ch = d["info"]["@"]
    ch[("name", ())] = Str(b)


def zlib_error():
    import zlib
    return zlib.error


def _rawfile_text(d):
    import zlib
    ch = d["data"]["@"]
    lf = ch.get(("buffer", ()), ch.get(("compressedBuffer", ())))
    if isinstance(lf, Ref):
        lf = lf.target
    raw = lf.raw
    if d.get("compressedLen"):
        raw = zlib.decompress(raw[:d["compressedLen"]])
    return raw[:d["len"]]


# createfx lines that place an effect: "ent = createOneshotEffect( ... );", also called by
# its script's name (maps\mp\_utility::createOneshotEffect), as most MW2 and CoD4 maps do.
_PLACE_FX = re.compile(r"ent\s*=\s*(?:[\w\\/]+::)?create(?:OneshotEffect|LoopEffect|Exploder)\b")
# A setExpFog call at the start of a line, its arguments over one line or several.
_FOG = re.compile(r'(?m)^([ \t]*)(setExpFog\w*[ \t]*\((?:[^;"]|"[^"]*")*?\)[ \t]*;)')


def strip_effects(text):
    """A createfx script without the effects it places: each "ent = create...Effect(...)" or
    createExploder line and the "ent.v[...] = ...;" lines after it. Sounds
    (createLoopSound, createIntervalSound) stay. Returns (text, effects taken out)."""
    out, skip, n = [], False, 0
    for line in text.split("\n"):
        st = line.strip()
        if _PLACE_FX.match(st):
            skip = True
            n += 1
            continue
        if skip and (st.startswith("ent.") or st.startswith("ent ") and "=" in st and "create" not in st):
            continue
        skip = False
        out.append(line)
    return "\n".join(out), n


def strip_fog(text):
    """A script with its setExpFog calls commented out, line by line (a call can span several).
    Returns (text, calls commented out)."""
    def comment(m):
        lines = m.group(2).split("\n")
        return m.group(1) + "// " + lines[0] + "".join(
            "\n" + re.sub(r"^([ \t]*)", r"\1// ", x) for x in lines[1:])
    return _FOG.subn(comment, text)


def _is_builder_signature(d):
    """The rawfile IW4x's tools sign a file with: "FastFile built using the IW4x ZoneBuilder"
    (mp_backlot) or "FastFile built using IW4x ZoneTool!" (mp_ancient), stored as is but marked
    compressed. Any rawfile marked compressed whose data isn't a zlib stream is treated the same:
    the 360 would try to inflate it."""
    import zlib
    ch = d.get("data", {}).get("@", {})
    lf = ch.get(("buffer", ()), ch.get(("compressedBuffer", ())))
    if isinstance(lf, Ref):
        lf = lf.target
    if not isinstance(lf, Leaf):
        return False
    if lf.raw.startswith(b"FastFile built using"):
        return True
    n = d.get("compressedLen") or 0
    if not n:
        return False
    try:
        zlib.decompress(lf.raw[:n])
    except zlib.error:
        return True
    return False


def _set_rawfile_text(d, data):
    ch = d["data"]["@"]
    old = ch.pop(("buffer", ()), None) or ch.pop(("compressedBuffer", ()))
    d["compressedLen"] = 0
    d["len"] = len(data)
    d["data"]["@"][("buffer", ())] = Leaf(old.t, len(data) + 1, data + b"\0", old.E)


def _team_maps(arena, team):
    """Stock maps whose file carries team (from mp/basemaps.arena)."""
    out = []
    for block in re.findall(rb"\{(.*?)\}", arena or b"", re.S):
        kv = dict(re.findall(rb'(\w+)\s+"?([^"\s]*)"?', block))
        if team.encode() in (kv.get(b"allieschar"), kv.get(b"axischar")):
            out.append(kv[b"map"].decode())
    return out


def _arena_teams(text, map_name):
    for block in re.findall(rb"\{(.*?)\}", text, re.S):
        kv = dict(re.findall(rb'(\w+)\s+"?([^"\s]*)"?', block))
        if kv.get(b"map") == map_name.encode():
            if b"allieschar" in kv and b"axischar" in kv:
                return kv[b"allieschar"].decode(), kv[b"axischar"].decode()
    return None


# ================================================================ command line

@contextlib.contextmanager
def no_gc():
    """Python's cycle collector paused: reading a file makes millions of objects that all stay,
    and the collector would keep looking through them all for nothing (about half the time)."""
    import gc
    was = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was:
            gc.enable()


def load_tree(path):
    ff, zone = mw2ff.read_fastfile(path)
    sch = mw2ff.schema_for(ff.platform)
    with no_gc():
        root, r = tree.read_tree(zone, sch)
    return ff, zone, root


def map_teams(pc_path, teams=None):
    """(allies, axis) the map wants: given, or from its .arena file next to the .ff, or the
    game's defaults for a map it doesn't know."""
    if teams:
        return tuple(teams)
    arena = os.path.splitext(pc_path)[0] + ".arena"
    if os.path.exists(arena):
        text = open(arena, "rb").read()
        kv = dict(re.findall(rb'(\w+)[ \t]+"?([^"\s]*)"?', text))
        if b"allieschar" in kv and b"axischar" in kv:
            return kv[b"allieschar"].decode(), kv[b"axischar"].decode()
    return "us_army", "opforce_composite"


def load_stock(path, log=None):
    """A stock 360 file's tree, with each picture whose pixels are in the disc's
    imagefile*.pak files marked with its container entries ("_pak").
    Read from the cache next to the file (mw2port_cache) when it has the file as it is now."""
    root = stock_cache_load(path)
    if root is not None:
        if log:
            log("  (ready from last time, in %s)" % STOCK_CACHE_DIR)
        return root
    ff, zone = mw2ff.read_fastfile(path)
    # The always-loaded files (common_mp) are only looked things up in: animation data, which
    # can't be read back out of the tree, may be left rough there.
    resident = os.path.splitext(os.path.basename(path))[0].lower() == "common_mp"
    with no_gc():
        root, r = tree.read_tree(zone, mw2ff.schema_for(ff.platform), lenient=resident)
    with contextlib.redirect_stdout(io.StringIO()):
        images = ff.images
    paks = {i["offset"]: k for k, i in enumerate(x for x in images if x["pak"])}
    for info, inst, start, end in r.assets:
        if info.name == "GfxImage" and start in paks:
            k = paks[start]
            r.reg[id(inst)][1]["_pak"] = [tuple(t) for t in ff.table[k * LEVELS:(k + 1) * LEVELS]]
    stock_cache_save(path, root, log)
    return root


# ------------------------------------------------------------ stock file cache
# Reading a stock file into a tree takes most of a conversion, and stock files don't change,
# so each tree is kept (pickled) in mw2port_cache next to the file, as it is before a
# conversion changes it. A cached tree is used only for the same file (size and time) read
# by the same code and definitions; anything wrong with it and the file is read again.

STOCK_CACHE_DIR = "mw2port_cache"
_cache_tag = None


def _stock_cache_tag():
    """Changes whenever the code or definitions that read a stock file change."""
    global _cache_tag
    if _cache_tag is None:
        import hashlib
        import inspect
        import mw2tex
        here = os.path.dirname(os.path.abspath(__file__))
        h = hashlib.sha1(b"%d.%d" % sys.version_info[:2])
        files = [os.path.join(here, n) for n in ("mw2ff.py", "tree.py", "zone.py", "cdefs.py",
                                                 "schema.py", "codec.py")] + [mw2tex.__file__]
        for d in ("defs", "defs_pc"):
            for top, dirs, names in os.walk(os.path.join(here, d)):
                dirs.sort()
                files += [os.path.join(top, n) for n in sorted(names)]
        for f in files:
            with open(f, "rb") as fh:
                h.update(fh.read())
        h.update(inspect.getsource(load_stock).encode())
        _cache_tag = h.hexdigest()[:16]
    return _cache_tag


def _stock_cache_path(path):
    st = os.stat(path)
    base = os.path.basename(path).lower()
    return os.path.join(os.path.dirname(os.path.abspath(path)), STOCK_CACHE_DIR, "%s.%s.%x.%x.pickle" % (
        base, _stock_cache_tag(), st.st_size, st.st_mtime_ns))


def _schema_types(sch):
    """Every type of a schema, in a fixed order (their place in it stands for them in a cache)."""
    out, seen = [], set()
    stack = list(reversed(list(sch.defs.types.values())))
    while stack:
        t = stack.pop()
        if id(t) in seen:
            continue
        seen.add(id(t))
        out.append(t)
        if isinstance(t, Compound):
            stack.extend(reversed([m.type for m in t.members]))
    return out


_PICKLED = (dict, list, tuple, str, bytes, int, float, bool, type(None), tree.PtrList, tree.Tail,
            tree.AssetEntry, Str, Leaf, Ref, tree.InsertSlot)


def _dump_tree(root, fh):
    import cdefs
    import copyreg
    import pickle
    types = _schema_types(mw2ff.schema_for("xbox"))
    index = {id(t): i for i, t in enumerate(types)}

    class P(pickle.Pickler):
        def persistent_id(self, o):
            if isinstance(o, cdefs.Type):
                return index[id(o)]     # a type outside the schema can't be cached: KeyError
            if type(o) not in _PICKLED and not (o in _PICKLED if isinstance(o, type) else
                                                o in (copyreg.__newobj__, copyreg._reconstructor)):
                raise TypeError("can't cache a %s" % getattr(o, "__name__", type(o).__name__))
            return None

    fh.write(b"mw2port tree\n")
    pickle.dump([(t.kind, t.name, t.size) for t in types], fh, protocol=pickle.HIGHEST_PROTOCOL)
    P(fh, protocol=pickle.HIGHEST_PROTOCOL).dump(root)


def _load_tree(fh):
    import pickle
    if fh.readline() != b"mw2port tree\n":
        raise ValueError("not a cached tree")
    types = _schema_types(mw2ff.schema_for("xbox"))
    if pickle.load(fh) != [(t.kind, t.name, t.size) for t in types]:
        raise ValueError("cached with other definitions")

    class U(pickle.Unpickler):
        def persistent_load(self, i):
            return types[i]

    return U(fh).load()


def stock_cache_load(path):
    """The cached tree of a stock file, or None."""
    try:
        cache = _stock_cache_path(path)
        if not os.path.exists(cache):
            return None
        with open(cache, "rb") as fh, no_gc():
            return _load_tree(fh)
    except Exception:  # noqa: BLE001 - a bad cache only means reading the file again
        return None


def stock_cache_save(path, root, log=None):
    """Keeps a stock file's tree for next time (and drops older ones of that file). Never
    fails: a tree that can't be cached is read from the file again next time."""
    import threading
    try:
        cache = _stock_cache_path(path)
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        tmp = cache + ".tmp"
        err = []

        def dump():
            try:
                with open(tmp, "wb") as fh, no_gc():
                    _dump_tree(root, fh)
            except BaseException as e:  # noqa: BLE001 - reported below
                err.append(e)

        # Pickling follows the tree's nesting: give it room (its own thread, a big stack).
        # Windows takes at most 256 MB; the depth allowed leaves the stack room to spare.
        limit, old = sys.getrecursionlimit(), threading.stack_size()
        try:
            for size, depth in ((256 << 20, 500000), (64 << 20, 120000)):
                try:
                    threading.stack_size(size)
                    break
                except ValueError:
                    continue
            else:
                raise ValueError("no room for a big stack here")
            sys.setrecursionlimit(max(limit, depth))
            t = threading.Thread(target=dump)
            t.start()
            t.join()
        finally:
            threading.stack_size(old)
            sys.setrecursionlimit(limit)
        if err:
            raise err[0]
        os.replace(tmp, cache)
        name = os.path.basename(path).lower() + "."
        for n in os.listdir(os.path.dirname(cache)):
            if n.startswith(name) and n.endswith(".pickle") and n != os.path.basename(cache):
                os.remove(os.path.join(os.path.dirname(cache), n))
    except Exception as e:  # noqa: BLE001
        try:
            os.remove(tmp)
        except Exception:  # noqa: BLE001
            pass
        if log:
            log("  note: couldn't keep %s ready for next time (%s: %s); it's read again then"
                % (os.path.basename(path), type(e).__name__, e))


RESIDENT_SOUND = "resident"     # Porter._encoded: a sound the always-loaded files have


def _sound_name(path):
    """The 360 name of a PC sound file (dir/name.wav, .mp3, ...): stock 360 sounds have no
    extension."""
    base, ext = os.path.splitext(path)
    return base if ext.lower() in (b".wav", b".mp3", b".flac", b".ogg") else path


def _sound_data(ls):
    """A PC LoadedSound's audio bytes, or None (one that only names its sound)."""
    s = ls.get("sound") if isinstance(ls, dict) else None
    d = s.get("@", {}).get(("data", ())) if isinstance(s, dict) else None
    d = d.target if isinstance(d, Ref) else d
    return bytes(d.raw) if isinstance(d, Leaf) and d.raw else None


def wav_pcm(raw, info=None):
    """(16-bit PCM bytes, rate, channels) of a .wav file, or of plain PCM described by a PC
    LoadedSound's info; None for any other format (ADPCM, 8-bit, ...)."""
    if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        fmt = data = None
        i = 12
        while i + 8 <= len(raw):
            cid, ln = raw[i:i + 4], struct.unpack_from("<I", raw, i + 4)[0]
            if cid == b"fmt ":
                fmt = raw[i + 8:i + 8 + ln]
            elif cid == b"data":
                data = raw[i + 8:i + 8 + ln]
            i += 8 + ln + (ln & 1)
        if fmt is None or data is None or len(fmt) < 16:
            return None
        tag, ch, rate, _, _, bits = struct.unpack_from("<HHIIHH", fmt)
        if tag == 0xFFFE and len(fmt) >= 26:            # WAVE_FORMAT_EXTENSIBLE: its subformat
            tag = struct.unpack_from("<H", fmt, 24)[0]
        if tag != 1 or bits != 16 or ch not in (1, 2) or not rate or len(data) < 2 * ch:
            return compressed_pcm(raw)
        return data[:len(data) // (2 * ch) * 2 * ch], rate, ch
    if isinstance(info, dict) and info.get("format") == 1 and info.get("bits") == 16 \
            and info.get("channels") in (1, 2) and info.get("rate"):
        ch = info["channels"]
        return raw[:len(raw) // (2 * ch) * 2 * ch], info["rate"], ch
    return compressed_pcm(raw)


def compressed_pcm(raw):
    """(16-bit PCM bytes, rate, channels) of an .mp3 (CoD4 maps' ambient tracks), an ADPCM .wav,
    a .flac or an .ogg, decoded with miniaudio; None without it or for anything else."""
    try:
        import miniaudio
    except ImportError:
        compressed_pcm.missing = True
        return None
    for read in (miniaudio.mp3_read_s16, miniaudio.wav_read_s16, miniaudio.flac_read_s16,
                 miniaudio.vorbis_read):
        try:
            d = read(bytes(raw))
        except Exception:  # noqa: BLE001 - not this format: try the next
            continue
        if d.nchannels in (1, 2) and d.sample_rate and d.num_frames:
            return d.samples.tobytes(), d.sample_rate, d.nchannels
    return None


compressed_pcm.missing = False


def picture_index(iwds):
    """{lowercase picture name: (zipfile, path)} for the images/*.iwi in the .iwd files given;
    later files win. Paths are matched without caring about case or slash direction, as the
    game (on Windows) does."""
    found = {}
    for zf in iwds:
        for n in zf.namelist():
            low = n.replace("\\", "/").lower()
            if low.startswith("images/") and low.endswith(".iwi"):
                found[low[7:-4]] = (zf, n)
    return found


def game_iwd_files(folder):
    """The .iwd files in a folder and its subfolders (the PC game's main folder, a copy of its
    iw_*.iwd, or IW4x's iw4x folder), in the order the game loads them."""
    if not folder or not os.path.isdir(folder):
        return []
    found = []
    for top, dirs, names in os.walk(folder):
        # Other maps and mods aren't the game's own files.
        dirs[:] = [n for n in dirs if n.lower() not in ("usermaps", "mods")]
        found += [os.path.join(top, n) for n in names if n.lower().endswith(".iwd")]
    return sorted(found, key=lambda p: os.path.relpath(p, folder).lower())


def effect_index(root):
    """{name: effect} of a file's own effects (listed in its asset list)."""
    out = {}
    for e in root["assets"]:
        if e[0] == "fx" and isinstance(e[1], dict):
            n = _name(e[1])
            if n and not n.startswith(b","):
                out.setdefault(n, e[1])
    return out


_ZONE_NAME = re.compile(rb"[\x20-\x7e]{1,255}")
_ZONE_NAMES = re.compile(rb"\0([\x20-\x7e]{1,255})(?=\0)")


def zone_names(path):
    """Every name (printable text between two NULs) in a stock file's zone, kept in
    mw2port_cache next to it: effect_donors looks the map's effects up in every stock map,
    which otherwise means unpacking all of them for each conversion."""
    st = os.stat(path)
    base = os.path.basename(path).lower() + ".names."
    folder = os.path.join(os.path.dirname(os.path.abspath(path)), STOCK_CACHE_DIR)
    cache = os.path.join(folder, "%s%x.%x.txt" % (base, st.st_size, st.st_mtime_ns))
    try:
        with open(cache, "rb") as fh:
            return set(fh.read().split(b"\n"))
    except OSError:
        pass
    _, zone = mw2ff.read_fastfile(path)
    names = set(_ZONE_NAMES.findall(zone))
    try:
        os.makedirs(folder, exist_ok=True)
        with open(cache + ".tmp", "wb") as fh:
            fh.write(b"\n".join(sorted(names)))
        os.replace(cache + ".tmp", cache)
        for n in os.listdir(folder):
            if n.startswith(base) and n != os.path.basename(cache):
                os.remove(os.path.join(folder, n))
    except OSError:
        pass
    return names


def stock_techset_names(path):
    """The (lower-case) names of the shader sets a stock file has, kept in mw2port_cache next
    to it (worked out once from its tree); empty if it can't be read."""
    return _stock_names(path, "techsets", "MaterialTechniqueSet")


def stock_model_names(path):
    """The (lower-case) names of the models (XModel) a stock file has, kept like
    stock_techset_names."""
    return _stock_names(path, "models", "XModel")


def _stock_names(path, tag, typ):
    st = os.stat(path)
    base = os.path.basename(path).lower() + "." + tag + "."
    folder = os.path.join(os.path.dirname(os.path.abspath(path)), STOCK_CACHE_DIR)
    cache = os.path.join(folder, "%s%x.%x.txt" % (base, st.st_size, st.st_mtime_ns))
    try:
        with open(cache, "rb") as fh:
            return set(fh.read().split(b"\n")) - {b""}
    except OSError:
        pass
    try:
        with no_gc():
            names = set(pool_names(load_stock(path)).get(typ, ()))
    except Exception:  # noqa: BLE001 - a file that can't be read offers nothing
        return set()
    try:
        os.makedirs(folder, exist_ok=True)
        with open(cache + ".tmp", "wb") as fh:
            fh.write(b"\n".join(sorted(names)))
        os.replace(cache + ".tmp", cache)
        for n in os.listdir(folder):
            if n.startswith(base) and n != os.path.basename(cache):
                os.remove(os.path.join(folder, n))
    except OSError:
        pass
    return names


def map_entity_text(root):
    """The entity string (MapEnts) of a tree, as text ("" if it has none)."""
    for o in iter_objects(root["assets"]):
        if isinstance(o, dict) and o.get("_asset") == "MapEnts":
            lf = o.get("@", {}).get(("entityString", ()))
            lf = lf.target if isinstance(lf, Ref) else lf
            if isinstance(lf, Leaf):
                return lf.raw.split(b"\0", 1)[0].decode("latin-1")
    return ""


def destructible_names(types_script, wanted):
    """{destructible type: (names its 360 script function uses, colors that function is
    called with)} for the destructible types wanted, read from the 360's
    common_scripts/_destructible_types.gsc: the function each type's case calls, with the
    color it's given put into the names it builds ("vehicle_80s_sedan1_" + color + "_hood")."""
    text = types_script.decode("latin-1") if isinstance(types_script, bytes) else types_script
    cases = {}          # type -> (function, its argument)
    for m in re.finditer(r'case\s+"([^"]+)"\s*:\s*(\w+)\s*\(\s*(?:"([^"]*)")?\s*\)\s*;', text):
        cases.setdefault(m.group(1).lower(), (m.group(2), m.group(3)))
    colors = {}
    for f, arg in cases.values():
        if arg is not None:
            colors.setdefault(f, set()).add(arg)
    out = {}
    for t in wanted:
        hit = cases.get(t.lower())
        if hit is None:
            continue
        f, arg = hit
        m = re.search(r"(?m)^%s\s*\(\s*(\w*)\s*\)\s*\n\{" % re.escape(f), text)
        if m is None:
            continue
        end = text.find("\n}", m.end())
        body = text[m.end():end if end >= 0 else len(text)]
        param = m.group(1)
        names = set()
        part = r'"[^"\n]*"' + (r"|\b%s\b" % param if param else "")
        for e in re.finditer(r"(?:%s)(?:\s*\+\s*(?:%s))*" % (part, part), body):
            pieces = re.findall(part, e.group(0))
            if not any(p.startswith('"') for p in pieces):
                continue
            names.add("".join(p[1:-1] if p.startswith('"') else (arg or "") for p in pieces))
        out[t] = (names, colors.get(f, set()))
    return out


def destructible_plan(pc_root, types_script, have, stock_paths):
    """The models the map's destructible entities (destructible_type) need that neither the map
    nor the always-loaded files have: [(name needed, stock name to copy, stock file,
    (stock color, map color) or None)]. A model no stock file has is taken in another color the
    function knows (vehicle_80s_sedan1_red_hood for _silv_hood), its paint then the map's own.
    have: lower-case model names the map and the always-loaded files have."""
    types = set(re.findall(r'"destructible_type"\s+"([^"]+)"', map_entity_text(pc_root)))
    if not types:
        return [], {}
    found = {p: stock_model_names(p) for p in stock_paths}
    plan, missing = [], {}
    for t, (names, colors) in sorted(destructible_names(types_script, types).items()):
        own = next((c for c in colors if t.lower().endswith("_" + c.lower())), None)
        for n in sorted(names):
            low = n.lower()
            if low == t.lower() or "/" in low or " " in low or not low.startswith(
                    t.lower().rsplit("_", 1)[0] if own else t.lower()) or low.encode() in have:
                continue
            tries = [(low, None)]
            if own:
                tries += [(low.replace("_%s_" % own.lower(), "_%s_" % c.lower(), 1),
                           (c.lower(), own.lower())) for c in sorted(colors) if c != own]
            for src, swap in tries:
                p = next((p for p in stock_paths if src.encode() in found[p]), None)
                if p is not None and (swap is None or src != low):
                    plan.append((low, src, p, swap))
                    break
            else:
                if own and "_%s_" % own.lower() in low:
                    missing.setdefault(t, []).append(low)
    return plan, missing


def destructible_donors(root, ref_paths, stock_paths, read, log=print):
    """destructible_plan for PC map root, with the 360's destructible script and model names
    from the always-loaded files among ref_paths (read: path -> tree)."""
    script, have = None, set(n.lower() for n in pool_names(root).get("XModel", ()))
    for p in ref_paths:
        if os.path.splitext(os.path.basename(p))[0].lower() not in RESIDENT:
            continue
        have |= stock_model_names(p)
        for e in read(p)["assets"]:
            if e[0] == "rawfile" and isinstance(e[1], dict) and \
                    (_name(e[1]) or b"").lower() == b"common_scripts/_destructible_types.gsc":
                script = _rawfile_text(e[1])
    if script is None:
        return []
    plan, missing = destructible_plan(root, script, have, list(stock_paths))
    if plan:
        log("  breakable parts the map lacks: %d models from %s" % (
            len(plan), ", ".join(sorted(set(os.path.basename(p) for _, _, p, _ in plan)))))
    for t, names in sorted(missing.items()):
        log("  note: no stock map has %s's %s" % (t, ", ".join(names[:6]) + (" ..." if len(names) > 6 else "")))
    return plan


def stock_alias_names(path):
    """The (lower-case) sound alias names a stock file has, kept like stock_techset_names."""
    return _stock_names(path, "aliases", "snd_alias_list_t")


def destructible_sound_plan(pc_root, types_script, have, stock_paths):
    """[(alias name, stock file)] of the sounds the 360's destructible script plays for the
    map's destructible entities (the names in each type's function that are sound aliases of
    some stock file) that the map and the always-loaded files lack (have: lower-case names)."""
    types = set(re.findall(r'"destructible_type"\s+"([^"]+)"', map_entity_text(pc_root)))
    if not types:
        return []
    found = {p: stock_alias_names(p) for p in stock_paths}
    plan, seen = [], set()
    for t, (names, _colors) in sorted(destructible_names(types_script, types).items()):
        for n in sorted(names):
            low = n.lower().encode("latin-1")
            if low in have or low in seen:
                continue
            p = next((p for p in stock_paths if low in found[p]), None)
            if p is not None:
                plan.append((low, p))
                seen.add(low)
    return plan


def destructible_sound_donors(root, ref_paths, stock_paths, read, log=print):
    """destructible_sound_plan for PC map root, with the 360's destructible script and alias
    names from the always-loaded files among ref_paths (read: path -> tree)."""
    script = None
    have = set(n.lower() for n in pool_names(root).get("snd_alias_list_t", ()))
    for p in ref_paths:
        if os.path.splitext(os.path.basename(p))[0].lower() not in RESIDENT:
            continue
        have |= stock_alias_names(p)
        for e in read(p)["assets"]:
            if e[0] == "rawfile" and isinstance(e[1], dict) and \
                    (_name(e[1]) or b"").lower() == b"common_scripts/_destructible_types.gsc":
                script = _rawfile_text(e[1])
    if script is None:
        return []
    # (Stock files already being read first: no extra file to read for what they have.)
    order = [p for p in stock_paths if p in ref_paths] + [p for p in stock_paths if p not in ref_paths]
    plan = destructible_sound_plan(root, script, have, order)
    if plan:
        log("  destructible sounds the map lacks: %d from %s" % (
            len(plan), ", ".join(sorted(set(os.path.basename(p) for _, p in plan)))))
    return plan


def stock_streamed_images(path):
    """{lower-case name: GfxImage} of the pictures a stock file streams from the 360's own
    picture packs (a header and where its levels sit in imagefile1-4.pak, no pixels), kept in
    mw2port_cache next to it; empty if it can't be read."""
    import pickle
    st = os.stat(path)
    base = os.path.basename(path).lower() + ".streamed."
    folder = os.path.join(os.path.dirname(os.path.abspath(path)), STOCK_CACHE_DIR)
    cache = os.path.join(folder, "%s%x.%x.%s.pickle" % (base, st.st_size, st.st_mtime_ns,
                                                         _stock_cache_tag()))
    try:
        with open(cache, "rb") as fh:
            return pickle.load(fh)
    except (OSError, EOFError, pickle.UnpicklingError, AttributeError, ImportError):
        pass
    out = {}
    try:
        with no_gc():
            for o in iter_objects(load_stock(path)["assets"]):
                if isinstance(o, dict) and o.get("_asset") == "GfxImage" and o.get("streaming") == 1 \
                        and o.get("pixels") is None and o.get("streams"):
                    n = asset_name(o) or b""
                    if n and not n.startswith((b",", b"*", b"$")):
                        out.setdefault(n.lower(), {k: v for k, v in o.items() if k not in ("_slot", "_forward")})
    except Exception:  # noqa: BLE001 - a file that can't be read offers nothing
        return {}
    try:
        os.makedirs(folder, exist_ok=True)
        with open(cache + ".tmp", "wb") as fh:
            pickle.dump(out, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(cache + ".tmp", cache)
        for n in os.listdir(folder):
            if n.startswith(base) and n != os.path.basename(cache):
                os.remove(os.path.join(folder, n))
    except OSError:
        pass
    return out


def pc_techsets(root):
    """Names of the shader sets a PC file's materials use (PC files hold most assets inside
    others, so the whole tree is walked; names only, ",name", count too)."""
    out = set()
    for o in iter_objects(root["assets"]):
        if isinstance(o, dict) and o.get("_asset") == "MaterialTechniqueSet":
            n = asset_name(o) or _name(o)
            if n:
                out.add(n.lstrip(b","))
    return out


def techset_donors(root, ref_paths, candidates, composites=False, log=print, most=2):
    """Stock 360 maps (of candidates) to read as well for shader sets the map's materials use
    that the stock files already read don't have (and, with merge_decals, the composite sets
    its decals could become): at most `most`, those carrying the most of them. Without them a
    material got the nearest set the files read had (mp_rust without its own stock file:
    3 of 664 materials lost an input, a detail map among them; mp_waw_castle: most decals
    stayed unmerged, their composite sets in other stock maps)."""
    import decals
    names = pc_techsets(root)
    names |= {n.replace(b"_hsm_", b"_sm_") for n in names}     # (PC-only "high" shadow sets)
    want = set(names)
    if composites:
        grounds = {g for g in (decals.ground_code(n) for n in names) if g}
        l1 = {c for c in (decals.layer_code(n, 1) for n in names) if c}
        l2 = {c for c in (decals.layer_code(n, 2) for n in names) if c}
        for g in grounds:
            for a in l1:
                # (composite sets are named without wc_: l_sm_r0c0n0_b1c1n1s1p0)
                want.add(g[0] + b"_" + a[0] + (b"p0" if g[1] or a[1] else b""))
                for b in l2:
                    want.add(g[0] + b"_" + a[0] + b"_" + b[0] + (b"p0" if g[1] or a[1] or b[1] else b""))
    have = set()
    for p in ref_paths:
        have |= stock_techset_names(p)
    want = {n.lower() for n in want} - have
    if not want:
        return []
    hits = {p: want & stock_techset_names(p) for p in candidates}
    out = []
    while len(out) < most:
        best = max(hits, key=lambda p: (len(hits[p] & want), p not in out), default=None)
        if best is None or best in out or not hits[best] & want:
            break
        out.append(best)
        want -= hits[best]
    if out:
        log("  shader sets the files given lack: reading %s as well" % ", ".join(
            os.path.basename(p) for p in out))
    return out


def effect_donors(root, ref_roots, candidates, log=print, most=2):
    """Stock 360 maps (of candidates) to read as well for the map's effects that the stock
    files already read don't have: at most `most`, those carrying the most of them."""
    have = set()
    for r in ref_roots:
        have.update(effect_index(r))
    want = set(_name(e[1]) for e in root["assets"] if e[0] == "fx" and isinstance(e[1], dict)) - have
    want.discard(None)
    if not want or not candidates:
        return []
    hits = {}
    plain = all(_ZONE_NAME.fullmatch(n) for n in want)
    for p in candidates:
        try:
            if plain:
                hits[p] = want & zone_names(p)
                continue
            ff, zone = mw2ff.read_fastfile(p)
        except Exception:
            continue
        hits[p] = set(n for n in want if b"\0" + n + b"\0" in zone)
    out = []
    while len(out) < most:
        best = max(hits, key=lambda p: (len(hits[p] & want), p not in out), default=None)
        if best is None or best in out or not hits[best] & want:
            break
        out.append(best)
        want -= hits[best]
    return out


# How many assets of each kind the game holds at once (TU6 default_mp.xex, the table its
# "Exceeded limit of %d '%s' assets" check reads), shared by the map and the files that stay
# loaded with it (common_mp, code_post_gfx_mp, patch_mp). Over one, the load stops part way.
POOL_LIMITS = {
    "PhysPreset": 64, "PhysCollmap": 1024, "XAnimParts": 4096, "XModelSurfs": 4096, "XModel": 1536,
    "Material": 4096, "MaterialPixelShader": 8096, "MaterialTechniqueSet": 768, "GfxImage": 3584,
    "snd_alias_list_t": 16000, "SndCurve": 64, "LoadedSound": 1350, "GfxLightDef": 32,
    "FxEffectDef": 600, "FxImpactTable": 4, "RawFile": 1024, "StringTable": 400, "TracerDef": 32,
}
_pool_cache = {}


def _file_key(path):
    """(path, size, time) of a file, to keep what's worked out from it; None if it isn't there."""
    try:
        st = os.stat(path)
        return (os.path.abspath(path), st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def _pool_name(o):
    """The name an asset takes a place in the game's pool under."""
    n = alias_name(o) if o["_asset"] == "snd_alias_list_t" else (asset_name(o) or _name(o))
    if n is None:
        f = o.get("@", {}).get(("filename", ()))
        n = f.b if isinstance(f, Str) else None
    return n


def pool_names(root, key=None):
    """{asset type: set of names} of the assets a tree brings (named references to assets the
    game already has, ",name", take no place). key: the stock file it was read from (path,
    size, time), to keep the names for next time (not the tree: that kept every conversion's
    common_mp and code_post_gfx_mp trees in memory)."""
    if key is not None and key in _pool_cache:
        return _pool_cache[key]
    out = {}
    for o in iter_objects(root["assets"]):
        if not (isinstance(o, dict) and o.get("_asset") in POOL_LIMITS):
            continue
        n = _pool_name(o)
        if n and not n.startswith(b","):
            out.setdefault(o["_asset"], set()).add(n.lower())
    if key is not None:
        _pool_cache[key] = out
    return out


# The highest and the median of each measure over the 16 stock 360 multiplayer maps (TU6 disc
# files, measured the way map_measures does), and the map holding the highest.
STOCK_MAP_HIGHS = [
    ("placed models", 13755, 7039, "mp_invasion"),
    ("world surfaces", 15771, 9526, "mp_checkpoint"),
    ("see-through/decal surfaces", 740, 417, "mp_nightshift"),
    ("world triangles", 326015, 198531, "mp_nightshift"),
    ("world vertices", 474713, 274218, "mp_nightshift"),
    ("lightmaps", 3, 2, "mp_rust"),
    ("reflection probes", 28, 20, "mp_quarry"),
    ("rooms", 41, 24, "mp_invasion"),
    ("portals", 362, 166, "mp_rundown"),
    ("collision brushes", 29932, 15095, "mp_checkpoint"),
    ("collision triangles", 154652, 50379, "mp_afghan"),
    ("model kinds", 732, 523, "mp_estate"),
    ("materials", 2214, 1579, "mp_favela"),
    ("effects", 147, 108, "mp_quarry"),
    ("pictures in the file", 42, 32, "mp_estate"),
    ("picture memory (MB)", 24.9, 13.7, "mp_checkpoint"),
    ("models in draw range (mean)", 5571, 3556, "mp_afghan"),
    ("models in draw range (max)", 7176, 4774, "mp_afghan"),
]
# What the 360 can draw in one view (TU6 0x823EE148): the world's see-through surfaces go in a
# list of 2,048; the rest are left out.
SEE_THROUGH_LIST = 0x800
WHY_HIGH = {
    "see-through/decal surfaces": "blended layers drawn over other surfaces: a frame rate cost; "
                                  "Merge decal layers into the ground (test) folds ground decals in",
    "pictures in the file": "stock maps stream most pictures from the game's packs; held in the "
                            "file they take memory and load time (the picture budget trims them)",
    "picture memory (MB)": "memory the map's pictures take while it is loaded; a lower picture "
                           "budget brings it down",
    "models in draw range (mean)": "models the 360 may have to draw from one spot: frame rate; "
                                   "Cap model draw distance (test) lowers it",
    "models in draw range (max)": "the busiest spot",
}


def map_measures(root):
    """{measure: value} of a map tree, as STOCK_MAP_HIGHS has them for the stock maps."""
    def first(t):
        return next((e[1] for e in root["assets"] if e[0] == t and isinstance(e[1], dict)), None)
    G, C = first("gfx_map"), first("col_map_mp")
    if G is None:
        return {}

    def tgt(c):
        return deref(c) if isinstance(c, Ref) else c
    m = {}
    dp = G.get("dpvs") or {}
    m["placed models"] = dp.get("smodelCount", 0)
    m["world surfaces"] = dp.get("staticSurfaceCount", 0)
    m["see-through/decal surfaces"] = max(0, dp.get("litTransSurfsEnd", 0) - dp.get("litTransSurfsBegin", 0))
    draw = G.get("draw") or {}
    m["world triangles"] = draw.get("indexCount", 0) // 3
    m["world vertices"] = draw.get("vertexCount", 0)
    m["lightmaps"] = draw.get("lightmapCount", 0)
    m["reflection probes"] = draw.get("reflectionProbeCount", 0)
    m["rooms"] = (G.get("dpvsPlanes") or {}).get("cellCount", 0)
    cells = tgt(G.get("@", {}).get(("cells", ()))) or []
    m["portals"] = sum(c.get("portalCount", 0) for c in cells if isinstance(c, dict))
    if C:
        m["collision brushes"] = C.get("numBrushes", 0)
        m["collision triangles"] = C.get("triCount", 0)
    kinds, seen, mem, pics = {}, set(), 0, 0
    for o in iter_objects(root["assets"]):
        if not isinstance(o, dict) or id(o) in seen:
            continue
        seen.add(id(o))
        a = o.get("_asset")
        if a and not (_name(o) or b"").startswith(b","):
            kinds[a] = kinds.get(a, 0) + 1
        if a == "GfxImage" and not o.get("_pak") and not o.get("streaming") \
                and isinstance(o.get("cardMemory"), int) and not (_name(o) or b"").startswith(b","):
            mem += o["cardMemory"]
            pics += 1
    m["model kinds"] = kinds.get("XModel", 0)
    m["materials"] = kinds.get("Material", 0)
    m["effects"] = kinds.get("FxEffectDef", 0)
    m["pictures in the file"] = pics
    m["picture memory (MB)"] = round(mem / 2.0 ** 20, 1)
    # Models within draw range (cull distance, else the last detail level's) of ground spots:
    # every placed model's own spot, a sample of up to 150 of them.
    pts, rng = [], []
    for d in tgt(dp.get("@", {}).get(("smodelDrawInsts", ()))) or []:
        if not isinstance(d, dict) or not d.get("origin"):
            continue
        mo = tgt(d.get("@", {}).get(("model", ())))
        if isinstance(mo, tree.InsertSlot):
            mo = mo.asset
        lods = [l.get("dist", 0) for l in ((mo.get("lodInfo") or [])[:mo.get("numLods") or 0]
                                         if isinstance(mo, dict) else []) if isinstance(l, dict)]
        far = min([x for x in (d.get("cullDist") or 0, max(lods) if lods else 0) if x > 0] or [1e9])
        pa = d.get("packedAxis")
        sc = struct.unpack("<f", struct.pack("<I", pa[3] & 0xFFFFFFFF))[0] if pa and len(pa) > 3 else 1.0
        pts.append(d["origin"])
        rng.append((far * max(sc, 1e-3)) ** 2)
    if pts:
        step = max(1, len(pts) // 150)
        counts = [sum(1 for p, r in zip(pts, rng)
                      if (p[0] - s_[0]) ** 2 + (p[1] - s_[1]) ** 2 + (p[2] - s_[2]) ** 2 <= r)
                  for s_ in pts[::step]]
        m["models in draw range (mean)"] = int(round(sum(counts) / float(len(counts))))
        m["models in draw range (max)"] = max(counts)
    return m


def map_limits_report(root, log):
    """Log the map's measures against the stock 360 maps' highest, flagging what goes past."""
    m = map_measures(root)
    if not m:
        return
    over = []
    log("  map measures against the 16 stock 360 maps (value / stock highest / stock median):")
    for name, high, median, who in STOCK_MAP_HIGHS:
        if name not in m:
            continue
        v = m[name]
        flag = "  OVER (stock highest: %s)" % who if v > high else ""
        log("    %-28s %9s / %9s / %9s%s" % (name, v, high, median, flag))
        if v > high:
            over.append(name)
    if m.get("see-through/decal surfaces", 0) > SEE_THROUGH_LIST:
        log("    note: more see-through/decal surfaces than the %d the 360 draws in one view; "
            "with most in view, some are left out" % SEE_THROUGH_LIST)
    for name in over:
        if name in WHY_HIGH:
            log("    %s: %s" % (name, WHY_HIGH[name]))


def drop_pc_tables(root, log):
    """The PC's config string tables (mp/configstrings/configstrings_pc_<map>_<mode>.csv) go:
    the 360 game only ever looks for configStrings_360_<map>_<mode>.csv (TU6 0x822F5520), as a
    lookup that shortens network messages and a checksum host and players compare; without
    one, both use the same default. The PC ones only took memory and table room."""
    keep, gone = [], 0
    for e in root["assets"]:
        n = (_name(e[1]) or b"").lower() if e[0] == "stringtable" and isinstance(e[1], dict) else b""
        if n.lstrip(b",").startswith(b"mp/configstrings/configstrings_pc_"):
            gone += 1
            continue
        keep.append(e)
    if gone:
        root["assets"][:] = keep
        log("  %d PC config string tables left out (the 360 never reads them)" % gone)


def check_pools(root, refs, log, warn):
    """Stop when the map and the files loaded with it hold more assets of a kind than the game
    has room for; warn when it is close. Each full copy the map carries of an asset those files
    already have takes a place of its own (65 copies of $default stopped converted mp_showdown
    with "Exceeded limit of 64 'sndcurve' assets"); stock maps carry none, only names."""
    have = {}
    for name, r in refs:
        if os.path.splitext(os.path.basename(name))[0].lower() in RESIDENT + ("patch_mp",):
            for t, names in pool_names(r, _file_key(name)).items():
                have.setdefault(t, set()).update(names)
    copies = Counter()
    for o in iter_objects(root["assets"]):
        if isinstance(o, dict) and o.get("_asset") in POOL_LIMITS:
            n = _pool_name(o)
            if n and not n.startswith(b",") and n.lower() in have.get(o["_asset"], ()):
                copies[o["_asset"]] += 1
    over, used = [], []
    for t, names in pool_names(root).items():
        n = len(names | have.get(t, set())) + copies[t]
        lim = POOL_LIMITS[t]
        used.append((n / float(lim), "%s %d/%d" % (t, n, lim)))
        if n > lim:
            over.append("%s: %d of %d" % (t, n, lim))
        elif n > lim * 0.95:
            warn("%d %s assets with the files loaded alongside, of the %d the game has room for"
                 % (n, t, lim))
    log("  game asset room used (with the files loaded alongside): %s"
        % ", ".join(u for _, u in sorted(used, reverse=True)[:4]))
    if over:
        raise PortError("the map holds more assets than the game has room for (%s); it would stop "
                        "loading part way" % ", ".join(over))


# The big parts of a stock map's own world: its drawn geometry and its collision, about 70% of
# its tree (some 160 MB). Of a stock map read only for its teams' models or for effects, the
# converter uses neither (the template map's world is kept whole).
BIG_WORLD = ("gfx_map", "col_map_mp")


def drop_world(root):
    """Let a donor stock map's drawn geometry and collision go, freeing their memory for what's
    read next. The named assets inside them (lightmaps, reflection probes, ...) stay, in the
    same place in the asset list and in the same order, so every look-up by name finds what it
    did before."""
    import gc
    ents = []
    for e in root["assets"]:
        if e[0] in BIG_WORLD and isinstance(e[1], dict):
            inner = [o for o in iter_objects(e[1]) if isinstance(o, dict) and "_asset" in o
                     and o is not e[1]]
            inner.reverse()     # (iter_objects takes a list's last item first)
            e = tree.AssetEntry(["world_parts", inner])
        ents.append(e)
    root["assets"] = ents
    gc.collect()        # (the parts point back into each other: freed only by the collector)


def port(pc_path, out_path, iwd_path=None, ref_paths=(), log=print, teams=None, loaded=None,
         game_iwds=(), texture_budget=0, card_pak=False, fx_paths=(), fixes=None, pak_dir=None,
         measures=True, cache_dir=None, keep_loaded=True, donors=(), pak_start=STREAM_PAK):
    """pak_dir, pak_start: where stream_pictures' paks go (PakWriter) and the first pak number.
    loaded: {path: tree} of stock files already read (load_stock), to reuse; with
    keep_loaded=False they are let go once converting is done (the caller is done with them).
    donors: stock maps of ref_paths read only for their teams (or effects); their own world is
    drawn geometry and collision are let go as soon as they're read (drop_world), as are those
    of the effect maps read here.
    game_iwds: the PC game's .iwd files, for pictures the map's own .iwd doesn't have.
    fx_paths: stock 360 maps to take the map's effects from (those carrying the most of them
    are read too). fixes: {name: bool} of FIXES (default all on). measures: log a map's
    measures against the stock maps (map_limits_report) after writing it. cache_dir: a
    mw2port_cache folder to keep slow picture steps in between conversions (PictureCache)."""
    fixes = fix_set(fixes)
    if not fixes["texture_budget"]:
        texture_budget = 0
    ff, zone, root = load_tree(pc_path)
    if ff.platform != "pc":
        raise PortError("%s is not a PC fastfile" % pc_path)
    refs = []
    if loaded is None:
        loaded, keep_loaded = {}, False
    ref_paths = list(ref_paths)
    donors = set(donors)

    def read(p):
        if p not in loaded:
            log("reading stock 360 file %s" % os.path.basename(p))
            loaded[p] = load_stock(p, log)
            if p in donors:
                drop_world(loaded[p])
        return loaded[p]

    if fx_paths and fixes["stock_effects"]:
        for p in ref_paths:
            read(p)
        extra = effect_donors(root, [loaded[p] for p in ref_paths if os.path.splitext(
            os.path.basename(p))[0].lower() not in RESIDENT],
                              [p for p in fx_paths if p not in ref_paths], log)
        ref_paths += extra
        donors.update(extra)
    if fx_paths and fixes["stock_techsets"]:
        extra = techset_donors(root, ref_paths, [p for p in fx_paths if p not in ref_paths],
                               fixes["merge_decals"], log)
        ref_paths += extra
        donors.update(extra)
    breakables = []
    if fx_paths and fixes["destructible_parts"]:
        breakables = destructible_donors(root, ref_paths, fx_paths, read, log)
        extra = sorted(set(p for _, _, p, _ in breakables) - set(ref_paths))
        ref_paths += extra
        donors.update(extra)
    sound_plan = []
    if fx_paths and fixes["destructible_sounds"] and fixes["stock_sounds"]:
        sound_plan = destructible_sound_donors(root, ref_paths, fx_paths, read, log)
        # (Stock maps already among ref_paths are preferred: no extra file to read.)
        extra = sorted(set(p for _, p in sound_plan) - set(ref_paths))
        ref_paths += extra
        donors.update(extra)
    for p in ref_paths:
        refs.append((p, read(p)))
    if not iwd_path:
        iwd_path = []
    elif isinstance(iwd_path, str):
        iwd_path = [iwd_path]
    iwd = [zipfile.ZipFile(p) for p in iwd_path]
    log("converting %s" % os.path.basename(pc_path))
    pak = None
    if fixes["stream_pictures"]:
        pak = PakWriter(pak_dir or os.path.dirname(os.path.abspath(out_path)), pak_start)
    porter = Porter(root, refs, iwd, log, [zipfile.ZipFile(p) for p in game_iwds], texture_budget,
                    fixes, pak)
    if cache_dir:
        porter.picture_cache = PictureCache(os.path.join(cache_dir, "pictures"))
    porter.breakables = breakables
    porter.destructible_aliases = [n for n, _ in sound_plan]
    if fixes["stock_streamed_pictures"]:
        for p in list(ref_paths) + [p for p in fx_paths if p not in ref_paths]:
            for k, v in stock_streamed_images(p).items():
                porter.stock_streamed.setdefault(k, v)
    off = [k for k, v in fixes.items() if not v and DEFAULT_FIXES[k]]
    on = [k for k, v in fixes.items() if v and not DEFAULT_FIXES[k]]
    if off:
        log("  fixes switched off: %s" % ", ".join(off))
    if on:
        log("  switched on (off by default): %s" % ", ".join(on))
    porter.convert()
    if porter.picture_cache:
        c = porter.picture_cache
        if c.hits:
            log("  %d of %d slow picture steps ready from last time (in %s)"
                % (c.hits, c.hits + c.misses, STOCK_CACHE_DIR))
        c.prune()
    if porter.map_name:
        porter.add_teams(refs, map_teams(pc_path, teams))
        if card_pak:
            porter.cards_to_pak()
        # The stock team copied in: the PC copy of a stock map carries its own (PC mp_rust).
        if porter.fixes["merge_duplicates"]:
            porter.merge_same_named(root["assets"])
    # Last, once everything the file carries is in (teams are added above): the layout
    # passes look at the whole asset list.
    if porter.fixes["list_techsets"] or porter.fixes["stock_layout"]:
        porter.list_techsets(root["assets"])
    if porter.fixes["stock_layout"]:
        porter.stock_layout(root["assets"])
    if fixes["drop_pc_tables"]:
        drop_pc_tables(root, log)
    check_pools(root, refs, log, porter.warn)
    # Done with the stock files: let them go before writing, so the writer and the measures
    # read-back reuse their memory (on mp_backlot the peak was about 0.8 GB higher without).
    # What they lent the map is in root by now; the porter keeps only what's used below.
    refs = None
    if not keep_loaded:
        loaded.clear()
    keep = {k: porter.__dict__[k] for k in ("root", "P", "X", "log", "warnings", "streamed")}
    porter.__dict__.clear()
    porter.__dict__.update(keep)
    is_map = any(e[0] == "gfx_map" for e in root["assets"])
    import gc
    gc.collect()        # (trees point back into themselves: freed only by the collector)
    xs = schema_mod.load("xbox")
    w = tree.TreeWriter(root, xs, keep_fixes=False)
    w.map_rel = lambda r: map_rel(r, porter.P, porter.X)
    w.dedupe = fixes["dedupe_assets"]
    out = w.write()
    out = reserve_callback_block(out, porter)
    _write_x360(out, out_path, pak_table(out, porter.root, w.starts, w.written))
    log("wrote %s (%d bytes of zone)" % (out_path, len(out)))
    if w.deduped:
        log("  %d places point at an asset written earlier instead of holding a copy of it"
            % w.deduped)
    if measures and is_map:
        # Measured on the file as written (as the console loads it), with the converted tree
        # let go first: the read-back is as big again.
        w = root = porter.root = None
        gc.collect()
        try:
            with no_gc():
                written, _ = tree.read_tree(out, xs)
            map_limits_report(written, log)
        except Exception as e:      # the report must never stop a conversion
            porter.warn("map measures couldn't be taken (%s)" % e)
    if pak is not None and porter.streamed:
        changed = pak.save()
        log("  %d pictures stream from %s (the console needs each of them)" % (
            porter.streamed, ", ".join("imagefile%d.pak" % n for n in sorted(pak.used))))
        for path, added, total in changed:
            log("  %s: %.1f MB added, %.1f MB in all: copy it to the game folder, next to "
                "default_mp.xex" % (os.path.basename(path), added, total))
        if not changed:
            log("  every one was in the paks already (no pak changed)")
    return out, porter


def _pak_sizes(folder):
    """{pak number: size} of the stream_pictures paks in folder."""
    out = {}
    for n in range(STREAM_PAK, STREAM_PAK_LAST + 1):
        p = os.path.join(folder, "imagefile%d.pak" % n)
        if os.path.exists(p):
            out[n] = os.path.getsize(p)
    return out


def port_map(pc_path, out_dir, stock_paths, teams=None, log=print, game_iwds=(),
             texture_budget=TEXTURE_BUDGET_MB, card_ui=None, fixes=None, pak_dir=None,
             write_card_pak=True, measures=True, pak_start=STREAM_PAK):
    """Convert a PC map (its .ff, and _load.ff / .iwd / .arena next to it when there) into
    out_dir, picking what it needs from the stock 360 files given: code_post_gfx_mp.ff, a
    stock map (render settings, shaders) and the stock maps that carry the map's teams.
    Returns the paths written."""
    stock = {os.path.basename(p).lower(): p for p in stock_paths}
    cpg = stock.get("code_post_gfx_mp.ff")
    if cpg is None:
        raise PortError("code_post_gfx_mp.ff (from the console) is needed next to the stock maps")
    maps = sorted(p for n, p in stock.items() if n.startswith("mp_") and not n.endswith("_load.ff"))
    if not maps:
        raise PortError("at least one stock 360 map (for example mp_favela.ff) is needed")
    template = stock.get("mp_favela.ff") or maps[0]
    cache_dir = os.path.join(os.path.dirname(os.path.abspath(cpg)), STOCK_CACHE_DIR)
    loads = sorted(p for n, p in stock.items() if n.endswith("_load.ff"))
    base = os.path.splitext(pc_path)[0]
    name = os.path.basename(base)
    iwd = [base + ".iwd"] if os.path.exists(base + ".iwd") else []
    # One pak for every map (stream_pictures), next to the maps' folders (mw2port_out).
    pak_dir = pak_dir or os.path.dirname(os.path.normpath(os.path.abspath(out_dir)))
    paks_before = _pak_sizes(pak_dir)
    os.makedirs(out_dir, exist_ok=True)
    loaded = {}
    log("reading stock 360 file code_post_gfx_mp.ff")
    loaded[cpg] = load_stock(cpg, log)
    # The game always has common_mp loaded too: its materials (heat distortion, ...) are the
    # render state templates for a ported map's own materials of those shader sets.
    common = stock.get("common_mp.ff")
    if common is None:
        log("  note: common_mp.ff isn't next to the stock maps, so the materials only it carries "
            "(heat distortion, ...) can't be used")
    arena = next((_rawfile_text(e[1]) for e in loaded[cpg]["assets"]
                  if e[0] == "rawfile" and _name(e[1]) == b"mp/basemaps.arena"), b"")
    want = map_teams(pc_path, teams)
    # A PC copy of a stock map (PC mp_rust): the stock 360 map of that name comes first for
    # what it has (scripts, sounds, materials), and its teams are the 360's for the map.
    same = stock.get(name.lower() + ".ff")
    if not teams and not os.path.exists(base + ".arena") and _arena_teams(arena, name.lower()):
        want = _arena_teams(arena, name.lower())
    refs = [cpg] + ([common] if common else []) + ([same] if same and same != template else []) + [template]
    donors = []
    for team in want:
        carriers = set(_team_maps(arena, team.lower()))
        donor = next((p for p in maps if os.path.splitext(os.path.basename(p))[0].lower() in carriers), None)
        if donor and donor not in refs:
            refs.append(donor)
            donors.append(donor)
    written = []
    if os.path.exists(base + "_load.ff"):
        tpl_load = os.path.splitext(template)[0] + "_load.ff"
        load_ref = tpl_load if tpl_load in loads else (loads[0] if loads else None)
        if load_ref is None:
            log("  note: no stock *_load.ff given; the loading screen file is left out")
        else:
            out = os.path.join(out_dir, name + "_load.ff")
            try:
                port(base + "_load.ff", out, iwd, [cpg, template, load_ref], log, loaded=loaded,
                     game_iwds=game_iwds, fixes=fixes, pak_dir=pak_dir, pak_start=pak_start,
                     cache_dir=cache_dir)
                written.append(out)
            except (ValueError, PortError, mw2ff.zone_mod.ZoneError) as e:
                # The map works without it: the game shows a plain loading screen.
                with open(base + "_load.ff", "rb") as fh:
                    head = fh.read(16)
                log("  note: %s_load.ff couldn't be converted, so it's left out (the map still "
                    "works, with a plain loading screen): %s [file starts %s]"
                    % (name, e, head.hex(" ")))
    out = os.path.join(out_dir, name + ".ff")
    port(pc_path, out, iwd, refs, log, teams=want, loaded=loaded, game_iwds=game_iwds,
         texture_budget=texture_budget, card_pak=bool(card_ui), fx_paths=maps, fixes=fixes,
         pak_dir=pak_dir, pak_start=pak_start, measures=measures, cache_dir=cache_dir, keep_loaded=False,
         donors=donors)
    written.append(out)
    # The paks this map added pictures to (the ones to copy to the console again).
    written += [os.path.join(pak_dir, "imagefile%d.pak" % n) for n, size in sorted(_pak_sizes(pak_dir).items())
                if paks_before.get(n) != size]
    if card_ui and write_card_pak:
        # card_ui: a ui_mp.ff (mw2tex's built one, or the stock one) to fill the slots from.
        import mw2tex
        written.append(mw2tex.write_card_pak(card_ui, out_dir, log=log))
    elif card_ui:
        log("  imagefile8.pak isn't written with this map (titles and emblems still come from "
            "it): use the one already on the console, or Build imagefile8.pak")
    return written


def port_map_variants(pc_path, out_dir, stock_paths, teams=None, log=print, game_iwds=(),
                      texture_budget=TEXTURE_BUDGET_MB, card_ui=None, fixes=None,
                      write_card_pak=True, measures=True, pak_start=STREAM_PAK):
    """The map as port_map makes it with the fixes given, plus one test variant per fix that
    is on, with just that fix switched off: out_dir/variants/no_<fix>/. One batch of files to
    try on the console, to find which fix helps or hurts. Returns the paths written; a
    variant that fails to convert doesn't stop the others."""
    fixes = fix_set(fixes)
    written = port_map(pc_path, out_dir, stock_paths, teams, log, game_iwds, texture_budget,
                       card_ui, fixes, write_card_pak=write_card_pak, measures=measures,
                       pak_start=pak_start)
    main_paks = os.path.dirname(os.path.normpath(os.path.abspath(out_dir)))
    labels = {k: label for k, label, _ in FIXES}
    for k in [k for k, v in fixes.items() if v]:
        log("")
        log("===== test variant: %s switched off =====" % labels[k])
        vdir = os.path.join(out_dir, "variants", "no_" + k)
        try:
            # Titles and emblems: the pak written next to the main file serves every variant.
            files = [f for f in port_map(pc_path, vdir, stock_paths, teams, log, game_iwds,
                                         texture_budget, None, dict(fixes, **{k: False}),
                                         pak_dir=main_paks, pak_start=pak_start, measures=measures)
                     if not f.endswith(".pak")]
        except (PortError, mw2ff.zone_mod.ZoneError, ValueError) as e:
            log("  variant no_%s stopped: %s" % (k, e))
            continue
        if all(_same_file(f, os.path.join(out_dir, os.path.basename(f))) for f in files):
            # The fix changes nothing in this map: no point trying the variant.
            for f in files:
                os.remove(f)
            try:
                os.rmdir(vdir)
            except OSError:
                pass
            log("  no_%s: the same as the main file (this fix changes nothing in this map), "
                "so it's left out" % k)
            continue
        written += files
    return written


def _same_file(a, b):
    if not (os.path.exists(a) and os.path.exists(b)) or os.path.getsize(a) != os.path.getsize(b):
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        return fa.read() == fb.read()


def reserve_callback_block(zone, porter):
    """The 360 game fills block 5 (callback) at load time: a bump allocator there takes, for
    every material, 4 bytes per texture (0x821e7658), 4 per model surface (0x821e7908) and
    16 per some other assets (0x821dd2b0). The file carries no data for it, so a PC file
    converts to size 0 and the game writes through a null pointer (console-confirmed crash at
    0x821E7774). Stock files reserve 1-72 KB."""
    need = 0
    for o in iter_objects(porter.root):
        if isinstance(o, dict) and "_asset" in o:
            need += 16
            if o["_asset"] == "Material":
                need += 4 * (o.get("textureCount") or 0)
            elif o["_asset"] == "XModel":
                need += 4 * (o.get("numsurfs") or 0)
    # Other load-time allocations can land here too (0x82312da8 during asset callbacks), so
    # keep at least what a stock map reserves.
    need = max((need * 2 + 4096 + 0xFFF) & ~0xFFF, 0x10000)
    zone = bytearray(zone)
    if struct.unpack_from(">I", zone, 28)[0] < need:
        struct.pack_into(">I", zone, 28, need)
    return bytes(zone)


def rewrite_stock(path, out_path, log=print):
    """Test: read a stock 360 file and write it back out through the converter's own writer and
    container steps (TreeWriter, the callback block, the picture pak table), converting nothing.
    If the rewritten file shows the same problems on the console as converted maps (flicker,
    missing textures), the writer is at fault, not the conversion."""
    log("reading %s" % os.path.basename(path))
    root = load_stock(path)
    w = tree.TreeWriter(root, schema_mod.load("xbox"), keep_fixes=False)
    out = w.write()
    holder = type("Root", (), {"root": root})()
    out = reserve_callback_block(out, holder)
    _write_x360(out, out_path, pak_table(out, root, w.starts, w.written))
    log("wrote %s (%d bytes of zone; same bytes as the stock zone: %s)"
        % (out_path, len(out), "yes" if out == mw2ff.read_fastfile(path)[1] else "no"))
    return out_path


def map_rel(r, P, X):
    t = r.t
    if not isinstance(t, Compound):
        return r.rel
    tx = X.defs.types.get(t.name)
    if tx is None or tx.size == t.size and [m.offset for m in tx.members] == [m.offset for m in t.members]:
        return r.rel
    i, rem = divmod(r.rel, t.size)
    return i * tx.size + _member_off(t, tx, rem)


def _member_off(tp, tx, rem):
    if rem == 0:
        return 0
    for m in tp.members:
        if m.offset <= rem < m.offset + m.size:
            mx = next((x for x in tx.members if x.name == m.name), None)
            if mx is None:
                raise PortError("a pointer points at %s.%s, which the 360 doesn't have" % (tp.name, m.name))
            inner = rem - m.offset
            if isinstance(m.type, Compound) and not [q for q in m.mods if q != PTR]:
                return mx.offset + _member_off(m.type, mx.type, inner)
            return mx.offset + inner
    raise PortError("a pointer points into padding of %s" % tp.name)


# 360 container header of a stock file (magic, version 0x10D, flags, build time), then the
# pak table count (0: every image is inside this file) and the file size twice.
X360_HEAD = bytes.fromhex("49576666753130300000010d0101ca3ec038c2e4a000000001")


def _write_x360(zone, path, table=()):
    import zlib
    # Level 6 (zlib's default): several times faster than 9 on a whole map for a file only a
    # little bigger; the console inflates either.
    stream = zlib.compress(zone, 6)
    head = X360_HEAD + struct.pack(">I", len(table)) + b"".join(struct.pack(">III", *t) for t in table)
    total = len(head) + 8 + len(stream)
    # The second size also counts the pictures read from imagefile1.pak while loading.
    extra = sum(t[2] - t[1] for t in table if t[0] == 1)
    with open(path, "wb") as f:
        f.write(head + struct.pack(">II", total, total + extra) + stream)


def pak_table(zone, root, starts, written=None):
    """Stock pictures copied in whose pixels live in the disc's imagefile*.pak files: the
    container lists where, 4 entries per picture in the order the zone has them."""
    import mw2tex
    # (Following pointers too: a stock asset copied in can reach its pictures only through them.)
    seen, paks, todo = set(), [], [root]
    while todo:
        x = todo.pop()
        if isinstance(x, Ref):
            x = x.target
        if isinstance(x, tree.InsertSlot):
            x = x.asset
        if x is None or id(x) in seen or not isinstance(x, (dict, list)):
            continue
        seen.add(id(x))
        if isinstance(x, dict):
            if "_pak" in x and id(x) in starts:
                paks.append(x)
            todo.extend(v for k, v in x.items() if k != "_slot")
            todo.extend(x.get("@", {}).values() if isinstance(x.get("@"), dict) else ())
        else:
            todo.extend(x)
    paks.sort(key=lambda o: starts[id(o)])
    if written is not None:
        # Every written copy, in file order: a stock picture two copied-in models share is
        # written once for each (stock mp_rust's desertshrubs with stock_models on).
        paks = [d for _, d in sorted(written, key=lambda t: t[0]) if "_pak" in d]
    with contextlib.redirect_stdout(io.StringIO()):
        in_file = [i["name"] for i in mw2tex.FastFile.from_zone(zone).images if i["pak"]]
    found = len(in_file)
    if found != len(paks):
        ours = Counter((_name(o) or b"").decode("latin-1").lstrip(",") for o in paks)
        theirs = Counter(n.lstrip(",") for n in in_file)
        raise PortError("%d pak pictures written but %d found in the file (%s)" % (
            len(paks), found, ", ".join(sorted((theirs - ours) | (ours - theirs)))[:300]))
    return [t for o in paks for t in o["_pak"]]


def main(argv):
    ap = argparse.ArgumentParser(description="Convert a PC fastfile to Xbox 360 TU6 layout")
    ap.add_argument("pc_ff", help="the PC map (with --rewrite-stock: a stock 360 file)")
    ap.add_argument("out_ff")
    ap.add_argument("--rewrite-stock", action="store_true",
                    help="test: write the stock 360 file pc_ff back out through the converter's "
                    "writer, converting nothing")
    ap.add_argument("--iwd", nargs="*", default=[], help="the map's .iwd file(s)")
    ap.add_argument("--game", help="folder with the PC game's .iwd files (its main folder)")
    ap.add_argument("--ref360", nargs="*", default=[])
    ap.add_argument("--texture-budget", type=int, default=TEXTURE_BUDGET_MB, metavar="MB",
                    help="picture memory to stay within, in MB (0: no limit; default %(default)s)")
    ap.add_argument("--card-pak", action="store_true",
                    help="titles and emblems from fixed slots in imagefile8.pak (see mw2tex cardpak)")
    ap.add_argument("--teams", nargs=2, metavar=("ALLIES", "AXIS"),
                    help="teams to use (default: from the map's .arena next to the .ff)")
    ap.add_argument("--fix-off", action="append", default=[], metavar="FIX",
                    choices=[k for k, _, _ in FIXES],
                    help="switch a fix off (repeatable): " + ", ".join(k for k, _, _ in FIXES))
    ap.add_argument("--fix-on", action="append", default=[], metavar="FIX",
                    choices=[k for k, _, _ in FIXES],
                    help="switch a fix on that is off by default (repeatable): "
                    + ", ".join(sorted(DEFAULT_OFF)))
    ap.add_argument("--pak-start", type=int, default=STREAM_PAK, metavar="N",
                    help="stream_pictures: the first pak number for the map's pictures (%d-%d; "
                         "later ones follow when a pak is full)" % (STREAM_PAK, STREAM_PAK_LAST))
    ap.add_argument("--profile", nargs="?", const=30, type=int, metavar="N",
                    help="time the conversion: print the N functions taking the most time "
                         "(default 30) and save the full profile next to the output as .prof")
    a = ap.parse_args(argv)
    if a.rewrite_stock:
        rewrite_stock(a.pc_ff, a.out_ff)
        return

    def run():
        port(a.pc_ff, a.out_ff, a.iwd, a.ref360, teams=a.teams, game_iwds=game_iwd_files(a.game),
             texture_budget=a.texture_budget, card_pak=a.card_pak, pak_start=a.pak_start,
             fixes=fix_set({k: True for k in a.fix_on}, off=a.fix_off))

    if a.profile is None:
        run()
        return
    import cProfile
    prof = cProfile.Profile()
    try:
        prof.runcall(run)
    finally:
        print(profile_report(prof, os.path.splitext(a.out_ff)[0] + ".prof", a.profile))


def profile_report(prof, path, n=30):
    """Saves a cProfile.Profile's timings to path and returns the n functions that took the
    most time, as text: by time including what they call, and by their own time."""
    import pstats
    prof.dump_stats(path)
    text = io.StringIO()
    st = pstats.Stats(prof, stream=text)
    st.strip_dirs()
    text.write("\n===== most time including what they call =====\n")
    st.sort_stats("cumulative").print_stats(n)
    text.write("===== most time in the function itself =====\n")
    st.sort_stats("tottime").print_stats(n)
    text.write("full profile: %s (open with python -m pstats)\n" % path)
    return text.getvalue()


if __name__ == "__main__":
    main(sys.argv[1:])
