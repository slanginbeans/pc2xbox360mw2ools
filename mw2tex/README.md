# mw2tex: swap MW2 textures on Xbox 360 (TU6)

mw2tex changes pictures inside MW2's game files: emblems, calling card titles, camos, menu
pictures and map textures. You run it on your PC, then copy the files it makes to your console.
codxe loads them from `_codxe\zone\` instead of the stock files, so deleting them undoes everything.

There are two ways to use it:

- **The texture picker** (`mw2tex_gui.py`): a page in your web browser where you drag pictures onto
  textures. Start here.
- **Commands** (`mw2tex.py`): typed in PowerShell. Useful for batches and anything the picker doesn't do.

For tables (calling card titles, unlocks, challenges...), the picker's **Tables** tab is an editor, and
`mw2zone.py` does the same from PowerShell. See section 6.

---

## 1. One-time setup

1. **Install Python 3** from https://www.python.org/downloads/. On the first installer screen, tick
   **"Add python.exe to PATH"**.
2. **Install Pillow** (the picture library mw2tex uses). Open PowerShell and run:
   ```
   python -m pip install pillow
   ```
3. **Get the tool files.** Either:
   - download the repository:
     ```
     git clone https://github.com/slanginbeans/codxe_modified.git codxe_textures
     ```
     The tools are in `codxe_textures\tools\mw2tex\`. Later, run `git pull` in `codxe_textures` to get updates.
   - or just download `mw2tex.py`, `mw2tex_gui.py`, `mw2zone.py` and `mw2zone_gui.py` into one folder.
4. **Make a work folder** (for example `C:\mw2mods`) and copy into it:
   - `mw2tex.py`, `mw2tex_gui.py`, `mw2zone.py` and `mw2zone_gui.py`
   - the game files you want to change, copied from your console with FTP:
     - `ui_mp.ff` for emblems and calling card titles
     - `common_mp.ff` for camos
     - `code_post_gfx_mp.ff`, which holds the tables that say which title and emblem uses which picture
     - `mp_*.ff`, every map you play on. Needed for emblems and titles too: in a match (scoreboard, killcam)
       the game uses each map's own copy of those pictures, not the one in `ui_mp.ff`.
   - optional: `imagefile1.pak` to `imagefile4.pak`. These are only needed to *preview* camo and map
     textures. Replacing them works without the pak files.

## 2. Using the texture picker

1. Open your work folder in File Explorer, click the address bar, type `powershell` and press Enter.
2. Run:
   ```
   python mw2tex_gui.py
   ```
   Your browser opens the picker. Leave the PowerShell window open while you use it.
3. Pick a file at the top (for example `ui_mp.ff`) and press **Open**.
4. Find textures with the search box or the **Emblems / Titles / Camos** buttons. **Unused** shows the
   title and emblem pictures no title or emblem uses. With `code_post_gfx_mp.ff` in the folder, each
   title and emblem card says which ones use it.
5. **Replace one texture:** drag a picture (PNG, JPG, GIF, DDS...) onto its card, or press
   **Choose picture**. The card shows the old picture, an arrow and the new one.
   **Shared pictures:** many titles share one picture (all 42 "Expert" weapon titles use
   `cardtitle_assault_expert`). When you drop a picture on a shared one, the picker asks whether it's for
   all of them or only one. For only one, it puts your picture on an unused title picture and changes
   that title's table row to use it, so the others keep the old picture. **Undo** puts the row back too.
   **Pictures from the web:** press **From link** on a card and paste a picture's address (right-click a
   picture on a website, then "Copy image address"), or drag a picture from another browser tab onto a card.
   **Swap pictures without a new file:** on a title or emblem card, **Use another picture** makes one (or all)
   of its titles show a picture already in the game, like an unused one. **Show on a title** does it the
   other way round: pick which title should show this card's picture. Only the table changes, so this
   needs just `codxe_patch_mp.ff` on the console. **Undo** on the card puts it back.
6. **Replace many at once:** name each picture after the texture it replaces (for example
   `cardicon_bear.png`, `cardtitle_bloodsplat.jpg`) and drop them all anywhere on the page. The picker
   tells you which names didn't match.
7. **Animated emblems:** drop an animated GIF on an emblem. The **Animate** box is ticked for you. On
   Build, the emblem becomes a 32-frame animation at full 64x64 size (see section 4). Untick the box to
   use only the first frame.
8. Press **Undo** on a card to drop that change, or **Changed** to see everything you've queued.
9. Press **Build**. The picker writes the new files into a `mw2tex_out` folder inside your work folder,
   including `codxe_patch_mp.ff` when a table changed.
   When map files (`mp_*.ff`) are in your work folder, Build also puts your emblem and title changes
   into each of them, so they show in matches and killcams too. This takes a few seconds per map.
10. Copy everything in `mw2tex_out` to your console (section 3).

Each Build starts from the stock file and applies every queued change, so what's on the
**Changed** list is exactly what you get.

## 3. Putting the files on the console

1. With FTP, open the MW2 game folder (the one with `default_mp.xex`), then `_codxe`.
2. If there's no `zone` folder, make one (in FileZilla: right-click, then "Create directory"). Name it `zone`, all lowercase.
3. Copy the built files into `_codxe\zone\`, for example `_codxe\zone\ui_mp.ff`. If Build also made
   `imagefile5.pak` (it does when you change camos or map textures), copy that too.
4. Start MW2 with codxe.

To undo, delete the file from `_codxe\zone\`.

## 4. Good to know

- **Sizes are automatic.** Your picture is stretched to the game texture's size, so use the same shape
  for best results. Emblems are square (64x64); calling card titles are 240x48.
- **Gray-only textures.** Some pictures are stored as gray plus transparency: locked perk icons,
  `cardicon_skull_black`, `cardicon_electro`, `cardicon_simplegun`, `cardicon_skullnbones`,
  `cardtitle_camo_arctic`, `cardtitle_camo_digital` and `cardtitle_swordmaster_2`. The picker marks
  them "shows in gray only". mw2tex converts your picture to match, so it shows up in gray in game.
- **How animated emblems work.** An animated emblem is a flipbook: one 512x256 picture holding 32 frames
  of 64x64 in 4 rows of 8, read left to right, top to bottom. The emblem's material says how many
  rows and columns to cut. The game shows every frame for the same short time, so the speed is fixed.
  A GIF with fewer frames repeats each one to fill 32 slots (a 4-frame GIF shows each frame 8 times).
  Stock animated emblems: `cardicon_prestige10`, `cardicon_prestige10_02` and `cardicon_iw`. That last
  one uses the texture `cardicon_nvg_star`.
- **Emblems and titles in matches.** Menus use `ui_mp.ff`; in a match the game uses the copy inside each map
  file. So copy every map's `mp_*.ff` into your work folder once, and Build (or the `maps` command) updates them
  all. Maps you leave out keep the stock pictures. An animated emblem shows its first frame in matches for now.
- **Pak textures.** Camos on guns and map textures live in `imagefile*.pak`. Changed ones go in a new
  `imagefile5.pak`, which codxe loads from `_codxe\zone\`. Build adds to an existing `imagefile5.pak` in
  `mw2tex_out` rather than replacing it, so it can grow over time. Delete it and rebuild if it gets big.
- **Formats that can't be replaced yet** are marked on their card. Most textures are DXT1, DXT3 or DXT5,
  which all work.

## 5. Commands

Run these in PowerShell in your work folder. `out` is the folder the new files go in. Running more
commands with the same `out` folder keeps the earlier changes.

| What | Command |
| --- | --- |
| List every texture in a file (writes a .csv) | `python mw2tex.py list ui_mp.ff` |
| Show which titles/emblems share a picture, and the unused ones | `python mw2tex.py pictures ui_mp.ff code_post_gfx_mp.ff` |
| Export every table as .csv (into `tables`) | `python mw2tex.py tables code_post_gfx_mp.ff` |
| Pack edited tables into `codxe_patch_mp.ff` | `python mw2tex.py buildtables tables` |
| Export a texture as DDS | `python mw2tex.py extract ui_mp.ff . cardicon_bear` |
| Replace one texture | `python mw2tex.py put ui_mp.ff cardicon_bear bear.png out` |
| Replace many (pictures named after textures, in folder `pics`) | `python mw2tex.py replace ui_mp.ff pics out` |
| Turn an emblem into a full-size animation | `python mw2tex.py flipbook ui_mp.ff cardicon_expert_ak47 dance.gif out` |
| Resize a texture, then put a picture on it | `python mw2tex.py grow ui_mp.ff cardicon_bear 512 256 sheet.png out` |
| Copy changed emblems/titles into every `mp_*.ff` in the folder | `python mw2tex.py maps ui_mp.ff out` |
| Set how many frames an emblem plays | `python mw2tex.py animate ui_mp.ff cardicon_bear 4 8 out` |

`python mw2tex.py` with nothing after it prints the full help, including how the file format works.

## 6. Tables

The game keeps lists like calling card titles, emblems, unlocks and challenges in string tables
(`.csv` files packed inside the fastfiles). The multiplayer ones are in `code_post_gfx_mp.ff`.

### The table editor

1. Put `code_post_gfx_mp.ff` in your work folder, next to the four tool files.
2. Start the texture picker (`python mw2tex_gui.py`) and click **Tables** at the top. Or run
   `python mw2zone_gui.py` for the table editor on its own; it doesn't need Pillow but shows no pictures.
3. Pick `code_post_gfx_mp.ff`, press **Open**, and click a table on the left, for example
   `mp/cardtitletable.csv`.
4. Type in the box above the table to find rows (like `famas`), then click a cell and change it.
   Changed cells turn green; hover one to see the game's value. Each column suggests the values
   already used in it, so for titles you can pick a picture from the list. Cells that name a title or
   emblem picture show it (from `ui_mp.ff`, including a new picture you queued on the Textures tab).
5. Press **Build**. It writes `mw2tex_out\codxe_patch_mp.ff` with every table you changed, plus any
   pictures queued on the Textures tab. Copy them to `_codxe\zone\` on the console.

Your changes are kept in that file: next time you open the editor, it loads them back, so building
again keeps your earlier edits. **Undo this table** or **Undo all** puts tables back to the game's version.

### From PowerShell

1. Export the tables from a game file. They're saved under a `tables` folder, keeping the game's names:
   ```
   python mw2zone.py tables code_post_gfx_mp.ff
   ```
2. Keep only the tables you want to change in the `tables` folder and edit them in Excel or Notepad.
   Keep the folder layout, for example `tables\mp\cardTitleTable.csv`.
3. Build them into one file:
   ```
   python mw2zone.py build tables
   ```
4. Copy `codxe_patch_mp.ff` to `_codxe\zone\` on the console. codxe loads it after the game's own patch file,
   so your tables replace the stock ones with the same name. Delete it to undo.

## 7. If something goes wrong

- **"python is not recognized"**: Python isn't on PATH. Reinstall it and tick "Add python.exe to PATH".
- **"needs Pillow"**: run `python -m pip install pillow`.
- **The picker page doesn't open**: copy the address printed in PowerShell (like `http://127.0.0.1:8360/`)
  into your browser.
- **The game freezes or errors while loading**: delete the file you added in `_codxe\zone\` and tell
  Claude which texture you changed and how.
