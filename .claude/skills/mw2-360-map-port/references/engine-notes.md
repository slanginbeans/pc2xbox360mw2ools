# MW2 TU6 (Xbox 360) engine notes

Facts established by disassembling `default_mp.xex` (TU6) and comparing against the 16 stock
multiplayer maps. Addresses are virtual (image base 0x82000000).

## Contents
- Loaded-world pointers and struct offsets
- Visibility: rooms, portals, culling trees
- Per-model draw step
- Stock rules converted maps must follow
- Diagnostic dvars
- Console memory dumps (XBDM)

## Loaded-world pointers and struct offsets

- `0x83B3CA88`: pointer to the loaded `GfxWorld` (W).
- `0x83B3CBE4`: pointer to `W + 0x220` (the `dpvs` block). Use it as a sanity check.
- GfxWorld (0x2C8 bytes): `+0x34` dpvsPlanes.cellCount, `+0x44` aabbTreeCounts*,
  `+0x48` aabbTrees* (one GfxCellTree pointer per room), `+0x4C` cells*, `+0x218` shadowGeom*,
  `+0x220` dpvs.
- dpvs (`W+0x220`): `+0x00` smodelCount, `+0x30` smodelVisData[3], `+0x48` sortedSurfIndex*,
  `+0x4C` smodelInsts* (36 bytes: centre, half-size, lighting origin), `+0x54` surfacesBounds*
  (32 bytes: centre, half-size, padding), `+0x58` smodelDrawInsts* (44 bytes).
- GfxAabbTree node (40 bytes): `+0` centre, `+0xC` half-size, `+0x18` childCount,
  `+0x1A` surfaceCount, `+0x1C` startSurfIndex, `+0x1E` smodelIndexCount, `+0x20` smodelIndexes*,
  `+0x24` childrenOffset (bytes from this node to its first child).
- GfxStaticModelDrawInst (44 bytes): origin 0, packedAxis[3]+scale 12, model* 28, cullDist 32,
  lightingHandle 34, reflectionProbeIndex 36, primaryLightIndex 37, flags 38,
  firstMtlSkinIndex 39, groundLighting 40.

## Visibility: rooms, portals, culling trees

- Room selection `0x823D9360`: camera room from the BSP (`0x823D3A90`). Outside every room → all
  rooms with the plain frustum; `r_singleCell` → camera room only; otherwise portal walk
  `0x823D8B68` (queue based; dvars `r_portalWalkLimit` 0 = unlimited, `r_portalMinClipArea` 0.02,
  `r_portalMinRecurseDepth` 2, `r_portalBevels` 0.7).
- Each visible room queues a tree walk job (`0x823D86C8`, job 0xA) with the root of
  `aabbTrees[room]`; the walk is `0x8240DDA0`:
  - node box behind any plane → skip the node;
  - wholly in view → mark the node's own model list and surface range;
  - partly in view with children → recurse into children (the node's own list is ignored);
  - partly in view, no children → test each listed model's and surface's own box.
- Walk constants: plane sign vector ±1, thresholds 0.0.
- **Load-time reorder**: when a map loads, the game reorders placed models (grouped by model,
  file order kept inside a group) and renumbers each room's **root** model list in place. Every
  other node's list must be a slice of its room's root list (true for 16,017 of 16,017 stock
  lists) or it keeps stale numbers and names other models → whole models vanish depending on
  which nodes are in view. Fixed by `tree_list_slices`.
- `r_lockPvs 1` freezes the cull frustum and the "visibility origin" (`viewParms+0x2B0`), which
  the per-model draw step also uses for distance — so it doesn't isolate the walk alone.

## Per-model draw step (`0x823ECDD8`)

For each visible model: distance from the visibility origin / scale; cull distance check
(0 = never); detail level from `0x8232E1C0`; lighting cache `0x823F31D0` (≥4096 slots, drops a
model only if every slot was used this frame); then draw-list buckets per detail level.
The game recomputes `groundLighting` and assigns `lightingHandle` at load/run time.
World draw lists: opaque 0x2000, see-through 0x800 (2,048) per view; extras are left out.

## Stock rules converted maps must follow

- Model bone boxes (XBoneInfo): half-size ≥ 0, box = all detail levels' vertices,
  radiusSquared = half-diagonal² (6,466 of 6,466). Fixed by `bone_bounds`.
- Placement flag 0x02 (ground-lit) ⇔ non-zero ground colour (35,119 of 35,119).
- Every tree node's model list is a slice of its room's root list (see above).
- Reflection probes average colour level ~50–90; a map compiled without probes has identical
  stand-in probes that convert too bright (`probe_brightness` scales them to 70).
- Tree node boxes enclose the models they list; placed-model boxes enclose their vertices.
- Stock highs (16 maps): placed models 13,755; world surfaces 15,771; see-through/decal
  surfaces 740; world triangles 326,015; pictures held in the file 42 (24.9 MB);
  models in draw range from a spot ~5,600 mean.

## Diagnostic dvars (typed in the codxe console)

- `cg_drawViewpos 1` — player position for reporting exact spots.
- `r_lockPvs 1` — freeze visibility from the current view; walk around to see what was culled.
- `r_singleCell 1` — draw only the camera's room (no portals).
- `r_forceLod lowest|high` — force a detail level.
- `r_cacheSModelLighting 0` — static model lighting without the cache.
- `r_portalMinClipArea 1`, `r_portalBevels 0` — loosen portal clipping.

## Console memory dumps (XBDM)

The console needs XBDM (xbdm.xex via DashLaunch). XBDM speaks text on TCP 730
(`getmem addr=0x... length=0x...`). `scripts/xbdm_dump.py <console IP>` follows the pointer
chain above and saves world.bin, trees.bin, counts.bin, nodes0.bin, insts.bin, draws.bin,
lists.bin and a manifest. Compare against the converted file, matching placements by their box
records because the game reorders them. Minidumps from crashes hold only the crashing thread's
stack and can't show world data.
