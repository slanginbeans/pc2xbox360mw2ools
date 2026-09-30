"""Asset browser and editor for MW2 Xbox 360 fastfiles, running in your web browser.

Run it in the folder with your .ff files (or double-click mw2ff.bat there):
    python mw2ff_gui.py
Your browser opens the asset browser. Pick a fastfile to see every asset in it (weapons,
sounds, materials, menus, map pieces), open one to see its fields, change numbers, then press
Build. The rebuilt file goes in the mw2ff_out folder; copy it to _codxe\\zone\\ on the console.

For now only values that keep the file the same size can change: numbers, on/off flags, and
names with the same number of letters. Your changes are kept in the mw2ff_changes folder
between runs. Nothing is sent anywhere: the page talks only to this program on your own PC.
"""
import glob
import json
import os
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import codec as codec_mod  # noqa: E402
import mw2ff  # noqa: E402

FOLDER = os.getcwd()
OUT_DIR = os.path.join(FOLDER, "mw2ff_out")
CHANGES_DIR = os.path.join(FOLDER, "mw2ff_changes")
TEXTURE_OUT = os.path.join(FOLDER, "mw2tex_out")
PORT = 8380
MAX_BODY = 1024 * 1024
PAGE_SIZE = 150

lock = threading.Lock()
# The open file: name, decoded document, blobs, container header and the edits made to it.
state = {"file": None, "doc": None, "blobs": None, "container": None, "edits": [], "originals": {}}


# ---------------------------------------------------------------- files and edits

def files():
    names = sorted(os.path.basename(p) for p in glob.glob(os.path.join(FOLDER, "*.ff"))
                   if os.path.basename(p).lower().startswith("mp_") or os.path.splitext(p)[0].lower().endswith("_mp"))
    return {"folder": FOLDER, "files": names, "open": state["file"]}


def changes_path(name):
    return os.path.join(CHANGES_DIR, name + ".json")


def save_edits():
    os.makedirs(CHANGES_DIR, exist_ok=True)
    path = changes_path(state["file"])
    if state["edits"]:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"file": state["file"], "edits": state["edits"]}, fh, indent=1)
    elif os.path.exists(path):
        os.remove(path)


def open_file(name):
    path = os.path.join(FOLDER, os.path.basename(name))
    if not os.path.exists(path):
        raise ValueError("%s isn't in %s" % (name, FOLDER))
    print("Reading %s (big files take up to a minute)..." % name)
    ff, zone = mw2ff.read_fastfile(path)
    doc, blobs = mw2ff.decode(zone)
    state.update(file=os.path.basename(path), doc=doc, blobs=blobs,
                 container=ff.raw[:ff.header_end + 8], edits=[], originals={})
    kept, dropped = 0, 0
    if os.path.exists(changes_path(state["file"])):
        saved = json.load(open(changes_path(state["file"]), encoding="utf-8"))
        for e in saved.get("edits", []):
            try:
                apply_edit(e["asset"], e["rec"], e["path"], e["value"])
                kept += 1
            except (ValueError, KeyError, IndexError, TypeError):
                dropped += 1
    print("Opened %s: %d assets" % (name, len(doc["assets"])))
    return {"file": state["file"], "assets": asset_list(), "kept": kept, "dropped": dropped,
            "textures": os.path.exists(os.path.join(TEXTURE_OUT, state["file"]))}


def asset_list():
    edited = {e["asset"] for e in state["edits"]}
    out = []
    for a in state["doc"]["assets"]:
            out.append({"i": a["index"], "type": a["type"], "name": a["name"], "records": len(a["records"]),
                    "edited": a["index"] in edited})
    return out


def need_open():
    if state["doc"] is None:
        raise ValueError("open a file first")


