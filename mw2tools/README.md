# mw2tools: every MW2 (Xbox 360, TU6) tool in one window

`mw2tools.bat` opens one browser page with a tab per tool:

- **Textures & tables**: swap pictures, animate titles and emblems, edit tables (see
  `mw2tex/README.md`)
- **Fastfile editor**: every asset in a fastfile, scripts and in-game text (see
  `mw2ff/README.md`)
- **Map converter**: PC (IW4x) maps to the 360, with any two teams you pick (see "Converting
  PC maps" in `mw2ff/README.md`)

## Setting it up

1. Put `mw2tools.bat` in a folder, for example the folder where your old `mw2tex.bat` is.
   (Running `mw2tex.bat` once more also drops it there.)
2. Double-click it. It needs Python and Git, like the other launchers.

Everything except the launcher lives in the `mw2tools` folder next to it:

```
mw2tools.bat                 double-click this
mw2tools\                    your .ff files go here; the tools' output folders appear here too
mw2tools\app\                the tools themselves, updated from GitHub every time you start
mw2tools\old_launchers\      mw2tex.bat, mw2ff.bat, mw2port.bat, moved out of the way
```

The first time, the launcher tidies up the folder it's in. It moves your `.ff` and
`imagefile*.pak` files, the output folders (`mw2tex_out`, `mw2ff_out`, `mw2port_in`,
`mw2port_out` and the others), the `mw2tex_app` download and the old launchers into
`mw2tools`. Nothing is deleted, and nothing else in the folder is touched. Close any
old tool windows first, because Windows can't move files that are in use.

After an update, a newer `mw2tools.bat` replaces itself when you close its window.
