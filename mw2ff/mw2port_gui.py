"""Map converter for PC (IW4x) MW2 maps -> Xbox 360 TU6, running in your web browser.

Run it in your work folder, the one with your stock 360 .ff files (or double-click
mw2port.bat there):
    python mw2port_gui.py
Put each PC map in its own folder under mw2port_in (for an IW4x map, copy its whole
usermaps\\<map> folder: the .ff, _load.ff, .iwd and .arena). Pick the map, check the teams and
press Convert. The 360 files go in mw2port_out\\<map>; copy them to _codxe\\zone\\ on the console.
Pictures a map borrows from the PC game itself come from the iw_*.iwd files copied into
mw2port_pc_game (optional; without them those pictures are plain gray).

The converter needs these stock 360 files from the console in the work folder:
code_post_gfx_mp.ff, common_mp.ff, at least one stock map (mp_favela.ff works best) with its _load.ff, and
the stock maps that carry the teams the map uses. Nothing is sent anywhere: the page talks
only to this program on your own PC.
"""
import glob
import json
import os
import sys
import threading
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mw2ff  # noqa: E402
import port as port_mod  # noqa: E402

FOLDER = os.getcwd()
IN_DIR = os.path.join(FOLDER, "mw2port_in")
OUT_DIR = os.path.join(FOLDER, "mw2port_out")
GAME_DIR = os.path.join(FOLDER, "mw2port_pc_game")     # the PC game's iw_*.iwd files (optional)
SETTINGS = os.path.join(FOLDER, "mw2port_settings.json")
TEX_OUT = os.path.join(FOLDER, "mw2tex_out")             # where mw2tex writes its built ui_mp.ff
PORT = 8390

TEAM_NAMES = {
    "us_army": "US Army Rangers", "seals_udt": "Navy SEALs", "socom_141": "Task Force 141",
    "socom_141_desert": "Task Force 141 (desert)", "socom_141_forest": "Task Force 141 (forest)",
    "socom_141_arctic": "Task Force 141 (arctic)", "opforce_composite": "OpFor",
    "opforce_airborne": "Spetsnaz", "opforce_arctic": "Spetsnaz (arctic)", "militia": "Militia",
}

lock = threading.Lock()
job = {"running": False, "map": None, "log": [], "done": False, "error": None, "files": []}


def settings():
    try:
        with open(SETTINGS) as fh:
            return dict({"card_pak": False}, **json.load(fh))
    except (OSError, ValueError):
        return {"card_pak": False}


def save_settings(req):
    s = settings()
    if "card_pak" in req:
        s["card_pak"] = bool(req["card_pak"])
    with open(SETTINGS, "w") as fh:
        json.dump(s, fh)
    return s


def card_ui():
    """The ui_mp.ff the title/emblem slots are filled from: mw2tex's built one (your titles and
    emblems), else the stock one in the work folder; None if neither is there."""
    for p in (os.path.join(TEX_OUT, "ui_mp.ff"), os.path.join(FOLDER, "ui_mp.ff")):
        if os.path.exists(p):
            return p
    return None


def stock_maps():
    return [p for p in stock_files() if os.path.basename(p).lower().startswith("mp_")
            and not p.lower().endswith("_load.ff")]


# ---------------------------------------------------------------- files

def stock_files():
    out = []
    for p in sorted(glob.glob(os.path.join(FOLDER, "*.ff"))):
        n = os.path.basename(p).lower()
        if n in ("code_post_gfx_mp.ff", "common_mp.ff") or n.startswith("mp_"):
            try:
                if mw2ff.platform_of(p) == "xbox":
                    out.append(p)
            except OSError:
                pass
    return out


def pc_maps():
    maps = []
    for p in sorted(glob.glob(os.path.join(IN_DIR, "**", "*.ff"), recursive=True)):
        if p.lower().endswith("_load.ff"):
            continue
        try:
            if mw2ff.platform_of(p) != "pc":
                continue
        except OSError:
            continue
        base = os.path.splitext(p)[0]
        maps.append({
            "path": p,
            "name": os.path.basename(base),
            "where": os.path.relpath(p, FOLDER),
            "load": os.path.exists(base + "_load.ff"),
            "iwd": os.path.exists(base + ".iwd"),
            "arena": os.path.exists(base + ".arena"),
            "teams": list(port_mod.map_teams(p)),
        })
    return maps