def records(asset, start):
    need_open()
    a = state["doc"]["assets"][asset]
    out = []
    for n, rec in enumerate(a["records"][start:start + PAGE_SIZE], start):
        r = dict(rec)
        if "blob" in r:
            r = {"k": r["k"], "t": r.get("t"), "n": r.get("n"), "at": r.get("at"), "blob": r["blob"][1]}
        elif "hex" in r and len(r["hex"]) > 128:
            r["hex"] = r["hex"][:128] + "..."
        r.pop("fix", None)
        r["i"] = n
        out.append(r)
    edits = [e for e in state["edits"] if e["asset"] == asset]
    return {"asset": asset, "type": a["type"], "name": a["name"], "total": len(a["records"]),
            "start": start, "records": out, "edits": edits}


def _walk(value, path):
    """(container, key) of the value at path inside a record value."""
    parent, key = None, None
    cur = value
    for p in path:
        parent, key = cur, p
        cur = cur[p]
    return parent, key, cur


def _reread(c, rec, raw):
    """The record's value decoded again from its encoded bytes."""
    t = c.type_by_name(rec["t"])
    desc = ("partial", t) if rec["k"] == "partial" else ("type", t, rec["n"])
    return c.decode(desc, raw, bytearray()).get("v")


def apply_edit(asset, rec_index, path, value):
    """Set one number (path inside the record's "v"), or a string record's text (path == ["s"])."""
    need_open()
    a = state["doc"]["assets"][asset]
    rec = a["records"][rec_index]
    c = codec_mod.Codec(mw2ff.schema_mod.load())
    blobs = state["blobs"].get(asset, bytearray())
    size = len(c.encode(rec, blobs))
    key = (asset, rec_index, json.dumps(path))
    if path == ["s"]:
        if rec["k"] != "string" or "s" not in rec:
            raise ValueError("that isn't a text field")
        if not isinstance(value, str) or len(value.encode("utf-8")) != len(rec["s"].encode("utf-8")) or "\0" in value:
            raise ValueError("for now the new text must have exactly %d letters (the file can't grow yet)"
                             % len(rec["s"].encode("utf-8")))
        original = state["originals"].setdefault(key, rec["s"])
        rec["s"] = value
    else:
        if "v" not in rec or not path:
            raise ValueError("that field can't be changed")
        parent, last, old = _walk(rec["v"], path)
        if isinstance(old, bool) or not isinstance(old, (int, float)) or isinstance(value, bool) \
                or not isinstance(value, (int, float)):
            raise ValueError("only numbers can be changed here")
        if isinstance(old, int):
            if isinstance(value, float) and not value.is_integer():
                raise ValueError("this field holds whole numbers")
            value = int(value)
        else:
            value = float(value)
        original = state["originals"].setdefault(key, old)
        parent[last] = value
        try:
            raw = c.encode(rec, blobs)
            got = _walk(_reread(c, rec, raw), path)[2]
            ok = abs(got - value) <= 1e-6 * max(1.0, abs(value)) if isinstance(value, float) else got == value
        except Exception:
            ok = False
        if not ok:
            parent[last] = old
            raise ValueError("%s doesn't fit in this field" % value)
        value = got
    if len(c.encode(rec, blobs)) != size:
        raise ValueError("that change would change the file size")
    state["edits"] = [e for e in state["edits"]
                      if not (e["asset"] == asset and e["rec"] == rec_index and e["path"] == path)]
    if value != original:
        state["edits"].append({"asset": asset, "rec": rec_index, "path": path, "value": value,
                               "name": a["name"], "type": a["type"], "at": rec.get("at", "")})
    return {"ok": True, "value": value}


def edit(req):
    r = apply_edit(int(req["asset"]), int(req["rec"]), req["path"], req["value"])
    save_edits()
    return r


def revert_all():
    need_open()
    name = state["file"]
    state["edits"] = []
    save_edits()
    return open_file(name)


