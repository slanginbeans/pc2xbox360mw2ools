# Checking mw2ff against the game

These scripts run the TU6 game's own zone loader on a zone inside an emulator and compare
what it reads with what `zone.py` reads. They need the decrypted TU6 `default_mp` image
(not in the repo; set `TU6_PE` to its path) and the Python packages `unicorn` and `capstone`.

```
python cmp.py <file.ff>            walk with zone.py and the emulator; prints the first difference
python around.py <file.ff> <pos>   reads on both sides around a zone position
python tree.py <hexaddr>           pseudo-code of a Load_ function and everything it calls
python d.py <hexaddr> [count]      plain disassembly
python postload.py <map.ff> [--resident code_post_gfx_mp.ff common_mp.ff]
                                   load the files, then run the game's load-time steps on the
                                   map (material sort, world setup) and check what they did
```

The emulator result for each file is cached in `cache/`, so only the first run is slow.

Useful TU6 addresses (image loads at 0x82000000):

| Address | Function |
| --- | --- |
| 0x821afcb8 | DB_LoadXFile |
| 0x821b0024 | start of the asset list load (script strings, then Load_XAsset per asset) |
| 0x821dc6c8 | Load_XAssetHeader switch (asset type -> Load_ function) |
| 0x821e2e98 | Load_Stream(atStreamStart, ptr, size) |
| 0x821e2d18 / 0x821e2da0 | DB_PushStreamPos / DB_PopStreamPos |
| 0x821e2e28 | DB_AllocStreamPos(alignment - 1) |
| 0x821e2e58 | DB_InsertPointer |
| 0x821b2698 / 0x821b2728 | Load_XString / Load_XStringArray |
| 0x821e2c70 | DB_InitStreams |

Memory blocks on the 360: 0 temp, 1 physical, 2 runtime (not in the file), 3 virtual,
4 large, 5 callback.

## Load-time steps (postload.py, wide.py)

`postload.py` loads the always-loaded files and the map in one emulator, with the asset tables
filled in as DB_AddXAsset does (headers copied out of the temporary block, materials into the
game's own material pool at 0x82922598, named references pointed at what they name), then calls
R_SortMaterials (0x824069f8) and the world setup (0x82405198) and compares the world before and
after. The 360's CPU has 64-bit registers and these steps use them (packed sort keys); unicorn's
64-bit PowerPC can't load or store 64 bits, so `wide.py` keeps each register's high word beside
the 32-bit CPU and runs the functions that need it through Python (only those reachable from the
step that have a 64-bit instruction; the whole image gives the same results, 20 times slower).
Converted mp_waw_castle (Oct 1): 6,381 stale model numbers in 1,019 culling tree lists (the
vanishing models `tree_list_slices` fixed) and 6 surfaces the opaque draw range loses to the sort
(`surface_order`), in 30 seconds.