def team_list(stock):
    """Every team, and which stock maps in the folder carry it."""
    have = set(os.path.splitext(os.path.basename(p))[0].lower() for p in stock)
    out = []
    for team in port_mod.TEAM_ASSETS:
        carriers = port_mod.STOCK_MAP_TEAMS_BY_TEAM.get(team, [])
        out.append({"id": team, "name": TEAM_NAMES.get(team, team),
                    "maps": carriers, "have": [m for m in carriers if m in have]})
    return out


def status():
    stock = stock_files()
    names = [os.path.basename(p) for p in stock]
    missing = []
    if "code_post_gfx_mp.ff" not in [n.lower() for n in names]:
        missing.append("code_post_gfx_mp.ff")
    if not [n for n in names if n.lower().startswith("mp_") and not n.lower().endswith("_load.ff")]:
        missing.append("a stock map, for example mp_favela.ff")
    if not [n for n in names if n.lower().endswith("_load.ff")]:
        missing.append("a stock _load.ff, for example mp_favela_load.ff")
    game = [os.path.basename(p) for p in port_mod.game_iwd_files(GAME_DIR)]
    ui = card_ui()
    return {"folder": FOLDER, "in": IN_DIR, "out": OUT_DIR, "stock": names, "missing": missing,
            "game": game, "settings": settings(),
            "ui": os.path.relpath(ui, FOLDER) if ui else None, "stock_maps": len(stock_maps()),
            "maps": pc_maps(), "teams": team_list(stock),
            "job": {k: job[k] for k in ("running", "map", "done", "error", "files")},
            "log": job["log"][-400:]}


# ---------------------------------------------------------------- converting

def _log(msg):
    job["log"].append(str(msg))


def _convert_one(pc_path, teams):
    """Converts one map; returns (files written, error or None)."""
    name = os.path.splitext(os.path.basename(pc_path))[0]
    ui = card_ui() if settings()["card_pak"] else None
    try:
        files = port_mod.port_map(pc_path, os.path.join(OUT_DIR, name), stock_files(), teams, _log,
                                  port_mod.game_iwd_files(GAME_DIR), card_ui=ui)
        return [os.path.relpath(f, FOLDER) for f in files], None
    except port_mod.PortError as e:
        _log("Stopped: %s" % e)
        return [], str(e)
    except MemoryError:
        _log("Stopped: ran out of memory (close other programs and try again)")
        return [], "ran out of memory"
    except Exception as e:  # noqa: BLE001 - shown to the user, with the details for Claude
        _log(traceback.format_exc())
        _log("Stopped with an error. Send the lines above to Claude.")
        return [], "%s: %s" % (type(e).__name__, e)


def _run(items):
    """Converts the maps one after another; a map that fails doesn't stop the others."""
    failed = []
    try:
        for i, (m, teams) in enumerate(items):
            job["map"] = m["name"] if len(items) == 1 else "%s (%d of %d)" % (m["name"], i + 1, len(items))
            if len(items) > 1:
                _log("")
                _log("===== %s (%d of %d): %s vs %s =====" % (m["name"], i + 1, len(items),
                                                            TEAM_NAMES.get(teams[0], teams[0]),
                                                            TEAM_NAMES.get(teams[1], teams[1])))
            files, error = _convert_one(m["path"], teams)
            job["files"] += files
            if error:
                failed.append((m["name"], error))
        _log("")
        if job["files"]:
            _log("Done. Copy these to _codxe\\zone\\ on the console:")
            for f in job["files"]:
                _log("  " + f)
        if failed:
            _log("")
            _log("%d of %d map%s stopped:" % (len(failed), len(items), "" if len(items) == 1 else "s"))
            for name, error in failed:
                _log("  %s: %s" % (name, error))
            job["error"] = failed[0][1] if len(items) == 1 else "%d of %d maps stopped" % (len(failed), len(items))
    finally:
        job["running"] = False
        job["done"] = True


