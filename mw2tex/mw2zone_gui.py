"""Table editor for MW2 Xbox 360 fastfiles, running in your web browser.

Put this file, mw2zone.py and mw2tex.py in the folder with your .ff files, then run:
    python mw2zone_gui.py
Your browser opens the table editor. Pick a fastfile that has tables (code_post_gfx_mp.ff has
the multiplayer ones), click a table, change cells, then press Build. The result is
mw2tex_out\\codxe_patch_mp.ff; copy it to _codxe\\zone\\ on the console.

The texture picker (mw2tex_gui.py) shows this page too, under Tables at the top.
Only Python 3 is needed. Nothing is sent anywhere: the page talks only to this program on your PC.
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
import mw2tex  # noqa: E402
import mw2zone  # noqa: E402

FOLDER = os.getcwd()
OUT_DIR = os.path.join(FOLDER, "mw2tex_out")
OUT_NAME = "codxe_patch_mp.ff"
PORT = 8370
MAX_BODY = 16 * 1024 * 1024

lock = threading.Lock()
# tables: name -> {"rows", "columns", "unresolved"} from the open fastfile.
# edits: name -> rows, for every table that will go into the built file. Kept when another
# fastfile is opened, and filled from an earlier build on first use, so building again never
# drops a table you changed before.
state = {"ff_path": None, "tables": {}, "edits": {}, "loaded_build": False}


def out_path():
    return os.path.join(OUT_DIR, OUT_NAME)


def load_previous_build():
    if state["loaded_build"]:
        return []
    state["loaded_build"] = True
    if not os.path.exists(out_path()):
        return []
    names = []
    for table in mw2zone.read_tables(mw2tex.FastFile(out_path()).zone):
        state["edits"].setdefault(table["name"], table["rows"])
        names.append(table["name"])
    return names


def summary():
    tables = [{"name": name, "rows": len(t["rows"]), "columns": t["columns"],
               "unresolved": t["unresolved"], "changed": name in state["edits"]}
              for name, t in sorted(state["tables"].items())]
    # Tables changed earlier that the open fastfile doesn't have still go into the build.
    for name in sorted(state["edits"]):
        if name not in state["tables"]:
            rows = state["edits"][name]
            tables.append({"name": name, "rows": len(rows), "columns": len(rows[0]) if rows else 0,
                           "unresolved": 0, "changed": True, "elsewhere": True})
    return {"file": os.path.basename(state["ff_path"]) if state["ff_path"] else None,
            "tables": tables, "changed": sorted(state["edits"]),
            "built": os.path.exists(out_path())}


def open_fastfile(filename):
    path = os.path.join(FOLDER, os.path.basename(filename))
    if not os.path.exists(path):
        raise ValueError("%s is not in %s" % (filename, FOLDER))
    found = mw2zone.read_tables(mw2tex.FastFile(path).zone)
    tables = {}
    for table in found:
        tables.setdefault(table["name"], table)
    state.update(ff_path=path, tables=tables)
    restored = load_previous_build()
    result = summary()
    result["restored"] = restored
    return result


def get_table(name):
    stock = state["tables"].get(name)
    edited = state["edits"].get(name)
    if stock is None and edited is None:
        raise ValueError("no table named %s" % name)
    rows = edited if edited is not None else stock["rows"]
    return {"name": name, "rows": rows, "original": stock["rows"] if stock else None,
            "unresolved": stock["unresolved"] if stock else 0}


def set_table(name, rows):
    if not isinstance(rows, list) or not rows or not all(isinstance(r, list) for r in rows):
        raise ValueError("a table needs at least one row")
    columns = max(len(r) for r in rows)
    rows = [[str(v) for v in r] + [""] * (columns - len(r)) for r in rows]
    for row in rows:
        for value in row:
            try:
                value.encode("latin1")
            except UnicodeEncodeError:
                raise ValueError("%r has a character the game can't store; use plain letters" % value)
    stock = state["tables"].get(name)
    if stock is not None and rows == stock["rows"]:
        state["edits"].pop(name, None)  # back to stock, so it doesn't need to be in the build
    else:
        state["edits"][name] = rows
    return summary()


def revert(name=None):
    if name is None:
        state["edits"].clear()
    else:
        state["edits"].pop(name, None)
    return summary()


def build():
    if not state["edits"]:
        raise ValueError("nothing to build yet: change a cell first")
    tables = []
    log = []
    for name, rows in sorted(state["edits"].items()):
        columns = max(len(r) for r in rows)
        tables.append((name, rows, columns))
        stock = state["tables"].get(name)
        if stock is not None and len(stock["rows"]) == len(rows):
            changed = sum(a != b for r1, r2 in zip(stock["rows"], rows) for a, b in zip(r1, r2))
            log.append("%s: %d cell%s changed" % (name, changed, "" if changed == 1 else "s"))
        else:
            log.append("%s: %d rows" % (name, len(rows)))
    os.makedirs(OUT_DIR, exist_ok=True)
    mw2zone.write_fastfile(out_path(), mw2zone.build_zone(tables))
    return {"log": log, "written": [os.path.relpath(out_path(), FOLDER)], "folder": OUT_DIR}


# Tables whose column holds a picture (UI material) name, and which column.
PICTURE_COLUMNS = {"mp/cardtitletable.csv": 2, "mp/cardicontable.csv": 1}
TABLES_FILE = "code_post_gfx_mp.ff"


def ensure_open():
    """Opens code_post_gfx_mp.ff when no table file is open yet. True when tables are available."""
    if state["ff_path"] is None and os.path.exists(os.path.join(FOLDER, TABLES_FILE)):
        open_fastfile(TABLES_FILE)
    return state["ff_path"] is not None


def current_rows(name):
    if name in state["edits"]:
        return state["edits"][name]
    table = state["tables"].get(name)
    return table["rows"] if table else None


def picture_uses():
    """{material name (lowercase): [{"table", "row", "column", "id"}]} for every table row that
    shows a picture, with your edits applied."""
    uses = {}
    for name, column in PICTURE_COLUMNS.items():
        for index, row in enumerate(current_rows(name) or []):
            if column < len(row) and row[column]:
                uses.setdefault(row[column].lower(), []).append(
                    {"table": name, "row": index, "column": column, "id": row[0]})
    return uses


def set_cell(name, row, column, value):
    rows = [list(r) for r in current_rows(name)]
    rows[row][column] = value
    set_table(name, rows)


def files():
    names = sorted(os.path.basename(p) for p in glob.glob(os.path.join(FOLDER, "*.ff")))
    default = state["ff_path"] and os.path.basename(state["ff_path"])
    if not default:
        default = "code_post_gfx_mp.ff" if "code_post_gfx_mp.ff" in names else (names[0] if names else None)
    return {"folder": FOLDER, "files": names, "default": default}


def handle(method, path, query, body):
    """Answers one request for the table page. Returns (status, body, content type), or None when
    PATH isn't one of this page's addresses. The texture picker calls this for /tables."""
    if not (path == "/tables" or path.startswith("/tables/")):
        return None
    try:
        if method == "GET":
            if path in ("/tables", "/tables/"):
                return 200, PAGE, "text/html; charset=utf-8"
            if path == "/tables/api/files":
                return 200, files(), None
            if path == "/tables/api/list":
                return 200, summary(), None
            if path == "/tables/api/table":
                return 200, get_table(query.get("name", [""])[0]), None
        else:
            req = json.loads(body or b"{}")
            if path == "/tables/api/open":
                return 200, open_fastfile(req["file"]), None
            if path == "/tables/api/save":
                return 200, set_table(req["name"], req["rows"]), None
            if path == "/tables/api/revert":
                return 200, revert(None if req.get("all") else req["name"]), None
            if path == "/tables/api/build":
                return 200, build(), None
        return 404, {"error": "not found"}, None
    except SystemExit as e:
        return 400, {"error": str(e)}, None
    except (ValueError, KeyError) as e:
        return 400, {"error": str(e)}, None
    except Exception as e:  # keep the page usable and show what broke
        return 400, {"error": "%s: %s" % (type(e).__name__, e)}, None


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>mw2tex table editor</title>
<style>
:root{--bg:#17191d;--panel:#22252b;--card:#2a2e35;--line:#3a3f48;--text:#e6e8ec;--dim:#9aa1ad;--accent:#d6aa46;--ok:#5cb87a;--bad:#e06c6c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 system-ui,Segoe UI,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;flex-wrap:wrap;gap:10px;align-items:center}
h1{font-size:16px;margin:0 8px 0 0}select,input,button{font:inherit;color:var(--text);background:var(--card);border:1px solid var(--line);border-radius:6px;padding:6px 10px}
button{cursor:pointer}button.primary{background:var(--accent);color:#1b1b1b;border-color:var(--accent);font-weight:600}button:disabled{opacity:.5;cursor:default}
a.tab{color:var(--dim);text-decoration:none;padding:6px 4px}a.tab.on{color:var(--text);font-weight:600;border-bottom:2px solid var(--accent)}
.info{color:var(--dim);font-size:12px;padding:8px 16px}
.layout{display:grid;grid-template-columns:260px 1fr;gap:12px;padding:0 16px 40px;align-items:start}
@media (max-width:760px){.layout{grid-template-columns:1fr}}
aside{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px;position:sticky;top:64px;max-height:calc(100vh - 80px);overflow:auto}
aside input{width:100%;margin-bottom:6px}
.t{display:block;width:100%;text-align:left;border:0;background:none;padding:6px 8px;border-radius:6px;word-break:break-all}
.t:hover{background:var(--card)}.t.on{background:var(--card);outline:1px solid var(--accent)}
.t small{display:block;color:var(--dim);font-size:11px}.t.changed{color:var(--ok)}
section{min-width:0}.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:8px}
.bar h2{font-size:15px;margin:0 8px 0 0;word-break:break-all}#rowSearch{flex:1;min-width:200px}
.wrap{overflow:auto;max-height:calc(100vh - 150px);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;font-size:13px}th,td{border-bottom:1px solid var(--line);border-right:1px solid var(--line);padding:0}
th{position:sticky;top:0;background:var(--panel);color:var(--dim);font-weight:600;padding:4px 8px;z-index:1;text-align:left}
td.n{color:var(--dim);padding:0 8px;text-align:right;background:var(--panel);position:sticky;left:0}
td input{border:0;border-radius:0;background:transparent;padding:4px 8px;min-width:6ch}
td input:focus{outline:2px solid var(--accent);background:var(--card)}
td .cell{display:flex;align-items:center}td .cell input{flex:none}td img.mini{height:24px;max-width:120px;object-fit:contain;margin:2px 6px 2px 0;background:#30343b;border-radius:3px}
td.changed input{background:rgba(92,184,122,.18)}td.changed input:focus{background:rgba(92,184,122,.28)}
.empty{color:var(--dim);padding:40px 0;text-align:center}.warn{color:var(--accent);font-size:12px}
#toast{position:fixed;left:16px;right:16px;bottom:16px;max-width:760px;margin:auto;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;display:none;z-index:20;white-space:pre-wrap}
#toast.bad{border-color:var(--bad)}#toast.ok{border-color:var(--ok)}
</style></head><body>
<header>
  <h1>mw2tex</h1>
  <a class="tab" id="texTab" href="/" hidden>Textures</a><a class="tab on" href="/tables">Tables</a>
  <select id="file"></select><button id="openBtn">Open</button>
  <span style="flex:1"></span>
  <button id="clearBtn">Undo all</button>
  <button id="buildBtn" class="primary" disabled>Build</button>
</header>
<div class="info" id="info">Loading…</div>
<div class="layout">
  <aside><input id="tableSearch" placeholder="Find a table"><div id="list"></div></aside>
  <section id="main"><div class="empty">Pick a fastfile above and press Open. code_post_gfx_mp.ff has the multiplayer tables.</div></section>
</div>
<div id="toast"></div>
<script>
const $=s=>document.querySelector(s);let list={tables:[],changed:[]},cur=null,rows=null,orig=null,widths=[],saveTimer=null,combined=false,mats={},pics={};
const LIMIT=800;
function toast(msg,kind){const t=$("#toast");t.textContent=msg;t.className=kind||"";t.style.display="block";clearTimeout(t._h);t._h=setTimeout(()=>t.style.display="none",kind==="bad"?9000:6000)}
async function api(path,opts){const r=await fetch(path,opts);const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||("request failed: "+r.status));return j}
const post=(p,b)=>api(p,{method:"POST",body:JSON.stringify(b||{})});
function setList(j){list=j;const t=j.changed.length,p=Object.keys(pics).length,n=t+p;$("#buildBtn").disabled=!n;
 $("#buildBtn").textContent="Build"+(n?" ("+[t?t+" table"+(t>1?"s":""):"",p?p+" picture"+(p>1?"s":""):""].filter(Boolean).join(", ")+")":"");renderList()}
function renderList(){const q=$("#tableSearch").value.trim().toLowerCase();const el=$("#list");el.innerHTML="";
 for(const t of list.tables){if(q&&!t.name.toLowerCase().includes(q))continue;const b=document.createElement("button");b.className="t"+(cur===t.name?" on":"")+(t.changed?" changed":"");
  b.textContent=t.name;b.appendChild(Object.assign(document.createElement("small"),{textContent:t.rows+" rows x "+t.columns+" columns"+(t.changed?" · changed":"")+(t.elsewhere?" · from another file":"")}));
  b.onclick=()=>openTable(t.name);el.appendChild(b)}
 if(!el.children.length)el.appendChild(Object.assign(document.createElement("div"),{className:"empty",textContent:list.file?"No tables match.":"Open a fastfile first."}))}
async function openTable(name){await flush();const j=await api("/tables/api/table?name="+encodeURIComponent(name));cur=name;rows=j.rows.map(r=>r.slice());orig=j.original;$("#rowSearch")&&($("#rowSearch").value="");renderTable(j.unresolved);renderList()}
function isChanged(r,c){return !orig||!orig[r]||orig[r][c]!==rows[r][c]}
function columnValues(c){const s=new Set();for(const r of rows)if(r[c]&&s.size<500)s.add(r[c]);if(orig)for(const r of orig)if(r[c]&&s.size<500)s.add(r[c]);return [...s].sort()}
function renderTable(unresolved){const m=$("#main");m.innerHTML="";const cols=rows[0].length;
 widths=[];for(let c=0;c<cols;c++){let w=4;for(const r of rows)w=Math.max(w,(r[c]||"").length);widths.push(Math.min(w,48)+3)}
 const bar=document.createElement("div");bar.className="bar";bar.appendChild(Object.assign(document.createElement("h2"),{textContent:cur}));
 const s=Object.assign(document.createElement("input"),{id:"rowSearch",placeholder:"Show only rows containing…"});s.oninput=()=>fillRows();bar.appendChild(s);
 const u=Object.assign(document.createElement("button"),{textContent:"Undo this table"});u.onclick=async()=>{await flush();setList(await post("/tables/api/revert",{name:cur}));openTable(cur)};bar.appendChild(u);
 m.appendChild(bar);
 if(unresolved)m.appendChild(Object.assign(document.createElement("div"),{className:"warn",textContent:unresolved+" cells couldn't be read from the game file and show empty. Leave them alone unless you know what goes there."}));
 m.appendChild(Object.assign(document.createElement("div"),{className:"info",style:"padding:0 0 8px",textContent:"Click a cell to change it. Green cells differ from the game. Changes save as you type; press Build when you're done. Each column suggests values already used in it."}));
 for(let c=0;c<cols;c++){const d=document.createElement("datalist");d.id="col"+c;for(const v of columnValues(c))d.appendChild(Object.assign(document.createElement("option"),{value:v}));m.appendChild(d)}
 const w=document.createElement("div");w.className="wrap";const t=document.createElement("table");const h=document.createElement("tr");h.appendChild(document.createElement("th")).textContent="row";
 for(let c=0;c<cols;c++)h.appendChild(document.createElement("th")).textContent="column "+(c+1);t.appendChild(h);t.appendChild(document.createElement("tbody"));w.appendChild(t);m.appendChild(w);fillRows()}
function fillRows(){const tb=$("#main tbody");if(!tb)return;tb.innerHTML="";const q=($("#rowSearch").value||"").trim().toLowerCase();let shown=0,total=0;
 for(let r=0;r<rows.length;r++){if(q&&!rows[r].some(v=>v.toLowerCase().includes(q)))continue;total++;if(shown>=LIMIT)continue;shown++;
  const tr=document.createElement("tr");tr.appendChild(Object.assign(document.createElement("td"),{className:"n",textContent:r+1}));
  for(let c=0;c<rows[r].length;c++){const td=document.createElement("td");if(isChanged(r,c))td.className="changed";const box=document.createElement("div");box.className="cell";td.appendChild(box);const i=document.createElement("input");
   const pic=document.createElement("img");pic.className="mini";pic.alt="";pic.onerror=()=>pic.hidden=true;const showPic=()=>{const img=mats[(rows[r][c]||"").toLowerCase()];pic.hidden=!img;if(img){pic.title=img;pic.src=pics[img]?"/api/upload?name="+encodeURIComponent(img)+"&v="+encodeURIComponent(pics[img]):"/api/matthumb?m="+encodeURIComponent(rows[r][c])}};
   if(combined){showPic();box.appendChild(pic)}i.value=rows[r][c];i.setAttribute("list","col"+c);i.style.width=widths[c]+"ch";i.spellcheck=false;
   i.title=orig&&orig[r]&&orig[r][c]!==rows[r][c]?"Game value: "+(orig[r][c]||"(empty)"):"";
   i.oninput=()=>{rows[r][c]=i.value;td.className=isChanged(r,c)?"changed":"";i.title=orig&&orig[r]&&orig[r][c]!==i.value?"Game value: "+(orig[r][c]||"(empty)"):"";if(combined)showPic();queueSave()};box.appendChild(i);tr.appendChild(td)}
  tb.appendChild(tr)}
 if(total>shown){const tr=document.createElement("tr");const td=document.createElement("td");td.colSpan=rows[0].length+1;td.className="empty";td.textContent="Showing "+shown+" of "+total+" rows. Type in the box above to find the row you want.";tr.appendChild(td);tb.appendChild(tr)}}
function queueSave(){clearTimeout(saveTimer);saveTimer=setTimeout(flush,400)}
async function flush(){if(!saveTimer||!cur)return;clearTimeout(saveTimer);saveTimer=null;try{setList(await post("/tables/api/save",{name:cur,rows}))}catch(e){toast(e.message,"bad")}}
async function loadPictures(){if(!combined)return;try{const j=await api("/api/materials");mats={};for(const[k,v]of Object.entries(j.materials))mats[k.toLowerCase()]=v;pics=j.pending;
 if(j.file)$("#info").textContent+=" · pictures from "+j.file}catch(e){}}
async function loadFiles(){try{await fetch("/api/files").then(r=>{if(r.ok){$("#texTab").hidden=false;combined=true}})}catch(e){}
 const j=await api("/tables/api/files");const s=$("#file");s.innerHTML="";
 if(!j.files.length){s.innerHTML="<option>no .ff files here</option>";$("#info").textContent="No .ff files in "+j.folder+". Put code_post_gfx_mp.ff in that folder and reload this page.";return}
 for(const f of j.files)s.appendChild(Object.assign(document.createElement("option"),{textContent:f}));s.value=j.default;
 $("#info").textContent="Folder: "+j.folder+" · Build writes mw2tex_out\\codxe_patch_mp.ff";
 const l=await api("/tables/api/list");await loadPictures();setList(l);if(l.file){s.value=l.file}}
$("#openBtn").onclick=async()=>{await flush();const b=$("#openBtn");b.disabled=true;b.textContent="Opening…";try{const j=await post("/tables/api/open",{file:$("#file").value});cur=null;setList(j);
 $("#main").innerHTML='<div class="empty">Pick a table on the left.</div>';
 const found=j.tables.filter(t=>!t.elsewhere).length;toast("Opened "+j.file+": "+found+" table"+(found===1?"":"s")+(j.restored&&j.restored.length?"\nKept your earlier changes from mw2tex_out\\codxe_patch_mp.ff: "+j.restored.join(", "):""),found?"ok":"bad")}catch(e){toast(e.message,"bad")}b.disabled=false;b.textContent="Open"};
$("#tableSearch").oninput=renderList;
$("#clearBtn").onclick=async()=>{await flush();if(!list.changed.length)return;if(!confirm("Undo every table change? The next Build starts from the game's tables."))return;setList(await post("/tables/api/revert",{all:true}));if(cur)openTable(cur)};
$("#buildBtn").onclick=async()=>{await flush();const b=$("#buildBtn");b.disabled=true;b.textContent="Building…";try{const j=combined?await post("/api/build"):await post("/tables/api/build");toast("Built:\n"+j.log.join("\n")+"\n\nWrote "+j.written.join(", ")+".\nCopy "+(j.written.length>1?"them":"it")+" to _codxe\\zone\\ on your console.","ok")}catch(e){toast(e.message,"bad")}setList(await api("/tables/api/list"))};
window.addEventListener("beforeunload",()=>{if(saveTimer)flush()});
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
        path = "/tables" if url.path == "/" else url.path
        if path == "/favicon.ico":
            return self.reply(204, b"", "image/x-icon")
        with lock:
            result = handle(method, path, urllib.parse.parse_qs(url.query), body)
        self.reply(*(result or (404, {"error": "not found"}, None)))

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
        sys.exit("couldn't find a free port for the table editor")
    url = "http://127.0.0.1:%d/tables" % port
    print("mw2zone table editor running at %s" % url)
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
