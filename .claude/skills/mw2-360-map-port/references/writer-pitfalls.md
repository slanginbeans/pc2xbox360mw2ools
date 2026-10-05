# Writer pitfalls (mw2ff tree/zone writer)

The writer streams assets in asset-list order; each struct's pointers are streamed before what
they point at. Pointer field values in the tree dicts decide how a pointer is written:
`None` = null, `"follow"` = data follows inline, `"insert"` = a temp asset's slot, anything else
(e.g. `"0x00000001"`) = an alias resolved from the `Ref` in the dict's `"@"` children.

## Making a new alias pointer

```python
r = tree.Ref(1)                 # NOT Ref(0): the value is written as-is until the target is placed
r.target, r.rel, r.t = target, byte_offset, getattr(target, "t", None)
d["@"][("field", ())] = r
d["field"] = "0x00000001"       # NOT "0x00000000": that reads as a null pointer
```

Both mistakes write null pointers with non-zero counts, which crash or corrupt on the console.
Always read the file back and check the pointer resolves to the intended data.

## Forward pointers are fatal

An alias must point at something already written earlier in the zone. Checks:
- after writing, scan every alias (the forward-pointer check script used throughout this work);
- new assets that the world points at (e.g. composite materials) are listed **before** the
  world via `porter.extra_assets`;
- pointers between assets go through the target's asset-list entry (`Ref` target = the
  `AssetEntry`, rel 4), because temp-block data is reused per asset.

## Never alias into the temporary block

Temp-block data (materials, pictures, sounds, model surfaces written inline) is reused for the
next asset, so an alias into the temp block names whatever comes next. A temp asset that other
places point at must be written with `INSERT` (it reserves a slot in the virtual block), and
those places must alias the **slot**. Stock files have no temp aliases at all (mp_rust 0 of
68,943). merge_decals once broke this: a composite material held a picture inline with
`FOLLOWING` (no slot), so 341 later composite pictures and 115 world material-memory entries
aliased stale temp data and the console crashed as mp_waw_castle loaded. The writer now gives a
`_forward` temp asset written inline a slot, and points later pointers at that slot.
`scripts/check_aliases.py OUT.ff` checks both forward and temp aliases; run it on every
converted file before a PR.

## Something must be written before its first pointer, but lives later

Mark the asset `_forward = True`, make sure nothing holds it inline any more (replace inline
holders with alias Refs), and point every Ref at the asset dict itself (not its `InsertSlot`):
the first pointer then writes it in full and the rest resolve to it. A slot only gets a location
when the asset is written at that slot, so Refs to the slot fail after a forward write
(`ZoneError: a pointer refers to <InsertSlot> before it is written`).

## Model lists

Tree node `smodelIndexes` lists may be Refs into a shared array (`rel` = byte offset). Always
read them with the offset applied; ignoring it once produced a wrong conclusion. Lay lists out as
slices of the room's root list (see engine notes).

## Byte order and unions

Converted data is big-endian. Vector unions (`{"union": hex}`) hold big-endian floats; node and
model boxes are centre + half-size, not min/max. The PC side is little-endian.
