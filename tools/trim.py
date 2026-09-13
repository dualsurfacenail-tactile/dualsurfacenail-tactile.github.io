#!/usr/bin/env python3
"""Trim / crop / pixelate the site videos in a browser.

    python3 tools/trim.py                       # opens http://localhost:8765
    python3 tools/trim.py hero_binder hero_wipes   # only list these clips
    python3 tools/trim.py --render all|<name>   # re-render from tools/trims.json (no UI)

Settings live in tools/trims.json: {"name": {"in": s, "out": s, "crop": [x,y,w,h], "pix": [[x,y,w,h], ...]}}
(all coordinates in source pixels, 1920x1080). Outputs go to static/videos/<name>.mp4 + posters/<name>.jpg.
"""
import json, shlex, subprocess, sys, tempfile, threading, webbrowser
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "static/videos"
CFG = ROOT / "tools/trims.json"
PROXY = Path(tempfile.gettempdir()) / "dualsurface_proxies"
SRC = Path.home() / "Downloads/nail videos"
WIPES = Path.home() / "Downloads/IMG_8344 2 (1).mov"
WIPES_PIX = [[900, 670, 272, 256]]
COLOR = "colorspace=all=bt709:iall=bt2020:itrc=bt2020-10:format=yuv420p,eq=contrast=1.08:saturation=1.12"
# name: (source, max output width, default pixelate boxes). Card/binder/wipes task cards reuse the hero files.
ITEMS = {
    "hero_card":         (SRC / "card_flip_success.MOV", 1920, []),
    "hero_binder":       (SRC / "folder_success.MOV",    1920, []),
    "hero_wipes":        (WIPES,                          1920, WIPES_PIX),
    "tissue_success":    (SRC / "tissue_success.MOV",    1280, []),
    "paper_success":     (SRC / "paper_success.MOV",     1280, []),
    "bento_success":     (SRC / "lid_success.MOV",       1280, []),
}
LOCK = threading.Lock()


@lru_cache(None)
def probe(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                   "-show_entries", "stream=width,height,duration", "-of", "json", str(path)])
    s = json.loads(out)["streams"][0]
    return {"w": s["width"], "h": s["height"], "dur": float(s["duration"])}


def load():
    return json.loads(CFG.read_text()) if CFG.exists() else {}


def settings(name):
    d = {"in": 0, "out": None, "crop": None, "pix": [list(b) for b in ITEMS[name][2]]}
    d.update(load().get(name, {}))
    return d


def even(v):
    return int(round(v)) // 2 * 2


def filters(pix, crop, max_w):
    """COLOR -> pixelate boxes -> crop -> scale, as a filter_complex graph ending in [v]."""
    parts, cur = [f"[0:v]{COLOR}[c]"], "[c]"
    for i, (x, y, w, h) in enumerate(pix):
        x, y, w, h = even(x), even(y), max(2, even(w)), max(2, even(h))
        parts.append(f"{cur}split[a{i}][b{i}];[b{i}]crop={w}:{h}:{x}:{y},"
                     f"scale={max(1, w // 16)}:{max(1, h // 16)}:flags=neighbor,scale={w}:{h}:flags=neighbor[p{i}];"
                     f"[a{i}][p{i}]overlay={x}:{y}[s{i}]")
        cur = f"[s{i}]"
    tail = []
    if crop:
        x, y, w, h = map(even, crop)
        tail.append(f"crop={w}:{h}:{x}:{y}")
    tail.append(f"scale={min(max_w, even(crop[2]) if crop else max_w)}:-2")
    parts.append(f"{cur}{','.join(tail)}[v]")
    return ";".join(parts)


def run(cmd, log):
    log("$ " + shlex.join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.stderr.strip():
        log(r.stderr.strip())
    if r.returncode:
        raise RuntimeError(f"ffmpeg exited {r.returncode}")


def render(name, log=print):
    src, max_w, _ = ITEMS[name]
    s = settings(name)
    t0 = float(s["in"] or 0)
    t1 = float(s["out"] if s["out"] is not None else probe(src)["dur"])
    (OUT / "posters").mkdir(parents=True, exist_ok=True)
    mp4 = OUT / f"{name}.mp4"
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}", "-i", str(src),
         "-filter_complex", filters(s["pix"], s["crop"], max_w), "-map", "[v]", "-an",
         "-c:v", "libx264", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4)], log)
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp4), "-frames:v", "1", "-q:v", "3",
         str(OUT / "posters" / f"{name}.jpg")], log)
    log(f"done -> {mp4.name} ({mp4.stat().st_size / 1e6:.1f} MB, {t1 - t0:.1f} s)")


