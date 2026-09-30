# Checking mw2ff against the game

These scripts run the TU6 game's own zone loader on a zone inside an emulator and compare
what it reads with what `zone.py` reads. They need the decrypted TU6 `default_mp` image
(not in the repo; set `TU6_PE` to its path) and the Python packages `unicorn` and `capstone`.

```
python cmp.py <file.ff>            walk with zone.py and the emulator; prints the first difference
python around.py <file.ff> <pos>   reads on both sides around a zone position
python tree.py <hexaddr>           pseudo-code of a Load_ function and everything it calls
python d.py <hexaddr> [count]      plain disassembly
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
