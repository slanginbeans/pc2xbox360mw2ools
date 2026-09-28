"""Finds every wall graffiti texture in your MW2 map files and copies just those pictures
into graffiti_all.zip, so you only upload a few MB instead of the whole pak files.

Put this file and mw2tex.py in the folder that has the map .ff files (mp_*.ff) and
imagefile1.pak .. imagefile4.pak, then run:  python cut_graffiti.py
Needs nothing but Python 3. Upload graffiti_all.zip to the project.
"""
import glob, json, os, sys, zipfile, zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import mw2tex
except ImportError:
    sys.exit("mw2tex.py not found. Put it in the same folder as this script.")

maps = sorted(glob.glob("mp_*.ff")) + sorted(glob.glob("airport.ff"))
if not maps:
    sys.exit("No map files (mp_*.ff) in this folder.")
paks = {}
for n in range(1, 5):
    if os.path.exists("imagefile%d.pak" % n):
        paks[n] = open("imagefile%d.pak" % n, "rb")
if not paks:
    sys.exit("No imagefile .pak files in this folder.")

found = {}
with zipfile.ZipFile("graffiti_all.zip", "w") as out:
    for ff_path in maps:
        try:
            ff = mw2tex.FastFile(ff_path)
        except SystemExit as e:
            print("skipping %s: %s" % (ff_path, e))
            continue
        count = 0
        for image in ff.images:
            name = image["name"]
            if "graffiti" not in name.lower() or name.startswith("cardtitle") or not image["pak"]:
                continue
            count += 1
            if name in found:
                found[name]["maps"].append(ff_path[:-3])
                continue
            for lv in sorted(image["levels"], key=lambda l: -l["width"] * l["height"]):
                pak, start, end = ff.table[lv["entry"]]
                if pak not in paks:
                    continue
                paks[pak].seek(start)
                chunk = paks[pak].read(end - start)
                try:
                    zlib.decompress(chunk)
                except zlib.error:
                    continue
                out.writestr(name + ".bin", chunk)
                found[name] = {"maps": [ff_path[:-3]], "format": image["format"], "width": lv["width"],
                               "height": lv["height"], "mips": lv["mips"]}
                break
            else:
                print("could not copy %s (its pak file is missing)" % name)
        print("%s: %d graffiti textures" % (ff_path, count))
    out.writestr("manifest.json", json.dumps(found, indent=1))
print("wrote graffiti_all.zip with %d different graffiti textures" % len(found))