def proxy(src):
    """Small SDR preview of the full source, same colour treatment as the render."""
    PROXY.mkdir(exist_ok=True)
    p = PROXY / (src.stem.replace(" ", "_") + ".mp4")
    with LOCK:
        if not p.exists():
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vf", COLOR + ",scale=640:-2",
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-an", "-movflags", "+faststart", str(p)], check=True)
    return p


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, body, ctype="application/json", code=200):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/":
            return self.reply(PAGE.encode(), "text/html")
        if p == "/config":
            return self.reply({n: {"src": s.name, "max_w": w, **probe(s), "settings": settings(n)}
                               for n, (s, w, _) in ITEMS.items()})
        if p.startswith("/proxy/"):
            return self.reply(proxy(ITEMS[p[7:]][0]).read_bytes(), "video/mp4")
        if p.startswith("/out/"):
            f = OUT / Path(p[5:]).name
            return self.reply(f.read_bytes(), "video/mp4" if f.suffix == ".mp4" else "image/jpeg")
        self.reply(b"not found", "text/plain", 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        name = body["name"]
        if body.get("settings"):
            cfg = load()
            cfg[name] = body["settings"]
            CFG.write_text(json.dumps(cfg, indent=1))
        if self.path == "/save":
            return self.reply({"ok": True})
        log = []
        try:
            render(name, log.append)
            ok = True
        except Exception as e:  # noqa: BLE001 - surface anything to the UI
            log.append(str(e))
            ok = False
        self.reply({"ok": ok, "log": "\n".join(log)})


PAGE = r"""<!doctype html><meta charset="utf-8"><title>Trim · Crop · Pixelate</title>
<style>
body{margin:0;background:#111;color:#eee;font:14px system-ui;display:grid;grid-template-columns:240px 1fr;height:100vh}
aside{background:#181818;border-right:1px solid #2a2a2a;overflow:auto;padding:10px}
aside h3{margin:8px 10px 6px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#777}
aside button{display:block;width:100%;text-align:left;padding:7px 10px;margin:1px 0;background:none;border:0;border-radius:6px;color:#ccc;cursor:pointer;font:inherit}
aside button.on{background:#2b2b2b;color:#fff}
aside small{display:block;color:#777;font-size:11px}
main{padding:14px 18px;overflow:auto;display:flex;flex-direction:column;gap:10px}
#stage{position:relative;width:min(100%,1100px);aspect-ratio:16/9;background:#000;user-select:none;touch-action:none;overflow:hidden}
#stage video{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
.box{position:absolute;box-sizing:border-box;border:2px solid #f0a040;cursor:move}
.box.crop{box-shadow:0 0 0 9999px rgba(0,0,0,.6)}
.box.pix{border-color:#4cc3ff;background:rgba(76,195,255,.12);backdrop-filter:blur(7px)}
.box.sel{border-style:dashed}
.box .h{position:absolute;right:-6px;bottom:-6px;width:12px;height:12px;background:#fff;border-radius:2px;cursor:nwse-resize}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
button{background:#333;border:1px solid #444;color:#eee;padding:6px 12px;border-radius:6px;cursor:pointer;font:inherit}
button:disabled{opacity:.5}
button.pri{background:#d9782f;border-color:#d9782f;color:#fff;font-weight:600}
button.mode.on{background:#f0a040;border-color:#f0a040;color:#000}
button.mode.on.pix{background:#4cc3ff;border-color:#4cc3ff}
input[type=number]{width:72px;background:#222;color:#eee;border:1px solid #444;border-radius:4px;padding:4px}
#scrub{flex:1}
#seg{position:relative;height:5px;background:#333;border-radius:3px;margin:-6px 96px 0 44px}
#seg div{position:absolute;top:0;bottom:0;background:#d9782f;border-radius:3px}
pre{background:#000;color:#9c9;padding:10px;border-radius:6px;max-height:200px;overflow:auto;font-size:12px;white-space:pre-wrap;margin:0}
#result{width:min(100%,560px);background:#000;display:none;border-radius:6px}
.hint{color:#888;font-size:12px}
kbd{background:#333;border-radius:3px;padding:0 5px;font-family:inherit}
</style>
<aside id="list"></aside>
<main>
  <div id="stage"><video id="v" muted playsinline></video></div>
  <div class="row">
    <button class="mode on" id="mcrop" onclick="setMode('crop')">Crop</button>
    <button class="mode" id="mpix" onclick="setMode('pix')">Pixelate</button>
    <label><input type="checkbox" id="lock" checked> lock 16:9</label>
    <button onclick="S.crop=null;sel=-1;changed()">Reset crop</button>
    <button onclick="delSel()">Delete selected box</button>
    <span id="ninfo" class="hint"></span>
  </div>
  <div class="row">
    <button id="play" onclick="v.paused?v.play():v.pause()">Play</button>
    <input type="range" id="scrub" min="0" max="1" step="0.001" value="0">
    <span id="tc" style="min-width:90px">0.00 / 0.00</span>
  </div>
  <div id="seg"><div></div></div>
  <div class="row">
    In <input type="number" id="tin" step="0.05" min="0"> <button onclick="S.in=+v.currentTime.toFixed(2);changed()">In = now</button>
    Out <input type="number" id="tout" step="0.05" min="0"> <button onclick="S.out=+v.currentTime.toFixed(2);changed()">Out = now</button>
    <label><input type="checkbox" id="loop" checked> loop in→out</label>
    <span id="len" class="hint"></span>
  </div>
  <div class="row">
    <button class="pri" id="rbtn" onclick="render(name)">Render this</button>
    <button id="rall" onclick="renderAll()">Render all</button>
    <span class="hint">drag = new box · drag inside = move · corner = resize · <kbd>space</kbd> play · <kbd>I</kbd>/<kbd>O</kbd> in/out · <kbd>←</kbd>/<kbd>→</kbd> frame (<kbd>shift</kbd> = 1 s) · <kbd>⌫</kbd> delete box</span>
  </div>
  <pre id="log">Pick a video on the left. Settings auto-save to tools/trims.json.</pre>
  <video id="result" controls muted loop playsinline></video>
</main>
<script>
const $=id=>document.getElementById(id), v=$('v'), stage=$('stage');
let cfg, name, S, W, H, DUR, mode='crop', sel=-1, drag=null, saveT;

fetch('/config').then(r=>r.json()).then(c=>{cfg=c;
  const names=Object.keys(c), hero=names.filter(n=>n.startsWith('hero_')), tasks=names.filter(n=>!n.startsWith('hero_'));
  $('list').innerHTML=`<h3>Hero (1080p)</h3>${hero.map(btn).join('')}<h3>Policy rollouts (720p)</h3>${tasks.map(btn).join('')}`;
  pick(names[0]);});
const btn=n=>`<button id="b_${n}" onclick="pick('${n}')">${n}<small>${cfg[n].src} · ${cfg[n].dur.toFixed(1)} s</small></button>`;

function pick(n){ name=n; const it=cfg[n]; W=it.w; H=it.h; DUR=it.dur; S=it.settings; if(S.out==null) S.out=+DUR.toFixed(2);
  stage.style.aspectRatio=W+'/'+H; document.querySelectorAll('aside button').forEach(b=>b.classList.toggle('on',b.id==='b_'+n));
  $('scrub').max=DUR; v.src='/proxy/'+n; v.currentTime=S.in; sel=-1; $('result').style.display='none'; draw(); }

function setMode(m){ mode=m; $('mcrop').classList.toggle('on',m==='crop'); $('mpix').classList.toggle('on',m==='pix'); $('mpix').classList.toggle('pix',m==='pix'); }
function delSel(){ if(sel>=0){S.pix.splice(sel,1); sel=-1; changed();} }
function changed(){ draw(); clearTimeout(saveT); saveT=setTimeout(()=>fetch('/save',{method:'POST',body:JSON.stringify({name,settings:S})}),300); }

const k=()=>stage.getBoundingClientRect().width/W;
function draw(){
  stage.querySelectorAll('.box').forEach(b=>b.remove());
  const s=k(), mk=(b,cls,i)=>{const d=document.createElement('div'); d.className='box '+cls+(i===sel?' sel':''); d.dataset.i=i;
    d.style.cssText=`left:${b[0]*s}px;top:${b[1]*s}px;width:${b[2]*s}px;height:${b[3]*s}px`; d.innerHTML='<div class="h"></div>'; stage.appendChild(d);};
  if(S.crop) mk(S.crop,'crop',-1);
  S.pix.forEach((b,i)=>mk(b,'pix',i));
  $('ninfo').textContent=(S.crop?`crop ${S.crop[2]}×${S.crop[3]} @ ${S.crop[0]},${S.crop[1]}`:'crop: full frame')+` · ${S.pix.length} pixelate box${S.pix.length===1?'':'es'}`;
  $('tin').value=S.in; $('tout').value=S.out; $('len').textContent=`→ ${(S.out-S.in).toFixed(2)} s`;
  $('seg').firstElementChild.style.cssText=`left:${S.in/DUR*100}%;right:${100-S.out/DUR*100}%`;
}
window.onresize=draw;
$('tin').onchange=e=>{S.in=Math.max(0,Math.min(+e.target.value,S.out-0.1));changed();};
$('tout').onchange=e=>{S.out=Math.min(DUR,Math.max(+e.target.value,S.in+0.1));changed();};

const pos=e=>{const r=stage.getBoundingClientRect(),s=k();return [(e.clientX-r.left)/s,(e.clientY-r.top)/s];};
const ev=x=>Math.round(x/2)*2;
function clampBox(b,kind){
  if(kind==='crop'&&$('lock').checked&&b[3]>H){b[3]=H;b[2]=H*16/9;}
  b[2]=Math.min(b[2],W); b[3]=Math.min(b[3],H);
  b[0]=Math.max(0,Math.min(b[0],W-b[2])); b[1]=Math.max(0,Math.min(b[1],H-b[3]));
  for(let i=0;i<4;i++) b[i]=ev(b[i]);
}
stage.onpointerdown=e=>{
  const [sx,sy]=pos(e), el=e.target.closest('.box'), handle=e.target.classList.contains('h');
  let box, kind;
  if(el){ if(el.classList.contains('crop')){box=S.crop;kind='crop';sel=-1;} else {sel=+el.dataset.i;box=S.pix[sel];kind='pix';} }
  else { box=[sx,sy,0,0]; kind=mode; if(mode==='crop'){S.crop=box;sel=-1;} else {S.pix.push(box);sel=S.pix.length-1;} }
  drag={box,kind,op:handle?'resize':el?'move':'new',sx,sy,o:[...box]}; stage.setPointerCapture(e.pointerId); draw();
};
stage.onpointermove=e=>{
  if(!drag) return; const [sx,sy]=pos(e), b=drag.box, o=drag.o;
  if(drag.op==='move'){ b[0]=o[0]+sx-drag.sx; b[1]=o[1]+sy-drag.sy; }
  else {
    let x0=Math.min(o[0],sx), x1=Math.max(o[0],sx), y0=Math.min(o[1],sy), y1=Math.max(o[1],sy);
    if(drag.op==='resize'){ x0=o[0]; y0=o[1]; x1=Math.max(x0+8,sx); y1=Math.max(y0+8,sy); }
    if(drag.kind==='crop'&&$('lock').checked) y1=y0+(x1-x0)*9/16;
    b[0]=x0; b[1]=y0; b[2]=x1-x0; b[3]=y1-y0;
  }
  clampBox(b,drag.kind); draw();
};
stage.onpointerup=()=>{ if(!drag) return;
  if(drag.box[2]<8||drag.box[3]<8||(drag.kind==="crop"&&drag.box[2]<240)){ if(drag.kind==='crop') S.crop=null; else {S.pix.splice(S.pix.indexOf(drag.box),1); sel=-1;} }
  drag=null; changed(); };

v.onplay=()=>$('play').textContent='Pause'; v.onpause=()=>$('play').textContent='Play';
v.onended=()=>{ if($('loop').checked){v.currentTime=S.in; v.play();} };
let scrubbing=false; $('scrub').oninput=e=>{scrubbing=true; v.currentTime=+e.target.value;}; $('scrub').onchange=()=>scrubbing=false;
(function tick(){ if(S){ if(!scrubbing) $('scrub').value=v.currentTime; $('tc').textContent=`${v.currentTime.toFixed(2)} / ${DUR.toFixed(2)}`;
  if($('loop').checked&&!v.paused&&v.currentTime>=S.out) v.currentTime=S.in; } requestAnimationFrame(tick); })();

document.onkeydown=e=>{ if(e.target.tagName==='INPUT') return;
  const f=e.shiftKey?1:1/29.97;
  if(e.code==='Space'){e.preventDefault(); v.paused?v.play():v.pause();}
  else if(e.key==='i'){S.in=+v.currentTime.toFixed(2);changed();} else if(e.key==='o'){S.out=+v.currentTime.toFixed(2);changed();}
  else if(e.key==='ArrowLeft'){v.pause();v.currentTime=Math.max(0,v.currentTime-f);} else if(e.key==='ArrowRight'){v.pause();v.currentTime=Math.min(DUR,v.currentTime+f);}
  else if(e.key==='Backspace'||e.key==='Delete') delSel(); };

async function render(n){ $('rbtn').disabled=$('rall').disabled=true; $('log').textContent=`rendering ${n} …`;
  const r=await fetch('/render',{method:'POST',body:JSON.stringify({name:n,settings:n===name?S:null})}).then(r=>r.json());
  $('log').textContent=r.log; $('rbtn').disabled=$('rall').disabled=false;
  if(r.ok){$('result').style.display='block'; $('result').src=`/out/${n}.mp4?${Date.now()}`;} return r.ok; }
async function renderAll(){ for(const n of Object.keys(cfg)){ $('log').textContent=`rendering ${n} …`; if(!await render(n)) break; } }
</script>
"""

if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--render":
        for n in (ITEMS if sys.argv[2] == "all" else [sys.argv[2]]):
            render(n)
        sys.exit()
    if sys.argv[1:]:  # only list the named clips
        ITEMS = {n: ITEMS[n] for n in sys.argv[1:]}
    srv = ThreadingHTTPServer(("127.0.0.1", 8765), H)
    print("http://localhost:8765  (ctrl-c to stop)")
    webbrowser.open("http://localhost:8765")
    srv.serve_forever()
