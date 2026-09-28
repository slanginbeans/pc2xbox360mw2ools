"""Drag-and-drop texture replacer for MW2 Xbox 360 fastfiles, running in your web browser.

Put this file and mw2tex.py in the folder with your .ff files (and imagefile .pak files), then run:
    python mw2tex_gui.py
Your browser opens the texture picker. Pick a fastfile, drop pictures onto textures (or drop
many pictures at once, named after the textures), then press Build. The rebuilt files go in
the mw2tex_out folder; copy them to _codxe\\zone\\ on the console.

Needs Python 3 and Pillow (python -m pip install pillow). Nothing is sent anywhere: the page
talks only to this program on your own PC.
"""
import glob
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mw2tex  # noqa: E402

try:
    from PIL import Image
except ImportError:
    sys.exit("The texture picker needs Pillow. Install it with:  python -m pip install pillow")

FOLDER = os.getcwd()
OUT_DIR = os.path.join(FOLDER, "mw2tex_out")
WORK_DIR = tempfile.mkdtemp(prefix="mw2tex_gui_")
PORT = 8360
MAX_UPLOAD = 64 * 1024 * 1024

lock = threading.Lock()
state = {"ff_path": None, "ff": None, "images": {}, "pending": {}, "thumbs": {}, "gray": set()}


def can_animate(image):
    """Emblems (square menu pictures up to 128, or ones already laid out as a 512x256 flipbook)
    whose material we can find can become full-size animations."""
    if image["pak"]:
        return False
    lv = image["levels"][0]
    size = (lv["width"], lv["height"])
    if lv["mips"] != 1 or not (size == (512, 256) or (size[0] == size[1] and size[0] <= 128)):
        return False
    return mw2tex.material_of_image(state["ff"], image) is not None


def texture_list():
    rows = []
    for name, image in state["images"].items():
        largest = max(image["levels"], key=lambda l: l["width"] * l["height"])
        rows.append({
            "name": name,
            "format": mw2tex.FORMATS.get(image["format"], ("unsupported",))[0],
            "supported": mw2tex._supported(image),
            "stored": "pak" if image["pak"] else "ff",
            "width": largest["width"],
            "height": largest["height"],
            "can_animate": can_animate(image),
            "gray": name in state["gray"],
        })
    rows.sort(key=lambda r: r["name"])
    return rows


def open_fastfile(filename):
    path = os.path.join(FOLDER, os.path.basename(filename))
    if not os.path.exists(path):
        raise ValueError("%s is not in %s" % (filename, FOLDER))
    ff = mw2tex.FastFile(path)
    images = {}
    for image in ff.images:
        images.setdefault(image["name"], image)
    gray = set()
    for name, image in images.items():
        if image["format"] == 0x12 and not image["pak"]:
            try:
                if mw2tex.gray_alpha_packed(image, mw2tex.decode_texture(ff, image, unpack=False)):
                    gray.add(name)
            except Exception:
                pass
    state.update(ff_path=path, ff=ff, images=images, pending={}, thumbs={}, gray=gray)
    return texture_list()


def thumbnail(name):
    if name in state["thumbs"]:
        return state["thumbs"][name]
    image = state["images"].get(name)
    picture = None
    if image is not None:
        try:
            picture = mw2tex.decode_texture(state["ff"], image, FOLDER, max_side=256)
        except Exception:
            picture = None
    if picture is None:
        return None
    buf = io.BytesIO()
    picture.save(buf, "PNG")
    state["thumbs"][name] = buf.getvalue()
    return state["thumbs"][name]


def save_upload(name, filename, data):
    if name not in state["images"]:
        raise ValueError("no texture named %s" % name)
    ext = os.path.splitext(filename)[1].lower() or ".png"
    if ext not in mw2tex.PICTURE_TYPES:
        raise ValueError("%s isn't a picture type the tool can read" % filename)
    path = os.path.join(WORK_DIR, mw2tex._safe(name) + ext)
    with open(path, "wb") as fh:
        fh.write(data)
    try:
        Image.open(path).verify()
    except Exception:
        os.remove(path)
        raise ValueError("%s couldn't be opened as a picture" % filename)
    frames = mw2tex.frame_count(path)
    state["pending"][name] = {"path": path, "source": filename, "frames": frames, "animate": frames > 1}
    return state["pending"][name]