def _patch_stock():
    """Points every stock map's titles and emblems at imagefile8.pak and writes that pak."""
    import mw2tex
    try:
        ui = card_ui()
        if ui is None:
            raise port_mod.PortError("needs ui_mp.ff (from the console) in the work folder")
        out = os.path.join(OUT_DIR, "stock")
        os.makedirs(out, exist_ok=True)
        slots = mw2tex.load_card_slots()
        for path in stock_maps():
            ff = mw2tex.FastFile(path)
            n = mw2tex.point_cards_at_pak(ff, slots)
            dst = os.path.join(out, os.path.basename(path))
            ff.save(dst)
            job["files"].append(os.path.relpath(dst, FOLDER))
            _log("%s: %d titles and emblems now come from imagefile%d.pak" % (os.path.basename(path), n,
                                                                              mw2tex.CARD_PAK))
        pak = mw2tex.write_card_pak(ui, out, slots, log=_log)
        job["files"].append(os.path.relpath(pak, FOLDER))
        _log("")
        _log("Done. Copy these to _codxe\\zone\\ on the console:")
        for f in job["files"]:
            _log("  " + f)
        _log("From now on, after changing titles or emblems in mw2tex, only ui_mp.ff and "
             "imagefile8.pak from mw2tex_out need copying.")
    except Exception as e:  # noqa: BLE001
        job["error"] = "%s: %s" % (type(e).__name__, e) if not isinstance(e, port_mod.PortError) else str(e)
        _log("Stopped: %s" % job["error"])
    finally:
        job["running"] = False
        job["done"] = True


def patch_stock(req):
    if job["running"]:
        raise ValueError("something is already running")
    if not stock_maps():
        raise ValueError("no stock mp_*.ff maps in the work folder")
    job.update(running=True, map="stock maps", log=[], done=False, error=None, files=[])
    _log("Patching %d stock maps to take titles and emblems from imagefile8.pak..." % len(stock_maps()))
    threading.Thread(target=_patch_stock, daemon=True).start()
    return {"ok": True}


def convert(req):
    """req: {"maps": [{"path", "teams"}, ...]} (or one {"path", "teams"})."""
    if job["running"]:
        raise ValueError("a conversion is already running")
    maps = {m["path"]: m for m in pc_maps()}
    items = []
    for r in req.get("maps") or [req]:
        m = maps.get(r.get("path"))
        if m is None:
            raise ValueError("a map isn't in mw2port_in any more; press Refresh")
        teams = r.get("teams") or m["teams"]
        if len(teams) != 2 or any(t not in port_mod.TEAM_ASSETS for t in teams):
            raise ValueError("pick a team for each side of %s" % m["name"])
        items.append((m, teams))
    if not items:
        raise ValueError("pick a map")
    job.update(running=True, map=items[0][0]["name"], log=[], done=False, error=None, files=[])
    if len(items) == 1:
        m, teams = items[0]
        _log("Converting %s (%s vs %s). This takes a few minutes; keep this page open." % (
            m["name"], TEAM_NAMES.get(teams[0], teams[0]), TEAM_NAMES.get(teams[1], teams[1])))
    else:
        _log("Converting %d maps one after another (a few minutes each); keep this page open." % len(items))
    if settings()["card_pak"]:
        _log("Titles and emblems: from imagefile8.pak (filled from %s)." % (
            os.path.relpath(card_ui(), FOLDER) if card_ui() else "nothing: no ui_mp.ff in the work folder"))
    threading.Thread(target=_run, args=(items,), daemon=True).start()
    return {"ok": True}


def open_folder(which):
    path = {"in": IN_DIR, "out": OUT_DIR, "game": GAME_DIR}.get(which)
    if path is None:
        raise ValueError("unknown folder")
    os.makedirs(path, exist_ok=True)
    if hasattr(os, "startfile"):
        os.startfile(path)
    return {"ok": True, "path": path}