def build():
    need_open()
    zone = mw2ff.encode(state["doc"], state["blobs"])
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, state["file"])
    mw2ff.write_container(state["container"], zone, out)
    note = ""
    if os.path.exists(os.path.join(TEXTURE_OUT, state["file"])):
        note = ("mw2tex_out also has a %s with texture changes. The console can use only one of them, "
                "so this one doesn't include those textures." % state["file"])
    return {"written": os.path.relpath(out, FOLDER), "edits": len(state["edits"]), "note": note}


def unpack():
    need_open()
    target = os.path.join(FOLDER, "mw2ff_unpacked", os.path.splitext(state["file"])[0])
    mw2ff.unpack(os.path.join(FOLDER, state["file"]), target)
    return {"folder": os.path.relpath(target, FOLDER)}


# ---------------------------------------------------------------- web page

def handle(method, path, query, body):
    try:
        if method == "GET":
            if path in ("/", "/index.html"):
                return 200, PAGE, "text/html; charset=utf-8"
            if path == "/api/files":
                return 200, files(), None
            if path == "/api/assets":
                need_open()
                return 200, {"file": state["file"], "assets": asset_list()}, None
            if path == "/api/records":
                return 200, records(int(query["asset"][0]), int(query.get("start", ["0"])[0])), None
        else:
            req = json.loads(body or b"{}")
            if path == "/api/open":
                return 200, open_file(req["file"]), None
            if path == "/api/edit":
                return 200, edit(req), None
            if path == "/api/revert":
                return 200, revert_all(), None
            if path == "/api/build":
                return 200, build(), None
            if path == "/api/unpack":
                return 200, unpack(), None
        return 404, {"error": "not found"}, None
    except SystemExit as e:
        return 400, {"error": str(e)}, None
    except KeyError as e:
        return 400, {"error": "there's no field called %s" % e}, None
    except (ValueError, IndexError) as e:
        return 400, {"error": str(e)}, None
    except Exception as e:  # keep the page usable and show what broke
        return 400, {"error": "%s: %s" % (type(e).__name__, e)}, None


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>mw2ff asset browser</title>
<style>
:root{--bg:#17191d;--panel:#22252b;--card:#2a2e35;--line:#3a3f48;--text:#e6e8ec;--dim:#9aa1ad;--accent:#d6aa46;--ok:#5cb87a;--bad:#e06c6c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 system-ui,Segoe UI,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;flex-wrap:wrap;gap:10px;align-items:center}
h1{font-size:16px;margin:0 8px 0 0}select,input,button{font:inherit;color:var(--text);background:var(--card);border:1px solid var(--line);border-radius:6px;padding:6px 10px}
button{cursor:pointer}button.primary{background:var(--accent);color:#1b1b1b;border-color:var(--accent);font-weight:600}button:disabled{opacity:.5;cursor:default}
.info{color:var(--dim);font-size:12px;padding:8px 16px}
.layout{display:grid;grid-template-columns:340px 1fr;gap:12px;padding:0 16px 40px;align-items:start}
@media (max-width:760px){.layout{grid-template-columns:1fr}}
aside{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px;position:sticky;top:64px;max-height:calc(100vh - 80px);overflow:auto}
aside input,aside select{width:100%;margin-bottom:6px}
.t{display:block;width:100%;text-align:left;border:0;background:none;padding:5px 8px;border-radius:6px;word-break:break-all}
.t:hover{background:var(--card)}.t.on{background:var(--card);outline:1px solid var(--accent)}
.t small{color:var(--dim);font-size:11px;margin-right:6px}.t.edited{color:var(--ok)}
main{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;min-height:300px}
.rec{border-top:1px solid var(--line);padding:8px 0}.rec h3{margin:0 0 6px;font-size:12px;color:var(--dim);font-weight:400;word-break:break-all}
.rec h3 b{color:var(--text)}
.f{display:grid;grid-template-columns:minmax(120px,240px) 1fr;gap:2px 10px;align-items:center;margin-left:var(--d,0)}
.f>label{color:var(--dim);font-size:12px;word-break:break-all}.ro{color:var(--dim);font-family:ui-monospace,Consolas,monospace;font-size:12px;word-break:break-all}
.f input{width:160px;padding:3px 6px}.f input.changed{border-color:var(--ok);color:var(--ok)}
.sub{grid-column:1/3;color:var(--dim);font-size:12px;margin-top:4px}
.more{margin-top:10px}
#toast{position:fixed;right:16px;bottom:16px;max-width:460px;white-space:pre-wrap;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px;display:none;z-index:20}
#toast.ok{border-color:var(--ok)}#toast.bad{border-color:var(--bad)}
.hint{color:var(--dim)}
</style></head><body>
<header><h1>mw2ff asset browser</h1>
<select id="file"></select><button id="openBtn">Open</button>
<span style="flex:1"></span>
<button id="revertBtn" disabled>Undo all changes</button>
<button id="unpackBtn" disabled title="Writes every asset as files you can read, in mw2ff_unpacked">Unpack to folder</button>
<button id="buildBtn" class="primary" disabled>Build</button></header>
<div class="info" id="info"></div>
<div class="layout"><aside>
<input id="search" placeholder="Search asset names"><select id="type"><option value="">All asset types</option></select>
<div id="list"></div></aside>
<main id="main"><p class="hint">Pick a file at the top and press Open. Big files (common_mp, maps) take up to a minute to read.</p>
<p class="hint">Then pick an asset on the left. Numbers you change are saved right away and go into the file when you press Build.
For now only values that keep the file the same size can change: numbers, on/off flags, and names with the same number of letters.</p></main></div>
<div id="toast"></div>
<script>
const $=s=>document.querySelector(s);let assets=[],current=null,tTimer=null;
function toast(m,k){const t=$("#toast");t.textContent=m;t.className=k||"";t.style.display="block";clearTimeout(tTimer);tTimer=setTimeout(()=>t.style.display="none",k==="bad"?9000:6000)}
async function api(u){const r=await fetch(u);const j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);return j}
async function post(u,b){const r=await fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b||{})});const j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);return j}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
async function loadFiles(){const j=await api("/api/files");$("#info").textContent="Folder: "+j.folder+" · Build writes mw2ff_out\\<file>; copy it to _codxe\\zone\\ on the console.";
 $("#file").innerHTML=j.files.map(f=>`<option ${f===j.open?"selected":""}>${esc(f)}</option>`).join("")||"<option disabled>No multiplayer .ff files in this folder</option>";
 if(j.open)setAssets(await api("/api/assets"))}