def build():
    if not state["pending"]:
        raise ValueError("nothing to build yet: drop a picture on a texture first")
    log = []
    os.makedirs(OUT_DIR, exist_ok=True)
    ff_out = os.path.join(OUT_DIR, os.path.basename(state["ff_path"]))
    if os.path.exists(ff_out):
        os.remove(ff_out)  # always rebuild from the stock file, so the pending list is the whole change
    out = mw2tex.Output(state["ff_path"], OUT_DIR)
    for name, item in sorted(state["pending"].items()):
        image = out.ff.find(name)
        if item["animate"] and can_animate(state["images"][name]):
            ok = mw2tex.put_flipbook(out, image, item["path"], WORK_DIR)
            log.append("%s: %s as a 32-frame animation" % (name, item["source"]) if ok
                       else "%s: skipped (format not supported)" % name)
        else:
            ok = out.put(image, item["path"], convert=True)
            log.append("%s: %s" % (name, item["source"]) if ok else "%s: skipped (format not supported)" % name)
    out.save()
    written = [os.path.relpath(ff_out, FOLDER)]
    if out.pak_changed:
        written.append(os.path.relpath(out.pak_path, FOLDER))
    # In a match, emblems and titles come from each map's own copy, so copy the changes into
    # every map file in the folder too.
    if not os.path.basename(state["ff_path"]).lower().startswith("mp_"):
        maps = sorted(glob.glob(os.path.join(FOLDER, "mp_*.ff")))
        for path in maps:
            old = os.path.join(OUT_DIR, os.path.basename(path))
            if os.path.exists(old):
                os.remove(old)  # rebuilt from the stock map, like the file above
        if maps:
            for path in mw2tex.sync_maps(state["ff_path"], ff_out, maps, OUT_DIR, log.append):
                rel = os.path.relpath(path, FOLDER)
                if rel not in written:
                    written.append(rel)
        else:
            log.append("No mp_*.ff map files in this folder, so emblems and titles only change in menus. "
                       "Copy your maps here and build again to see them in matches and killcams.")
    return {"log": log, "written": written, "folder": OUT_DIR}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body, kind="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def fail(self, message):
        self.reply(400, {"error": message})

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        with lock:
            if url.path == "/":
                self.reply(200, PAGE, "text/html; charset=utf-8")
            elif url.path == "/api/files":
                files = sorted(os.path.basename(p) for p in glob.glob(os.path.join(FOLDER, "*.ff")))
                paks = sorted(os.path.basename(p) for p in glob.glob(os.path.join(FOLDER, "imagefile*.pak")))
                self.reply(200, {"folder": FOLDER, "files": files, "paks": paks,
                                 "open": os.path.basename(state["ff_path"]) if state["ff_path"] else None})
            elif url.path == "/api/textures":
                self.reply(200, {"textures": texture_list(), "pending": self.pending()})
            elif url.path == "/api/thumb":
                png = thumbnail(query.get("name", [""])[0])
                if png is None:
                    self.reply(404, b"", "image/png")
                else:
                    self.reply(200, png, "image/png")
            elif url.path == "/api/upload":
                name = query.get("name", [""])[0]
                item = state["pending"].get(name)
                if not item:
                    self.reply(404, b"", "image/png")
                elif item["path"].lower().endswith((".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp")):
                    self.reply(200, open(item["path"], "rb").read(), "application/octet-stream")
                else:  # DDS, TGA: browsers can't show these, so send a PNG copy
                    buf = io.BytesIO()
                    Image.open(item["path"]).convert("RGBA").save(buf, "PNG")
                    self.reply(200, buf.getvalue(), "image/png")
            elif url.path == "/favicon.ico":
                self.reply(204, b"", "image/x-icon")
            else:
                self.reply(404, {"error": "not found"})

    def pending(self):
        return {n: {"source": p["source"], "frames": p["frames"], "animate": p["animate"]}
                for n, p in state["pending"].items()}

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            return self.fail("that file is too big")
        body = self.rfile.read(length)
        with lock:
            try:
                if url.path == "/api/open":
                    textures = open_fastfile(json.loads(body)["file"])
                    self.reply(200, {"textures": textures, "pending": {}})
                elif url.path == "/api/upload":
                    name = query.get("name", [""])[0]
                    filename = query.get("filename", ["picture.png"])[0]
                    save_upload(name, filename, body)
                    self.reply(200, {"pending": self.pending()})
                elif url.path == "/api/option":
                    req = json.loads(body)
                    if req["name"] in state["pending"]:
                        state["pending"][req["name"]]["animate"] = bool(req["animate"])
                    self.reply(200, {"pending": self.pending()})
                elif url.path == "/api/remove":
                    req = json.loads(body)
                    names = list(state["pending"]) if req.get("all") else [req["name"]]
                    for name in names:
                        state["pending"].pop(name, None)
                    self.reply(200, {"pending": self.pending()})
                elif url.path == "/api/build":
                    self.reply(200, build())
                else:
                    self.reply(404, {"error": "not found"})
            except SystemExit as e:
                self.fail(str(e))
            except (ValueError, KeyError) as e:
                self.fail(str(e))
            except Exception as e:  # keep the page usable and show what broke
                self.fail("%s: %s" % (type(e).__name__, e))


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>mw2tex texture picker</title>
<style>
:root{--bg:#17191d;--panel:#22252b;--card:#2a2e35;--line:#3a3f48;--text:#e6e8ec;--dim:#9aa1ad;--accent:#d6aa46;--ok:#5cb87a;--bad:#e06c6c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 system-ui,Segoe UI,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;flex-wrap:wrap;gap:10px;align-items:center}
h1{font-size:16px;margin:0 8px 0 0}select,input,button{font:inherit;color:var(--text);background:var(--card);border:1px solid var(--line);border-radius:6px;padding:6px 10px}
button{cursor:pointer}button.primary{background:var(--accent);color:#1b1b1b;border-color:var(--accent);font-weight:600}button:disabled{opacity:.5;cursor:default}
.chips{display:flex;gap:6px;flex-wrap:wrap}.chip{padding:4px 10px;border-radius:999px}.chip.on{background:var(--accent);color:#1b1b1b;border-color:var(--accent)}
#search{min-width:220px;flex:1}.info{color:var(--dim);font-size:12px;padding:8px 16px}
main{padding:0 16px 40px;display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px;display:flex;flex-direction:column;gap:6px;position:relative}
.card.drag{outline:2px dashed var(--accent)}.card.changed{border-color:var(--ok)}
.pics{display:flex;gap:6px;align-items:center;justify-content:center;height:120px;border-radius:6px;background:repeating-conic-gradient(#30343b 0 25%,#383c44 0 50%) 0 0/16px 16px}
.pics img{max-width:100%;max-height:120px;image-rendering:pixelated;object-fit:contain}.pics.two img{max-width:47%}
.arrow{color:var(--dim)}.name{font-weight:600;word-break:break-all;font-size:13px}.meta{color:var(--dim);font-size:12px}
.row{display:flex;gap:6px;align-items:center;flex-wrap:wrap;font-size:12px}.row button{padding:3px 8px;font-size:12px}
.badge{position:absolute;top:6px;right:6px;background:var(--ok);color:#10210f;font-size:11px;font-weight:700;border-radius:4px;padding:1px 6px}
.none{color:var(--dim);font-size:12px}
#drop{position:fixed;inset:0;background:rgba(214,170,70,.12);border:4px dashed var(--accent);display:none;align-items:center;justify-content:center;font-size:22px;z-index:10;pointer-events:none}
#toast{position:fixed;left:16px;right:16px;bottom:16px;max-width:760px;margin:auto;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;display:none;z-index:20;white-space:pre-wrap}
#toast.bad{border-color:var(--bad)}#toast.ok{border-color:var(--ok)}
.empty{grid-column:1/-1;color:var(--dim);padding:40px 0;text-align:center}
</style></head><body>
<header>
  <h1>mw2tex</h1>
  <select id="file"></select><button id="openBtn">Open</button>
  <input id="search" placeholder="Search textures, e.g. cardicon_ or camo">
  <div class="chips" id="chips"></div>
  <button id="clearBtn">Clear changes</button>
  <button id="buildBtn" class="primary" disabled>Build</button>
</header>
<div class="info" id="info">Loading…</div>
<main id="grid"></main>
<div id="drop">Drop pictures named after textures to replace them all at once</div>
<div id="toast"></div>
<input type="file" id="picker" accept="image/*,.dds,.tga" hidden>
<script>
const $=s=>document.querySelector(s);let textures=[],pending={},filter="all",pickFor=null,shown=0;
const FILTERS=[["all","All"],["cardicon_","Emblems"],["cardtitle_","Titles"],["camo","Camos"],["changed","Changed"]];
const LIMIT=400;
function toast(msg,kind){const t=$("#toast");t.textContent=msg;t.className=kind||"";t.style.display="block";clearTimeout(t._h);t._h=setTimeout(()=>t.style.display="none",kind==="bad"?9000:6000)}
async function api(path,opts){const r=await fetch(path,opts);const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||("request failed: "+r.status));return j}
function chips(){$("#chips").innerHTML="";for(const[k,l]of FILTERS){const b=document.createElement("button");b.className="chip"+(filter===k?" on":"");b.textContent=l+(k==="changed"?" ("+Object.keys(pending).length+")":"");b.onclick=()=>{filter=k;render()};$("#chips").appendChild(b)}}
function visible(){const q=$("#search").value.trim().toLowerCase();return textures.filter(t=>{if(filter==="changed"&&!pending[t.name])return false;if(filter!=="all"&&filter!=="changed"&&!t.name.includes(filter))return false;return !q||t.name.toLowerCase().includes(q)})}
function render(){chips();const g=$("#grid");g.innerHTML="";const list=visible();shown=Math.min(list.length,LIMIT);
 $("#buildBtn").disabled=!Object.keys(pending).length;$("#buildBtn").textContent="Build"+(Object.keys(pending).length?" ("+Object.keys(pending).length+")":"");
 if(!textures.length){g.innerHTML='<div class="empty">Pick a fastfile above and press Open.</div>';return}
 if(!list.length){g.innerHTML='<div class="empty">No textures match.</div>';return}
 for(const t of list.slice(0,LIMIT))g.appendChild(card(t));
 if(list.length>LIMIT){const d=document.createElement("div");d.className="empty";d.textContent="Showing "+LIMIT+" of "+list.length+". Type in the search box to narrow it down.";g.appendChild(d)}}
function card(t){const c=document.createElement("div");c.className="card"+(pending[t.name]?" changed":"");const p=pending[t.name];
 const pics=document.createElement("div");pics.className="pics"+(p?" two":"");
 const orig=new Image();orig.loading="lazy";orig.alt="";orig.src="/api/thumb?name="+encodeURIComponent(t.name);orig.onerror=()=>{orig.replaceWith(Object.assign(document.createElement("span"),{className:"none",textContent:t.stored==="pak"?"no preview (pak file missing)":"no preview"}))};pics.appendChild(orig);
 if(p){pics.appendChild(Object.assign(document.createElement("span"),{className:"arrow",textContent:"→"}));const n=new Image();n.src="/api/upload?name="+encodeURIComponent(t.name)+"&v="+encodeURIComponent(p.source);pics.appendChild(n);c.appendChild(Object.assign(document.createElement("div"),{className:"badge",textContent:"NEW"}))}
 c.appendChild(pics);c.appendChild(Object.assign(document.createElement("div"),{className:"name",textContent:t.name}));
 c.appendChild(Object.assign(document.createElement("div"),{className:"meta",textContent:t.width+"x"+t.height+" "+t.format+" · "+(t.stored==="ff"?"in the .ff":"pak texture")+(t.gray?" · shows in gray only":"")}));
 const row=document.createElement("div");row.className="row";
 if(!t.supported){row.appendChild(Object.assign(document.createElement("span"),{className:"none",textContent:"This format can't be replaced yet"}))}
 else{const b=document.createElement("button");b.textContent=p?"Change":"Choose picture";b.onclick=()=>{pickFor=t.name;$("#picker").click()};row.appendChild(b);
  if(p){const u=document.createElement("button");u.textContent="Undo";u.onclick=()=>remove(t.name);row.appendChild(u);
   if(p.frames>1&&t.can_animate){const l=document.createElement("label");const cb=document.createElement("input");cb.type="checkbox";cb.checked=p.animate;cb.onchange=()=>option(t.name,cb.checked);l.append(cb," Animate ("+p.frames+" frames → 32)");row.appendChild(l)}
   else if(p.frames>1){row.appendChild(Object.assign(document.createElement("span"),{className:"none",textContent:"Animated file: only the first frame is used here"}))}}}
 c.appendChild(row);
 if(t.supported){c.ondragover=e=>{e.preventDefault();e.stopPropagation();c.classList.add("drag")};c.ondragleave=()=>c.classList.remove("drag");
  c.ondrop=e=>{e.preventDefault();e.stopPropagation();c.classList.remove("drag");$("#drop").style.display="none";const f=e.dataTransfer.files[0];if(f)upload(t.name,f).then(()=>{render();toast("Queued "+f.name+" for "+t.name,"ok")}).catch(err=>toast(err.message,"bad"))}}
 return c}
async function upload(name,file){const j=await api("/api/upload?name="+encodeURIComponent(name)+"&filename="+encodeURIComponent(file.name),{method:"POST",body:file});pending=j.pending}
async function remove(name){pending=(await api("/api/remove",{method:"POST",body:JSON.stringify({name})})).pending;render()}
async function option(name,animate){pending=(await api("/api/option",{method:"POST",body:JSON.stringify({name,animate})})).pending;render()}
$("#picker").onchange=async e=>{const f=e.target.files[0];e.target.value="";if(!f||!pickFor)return;try{await upload(pickFor,f);render();toast("Queued "+f.name+" for "+pickFor,"ok")}catch(err){toast(err.message,"bad")}};
async function loadFiles(){const j=await api("/api/files");const s=$("#file");s.innerHTML="";if(!j.files.length){s.innerHTML="<option>no .ff files here</option>";$("#info").textContent="No .ff files in "+j.folder+". Put your fastfiles (like ui_mp.ff) in that folder and reload this page.";return}
 for(const f of j.files){const o=document.createElement("option");o.textContent=f;s.appendChild(o)}s.value=j.open||(j.files.includes("ui_mp.ff")?"ui_mp.ff":j.files[0]);
 $("#info").textContent="Folder: "+j.folder+" · pak files here: "+(j.paks.join(", ")||"none (pak textures show no preview)");if(j.open){const t=await api("/api/textures");textures=t.textures;pending=t.pending;render()}else render()}
$("#openBtn").onclick=async()=>{$("#openBtn").disabled=true;$("#openBtn").textContent="Opening…";try{const j=await api("/api/open",{method:"POST",body:JSON.stringify({file:$("#file").value})});textures=j.textures;pending=j.pending;filter="all";render();toast("Opened "+$("#file").value+": "+textures.length+" textures","ok")}catch(e){toast(e.message,"bad")}$("#openBtn").disabled=false;$("#openBtn").textContent="Open"};
$("#search").oninput=()=>render();
$("#clearBtn").onclick=async()=>{if(!Object.keys(pending).length)return;if(!confirm("Clear all queued replacements?"))return;pending=(await api("/api/remove",{method:"POST",body:JSON.stringify({all:true})})).pending;render()};
$("#buildBtn").onclick=async()=>{const b=$("#buildBtn");b.disabled=true;b.textContent="Building…";try{const j=await api("/api/build",{method:"POST",body:"{}"});toast("Built:\n"+j.log.join("\n")+"\n\nWrote "+j.written.join(" and ")+".\nCopy "+(j.written.length>1?"them":"it")+" to _codxe\\zone\\ on your console.","ok")}catch(e){toast(e.message,"bad")}render()};
let depth=0;window.addEventListener("dragenter",e=>{if(e.dataTransfer.types.includes("Files")&&textures.length){depth++;$("#drop").style.display="flex"}});
window.addEventListener("dragleave",()=>{if(--depth<=0){depth=0;$("#drop").style.display="none"}});
window.addEventListener("dragover",e=>e.preventDefault());
window.addEventListener("drop",async e=>{e.preventDefault();depth=0;$("#drop").style.display="none";if(!textures.length)return;const names=new Set(textures.filter(t=>t.supported).map(t=>t.name.toLowerCase()));const byLower={};for(const t of textures)byLower[t.name.toLowerCase()]=t.name;
 const ok=[],miss=[];for(const f of e.dataTransfer.files){const stem=f.name.replace(/\.[^.]+$/,"").toLowerCase();if(names.has(stem)){try{await upload(byLower[stem],f);ok.push(byLower[stem])}catch(err){miss.push(f.name+" ("+err.message+")")}}else miss.push(f.name)}
 render();toast((ok.length?"Queued "+ok.length+": "+ok.join(", "):"Nothing matched.")+(miss.length?"\nNot matched (name the file after the texture, or drop it on a card): "+miss.join(", "):""),miss.length&&!ok.length?"bad":"ok")});
loadFiles().catch(e=>toast(e.message,"bad"));
</script></body></html>
"""


def main():
    port = PORT
    for attempt in range(20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    else:
        sys.exit("couldn't find a free port for the texture picker")
    url = "http://127.0.0.1:%d/" % port
    print("mw2tex texture picker running at %s" % url)
    print("Working folder: %s" % FOLDER)
    print("Leave this window open while you use it. Press Ctrl+C here to stop.")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(WORK_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