def handle(method, path, body):
    try:
        if method == "GET":
            if path in ("/", "/index.html"):
                return 200, PAGE, "text/html; charset=utf-8"
            if path == "/api/status":
                return 200, status(), None
        else:
            req = json.loads(body or b"{}")
            if path == "/api/convert":
                with lock:
                    return 200, convert(req), None
            if path == "/api/open":
                return 200, open_folder(req.get("which")), None
            if path == "/api/settings":
                return 200, save_settings(req), None
            if path == "/api/patch":
                with lock:
                    return 200, patch_stock(req), None
        return 404, {"error": "not found"}, None
    except (ValueError, OSError) as e:
        return 400, {"error": str(e)}, None


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>mw2port map converter</title>
<style>
:root{--bg:#17191d;--panel:#22252b;--card:#2a2e35;--line:#3a3f48;--text:#e6e8ec;--dim:#9aa1ad;--accent:#d6aa46;--ok:#5cb87a;--bad:#e06c6c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,Segoe UI,sans-serif}
header{background:var(--panel);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;flex-wrap:wrap;gap:10px;align-items:center}
h1{font-size:16px;margin:0 8px 0 0}h2{font-size:14px;margin:0 0 8px}
select,button{font:inherit;color:var(--text);background:var(--card);border:1px solid var(--line);border-radius:6px;padding:6px 10px}
button{cursor:pointer}button.primary{background:var(--accent);color:#1b1b1b;border-color:var(--accent);font-weight:600}button:disabled{opacity:.5;cursor:default}
.wrap{max-width:980px;margin:0 auto;padding:12px 16px 40px;display:grid;gap:12px}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.dim{color:var(--dim)}.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--accent)}
.map{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;padding:8px;border-radius:8px;cursor:pointer;border:1px solid transparent}
.map:hover{background:var(--card)}.map.on{background:var(--card);border-color:var(--accent)}
.map b{min-width:160px}.tag{font-size:12px}.map input{width:16px;height:16px;margin:0}
label.toggle{display:flex;gap:8px;align-items:flex-start;cursor:pointer}label.toggle input{margin-top:3px}
.teams{display:grid;grid-template-columns:auto 1fr;gap:8px 12px;align-items:center;max-width:620px}
.teams select{width:100%}
pre{background:#111317;border:1px solid var(--line);border-radius:6px;padding:10px;max-height:420px;overflow:auto;white-space:pre-wrap;word-break:break-word;font:12px/1.45 ui-monospace,Consolas,monospace;margin:8px 0 0}
code{font-family:ui-monospace,Consolas,monospace}
ul{margin:4px 0 0;padding-left:20px}
</style></head><body>
<header><h1>mw2port map converter</h1><span class="dim">PC (IW4x) maps to Xbox 360 TU6</span>
<span style="flex:1"></span><button id="refresh">Refresh</button></header>
<div class="wrap">
<section id="stock"></section>
<section><h2>1. PC map</h2>
<p class="dim">Put each PC map in its own folder in <code>mw2port_in</code>. For an IW4x map, copy its whole
<code>usermaps\&lt;map&gt;</code> folder (the .ff, _load.ff, .iwd and .arena files). Then press Refresh.</p>
<p><button id="openIn">Open mw2port_in</button> <button id="tickAll">Tick all</button> <button id="tickNone">Untick all</button>
<span class="dim">Tick several maps to convert them one after another; click a map's name to pick its teams.</span></p>
<div id="maps"></div></section>
<section><h2>PC game pictures (optional)</h2>
<p class="dim">Some IW4x maps borrow pictures from the PC game itself; without them those show up plain gray.
Copy the <code>iw_*.iwd</code> files from your PC MW2 (or IW4x) <code>main</code> folder into <code>mw2port_pc_game</code>, then press Refresh.</p>
<p><button id="openGame">Open mw2port_pc_game</button> <span id="game" class="dim"></span></p></section>
<section><h2>2. Teams (pick any two)</h2>
<p class="dim">Pick the two teams you want on this map. They start as the teams the map's .arena file asks for.
A team comes from a stock 360 map that has it, so that map's .ff has to be in your work folder. A team marked
"not here" is swapped for one you have.</p>
<div class="teams" id="teams"><span class="dim">Pick a map first.</span></div></section>
<section><h2>Titles and emblems</h2>
<label class="toggle"><input type="checkbox" id="cardPak"><span><b>Take titles and emblems from imagefile8.pak</b><br>
<span class="dim">Converted maps point their titles and emblems at fixed places in <code>imagefile8.pak</code>. After you change
titles or emblems in mw2tex, its Build writes a new <code>imagefile8.pak</code>; copy it (and ui_mp.ff) to the console and
the maps show the new ones without being converted or copied again. Needs <code>ui_mp.ff</code> in the work folder
(mw2tex's built one in mw2tex_out is used when it's there).</span></span></label>
<p><button id="patch">Patch stock maps</button> <span class="dim">Does the same for every stock <code>mp_*.ff</code> in the work
folder: the patched copies and <code>imagefile8.pak</code> go in <code>mw2port_out\stock</code>. Only their picture table changes.</span></p>
<p id="cardInfo" class="dim"></p></section>
<section><h2>3. Convert</h2>
<p><button id="convert" class="primary" disabled>Convert</button> <button id="openOut">Open mw2port_out</button> <span id="state" class="dim"></span></p>
<pre id="log" class="dim">Nothing converted yet.</pre></section>
</div>
<script>
const $=s=>document.querySelector(s);let S=null,pick=null,chosen={},ticked=new Set(),timer=null;
async function api(u,b){const o=b?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)}:{};const r=await fetch(u,o);const j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);return j}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function teamOpts(sel){return S.teams.map(t=>`<option value="${t.id}"${t.id===sel?" selected":""}>${esc(t.name)}${t.have.length?" (from "+esc(t.have[0])+")":" (not here: needs "+esc(t.maps.slice(0,2).join(" or ")||"a map that has it")+")"}</option>`).join("")}
function renderStock(){let h=`<h2>Stock 360 files in your work folder</h2><p class="dim">${esc(S.folder)}</p>`;
 h+=S.stock.length?`<p>${S.stock.map(esc).join(", ")}</p>`:`<p class="bad">None found.</p>`;
 if(S.missing.length)h+=`<p class="warn">Still needed (copy from the console with FTP): ${S.missing.map(esc).join("; ")}.</p>`;
 $("#stock").innerHTML=h}
