# Notice

`iw4_assets.h`, `commands.txt` and `assets/*.txt` are adapted from OpenAssetTools
(https://github.com/Laupetin/OpenAssetTools), files `src/Common/Game/IW4/IW4_Assets.h` and
`src/ZoneCode/Game/IW4/`. OpenAssetTools is licensed under the GNU General Public License v3.0,
so these files are too.

Changes made here: Xbox 360 TU6 layouts and load order for shaders, vertex declarations,
materials, techniques, images, menus, models and model surfaces, sounds, animations, the
sound driver globals asset, map world, collision, glass and water data. Each change was
checked against the TU6 executable's own loading code.

`../defs_pc/` holds the same OpenAssetTools files unchanged (commit f8f54426), for reading PC
(version 276) fastfiles such as IW4x maps.