function setAssets(j){assets=j.assets;const types=[...new Set(assets.map(a=>a.type))].sort();const sel=$("#type"),keep=sel.value;
 sel.innerHTML='<option value="">All asset types ('+assets.length+')</option>'+types.map(t=>`<option value="${t}">${t} (${assets.filter(a=>a.type===t).length})</option>`).join("");sel.value=types.includes(keep)?keep:"";
 ["#revertBtn","#unpackBtn","#buildBtn"].forEach(s=>$(s).disabled=false);drawList()}
function drawList(){const q=$("#search").value.toLowerCase(),t=$("#type").value;let rows=assets.filter(a=>(!t||a.type===t)&&(!q||a.name.toLowerCase().includes(q)));
 const n=rows.length;rows=rows.slice(0,600);
 $("#list").innerHTML=rows.map(a=>`<button class="t ${a.edited?"edited":""} ${current===a.i?"on":""}" data-i="${a.i}"><small>${a.type}</small>${esc(a.name||"(no name)")}</button>`).join("")+(n>600?`<p class="hint">${n-600} more; search to narrow it down.</p>`:"");
 $("#list").querySelectorAll(".t").forEach(b=>b.onclick=()=>showAsset(+b.dataset.i,0))}
$("#search").oninput=drawList;$("#type").onchange=drawList;
function isNum(v){return typeof v==="number"}
function fields(v,path,depth,rec,edited){
 // returns html rows for a value
 let h="";const pad=`style="--d:${depth*14}px"`;
 if(v&&typeof v==="object"&&!Array.isArray(v)&&!("union" in v)&&!("f32" in v)){
  for(const k of Object.keys(v)){const x=v[k];
   if(x&&typeof x==="object"&&!("union" in x)&&!("f32" in x)&&!(Array.isArray(x)&&x.length<=8&&x.every(isNum))){h+=`<div class="f" ${pad}><div class="sub">${esc(k)}</div></div>`+fields(x,path.concat([k]),depth+1,rec,edited)}
   else h+=`<div class="f" ${pad}><label>${esc(k)}</label><div>${leaf(x,path.concat([k]),rec,edited)}</div></div>`}
  return h}
 if(Array.isArray(v)){
  if(v.length>200)return `<div class="f" ${pad}><label></label><div class="ro">${v.length} values (open the unpacked folder to see them all)</div></div>`;
  v.forEach((x,i)=>{if(x&&typeof x==="object"&&!("union" in x)&&!("f32" in x)){h+=`<div class="f" ${pad}><div class="sub">[${i}]</div></div>`+fields(x,path.concat([i]),depth+1,rec,edited)}
   else h+=`<div class="f" ${pad}><label>[${i}]</label><div>${leaf(x,path.concat([i]),rec,edited)}</div></div>`});return h}
 return `<div class="f" ${pad}><label>value</label><div>${leaf(v,path,rec,edited)}</div></div>`}