function renderGame(){$("#game").innerHTML=S.game.length?`<span class="ok">${S.game.length} .iwd files: ${esc(S.game.slice(0,4).join(", "))}${S.game.length>4?", ...":""}</span>`:`<span class="warn">none yet</span>`}
function renderMaps(){if(!S.maps.length){$("#maps").innerHTML=`<p class="warn">No PC maps in mw2port_in yet.</p>`;pick=null;return}
 if(!pick||!S.maps.find(m=>m.path===pick))pick=S.maps[0].path;
 for(const p of [...ticked])if(!S.maps.find(m=>m.path===p))ticked.delete(p);
 $("#maps").innerHTML=S.maps.map(m=>`<div class="map${m.path===pick?" on":""}" data-p="${esc(m.path)}"><input type="checkbox" class="tick" data-p="${esc(m.path)}"${ticked.has(m.path)?" checked":""}><b>${esc(m.name)}</b>
 <span class="tag ${m.load?"ok":"warn"}">${m.load?"loading screen":"no _load.ff"}</span>
 <span class="tag ${m.iwd?"ok":"warn"}">${m.iwd?"pictures (.iwd)":"no .iwd"}</span>
 <span class="tag ${m.arena?"ok":"warn"}">${m.arena?".arena":"no .arena (default teams)"}</span>
 <span class="tag dim">${esc(m.where)}</span></div>`).join("");
 document.querySelectorAll(".map").forEach(d=>d.onclick=e=>{if(e.target.classList.contains("tick"))return;pick=d.dataset.p;renderMaps();renderTeams()});
 document.querySelectorAll(".tick").forEach(c=>c.onchange=()=>{c.checked?ticked.add(c.dataset.p):ticked.delete(c.dataset.p);renderJob()})}
