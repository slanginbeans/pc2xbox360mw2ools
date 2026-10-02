"""mw2tools: the MW2 (Xbox 360, TU6) tools in one browser window.

Double-click mw2tools.bat, or run from your work folder:
    python mw2tools_gui.py
One page opens with a tab per tool:
    Textures & tables   the texture picker and table editor (mw2tex)
    Fastfile editor     every asset in a fastfile, scripts and text (mw2ff)
    Map converter       PC (IW4x) maps to the 360 (mw2port)
All of them use the folder this runs in: your .ff files go there, and so do their output
folders (mw2tex_out, mw2ff_out, mw2port_out). Nothing is sent anywhere: the pages talk only to
this program on your own PC.

With --tray (how mw2tools.bat starts it, with pythonw: no window) it shows an icon by the
clock instead (left click: open the page; right click: Open, Show log, Quit) and writes what it
would have printed to mw2tools.log.
"""
import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
PORT = 8350
LOG = "mw2tools.log"

# (tab title, module, folder, what it's for)
APPS = [
    ("Textures & tables", "mw2tex_gui", "mw2tex",
     "Swap pictures, animate titles and emblems, edit tables"),
    ("Fastfile editor", "mw2ff_gui", "mw2ff",
     "Every asset in a fastfile: numbers, scripts, text"),
    ("Map converter", "mw2port_gui", "mw2ff",
     "PC (IW4x) maps to the 360, with the teams you pick"),
]

running = []    # {"title", "about", "url"} or {"title", "about", "error"}


