#!/usr/bin/env python3
"""Fail-closed authenticated browser acceptance for Priority 1.5 B1."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+-preview")
_VIEWPORTS = (
    (320, 568), (360, 800), (390, 844), (412, 915),
    (768, 1024), (820, 1180), (1024, 768), (1280, 720), (1440, 900),
    (599, 800), (600, 800), (601, 800), (899, 800), (900, 800), (901, 800),
)
_TOUCH_VIEWPORTS = frozenset(viewport for viewport in _VIEWPORTS if viewport[0] <= 1024)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--session-cookie-file", required=True, type=Path)
    parser.add_argument("--expected-source-sha", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--chromium", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    return parser


def validate_session_cookie_file(path: Path) -> str:
    if not path.is_absolute():
        raise ValueError("session cookie file path must be absolute")
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or path.resolve(strict=True) != path:
        raise ValueError("session cookie input must be a nonsymlink regular file")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise ValueError("session cookie input must be owner-only mode 0600")
        raw_bytes = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    if len(raw_bytes) > 4096:
        raise ValueError("session cookie input is invalid")
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("session cookie input is invalid") from error
    if not raw.endswith("\n") or raw.count("\n") != 1:
        raise ValueError("session cookie input must contain exactly one line")
    cookie = raw[:-1]
    if not cookie or len(cookie) > 4096 or "\r" in cookie or "\n" in cookie or "=" not in cookie:
        raise ValueError("session cookie input is invalid")
    return cookie


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _geometry(value: object) -> bool:
    return (
        type(value) is list and len(value) == 4
        and all(_number(item) for item in value)
    )


def build_report(*, source_sha: str, version: str, viewports: list[dict[str, object]]) -> dict[str, object]:
    if _SHA_RE.fullmatch(source_sha) is None or _VERSION_RE.fullmatch(version) is None:
        raise ValueError("report identity is invalid")
    if (
        type(viewports) is not list
        or len(viewports) != len(_VIEWPORTS)
        or any(type(item) is not dict for item in viewports)
        or [item.get("viewport") for item in viewports] != [list(item) for item in _VIEWPORTS]
    ):
        raise ValueError("exact responsive viewport evidence is required without omissions or duplicates")
    allowed = {
        "viewport", "interaction_exercised", "touch", "orientation", "document_overflow_x",
        "sticky_preview_inside_viewport", "mode_controls_inside_viewport",
        "sticky_mode_overlap", "touch_targets", "state", "drag_resize",
        "pointercancel", "frame_sha256",
    }
    target_fields = {"name", "width", "height"}
    state_fields = {"selected_layer", "focus", "draft_sha256", "zoom", "local_scroll", "preserved"}
    geometry_fields = {"before", "after_drag", "expected_after_drag", "after_resize", "expected_after_resize"}
    cancel_fields = {"capture_before", "capture_after", "geometry_unchanged"}
    clean = []
    for expected, item in zip(_VIEWPORTS, viewports, strict=True):
        if set(item) != allowed:
            raise ValueError("viewport report fields are invalid")
        touch = expected in _TOUCH_VIEWPORTS
        expected_orientation = "landscape" if expected[0] > expected[1] else "portrait"
        if (
            item["viewport"] != list(expected)
            or type(item["interaction_exercised"]) is not bool
            or item["interaction_exercised"] is not (expected == (412, 915))
            or type(item["touch"]) is not bool or item["touch"] is not touch
            or item["orientation"] != expected_orientation
            or not _number(item["document_overflow_x"])
            or item["document_overflow_x"] > 1
            or item["sticky_preview_inside_viewport"] is not True
            or item["mode_controls_inside_viewport"] is not True
            or item["sticky_mode_overlap"] is not False
            or type(item["frame_sha256"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", item["frame_sha256"]) is None
        ):
            raise ValueError("viewport layout evidence is invalid")
        targets = item["touch_targets"]
        if type(targets) is not list or (touch and not targets) or (not touch and targets):
            raise ValueError("touch target evidence is invalid")
        for target in targets:
            if (
                type(target) is not dict or set(target) != target_fields
                or type(target["name"]) is not str or not target["name"]
                or not _number(target["width"]) or not _number(target["height"])
                or target["width"] < 44 or target["height"] < 44
            ):
                raise ValueError("touch target evidence is invalid")
        state = item["state"]
        if (
            type(state) is not dict or set(state) != state_fields
            or type(state["selected_layer"]) is not str or not state["selected_layer"]
            or type(state["focus"]) is not str or not state["focus"]
            or type(state["draft_sha256"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", state["draft_sha256"]) is None
            or not _number(state["zoom"]) or state["zoom"] < 1
            or type(state["local_scroll"]) is not list or len(state["local_scroll"]) != 2
            or not all(_number(value) and value >= 0 for value in state["local_scroll"])
            or not any(value > 0 for value in state["local_scroll"])
            or state["preserved"] is not True
        ):
            raise ValueError("responsive interaction state evidence is invalid")
        geometry = item["drag_resize"]
        if (
            type(geometry) is not dict or set(geometry) != geometry_fields
            or not all(_geometry(geometry[field]) for field in geometry_fields)
            or geometry["after_drag"] != geometry["expected_after_drag"]
            or geometry["after_resize"] != geometry["expected_after_resize"]
        ):
            raise ValueError("exact drag and resize geometry evidence is invalid")
        cancel = item["pointercancel"]
        if (
            type(cancel) is not dict or set(cancel) != cancel_fields
            or cancel["capture_before"] is not True
            or cancel["capture_after"] is not False
            or cancel["geometry_unchanged"] is not True
        ):
            raise ValueError("pointercancel capture release evidence is invalid")
        clean.append(dict(item))
    if len({item["frame_sha256"] for item in clean}) != 1:
        raise ValueError("stable Advanced media frame parity is required")
    interaction = [item for item in clean if item["interaction_exercised"]]
    if len(interaction) != 1:
        raise ValueError("exactly one interaction viewport is required")
    if any(
        item["drag_resize"] != interaction[0]["drag_resize"]
        or item["pointercancel"] != interaction[0]["pointercancel"]
        for item in clean
    ):
        raise ValueError("global interaction parity must use the exercised evidence")
    return {"version": 2, "source_sha": source_sha, "application_version": version, "viewports": clean}


def write_report(path: Path, report: dict[str, object]) -> None:
    if not path.is_absolute():
        raise ValueError("report path must be absolute")
    parent = path.parent.resolve(strict=True)
    if parent != path.parent:
        raise ValueError("report parent must not contain symlinks")
    if path.exists():
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise ValueError("report path must be an owner-only nonsymlink regular file")
    raw = (json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _terminate(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


_CDP_PROBE = r"""
const debugPort=process.argv[1],baseUrl=process.argv[2],cookie=await new Promise((resolve,reject)=>{let value='';process.stdin.setEncoding('utf8');process.stdin.on('data',chunk=>value+=chunk);process.stdin.on('end',()=>resolve(value));process.stdin.on('error',reject)}),sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));let endpoint;
for(let attempt=0;attempt<60;attempt++){try{endpoint=await fetch(`http://127.0.0.1:${debugPort}/json/new`,{method:'PUT'}).then(response=>response.json());break}catch(_){await sleep(100)}}if(!endpoint)throw Error('CDP endpoint unavailable');
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map();let id=0,loadedResolve,loaded,errors=[];socket.onmessage=event=>{const message=JSON.parse(event.data);if(message.id&&pending.has(message.id)){const request=pending.get(message.id);pending.delete(message.id);message.error?request.reject(Error(JSON.stringify(message.error))):request.resolve(message.result)}else if(message.method==='Page.loadEventFired')loadedResolve?.();else if(message.method==='Runtime.exceptionThrown')errors.push(message.params.exceptionDetails.text)};await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>new Promise((resolve,reject)=>{const request=++id;pending.set(request,{resolve,reject});socket.send(JSON.stringify({id:request,method,params}))}),evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw Error(JSON.stringify(result.exceptionDetails));return result.result.value},waitFor=async expression=>{for(let attempt=0;attempt<240;attempt++){try{if(await evaluate(expression))return}catch(_){}await sleep(25)}throw Error('browser readiness timeout')},settle=()=>evaluate(`new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve(true))))`),metrics=async(width,height)=>{await send('Emulation.setTouchEmulationEnabled',{enabled:true,maxTouchPoints:2});await send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false,screenOrientation:{type:width>height?'landscapePrimary':'portraitPrimary',angle:width>height?90:0}});await settle()};
let touchId=10;const touchStart=async point=>{if(!point.hit)throw Error('Advanced media canvas point is obstructed');const current=touchId++;await send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:point.x,y:point.y,id:current,radiusX:2,radiusY:2,force:1}]});return current},touchCancel=()=>send('Input.dispatchTouchEvent',{type:'touchCancel',touchPoints:[]}),geometry=()=>evaluate(`(()=>{const module=customModulesFromEditor().find(item=>item.id===__b1ModuleId),layer=module.layers.find(item=>item.id===__b1LayerId),p=layer.properties;return [p.x,p.y,p.width,p.height]})()`),canvasPoint=(x,y)=>evaluate(`(async()=>{const canvas=document.querySelector('.advanced-card[data-id="'+CSS.escape(__b1ModuleId)+'"] canvas');canvas.scrollIntoView({block:'center'});await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));let box=canvas.getBoundingClientRect(),px=box.left+${x}*box.width/64,py=box.top+${y}*box.height/32;const preview=document.querySelector('.preview-live'),previewBox=preview.classList.contains('is-popped')?preview.getBoundingClientRect():{bottom:0},low=Math.min(innerHeight-20,previewBox.bottom+20),high=innerHeight-20;if(py<low||py>high){scrollBy(0,py-Math.max(low,Math.min(high,py)));await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));box=canvas.getBoundingClientRect();px=box.left+${x}*box.width/64;py=box.top+${y}*box.height/32}const hit=document.elementFromPoint(px,py);return{x:px,y:py,hit:hit===canvas||canvas.contains(hit)}})()`),drag=async(from,to)=>{const start=await canvasPoint(...from),finish=await canvasPoint(...to);if(!start.hit||!finish.hit)throw Error('Advanced media canvas point is obstructed');await send('Input.dispatchMouseEvent',{type:'mousePressed',x:start.x,y:start.y,button:'left',buttons:1,clickCount:1});await send('Input.dispatchMouseEvent',{type:'mouseMoved',x:finish.x,y:finish.y,button:'left',buttons:1});await send('Input.dispatchMouseEvent',{type:'mouseReleased',x:finish.x,y:finish.y,button:'left',buttons:0,clickCount:1});await settle()};
try{await send('Runtime.enable');await send('Page.enable');await send('Network.enable');await send('Network.setExtraHTTPHeaders',{headers:{Cookie:cookie}});await metrics(390,844);loaded=new Promise(resolve=>loadedResolve=resolve);await send('Page.navigate',{url:baseUrl});await loaded;await waitFor(`globalThis.matrixCustomCanvasReady===true&&document.readyState==='complete'`);
 await evaluate(`(async()=>{const drafts=customModulesFromEditor(),module=drafts.find(item=>item.type==='canvas'&&item.layers.some(layer=>layer.type==='media'&&layer.properties.source));if(!module)throw Error('authenticated Advanced media module is required');const layer=module.layers.find(item=>item.type==='media'&&item.properties.source);globalThis.__b1ModuleId=module.id;globalThis.__b1LayerId=layer.id;let card=document.querySelector('.advanced-card[data-id="'+CSS.escape(module.id)+'"]');if(!card)throw Error('Advanced media card is absent');card.querySelector('.layer-track-label[data-layer-id="'+CSS.escape(layer.id)+'"]')?.click();for(const [property,value]of [['x',8],['y',6],['width',16],['height',8]]){const input=document.querySelector('.advanced-card[data-id="'+CSS.escape(module.id)+'"] [data-property="'+property+'"]');if(!input)throw Error('Advanced media geometry control is absent');input.value=String(value);input.dispatchEvent(new Event('input',{bubbles:true}));await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))}card=document.querySelector('.advanced-card[data-id="'+CSS.escape(module.id)+'"]');await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));card=document.querySelector('.advanced-card[data-id="'+CSS.escape(module.id)+'"]');const focus=card.querySelector('[data-property="rotation"]')||card.querySelector('[data-property]'),scroll=card.querySelector('.aseprite-timeline-scroll');if(!focus||!scroll)throw Error('Advanced media state controls are absent');focus.focus({preventScroll:true});card.scrollIntoView({block:'center'});globalThis.resizePreviewPopout?.();await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));scroll.scrollLeft=0;scroll.scrollTop=Math.min(31,scroll.scrollHeight-scroll.clientHeight)})()`);await send('Emulation.setPageScaleFactor',{pageScaleFactor:2});await settle();await evaluate(`(()=>{const scroll=document.querySelector('.advanced-card[data-id="'+CSS.escape(__b1ModuleId)+'"] .aseprite-timeline-scroll');scroll.scrollLeft=0;scroll.scrollTop=Math.min(31,scroll.scrollHeight-scroll.clientHeight)})()`);
 const stateSnapshot=()=>evaluate(`(async()=>{const module=customModulesFromEditor().find(item=>item.id===__b1ModuleId),card=document.querySelector('.advanced-card[data-id="'+CSS.escape(__b1ModuleId)+'"]'),scroll=card.querySelector('.aseprite-timeline-scroll'),active=card.querySelector('.layer-track-label.active'),focus=document.activeElement,draft=new TextEncoder().encode(JSON.stringify(module)),digest=await crypto.subtle.digest('SHA-256',draft);return{selected_layer:active?.dataset.layerId||'',focus:focus?.dataset?.property||focus?.dataset?.control||'',draft_sha256:[...new Uint8Array(digest)].map(value=>value.toString(16).padStart(2,'0')).join(''),zoom:Math.round((visualViewport?.scale||1)*1000)/1000,local_scroll:[scroll.scrollLeft,scroll.scrollTop]}})()`);let expectedState=await stateSnapshot();
 const reports=[],sizes=[[320,568],[360,800],[390,844],[412,915],[768,1024],[820,1180],[1024,768],[1280,720],[1440,900],[599,800],[600,800],[601,800],[899,800],[900,800],[901,800]],interactionViewport=[412,915];let interactionEvidence=null;
 for(const [width,height]of sizes){await metrics(width,height);const resizedState=await stateSnapshot(),preserved=resizedState.selected_layer===expectedState.selected_layer&&resizedState.focus===expectedState.focus&&resizedState.draft_sha256===expectedState.draft_sha256&&Math.abs(resizedState.zoom-expectedState.zoom)<.001&&resizedState.local_scroll.every((value,index)=>Math.abs(value-expectedState.local_scroll[index])<=1);
  await send('Emulation.setPageScaleFactor',{pageScaleFactor:1});await evaluate(`(()=>{const canvas=document.querySelector('.advanced-card[data-id="'+CSS.escape(__b1ModuleId)+'"] canvas');canvas.scrollIntoView({block:'center'});globalThis.resizePreviewPopout?.()})()`);await settle();
  if(width===interactionViewport[0]&&height===interactionViewport[1]){await evaluate(`(async()=>{for(const [property,value]of [['x',8],['y',6],['width',16],['height',8]]){const input=document.querySelector('.advanced-card[data-id="'+CSS.escape(__b1ModuleId)+'"] [data-property="'+property+'"]');input.value=String(value);input.dispatchEvent(new Event('input',{bubbles:true}));await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))}})()`);const before=await geometry();await drag([9,7],[11,9]);const afterDrag=await geometry(),expectedAfterDrag=[before[0]+2,before[1]+2,before[2],before[3]];await drag([afterDrag[0]+afterDrag[2],afterDrag[1]+afterDrag[3]],[afterDrag[0]+afterDrag[2]+2,afterDrag[1]+afterDrag[3]+1]);const afterResize=await geometry(),expectedAfterResize=[afterDrag[0],afterDrag[1],afterDrag[2]+2,afterDrag[3]+1],beforeCancel=await geometry(),cancelPoint=await canvasPoint(beforeCancel[0]+1,beforeCancel[1]+1);await evaluate(`(()=>{const canvas=document.querySelector('.advanced-card[data-id="'+CSS.escape(__b1ModuleId)+'"] canvas');globalThis.__b1CancelCanvas=canvas;globalThis.__b1CancelPointer=null;canvas.addEventListener('pointerdown',event=>{globalThis.__b1CancelPointer=event.pointerId},{capture:true,once:true})})()`);await touchStart(cancelPoint);await sleep(25);const captureBefore=await evaluate(`__b1CancelPointer!==null&&__b1CancelCanvas.hasPointerCapture(__b1CancelPointer)`);await touchCancel();await settle();const captureAfter=await evaluate(`__b1CancelPointer!==null&&__b1CancelCanvas.hasPointerCapture(__b1CancelPointer)`),afterCancel=await geometry();interactionEvidence={drag_resize:{before,after_drag:afterDrag,expected_after_drag:expectedAfterDrag,after_resize:afterResize,expected_after_resize:expectedAfterResize},pointercancel:{capture_before:captureBefore,capture_after:captureAfter,geometry_unchanged:JSON.stringify(beforeCancel)===JSON.stringify(afterCancel)}};await evaluate(`(async()=>{for(const [property,value]of [['x',8],['y',6],['width',16],['height',8]]){const input=document.querySelector('.advanced-card[data-id="'+CSS.escape(__b1ModuleId)+'"] [data-property="'+property+'"]');input.value=String(value);input.dispatchEvent(new Event('input',{bubbles:true}));await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))}})()`)}await send('Emulation.setPageScaleFactor',{pageScaleFactor:2});await settle();
  const layout=await evaluate(`(async()=>{const inside=rect=>rect.left>=-1&&rect.top>=-1&&rect.right<=innerWidth+1&&rect.bottom<=innerHeight+1,overlap=(a,b)=>Math.min(a.right,b.right)-Math.max(a.left,b.left)>.5&&Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>.5,preview=document.querySelector('.preview-live'),modes=document.querySelector('.preview-modes'),visual=document.querySelector('.preview-visual'),previewRect=preview.getBoundingClientRect(),modeRect=modes.getBoundingClientRect(),visualRect=visual.getBoundingClientRect(),card=document.querySelector('.advanced-card[data-id="'+CSS.escape(__b1ModuleId)+'"]'),label=card.querySelector('.layer-track-label.active'),targets=[['preview-live',document.querySelector('label[for="preview-mode-live"]')],['preview-edit',document.querySelector('label[for="preview-mode-edit"]')],['selected-layer',label]].map(([name,node])=>{const rect=node.getBoundingClientRect();return{name,width:Math.round(rect.width*1000)/1000,height:Math.round(rect.height*1000)/1000}}),canvas=card.querySelector('canvas'),encoded=canvas.toDataURL('image/png').split(',',2)[1],bytes=Uint8Array.from(atob(encoded),character=>character.charCodeAt(0)),digest=await crypto.subtle.digest('SHA-256',bytes),hash=[...new Uint8Array(digest)].map(value=>value.toString(16).padStart(2,'0')).join(''),version=(document.querySelector('.sidebar-foot span:last-child')?.textContent||'').replace(/^Version /,'');return{version,document_overflow_x:document.documentElement.scrollWidth-document.documentElement.clientWidth,sticky_preview_inside_viewport:preview.classList.contains('is-popped')&&inside(previewRect),mode_controls_inside_viewport:inside(modeRect),sticky_mode_overlap:overlap(visualRect,modeRect),targets,frame_sha256:hash}})()`);
  await evaluate(`(()=>{const card=document.querySelector('.advanced-card[data-id="'+CSS.escape(__b1ModuleId)+'"]'),focus=card.querySelector('[data-property="rotation"]')||card.querySelector('[data-property]');focus.focus({preventScroll:true})})()`);await settle();await evaluate(`(()=>{const scroll=document.querySelector('.advanced-card[data-id="'+CSS.escape(__b1ModuleId)+'"] .aseprite-timeline-scroll');scroll.scrollLeft=0;scroll.scrollTop=Math.min(31,scroll.scrollHeight-scroll.clientHeight)})()`);expectedState=await stateSnapshot();const touch=width<=1024;reports.push({version:layout.version,viewport:[width,height],interaction_exercised:width===interactionViewport[0]&&height===interactionViewport[1],touch,orientation:width>height?'landscape':'portrait',document_overflow_x:layout.document_overflow_x,sticky_preview_inside_viewport:layout.sticky_preview_inside_viewport,mode_controls_inside_viewport:layout.mode_controls_inside_viewport,sticky_mode_overlap:layout.sticky_mode_overlap,touch_targets:touch?layout.targets:[],state:{...resizedState,preserved},frame_sha256:layout.frame_sha256})}if(!interactionEvidence)throw Error('safe interaction viewport was not exercised');for(const report of reports){report.drag_resize=interactionEvidence.drag_resize;report.pointercancel=interactionEvidence.pointercancel}
 console.log(JSON.stringify({reports,errors}))}finally{socket.close()}