function renderCards(){$("#cardPak").checked=!!S.settings.card_pak;
 $("#cardInfo").innerHTML=S.ui?`Filled from <code>${esc(S.ui)}</code>.`:`<span class="warn">No ui_mp.ff in the work folder or mw2tex_out yet: copy it from the console.</span>`}
function renderTeams(){const m=S.maps.find(x=>x.path===pick);if(!m){$("#teams").innerHTML=`<span class="dim">Pick a map first.</span>`;return}
 const c=chosen[m.path]||m.teams;
 $("#teams").innerHTML=`<label>Allies</label><select id="allies">${teamOpts(c[0])}</select><label>Axis</label><select id="axis">${teamOpts(c[1])}</select>`;
 const save=()=>chosen[m.path]=[$("#allies").value,$("#axis").value];$("#allies").onchange=save;$("#axis").onchange=save}
function renderJob(){const j=S.job;const n=ticked.size;$("#convert").textContent=n>1?"Convert "+n+" maps":"Convert";
 $("#convert").disabled=j.running||(!pick&&!n)||S.missing.length>0;$("#patch").disabled=j.running||!S.ui||!S.stock_maps;
 $("#state").textContent=j.running?"Converting "+j.map+"...":(j.error?"Stopped.":(j.done?"Done.":""));
 $("#state").className=j.error?"bad":(j.done&&!j.running?"ok":"dim");
 if(S.log.length){const l=$("#log");const end=l.scrollTop+l.clientHeight>=l.scrollHeight-8;l.textContent=S.log.join("\n");l.className="";if(end)l.scrollTop=l.scrollHeight}
 clearTimeout(timer);if(j.running)timer=setTimeout(()=>load(false),1500)}
async function load(all=true){try{S=await api("/api/status");if(all){renderStock();renderGame();renderMaps();renderTeams();renderCards()}renderJob()}catch(e){$("#state").textContent=e.message;$("#state").className="bad"}}
$("#refresh").onclick=()=>load();
$("#openIn").onclick=()=>api("/api/open",{which:"in"}).then(()=>load()).catch(e=>alert(e.message));
$("#openGame").onclick=()=>api("/api/open",{which:"game"}).then(()=>load()).catch(e=>alert(e.message));
$("#openOut").onclick=()=>api("/api/open",{which:"out"}).catch(e=>alert(e.message));
$("#convert").onclick=async()=>{const list=ticked.size?S.maps.filter(x=>ticked.has(x.path)):S.maps.filter(x=>x.path===pick);if(!list.length)return;
 try{await api("/api/convert",{maps:list.map(m=>({path:m.path,teams:chosen[m.path]||m.teams}))});load(false)}catch(e){alert(e.message)}};
$("#tickAll").onclick=()=>{S.maps.forEach(m=>ticked.add(m.path));renderMaps();renderJob()};
$("#tickNone").onclick=()=>{ticked.clear();renderMaps();renderJob()};
$("#cardPak").onchange=async e=>{try{S.settings=await api("/api/settings",{card_pak:e.target.checked})}catch(err){alert(err.message)}};
$("#patch").onclick=async()=>{if(!confirm("Write patched copies of every stock map ("+S.stock_maps+") and imagefile8.pak to mw2port_out\\stock?"))return;
 try{await api("/api/patch",{});load(false)}catch(e){alert(e.message)}};
load();
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
            body = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 1 << 20))
        if url.path == "/favicon.ico":
            return self.reply(204, b"", "image/x-icon")
        self.reply(*handle(method, url.path, body))

    def do_GET(self):
        self.serve("GET")

    def do_POST(self):
        self.serve("POST")


def main():
    os.makedirs(IN_DIR, exist_ok=True)
    os.makedirs(GAME_DIR, exist_ok=True)
    port = PORT
    for attempt in range(20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    else:
        sys.exit("couldn't find a free port for the map converter")
    url = "http://127.0.0.1:%d/" % port
    print("mw2port map converter running at %s" % url)
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