function leaf(x,path,rec,edited){
 if(isNum(x)){const key=JSON.stringify([rec,path]);return `<input type="number" step="any" value="${x}" data-rec="${rec}" data-path='${esc(JSON.stringify(path))}' class="${edited.has(key)?"changed":""}">`}
 if(Array.isArray(x))return x.map((y,i)=>leaf(y,path.concat([i]),rec,edited)).join(" ");
 if(x===null)return '<span class="ro">none</span>';
 if(x==="follow")return '<span class="ro">data follows</span>';
 if(typeof x==="object"&&"union" in x)return `<span class="ro">${esc(x.union.slice(0,64))}</span>`;
 if(typeof x==="object"&&"f32" in x)return `<span class="ro">float ${esc(x.f32)}</span>`;
 return `<span class="ro">${esc(String(x).slice(0,96))}</span>`}
async function showAsset(i,start){current=i;drawList();let j;try{j=await api("/api/records?asset="+i+"&start="+start)}catch(e){return toast(e.message,"bad")}
 const edited=new Set(j.edits.map(e=>JSON.stringify([e.rec,e.path])));
 let h=start?"":`<h2 style="margin:0 0 4px;font-size:16px">${esc(j.name||"(no name)")}</h2><p class="hint" style="margin:0 0 8px">${j.type} · ${j.total} pieces of data, in the order the game reads them</p>`;
 for(const r of j.records){const where=r.at?esc(r.at):"<b>"+esc(j.type)+"</b>";
  h+=`<div class="rec"><h3>${where} · ${esc(r.t||r.k)}${r.n>1?" × "+r.n:""}</h3>`;
  if(r.blob!==undefined)h+=`<div class="ro">${r.blob} bytes of data (pictures, vertices, sound and such)</div>`;
  else if(r.k==="string"&&r.s!==undefined){const key=JSON.stringify([r.i,["s"]]);h+=`<input style="width:100%" value="${esc(r.s)}" data-rec="${r.i}" data-path='["s"]' data-text="1" class="${edited.has(key)?"changed":""}">`}
  else if(r.hex!==undefined)h+=`<div class="ro">${esc(r.hex)}</div>`;
  else if(r.v!==undefined){h+=fields(r.v,[],0,r.i,edited)}
  h+="</div>"}
 if(start+j.records.length<j.total)h+=`<button class="more" id="more">Show more (${j.total-start-j.records.length} left)</button>`;
 if(start){$("#more")&&$("#more").remove();$("#main").insertAdjacentHTML("beforeend",h)}else $("#main").innerHTML=h;
 const m=$("#more");if(m)m.onclick=()=>showAsset(i,start+j.records.length);
 $("#main").querySelectorAll("input[data-rec]").forEach(inp=>{if(inp.dataset.bound)return;inp.dataset.bound=1;inp.onchange=async()=>{
  const value=inp.dataset.text?inp.value:Number(inp.value);if(!inp.dataset.text&&(inp.value===""||isNaN(value)))return toast("Type a number","bad");
  try{await post("/api/edit",{asset:current,rec:+inp.dataset.rec,path:JSON.parse(inp.dataset.path),value});inp.classList.add("changed");
   const a=assets.find(a=>a.i===current);if(a&&!a.edited){a.edited=true;drawList()}}
  catch(e){toast(e.message,"bad");showAsset(current,0)}}})}