def start(title, module, folder, about):
    """Import one tool and serve it on its own port, in this process."""
    sys.path.insert(0, os.path.join(TOOLS, folder))
    try:
        mod = __import__(module)
    except SystemExit as e:           # a tool that can't run says why with sys.exit
        return {"title": title, "about": about, "error": str(e)}
    except Exception as e:  # noqa: BLE001 - shown on the tab instead of stopping every tool
        return {"title": title, "about": about, "error": "%s: %s" % (type(e).__name__, e)}
    for d in (getattr(mod, "IN_DIR", None), getattr(mod, "GAME_DIR", None)):
        if d:
            os.makedirs(d, exist_ok=True)
    port = mod.PORT
    for attempt in range(20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), mod.Handler)
            break
        except OSError:
            port += 1
    else:
        return {"title": title, "about": about, "error": "no free port"}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return {"title": title, "about": about, "url": "http://127.0.0.1:%d/" % port}


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>mw2tools</title>
<style>
:root{--bg:#17191d;--panel:#22252b;--card:#2a2e35;--line:#3a3f48;--text:#e6e8ec;--dim:#9aa1ad;--accent:#d6aa46;--bad:#e06c6c}
*{box-sizing:border-box}html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--text);font:14px/1.4 system-ui,Segoe UI,sans-serif;display:flex;flex-direction:column}
nav{display:flex;flex-wrap:wrap;gap:6px;align-items:center;background:var(--panel);border-bottom:1px solid var(--line);padding:8px 12px}
nav b{margin-right:10px}
nav button{font:inherit;color:var(--text);background:var(--card);border:1px solid var(--line);border-radius:6px;padding:6px 12px;cursor:pointer}
nav button.on{background:var(--accent);border-color:var(--accent);color:#1b1b1b;font-weight:600}
nav small{color:var(--dim);margin-left:auto}
.pane{flex:1;display:none;min-height:0}.pane.on{display:block}
iframe{border:0;width:100%;height:100%;background:var(--bg)}
.err{padding:24px;color:var(--bad);white-space:pre-wrap}
</style></head><body>
<nav id="nav"><b>mw2tools</b></nav>
<script>
const apps=__APPS__;const nav=document.getElementById("nav");const panes=[];
function show(i){panes.forEach((p,k)=>p.classList.toggle("on",k===i));nav.querySelectorAll("button").forEach((b,k)=>b.classList.toggle("on",k===i));
 const p=panes[i];if(p.dataset.src&&!p.firstChild){const f=document.createElement("iframe");f.src=p.dataset.src;p.appendChild(f)}
 try{localStorage.setItem("mw2tools_tab",i)}catch(e){}}
apps.forEach((a,i)=>{const b=document.createElement("button");b.textContent=a.title;b.title=a.about;b.onclick=()=>show(i);nav.appendChild(b);
 const p=document.createElement("div");p.className="pane";
 if(a.url)p.dataset.src=a.url;else{const e=document.createElement("div");e.className="err";e.textContent=a.title+" couldn't start:\n"+a.error+"\n\nSend this to Claude.";p.appendChild(e)}
 document.body.appendChild(p);panes.push(p)});
const s=document.createElement("small");s.textContent="Your files: __FOLDER__";nav.appendChild(s);
let t=0;try{t=+localStorage.getItem("mw2tools_tab")||0}catch(e){}show(Math.min(t,apps.length-1));
</script></body></html>
"""


def busy():
    """What would be cut short by stopping now (a map conversion), or None."""
    port = sys.modules.get("mw2port_gui")
    if port is not None and port.job.get("running"):
        return "a map conversion (%s) is still running" % port.job.get("map")
    return None


class Handler(BaseHTTPRequestHandler):
    server_ref = None       # the page's server, for /quit

    def log_message(self, *args):
        pass

    def do_POST(self):
        # Another mw2tools starting (after an update) asks this one to make way for it.
        if self.path != "/quit":
            self.send_response(404)
            self.end_headers()
            return
        why = busy()
        body = json.dumps({"busy": why}).encode()
        self.send_response(409 if why else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        if not why:
            threading.Thread(target=Handler.server_ref.shutdown, daemon=True).start()

    def do_GET(self):
        if self.path.split("?")[0] not in ("/", "/index.html"):
            self.send_response(404)
            self.end_headers()
            return
        page = PAGE.replace("__APPS__", json.dumps(running)).replace(
            "__FOLDER__", json.dumps(os.getcwd())[1:-1].replace("<", ""))
        body = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _running_copy():
    """The address of an mw2tools already running on this PC, or None."""
    import urllib.request
    for port in range(PORT, PORT + 20):
        url = "http://127.0.0.1:%d/" % port
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if b"<title>mw2tools</title>" in r.read(4096):
                    return url
        except Exception:  # noqa: BLE001 - nothing there (or something else)
            continue
    return None


def _make_way(url, say):
    """An mw2tools is already running: ask it to stop, so this (perhaps just updated) copy takes
    over. Returns False when it can't stop now (a conversion is running) or won't (an older
    copy without /quit) and this one should step aside."""
    import time
    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen(urllib.request.Request(url + "quit", data=b"", method="POST"), timeout=5).read()
    except urllib.error.HTTPError as e:
        if e.code == 409:
            say("mw2tools is already running and %s, so it keeps going. Opening it; start "
                "mw2tools again when it's done to use any update." % json.loads(e.read())["busy"])
        return False
    except Exception:  # noqa: BLE001 - it went away by itself
        return True
    for _ in range(50):
        if _running_copy() != url:
            return True
        time.sleep(0.2)
    return False


def main():
    tray = "--tray" in sys.argv and os.name == "nt"
    if tray:
        # pythonw: no console, so problems go in a box (and, once it's open, the log).
        import tray as tray_mod

        def say(text):
            print(text)
            tray_mod.message_box(text)
    else:
        def say(text):
            print(text)

    other = _running_copy()
    if other and not _make_way(other, say):
        if "--no-browser" not in sys.argv:
            webbrowser.open(other)
        return
    if tray:
        # Opened after any copy that was running has stopped: it wrote to the same file.
        sys.stdout = sys.stderr = open(LOG, "w", buffering=1, encoding="utf-8", errors="replace")

    for app in APPS:
        info = start(*app)
        running.append(info)
        print("  %-18s %s" % (info["title"], info.get("url") or "couldn't start: " + info["error"]))
    port = PORT
    for attempt in range(20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    else:
        sys.exit("couldn't find a free port for mw2tools")
    Handler.server_ref = server
    url = "http://127.0.0.1:%d/" % port
    print("mw2tools running at %s" % url)
    print("Your files: %s" % os.getcwd())

    icon = None
    if tray:
        def open_page():
            webbrowser.open(url)

        def show_log():
            os.startfile(os.path.abspath(LOG))

        def quit_tools():
            why = busy()
            if why and tray_mod.message_box(
                    "%s. Quitting stops it. Quit anyway?" % (why[0].upper() + why[1:]), "mw2tools",
                    tray_mod.MB_YESNO | tray_mod.MB_ICONWARNING) != tray_mod.IDYES:
                return
            server.shutdown()

        icon = tray_mod.Tray("mw2tools", open_page, [("Open mw2tools", open_page), ("Show log", show_log),
                                                      None, ("Quit mw2tools", quit_tools)])
        try:
            icon.start()
        except Exception as e:  # noqa: BLE001 - without the icon it couldn't be found or stopped
            say("mw2tools couldn't put its icon by the clock (%s: %s), so it stops: without the "
                "icon you couldn't close it.\n\nTo use it in a window instead, put an empty file "
                "named keep_window.txt in %s and start mw2tools.bat again. Send this message to "
                "Claude." % (type(e).__name__, e, os.getcwd()))
            server.server_close()
            return
        icon.notify("mw2tools is running", "It's in the icons by the clock (behind the ^ arrow). "
                    "Click the icon to open it, right-click it to quit.")
        print("Running with an icon by the clock: right-click it to quit.")
    else:
        print("Leave this window open while you use it. Press Ctrl+C here to stop.")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if icon is not None:
            icon.stop()
        tex = sys.modules.get("mw2tex_gui")
        if tex is not None and hasattr(tex, "WORK_DIR"):
            import shutil
            shutil.rmtree(tex.WORK_DIR, ignore_errors=True)
        print("mw2tools stopped.")


if __name__ == "__main__":
    if "--tray" in sys.argv and os.name == "nt":
        try:
            main()
        except BaseException as e:  # noqa: BLE001 - no console: say it in a box
            if not isinstance(e, (SystemExit, KeyboardInterrupt)) or (isinstance(e, SystemExit) and e.code):
                import traceback
                traceback.print_exc()
                import tray
                tray.message_box("mw2tools stopped with an error:\n\n%s\n\nThe details are in %s. "
                                 "Send them to Claude." % (e, os.path.abspath(LOG)),
                                 "mw2tools", tray.MB_ICONERROR)
    else:
        main()
