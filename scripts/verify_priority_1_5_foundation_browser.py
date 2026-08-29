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
_VIEWPORTS = ((390, 844), (1280, 720))


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


def build_report(*, source_sha: str, version: str, viewports: list[dict[str, object]]) -> dict[str, object]:
    if _SHA_RE.fullmatch(source_sha) is None or _VERSION_RE.fullmatch(version) is None:
        raise ValueError("report identity is invalid")
    if (
        type(viewports) is not list
        or len(viewports) != 2
        or any(type(item) is not dict for item in viewports)
        or [item.get("viewport") for item in viewports] != [list(item) for item in _VIEWPORTS]
    ):
        raise ValueError("mobile and desktop viewport evidence is required")
    allowed = {"viewport", "frame", "geometry", "frame_sha256"}
    clean = []
    for item in viewports:
        if set(item) != allowed:
            raise ValueError("viewport report fields are invalid")
        if (
            type(item["viewport"]) is not list
            or type(item["frame"]) is not list
            or type(item["geometry"]) is not list
            or len(item["frame"]) != 4
            or len(item["geometry"]) != 4
            or any(type(value) not in (int, float) or not math.isfinite(value)
                   for value in [*item["frame"], *item["geometry"]])
            or type(item["frame_sha256"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", item["frame_sha256"]) is None
        ):
            raise ValueError("viewport frame geometry is invalid")
        clean.append(dict(item))
    return {"version": 1, "source_sha": source_sha, "application_version": version, "viewports": clean}


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
const debugPort=process.argv[1],baseUrl=process.argv[2],cookie=(await new Promise((resolve,reject)=>{let value='';process.stdin.setEncoding('utf8');process.stdin.on('data',chunk=>value+=chunk);process.stdin.on('end',()=>resolve(value));process.stdin.on('error',reject)}));
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));let endpoint;
for(let attempt=0;attempt<60;attempt++){try{endpoint=await fetch(`http://127.0.0.1:${debugPort}/json/new`,{method:'PUT'}).then(response=>response.json());break}catch(_){await sleep(100)}}
if(!endpoint)throw Error('CDP endpoint unavailable');
const socket=new WebSocket(endpoint.webSocketDebuggerUrl),pending=new Map();let id=0,loadedResolve,loaded,errors=[];
socket.onmessage=event=>{const message=JSON.parse(event.data);if(message.id&&pending.has(message.id)){const request=pending.get(message.id);pending.delete(message.id);message.error?request.reject(Error(JSON.stringify(message.error))):request.resolve(message.result)}else if(message.method==='Page.loadEventFired')loadedResolve?.();else if(message.method==='Runtime.exceptionThrown')errors.push(message.params.exceptionDetails.text)};
await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject});
const send=(method,params={})=>new Promise((resolve,reject)=>{const request=++id;pending.set(request,{resolve,reject});socket.send(JSON.stringify({id:request,method,params}))});
const evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw Error(JSON.stringify(result.exceptionDetails));return result.result.value};
const waitFor=async expression=>{for(let attempt=0;attempt<200;attempt++){try{if(await evaluate(expression))return}catch(_){}await sleep(25)}throw Error('browser readiness timeout')};
try{
 await send('Runtime.enable');await send('Page.enable');await send('Network.enable');
 await send('Network.setExtraHTTPHeaders',{headers:{Cookie:cookie}});
 const reports=[];
 for(const [width,height]of [[390,844],[1280,720]]){
  await send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width===390});
  loaded=new Promise(resolve=>loadedResolve=resolve);await send('Page.navigate',{url:baseUrl});await loaded;
  await waitFor(`globalThis.matrixCustomCanvasReady===true&&document.readyState==='complete'`);
  const result=await evaluate(`(async()=>{
   const drafts=customModulesFromEditor(),module=drafts.find(item=>item.type==='canvas'&&item.layers.some(layer=>layer.type==='media'&&layer.properties.source));
   if(!module)throw Error('authenticated Advanced media module is required');
   const card=document.querySelector('.advanced-card[data-id="'+CSS.escape(module.id)+'"]');if(!card)throw Error('Advanced media card is absent');
   const layer=module.layers.find(item=>item.type==='media'&&item.properties.source),label=card.querySelector('.layer-track-label[data-layer-id="'+CSS.escape(layer.id)+'"]');label?.click();
   const eventAt=(canvas,x,y,type,pointerId)=>{const box=canvas.getBoundingClientRect();canvas.dispatchEvent(new PointerEvent(type,{bubbles:true,pointerId,clientX:box.left+x*box.width/64,clientY:box.top+y*box.height/32,buttons:type==='pointerup'?0:1}))};
   let canvas=card.querySelector('canvas');canvas.setPointerCapture=()=>{};let before=customModulesFromEditor().find(item=>item.id===module.id).layers.find(item=>item.id===layer.id).properties;
   eventAt(canvas,before.x+1,before.y+1,'pointerdown',1);eventAt(canvas,before.x+2,before.y+2,'pointermove',1);eventAt(canvas,before.x+2,before.y+2,'pointerup',1);
   canvas=document.querySelector('.advanced-card[data-id="'+CSS.escape(module.id)+'"] canvas');canvas.setPointerCapture=()=>{};let moved=customModulesFromEditor().find(item=>item.id===module.id).layers.find(item=>item.id===layer.id).properties;
   eventAt(canvas,moved.x+moved.width,moved.y+moved.height,'pointerdown',2);eventAt(canvas,moved.x+moved.width+2,moved.y+moved.height+1,'pointermove',2);eventAt(canvas,moved.x+moved.width+2,moved.y+moved.height+1,'pointerup',2);
   canvas=document.querySelector('.advanced-card[data-id="'+CSS.escape(module.id)+'"] canvas');await new Promise(resolve=>setTimeout(resolve,300));
   const after=customModulesFromEditor().find(item=>item.id===module.id).layers.find(item=>item.id===layer.id).properties,box=canvas.getBoundingClientRect(),encoded=canvas.toDataURL('image/png').split(',',2)[1],bytes=Uint8Array.from(atob(encoded),character=>character.charCodeAt(0)),digest=await crypto.subtle.digest('SHA-256',bytes),hash=[...new Uint8Array(digest)].map(value=>value.toString(16).padStart(2,'0')).join(''),version=(document.querySelector('.sidebar-foot span:last-child')?.textContent||'').replace(/^Version /,'');
   return {version,viewport:[innerWidth,innerHeight],frame:[box.x,box.y,box.width,box.height].map(value=>Math.round(value*1000)/1000),geometry:[after.x,after.y,after.width,after.height],frame_sha256:hash};
  })()`);reports.push(result);
 }
 console.log(JSON.stringify({reports,errors}));
}finally{socket.close()}
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
                input=cookie, text=True, capture_output=True, timeout=60, check=False,
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
            raise ValueError("browser did not complete both required viewports")
        if any(item.get("version") != arguments.expected_version for item in viewports):
            raise ValueError("deployed application version mismatch")
        if viewports[0].get("geometry") != viewports[1].get("geometry") or viewports[0].get("frame_sha256") != viewports[1].get("frame_sha256"):
            raise ValueError("mobile and desktop Advanced media frame parity mismatch")
        public_viewports = [{key: value for key, value in item.items() if key != "version"} for item in viewports]
        report = build_report(source_sha=arguments.expected_source_sha, version=arguments.expected_version, viewports=public_viewports)
        write_report(arguments.report, report)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