$("#openBtn").onclick=async()=>{const b=$("#openBtn"),f=$("#file").value;if(!f)return;b.disabled=true;b.textContent="Reading…";
 try{const j=await post("/api/open",{file:f});setAssets(j);current=null;$("#main").innerHTML=`<p class="hint">${j.assets.length} assets. Pick one on the left.</p>`;
  let m="Opened "+j.file+": "+j.assets.length+" assets";if(j.kept)m+="\nKept "+j.kept+" earlier change"+(j.kept>1?"s":"");if(j.dropped)m+="\n"+j.dropped+" earlier change(s) no longer fit and were dropped";
  if(j.textures)m+="\nNote: mw2tex_out has a "+j.file+" with texture changes. The console uses only one copy, and a build here won't include those textures.";toast(m,"ok")}
 catch(e){toast(e.message,"bad")}b.disabled=false;b.textContent="Open"};
$("#buildBtn").onclick=async()=>{const b=$("#buildBtn");b.disabled=true;b.textContent="Building…";
 try{const j=await post("/api/build");toast("Wrote "+j.written+" with "+j.edits+" change"+(j.edits===1?"":"s")+".\nCopy it to _codxe\\zone\\ on your console."+(j.note?"\n\n"+j.note:""),"ok")}catch(e){toast(e.message,"bad")}
 b.disabled=false;b.textContent="Build"};
$("#revertBtn").onclick=async()=>{if(!confirm("Undo every change to this file?"))return;try{const j=await post("/api/revert");setAssets(j);if(current!==null)showAsset(current,0);toast("All changes undone","ok")}catch(e){toast(e.message,"bad")}};
$("#unpackBtn").onclick=async()=>{const b=$("#unpackBtn");b.disabled=true;b.textContent="Unpacking…";try{const j=await post("/api/unpack");toast("Unpacked the original file into "+j.folder,"ok")}catch(e){toast(e.message,"bad")}b.disabled=false;b.textContent="Unpack to folder"};
loadFiles().catch(e=>toast(e.message,"bad"));
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body, kind):
        if isinstance(body, (dict, list)):
            body, kind = json.dumps(body).encode(), "application/json"
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", kind or "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def serve(self, method):
        url = urllib.parse.urlparse(self.path)
        body = b""
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self.reply(400, {"error": "that request is too big"}, None)
            body = self.rfile.read(length)
        if url.path == "/favicon.ico":
            return self.reply(204, b"", "image/x-icon")
        with lock:
            result = handle(method, url.path, urllib.parse.parse_qs(url.query), body)
        self.reply(*result)

    def do_GET(self):
        self.serve("GET")

    def do_POST(self):
        self.serve("POST")


def main():
    port = PORT
    for attempt in range(20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    else:
        sys.exit("couldn't find a free port for the asset browser")
    url = "http://127.0.0.1:%d/" % port
    print("mw2ff asset browser running at %s" % url)
    print("Working folder: %s" % FOLDER)
    print("Leave this window open while you use it. Press Ctrl+C here to stop.")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
