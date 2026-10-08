# Stock 360 rules ("stock always does")

Rules that hold in every stock 360 multiplayer file checked (16 maps unless said otherwise),
with how they were measured and where the converter stands. Re-mine and check with:

    python scripts/stock_rules.py --stock <folder of stock .ff files> OUT.ff
    python scripts/placement.py OUT.ff <stock map.ff> [more stock maps]

`stock_rules.py` mines value rules (a field that takes one or a few values in every stock asset
of a type), listing rules (stored as its own asset list entry, inline, or by name), name rules
and common_mp clashes. `placement.py` compares which memory block each kind of data loads into
and its alignment. A converted map should break nothing in either; when one breaks, either fix
the converter or write down here why the difference is harmless.

## File layout

| Rule | Evidence | Converter |
|---|---|---|
| Every full shader set (MaterialTechniqueSet) is an asset list entry of its own, before the materials that use it; never written inside a material | 2138 listed, 0 inline (16 maps); mp_invasion lists `effect_zfeather_falloff_add_eyeoffset` at 1142, the car fire using it at 1160 | `list_techsets` / `stock_layout` (test) |
| The map's entities (MapEnts) are written inside the collision map (clipMap_t.mapEnts, an inline temporary asset), never as an entry | 16 inline, 0 listed | `stock_layout` (test); the PC file lists them |
| One-of-a-kind assets in this order: configstrings string tables, com_map, fx_map, lightdef, (materials, shader sets), gfx_map, game_map_mp, col_map_mp, impactfx | identical in all 16 maps | `stock_layout` (test); converted maps had col_map_mp before fx_map and gfx_map (PC order) |
| No two full assets of one type and name in a file | 0 in 16 maps; converted mp_backlot had 65 names twice (team models from two stock maps, prop effects, pixel shaders from shader sets of different maps) | `stock_layout` (test) keeps one copy (the stock one when copies differ) |
| An asset held or pointed at from more than one place is written once; later users point at its slot | stock never repeats a name | the writer wrote a second full copy of an inline temporary asset at its next holder (cardboard box model in an effect and in the collision map); `stock_layout` gives such assets a slot |
| No full copy of anything the always-loaded common_mp.ff has (pictures, materials, models, effects, shader sets, physics presets, raw files); those are named (`,name`) or left to common_mp | 0 in 16 maps for every type except GfxLightDef `light_point_linear`, which every map carries | `name_common_assets` (on): a second copy takes the name over when the map loads (PC mp_backlot's 128x128 `fire_roar_pm_atlas` over common_mp's streamed one) |
| Every pointer points at something already written, never into the temporary block | `check_aliases.py`: 0 forward, 0 temp in stock | `check_aliases.py` before every PR |
| Each kind of data loads into the same block as in stock (headers TEMP, data VIRTUAL; vertices, indices, picture pixels, shader code PHYSICAL) | `placement.py`: 0 differences on converted mp_backlot | holds |

## Per map content

| Rule | Evidence | Converter |
|---|---|---|
| Every map carries the sounds its destructibles' script plays that common_mp lacks (a burning car's `fire_vehicle_med`, `fire_vehicle_flareup_med`) | all 16 maps carry both; common_mp doesn't | `destructible_sounds` (on) |
| Six configstrings string tables per map (`mp/configstrings/configstrings_360_<map>_{dm,sab,sd,war,ctf,koth}.csv`) | all 16 maps | not made (a network optimisation; maps load and play without) |
| A map impact effects table (impactfx) | all 16 maps; code_post_gfx_mp has `default` | not made (bullet impacts fall back to the default table) |
| Material gameFlags are the same on PC and 360 (0x8 sky, 0x4 also flares, foliage, fences, ...) | 3,400 same-named materials in 5 PC IW4x stock maps and their 360 originals: identical | copied as they are |
| A placed model (GfxStaticModelDrawInst) with a ground colour has the ground-lit flag (flags 0x02); none without one | 35,119 of 35,119 (16 maps) | `ground_lit_flag` (on); converted mp_backlot had 2029 models without it: props in shade looked lit |
| Sort keys 0-33 only on materials whose shader set has the lit technique (9); 34 and up only on ones without it (distortion is 43, effects 47-53) | 0 mixed keys in 16 maps + common_mp + code_post_gfx_mp (`tech9.py`-style check: group every loaded material by sort key, look at technique 9) | `sort_key_kind` (on): converted mp_bo2cove's `mc/mtl_p6_cas_rock_foliage_cover_blend`, a lit model with the PC key 43, crashed the console on load: TU6's material sort (0x82406418) compares two materials' technique 9 shaders and reads the second one's without checking it exists (KMODE_EXCEPTION_NOT_HANDLED at 0x82406530, address 0x4C) |
| A material's stateBitsEntry names a valid state for every technique its shader set has | 0 exceptions in stock mp_invasion and converted mp_backlot | holds |
| Composite (merged decal) surfaces' layer data (`tris.vertexLayerData`) stays inside `draw.vertexLayerDataSize`; strides 8–24 bytes per vertex | mp_rust, mp_terminal | holds (Merge decal layers) |
| World pictures: `$outdoor` 512x512 (format 0x28000102), lightmaps primary 0x2800007A / secondary 0x18280086, reflection probes 64x64 cubes with 7 levels | mp_invasion, mp_rust, mp_afghan | holds; converted maps' `$outdoor` can be nearly blank (the PC compile had no outdoor map) |
| GfxWorld scalars inside the stock ranges | 6 maps | holds except `checksum` (small) and `mapVtxChecksum` (0), straight from the IW4x compile; no rendering code found reading them |
| The car fire effect `smoke/car_damage_blacksmoke_fire` and its shader set are identical in every stock map | 14 maps carry it; shader set's techniques identical | converted copy identical in content |

## Not yet matched (open)

- Configstrings tables and an impact table per map.
- `$outdoor` picture content (rain/dust fade under cover).
- World checksums.
