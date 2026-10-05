---
name: mw2-360-map-port
description: Working on the pc2xbox360mw2ools converter that ports PC (IW4x) Modern Warfare 2 maps to Xbox 360 MW2 TU6 (mw2ff/port.py, the mw2port converter page, mw2tex), and debugging converted maps on a Jtag/RGH console with codxe. Use this whenever the user mentions converting or porting MW2 maps to the 360, mw2port, port.py, codxe, a converted map that crashes, freezes, flickers, has vanishing or shiny models, missing textures, bad lighting or low frame rate, imagefile8.pak titles and emblems, stock 360 .ff files, test switches/fixes, console memory dumps or XBDM, even if they don't name the repo.
---

# MW2 PC → Xbox 360 map porting

This repo (`slanginbeans/pc2xbox360mw2ools`) converts PC IW4x map fastfiles into Xbox 360 MW2 TU6
fastfiles. The user plays the results on a real Jtag console through codxe (`_codxe\zone\`), and is
the only one who can test on hardware. Everything you do is offline analysis plus changes that the
user then tries on the console. Treat each console result as expensive: think hard about what a
test will tell you before asking for it.

## Where things are

- `mw2ff/port.py` — the converter. `FIXES` (switch id, label, help text) and `DEFAULT_OFF` define
  every switch; `post_GfxWorld` runs the world fixes in order; `port_map()` is the entry point;
  `map_limits_report` logs a map's measures against the 16 stock maps after writing.
- `mw2ff/mw2port_gui.py` — the converter page (local web UI the user runs from their work folder,
  with Convert / Cancel, switches, picture budget, imagefile8.pak options).
- `mw2ff/tree.py` / `zone.py` — the zone reader and writer. `port.load_tree(path)` reads a
  converted or PC file; `port.load_stock(path)` reads a stock 360 file (cached).
- `mw2ff/decals.py` — Merge decal layers into the ground (composite materials).
- `mw2ff/README.md` — one table row per switch; keep it in step with `FIXES`.
- `mw2tex/` — titles/emblems tooling and `imagefile8.pak`.
- TU6 executable research: an unpacked `default_mp.xex` image (base 0x82000000) with small
  disassembly helpers, kept outside the repo (never commit game files). The codxe source has
  `src/game/iw4/mp_tu6/symbols.h` with known function names.

Read `references/engine-notes.md` before reasoning about how the 360 culls, draws or loads a map,
and `references/writer-pitfalls.md` before writing code that creates or moves pointers.

## How the user likes to work

- Ask before running conversions or anything long unless the request already implies it; never
  ask the user to test on the console without explaining what each outcome would mean.
- New experiments go in as **test switches, off by default** ("add as toggle"). Real fixes that
  bring data in line with stock go in **on by default**. Every switch needs a `FIXES` entry, a
  `DEFAULT_OFF` entry if off, and a README row.
- When the user says "pr and merge": commit on the working branch, push, open a PR against `main`
  (their launcher builds from `main`), and merge it. Commit trailers and PR footers follow the
  session's attribution instructions; never put model names in commits or PRs.
- Explain results in plain language: what was found, what changed, what to try on the console and
  what each result would mean. The user is technical but not a reverse engineer.
- Be honest when a hypothesis fails or an earlier claim was wrong (e.g. a script bug); say so
  plainly and correct it.

## Checking a change offline (before every PR)

Convert the affected maps (at least one small one like mp_ancient and one large one like
mp_backlot or mp_waw_castle) with the relevant switches, then:

1. **Read the file back** with `port.load_tree` — it must parse to the end.
2. **No forward or temp-block aliases**: every alias must point at something already written,
   and never into the temporary block (reused per asset). Both are fatal on the console even
   though the PC-side reader copes. Run `scripts/check_aliases.py OUT.ff` (exit 0 = clean).
3. **Check the specific change** in the read-back data (not the in-memory tree): counts, values,
   that only the intended things changed, and that maps the change shouldn't touch are identical.
4. **Compare against stock**: the 16 stock 360 maps are the ground truth. When unsure what a
   field should hold, measure it across the stock files first and write the rule down.

## Debugging method that works

1. **Get the symptom precise.** Which assets, from where, at what distance, does turning matter,
   does every copy of a model behave alike? Get `cg_drawViewpos 1` coordinates for good and bad
   cases, then find the exact instances in the converted file.
2. **Compare good against bad** field by field (placement, model, box, tree path, lighting,
   material) and against stock-wide rules. Scan for values that stock never has (negative
   half-sizes, NaN, out-of-range).
3. **Use dvars to isolate the stage** before writing code (see engine notes): `r_lockPvs`,
   `r_singleCell`, `r_forceLod`, `r_cacheSModelLighting`, `r_portalMinClipArea`, `r_portalBevels`.
4. **Bisect with test switches** that change one thing (huge boxes on all nodes / leaves only /
   inner only / grown by N, swap two placements' numbers, room visibility off).
5. **When the file looks right but the console misbehaves, read console memory.** Emulating the
   game's code from the file can't see what the game rewrites at load. `scripts/xbdm_dump.py`
   dumps the loaded world (pointer chain in the engine notes) over XBDM; compare it with the
   file. This is how the vanishing-models bug was finally found: the game reorders placed models
   on load and only renumbers each room's root model list.

## Lessons that cost a lot to learn

- Stock 360 data is the reference for every structural choice; PC files compiled by IW4x tools
  often break stock rules (bone boxes with negative half-size, no reflection probes, ground
  colours without the ground-lit flag, model lists outside the root list).
- "Fixes" that make data more correct can still be irrelevant to the symptom; keep them if they
  match stock, but don't stop investigating.
- Distrust your own analysis scripts: a shared array read without its offset once produced a
  completely wrong conclusion. Cross-check surprising results a second way.
- The map measures report (logged after each conversion) shows what's above stock — use it for
  frame-rate questions before guessing.