"""

def run_browser(base_url: str, cookie: str, chromium: Path) -> list[dict[str, object]]:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("base URL must be an origin or path without credentials/query/fragment")
    if not chromium.is_absolute() or chromium.resolve(strict=True) != chromium or not os.access(chromium, os.X_OK):
        raise ValueError("chromium must be an absolute executable nonsymlink path")
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Node.js is required for the raw CDP harness")
    with tempfile.TemporaryDirectory(prefix="matrix-b1-browser-") as directory:
        profile = Path(directory) / "profile"
        browser = subprocess.Popen([
            str(chromium), "--headless", "--no-sandbox", "--disable-gpu",
            "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            active = profile / "DevToolsActivePort"
            for _ in range(100):
                if active.is_file():
                    break
                if browser.poll() is not None:
                    raise RuntimeError("Chromium exited before publishing a CDP port")
                time.sleep(.05)
            if not active.is_file():
                raise RuntimeError("Chromium CDP port unavailable")
            completed = subprocess.run(
                [node, "--input-type=module", "-e", _CDP_PROBE, active.read_text().splitlines()[0], base_url.rstrip("/") + "/"],
                input=cookie, text=True, capture_output=True, timeout=120, check=False,
            )
        finally:
            _terminate(browser)
    if completed.returncode != 0:
        raise RuntimeError("authenticated browser acceptance failed")
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    if result.get("errors") or not isinstance(result.get("reports"), list):
        raise RuntimeError("browser errors or malformed evidence")
    return result["reports"]


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if _SHA_RE.fullmatch(arguments.expected_source_sha) is None:
            raise ValueError("expected source SHA must be 40 lowercase hex characters")
        if _VERSION_RE.fullmatch(arguments.expected_version) is None:
            raise ValueError("expected version is invalid")
        cookie = validate_session_cookie_file(arguments.session_cookie_file)
        viewports = run_browser(arguments.base_url, cookie, arguments.chromium)
        if [item.get("viewport") for item in viewports] != [list(item) for item in _VIEWPORTS]:
            raise ValueError("browser did not complete the exact responsive viewport matrix")
        if any(item.get("version") != arguments.expected_version for item in viewports):
            raise ValueError("deployed application version mismatch")
        public_viewports = [{key: value for key, value in item.items() if key != "version"} for item in viewports]
        report = build_report(source_sha=arguments.expected_source_sha, version=arguments.expected_version, viewports=public_viewports)
        write_report(arguments.report, report)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
