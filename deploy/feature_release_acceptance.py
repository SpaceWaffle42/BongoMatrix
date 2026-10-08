"""Finding-bound release caller, not release authorization.

Target default: stdin operator JSON, in the app PrivateNetwork namespace, <=55s.
Host --host: existing SSH stdio -> nsenter TCP bytes -> loopback-only Chrome.
No pairing, saved config, uploads, normalization, or state restore is performed.
Independent exact-file review, main CI and live disposable PID1 proof are gates.
"""
from __future__ import annotations
import argparse
import base64
import struct
import zlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import select
import shlex
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time

OPERATOR_SHA = '3456cba7611fba503657fdd807e09bde4463187333bac96f2677473b8211ad85'
IDENTITIES = ('binding_sha256','source_identity','spec_sha256','stage','phase','app_pid','nonce')
HEX = re.compile(r'[0-9a-f]{64}')
LIMIT = 2 * 1024 * 1024


class CallerError(ValueError): pass


def require(condition, message):
    if not condition: raise CallerError(message)


def encoded(value): return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
def sha(data): return hashlib.sha256(data).hexdigest()


def parent_fd(path):
    path=Path(path).absolute()
    require('..' not in path.parts, 'noncanonical artifact path')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for name in path.parts[1:-1]:
            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd); fd=child
        return fd,path.name
    except BaseException:
        os.close(fd); raise


def read_private(path, limit=LIMIT):
    parent,name=parent_fd(path)
    try: fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
    finally: os.close(parent)
    try:
        before=os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and before.st_size<=limit,
                'artifact not bounded regular single-link file')
        chunks=[]; total=0
        while total<=limit:
            chunk=os.read(fd,min(65536,limit+1-total))
            if not chunk: break
            chunks.append(chunk); total+=len(chunk)
        after=os.fstat(fd)
        require(total<=limit and (before.st_size,before.st_mtime_ns,before.st_ino)==
                (after.st_size,after.st_mtime_ns,after.st_ino),'artifact changed/oversized')
        return b''.join(chunks)
    finally: os.close(fd)


def publish_private(path, value):
    parent,name=parent_fd(path)
    try:
        fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
        try:
            data=encoded(value)+b'\n'
            with os.fdopen(fd,'wb',closefd=False) as stream: stream.write(data); stream.flush()
            os.fsync(fd)
        finally: os.close(fd)
        os.fsync(parent)
    finally: os.close(parent)


def load(path): return json.loads(read_private(path))


def png_rgba(value):
    """Decode bounded non-interlaced RGB/RGBA proof PNG; reject missing pixels."""
    require(type(value) is str and len(value)<131072,'raw PNG evidence absent')
    try: raw=base64.b64decode(value,validate=True)
    except ValueError: raise CallerError('raw PNG malformed') from None
    require(raw.startswith(b'\x89PNG\r\n\x1a\n'),'raw PNG signature absent')
    offset=8; compressed=bytearray(); channels=None; ended=False
    while offset+12<=len(raw):
        length=struct.unpack('>I',raw[offset:offset+4])[0]
        kind=raw[offset+4:offset+8]; body=raw[offset+8:offset+8+length]
        require(offset+12+length<=len(raw),'PNG chunk truncated')
        crc=struct.unpack('>I',raw[offset+8+length:offset+12+length])[0]
        require(zlib.crc32(kind+body)&0xffffffff==crc,'PNG CRC mismatch')
        if kind==b'IHDR':
            require(len(body)==13,'PNG header invalid')
            w,h,depth,color,compression,filtering,interlace=struct.unpack('>IIBBBBB',body)
            require((w,h,depth,compression,filtering,interlace)==(64,32,8,0,0,0) and color in (2,6),'PNG geometry/encoding invalid')
            channels=3 if color==2 else 4
        if kind==b'IDAT': compressed.extend(body)
        offset+=length+12
        if kind==b'IEND': ended=True; break
    require(ended and offset==len(raw) and channels in (3,4),'PNG incomplete')
    inflater=zlib.decompressobj()
    decoded=inflater.decompress(bytes(compressed),32769)
    require(inflater.eof and not inflater.unconsumed_tail and not inflater.unused_data and len(decoded)==32*(1+64*channels),'PNG raster invalid')
    stride=64*channels; previous=bytearray(stride); output=bytearray(); pos=0
    for _ in range(32):
        mode=decoded[pos]; pos+=1; row=bytearray(decoded[pos:pos+stride]); pos+=stride
        require(mode in range(5),'PNG filter invalid')
        for x in range(stride):
            left=row[x-channels] if x>=channels else 0; up=previous[x]; ul=previous[x-channels] if x>=channels else 0
            if mode==1: add=left
            elif mode==2: add=up
            elif mode==3: add=(left+up)//2
            elif mode==4:
                a=left+up-ul; distances=[abs(a-left),abs(a-up),abs(a-ul)]
                add=(left,up,ul)[distances.index(min(distances))]
            else: add=0
            row[x]=(row[x]+add)&255
        if channels==4: output.extend(row)
        else:
            for x in range(0,stride,3): output.extend(row[x:x+3]); output.append(255)
        previous=row
    return bytes(output)


def validate_browser_proof(request, proof):
    require(type(proof) is dict,'browser proof absent')
    for key in IDENTITIES: require(proof.get(key)==request[key], 'browser proof identity mismatch')
    require(proof.get('browser') is True and proof.get('framebuffer') is True and
            proof.get('offline') is False,'actual browser/framebuffer assertions absent')
    require(proof.get('version_visible')==request['version'] and proof.get('page_errors')==0 and
            proof.get('saved_config_unchanged') is True,'version/browser/state assertion failed')
    if request['phase']=='rollback':
        frame=proof.get('predecessor_frame',{})
        require(sha(png_rgba(frame.get('png')))==frame.get('sha256'),'predecessor retained PNG missing/wrong')
        require(proof.get('predecessor_dashboard') is True and frame.get('dimensions')==[64,32]
                and frame.get('http')==200 and type(frame.get('revision')) is int and frame['revision']>0
                and HEX.fullmatch(str(frame.get('sha256',''))),'predecessor live frame absent')
    else:
        require(set(proof.get('roles',{}))=={'splash','sleep'},'candidate role assertions absent')
        for role,p in proof['roles'].items():
            require(all(p.get(k) is True for k in ('library_native','checkbox','independent',
                    'inspect_selected','singleton')),'native browser assertion absent')
            require(type(p.get('selected_id')) is str and p['selected_id'] and
                    type(p.get('source')) is str and p['source'] and p.get('layout_x')==7,
                    'selected row/layout absent')
            require(sha(png_rgba(p.get('native_png')))==p.get('frame_pixels_sha256') and
                    sha(png_rgba(p.get('inspector_png')))==p.get('inspector_pixels_sha256'),
                    'retained actual PNG bytes do not prove matching pixels')
            require(p.get('png_dimensions')==[64,32] and p.get('inspect_http')==200 and
                    p.get('native_http')==202 and p.get('heartbeat_http')==202 and
                    p.get('foreign_heartbeat_http')==409 and p.get('cancel_http')==202,
                    'backend/lease/frame failure')
            require(HEX.fullmatch(str(p.get('frame_pixels_sha256',''))) and
                    p.get('frame_pixels_sha256')==p.get('inspector_pixels_sha256'), 'actual frame pixels absent/wrong')
            require(type(p.get('frame_revision_before')) is int and
                    type(p.get('frame_revision_after')) is int and
                    p['frame_revision_after']>p['frame_revision_before']>=0 and
                    type(p.get('generation')) is int and p['generation']>0 and
                    re.fullmatch('[0-9a-f]{32}',str(p.get('owner',''))) and
                    p.get('current_module')=='device-draft:'+role, 'stale/wrong frame provenance')
    return proof


def verify_sources(binding, release, phase):
    root=Path(binding['destination'])
    expected=dict(binding['unaffected'])
    expected.update(binding['predecessor'] if phase=='rollback' else
                    {e['path']:e for e in release['files']})
    result={}
    for relative,e in expected.items():
        require(not Path(relative).is_absolute() and '..' not in Path(relative).parts,'source path unsafe')
        path=root/relative
        if e.get('absent'):
            require(not path.exists() and not path.is_symlink(),'predecessor addition not absent'); continue
        data=read_private(path,16*1024*1024)
        require(sha(data)==e['sha256'],'phase source bytes mismatch')
        info=path.stat(follow_symlinks=False)
        for k,v in (('uid',info.st_uid),('gid',info.st_gid),('mode',f'{stat.S_IMODE(info.st_mode):04o}')):
            if k in e: require(e[k]==v,'source metadata mismatch')
        result[relative]=sha(data)
    return result


def stop_owned(process):
    # Always signal the owned session group, even if its original leader exited.
    try: os.killpg(process.pid,signal.SIGTERM)
    except ProcessLookupError: pass
    try: process.wait(timeout=.5)
    except subprocess.TimeoutExpired: pass
    try: os.killpg(process.pid,signal.SIGKILL)
    except ProcessLookupError: pass
    process.wait(timeout=2)


def namespace_identity(pid, *, inside):
    require(type(pid) is int and pid>1,'invalid app PID')
    done=subprocess.run(['/usr/bin/systemctl','show','matrix-web.service',
        '--property=ActiveState,MainPID,ControlGroup,PrivateNetwork'],capture_output=True,timeout=3)
    require(done.returncode==0,'service identity unavailable')
    props=dict(line.split('=',1) for line in done.stdout.decode().splitlines() if '=' in line)
    require(props.get('ActiveState')=='active' and props.get('MainPID')==str(pid) and
            props.get('PrivateNetwork')=='yes','app PID/PrivateNetwork mismatch')
    group=props.get('ControlGroup','')
    require(group.startswith('/') and '..' not in Path(group).parts,'cgroup identity invalid')
    private=os.stat(f'/proc/{pid}/ns/net').st_ino
    require(private!=os.stat('/proc/1/ns/net').st_ino,'app is reachable in host namespace')
    members=[]
    for path in (Path('/sys/fs/cgroup')/group.lstrip('/')).rglob('cgroup.procs'):
        members.extend(int(p) for p in path.read_text().split())
    require(pid in members and all(os.stat(f'/proc/{p}/ns/net').st_ino==private for p in members),
            'app writer cgroup namespace mismatch')
    if inside: require(os.stat('/proc/self/ns/net').st_ino==private,'caller not in app namespace')
    ticks=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]
    return dict(pid=pid,start_ticks=ticks,net_inode=private,cgroup=group,
                boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())


def operator(stage):
    path=Path(stage)/'scoped_release_operator.py'
    require(sha(read_private(path))==OPERATOR_SHA,'frozen operator differs')
    spec=importlib.util.spec_from_file_location('frozen_operator',path)
    require(spec is not None and spec.loader is not None,'operator load unavailable')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def state_evidence(stage,binding):
    module=operator(stage)
    checkpoint=read_private(Path(stage)/'checkpoint.json')
    run=load(Path(stage)/'run.json')
    # Rollback links to the same contained initial checkpoint. Final is fresh.
    require(run['checkpoint_sha256']==sha(checkpoint),'checkpoint/run link mismatch')
    require(run['binding_sha256']==sha(read_private(Path(stage)/'binding.json')),'checkpoint binding mismatch')
    expected=module.verify_checkpoint(stage)
    actual={r:module.tree_snapshot(Path(binding['destination'])/r) for r in module.MUTABLE_ROOTS}
    require(actual==expected,'actual private bytes/metadata differ from checkpoint')
    return dict(checkpoint_sha256=sha(checkpoint),state_sha256=sha(encoded(actual)))


def validate_request(context):
    require(set(context)==set(IDENTITIES)-{'nonce'},'operator stdin fields mismatch')
    require(context['phase'] in ('initial','rollback','final'),'phase invalid')
    for k in ('binding_sha256','source_identity','spec_sha256'):
        require(HEX.fullmatch(str(context[k])),'operator identity invalid')
    stage=Path(context['stage'])
    require(stage.is_absolute() and '..' not in stage.parts,'stage unsafe')
    info=stage.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and stat.S_IMODE(info.st_mode)==0o700,'stage not root-private')
    binding=load(stage/'binding.json'); release=load(stage/'release-spec.json')
    require(sha(read_private(stage/'binding.json'))==context['binding_sha256'] and
            binding['source_identity']==context['source_identity'] and binding['stage']==str(stage) and
            binding['spec_sha256']==context['spec_sha256']==sha(read_private(stage/'release-spec.json')),
            'actual stage binding differs')
    return binding,release


DIAGNOSTIC_SHA = '709c66be37fe70a0e91c5f3072afc4511bab242c16bd1c7d9275bdb7363bd5f5'


def failure_custody():
    path=Path(__file__).with_name('release_failure_custody.py')
    require(sha(read_private(path))==DIAGNOSTIC_SHA,'diagnostic helper identity mismatch')
    spec=importlib.util.spec_from_file_location('release_failure_custody',path)
    require(spec is not None and spec.loader is not None,'diagnostic helper unavailable')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def target(context, *, wait_seconds=46):
    stage_name='TARGET_REQUEST'; request={}
    try:
        deadline=time.monotonic()+wait_seconds
        stage_name='TARGET_REQUEST'
        binding,release=validate_request(context)
        stage_name='TARGET_PROCESS'
        identity=namespace_identity(context['app_pid'],inside=True)
        stage_name='TARGET_SOURCE'
        hashes=verify_sources(binding,release,context['phase'])
        stage_name='TARGET_STATE'
        before=state_evidence(context['stage'],binding)
        stage_name='TARGET_VERSION'
        version_match=re.search(rb'^APPLICATION_VERSION = "([^"]+)"$',read_private(Path(binding['destination'])/'webapp.py'),re.M)
        require(version_match is not None,'phase version missing')
        stage_name='TARGET_DRIVER_PIN'
        driver=load(Path(context['stage'])/'caller-driver.json')
        require(set(driver)=={'sha256'} and HEX.fullmatch(str(driver['sha256'])),'reviewed driver pin absent')
        request={**context,'nonce':secrets.token_hex(32),'version':version_match[1].decode(),
                 'process_identity':identity,'issued_at':time.time(),'driver_sha256':driver['sha256']}
        stage=Path(context['stage']); phase=context['phase']
        stage_name='TARGET_REQUEST_PUBLISH'
        publish_private(stage/f'caller-request-{phase}.json',request)
        stage_name='TARGET_RECEIPT_WAIT'
        receipt=stage/f'caller-receipt-{phase}.json'
        while time.monotonic()<deadline:
            if receipt.exists(): break
            time.sleep(min(.05,max(0,deadline-time.monotonic())))
        require(receipt.exists(),'bounded real browser receipt unavailable')
        stage_name='TARGET_RECEIPT_VALIDATE'
        proof=load(receipt)
        validate_browser_proof(request,proof)
        require(proof.get('driver_sha256')==driver['sha256'],'reviewed driver identity differs')
        stage_name='TARGET_PROCESS_RECHECK'
        require(namespace_identity(context['app_pid'],inside=True)==identity,'app identity changed during journey')
        stage_name='TARGET_SOURCE_RECHECK'
        require(verify_sources(binding,release,phase)==hashes,'runtime changed during journey')
        stage_name='TARGET_STATE_RECHECK'
        after=state_evidence(stage,binding)
        require(after==before,'private state changed during browser journey')
        stage_name='TARGET_NATIVE_DRIVER'
        retained={**request,'browser_evidence':proof,'runtime_hashes':hashes,
                  'preservation_before':before,'preservation_after':after,
                  'native_driver':native_driver(context['app_pid']), 'accepted':True}
        stage_name='TARGET_PROOF_PUBLISH'
        path=stage/f'caller-proof-{phase}.json'
        publish_private(path,retained)
        stage_name='TARGET_PROOF_READBACK'
        return {k:request[k] for k in IDENTITIES}|{
            'accepted':True,'checks':{'browser':True,'framebuffer':True,'preservation':True},
            'evidence_sha256':sha(read_private(path))}
    except BaseException as primary:
        try:
            pins={k:context[k] for k in IDENTITIES if k in context}
            pins.update(operator_sha256=OPERATOR_SHA,caller_sha256=sha(read_private(Path(__file__))))
            for key in ('nonce','driver_sha256'):
                if key in request:pins[key]=request[key]
            if request:pins['request_sha256']=sha(encoded(request)+b'\n')
            failure_custody().retain(pins,'target',stage_name,primary,2)
        except BaseException:
            print('MATRIX_FAILURE_CUSTODY_UNAVAILABLE; no acceptance',file=sys.stderr)
        raise


def native_driver(pid):
    maps=Path(f'/proc/{pid}/maps').read_text()
    paths=sorted({line.split()[-1] for line in maps.splitlines()
                  if re.search(r'/rgbmatrix/[^ ]+\.so(?: |$)',line)})
    require(paths,'native rgbmatrix mapping unproved (simulation cannot pass)')
    return {'mapped_extensions':[{ 'path':p,'sha256':sha(read_private(p,32*1024*1024))} for p in paths],
            'limit':'native extension mapped; logical successful-swap pixels, not photographic LED observation'}


def phase_request(stage,phase):
    request=load(Path(stage)/f'caller-request-{phase}.json')
    require(request['stage']==stage and request['phase']==phase and
            HEX.fullmatch(request['nonce']) and 0<=time.time()-request['issued_at']<55,'phase request stale/wrong')
    identity=namespace_identity(request['app_pid'],inside=False)
    require(identity==request['process_identity'],'driver context PID changed')
    return request


def driver_context(stage,phase):
    request=phase_request(stage,phase)
    binding=load(Path(stage)/'binding.json')
    root=Path(binding['destination'])
    registry=json.loads(read_private(root/'data/paired-devices.json'))
    require(type(registry) is list and registry,'no existing registered device; no pairing mutation authorized')
    device=registry[0]['id']
    require(re.fullmatch('[A-Za-z0-9_-]{32}',device),'registered device invalid')
    # Actual WSGI sets secret_key and session_generation to the same existing secret.
    # Use Flask's own serializer, but NEVER import WSGI/start a second controller.
    code="from flask import Flask; import json,sys; from flask.sessions import SecureCookieSessionInterface; d=json.load(sys.stdin); a=Flask('caller'); a.secret_key=d['key']; print(SecureCookieSessionInterface().get_signing_serializer(a).dumps({'paired':True,'generation':d['key'],'device_id':d['device'],'_permanent':True}))"
    secret=read_private(root/'data/session.key',256).decode().strip()
    require(len(secret)>=32,'existing session key unavailable')
    done=subprocess.run([str(root/'venv/bin/python'),'-I','-B','-c',code],
        input=encoded({'key':secret,'device':device}),capture_output=True,timeout=3)
    require(done.returncode==0,'existing authorized session serializer unavailable')
    cookie=done.stdout.decode().strip()
    require(cookie and '\n' not in cookie,'session serialization failed')
    return {**request,'cookie':cookie,'caller_sha256':sha(read_private(Path(__file__))) }


def pipe_relay(pid, stage, phase):
    request=phase_request(stage,phase)
    require(request['app_pid']==pid,'relay phase PID mismatch')
    require(namespace_identity(pid,inside=True)==request['process_identity'],'relay app birth identity mismatch')
    end=time.monotonic()+50
    with socket.create_connection(('127.0.0.1',5000),timeout=3) as peer:
        peer.setblocking(False); os.set_blocking(0,False); os.set_blocking(1,False)
        pending_to_peer=bytearray(); pending_to_stdout=bytearray(); input_open=True; peer_open=True
        while time.monotonic()<end and (peer_open or pending_to_stdout):
            reads=([0] if input_open and len(pending_to_peer)<65536 else [])+([peer] if peer_open and len(pending_to_stdout)<65536 else [])
            writes=([peer] if pending_to_peer else [])+([1] if pending_to_stdout else [])
            ready_r,ready_w,_=select.select(reads,writes,[],min(.2,max(0,end-time.monotonic())))
            if 0 in ready_r:
                b=os.read(0,65536)
                if b: pending_to_peer.extend(b)
                else: input_open=False
            if peer in ready_r:
                b=peer.recv(65536)
                if b: pending_to_stdout.extend(b)
                else: peer_open=False
            if peer in ready_w:
                n=peer.send(pending_to_peer); del pending_to_peer[:n]
            if 1 in ready_w:
                n=os.write(1,pending_to_stdout); del pending_to_stdout[:n]
            if not input_open and not pending_to_peer:
                try: peer.shutdown(socket.SHUT_WR)
                except OSError: pass
        require(time.monotonic()<end,'relay deadline expired')


def ssh_argv(args,remote):
    return ['/usr/bin/ssh','-F','/dev/null','-o','BatchMode=yes','-o','IdentitiesOnly=yes',
            '-o','IdentityAgent=none','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=3',
            '-o','UserKnownHostsFile='+args.known_hosts,'-i',args.identity,'--',args.target,
            shlex.join(remote)]


def remote(args,mode,*,body=None,timeout=3):
    argv=ssh_argv(args,['sudo','-n','/usr/bin/python3','-I','-B',args.program,
                      mode,'--stage',args.stage,'--phase',args.phase])
    done=subprocess.run(argv,input=body,capture_output=True,timeout=timeout)
    require(done.returncode==0,'private SSH caller adapter failed (output withheld)')
    require(len(done.stdout)<=LIMIT,'SSH receipt oversized')
    return done.stdout


class Bridge(socketserver.ThreadingTCPServer):
    allow_reuse_address=False
    daemon_threads=True
    block_on_close=True


class BridgeHandler(socketserver.BaseRequestHandler):
    def handle(self):
        server=self.server; process=None
        try:
            require(time.monotonic()<server.deadline,'bridge expired')
            request=server.context
            command=['sudo','-n','/usr/bin/nsenter','--target',str(request['app_pid']),'--net','--',
                     '/usr/bin/python3','-I','-B',server.args.program,'--pipe','--app-pid',str(request['app_pid']),
                     '--stage',server.args.stage,'--phase',server.args.phase]
            process=subprocess.Popen(ssh_argv(server.args,command),stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True)
            with server.owned_lock: server.owned.add(process)
            relay_socket_process(self.request,process,server.deadline)
        except (OSError,ValueError,subprocess.SubprocessError): server.failed.set()
        finally:
            if process is not None:
                stop_owned(process)
                with server.owned_lock: server.owned.discard(process)


def relay_socket_process(peer,process,deadline):
    require(process.stdin is not None and process.stdout is not None,'pipe missing')
    peer.setblocking(False); os.set_blocking(process.stdin.fileno(),False); os.set_blocking(process.stdout.fileno(),False)
    to_child=bytearray(); to_peer=bytearray(); peer_open=True; child_open=True
    while time.monotonic()<deadline and (child_open or to_peer):
        reads=([peer] if peer_open and len(to_child)<65536 else [])+([process.stdout] if child_open and len(to_peer)<65536 else [])
        writes=([process.stdin] if to_child else [])+([peer] if to_peer else [])
        rr,ww,_=select.select(reads,writes,[],min(.1,max(0,deadline-time.monotonic())))
        if peer in rr:
            b=peer.recv(65536)
            if b: to_child.extend(b)
            else: peer_open=False
        if process.stdout in rr:
            b=os.read(process.stdout.fileno(),65536)
            if b: to_peer.extend(b)
            else: child_open=False
        if process.stdin in ww:
            n=os.write(process.stdin.fileno(),to_child); del to_child[:n]
        if peer in ww:
            n=peer.send(to_peer); del to_peer[:n]
        if not peer_open and not to_child and not process.stdin.closed: process.stdin.close()
    require(time.monotonic()<deadline,'owned bridge timed out')


FAILURE_STAGES = frozenset('INPUT_PARSE CDP_CONNECT PAGE_READY VERSION_CHECK SAVED_BEFORE SELECT DRAFT_ASSERT INSPECT_HTTP INSPECT_DECODE EARLIER_FRAME DEVICE_START HEARTBEAT_OWN HEARTBEAT_FOREIGN STATUS_ASSERT FRAME_POLL FRAME_DIMENSIONS_ASSERT FRAME_REVISION_ASSERT FRAME_PIXELS_ASSERT CANCEL DRAFT_AFTER SAVED_AFTER PAGE_ERRORS'.split())
FAILURE_ROUTES = frozenset(['/api/config','/api/status','/api/display-frame'] + ['/api/'+prefix+'/'+role for prefix in ('edit-preview','device-preview') for role in ('splash','sleep')] + ['/api/device-preview/heartbeat','/api/device-preview/cancel'])


def validate_failure(raw, phase):
    """Exact finite schema, no coercion/reflection, duplicate keys rejected."""
    require(type(raw) is bytes and 0<len(raw)<=8192,'malformed browser failure')
    def pairs(items):
        result={}
        for key,value in items:
            require(key not in result,'duplicate diagnostic key');result[key]=value
        return result
    def constant(_): raise CallerError('nonfinite diagnostic')
    value=json.loads(raw,object_pairs_hook=pairs,parse_constant=constant)
    def keys(obj, expected): require(type(obj) is dict and set(obj)==set(expected),'diagnostic keys')
    def integer(v, maximum=2147483647, nullable=False):
        require((nullable and v is None) or (type(v) is int and 0<=v<=maximum),'diagnostic integer')
    keys(value,('schema','kind','component','phase','stage','role','exception_class','http','frames','cleanup'))
    require(type(value['schema']) is int and value['schema']==1 and value['kind']=='MATRIX_CALLER_FAILURE' and value['component']=='browser','diagnostic schema')
    require(value['phase']==phase and phase in ('initial','rollback','final'),'diagnostic phase')
    require(type(value['stage']) is str and value['stage'] in FAILURE_STAGES,'diagnostic stage')
    require(value['role'] in ('none','splash','sleep','rollback'),'diagnostic role')
    require(value['exception_class'] in ('SyntaxError','TypeError','RangeError','AssertionError','Error','OTHER'),'diagnostic class')
    h=value['http'];keys(h,('method','path','status'))
    require(h['method'] in (None,'GET','POST') and (h['path'] is None or (type(h['path']) is str and h['path'] in FAILURE_ROUTES)),'diagnostic HTTP')
    require(h['status'] is None or type(h['status']) is int and 100<=h['status']<=599,'diagnostic status')
    f=value['frames'];keys(f,('inspector_count','observed_count','inspector_hashes','observed_hashes','hashes_truncated','before_revision','last_revision','max_revision','dimensions','revision_advanced','pixels_matched','match_seen','elapsed_ms'))
    for key in ('inspector_count','observed_count','hashes_truncated'): integer(f[key],1000000)
    for key in ('before_revision','last_revision','max_revision'): integer(f[key],nullable=True)
    integer(f['elapsed_ms'],60000)
    for key in ('revision_advanced','pixels_matched','match_seen'): require(type(f[key]) is bool,'diagnostic boolean')
    require(f['dimensions'] is None or type(f['dimensions']) is list and len(f['dimensions'])==2,'diagnostic dimensions')
    if f['dimensions'] is not None:
        for n in f['dimensions']: integer(n,4096)
    for key in ('inspector_hashes','observed_hashes'):
        require(type(f[key]) is list and len(f[key])<=16,'diagnostic digest count')
        for digest in f[key]: require(type(digest) is str and HEX.fullmatch(digest),'diagnostic digest')
        require(len(f[key])==len(set(f[key])),'diagnostic duplicate digest')
    keys(value['cleanup'],('attempted','completed'))
    require(all(type(v) is bool for v in value['cleanup'].values()),'diagnostic cleanup')
    return value


def retain_failure(context, evidence, driver, node, raw, classification):
    """Only validated host-owned request pins link this diagnostic; never a proof."""
    fields=('phase','app_pid','nonce','binding_sha256','source_identity','spec_sha256','driver_sha256','caller_sha256')
    if not all(k in context for k in fields): return
    phase=context['phase'];require(phase in ('initial','rollback','final'),'host diagnostic phase')
    require(type(context['app_pid']) is int and context['app_pid']>0,'host diagnostic PID')
    for k in fields[2:]: require(type(context[k]) is str and HEX.fullmatch(context[k]),'host diagnostic identity')
    require(context['driver_sha256']==sha(read_private(driver)) and context['caller_sha256']==sha(read_private(Path(__file__))),'host diagnostic source differs')
    diagnostic=None
    if classification=='BROWSER_FAILED':
        try: diagnostic=validate_failure(raw,phase)
        except (ValueError,TypeError,KeyError,RecursionError): classification='MALFORMED_BROWSER_FAILURE'
    record={'schema':1,'kind':'MATRIX_HOST_FAILURE','acceptance':False,'request':{k:context[k] for k in fields},'host_pid':os.getpid(),'node_pid':node.pid if node else None,'exit_code':node.returncode if node else None,'classification':classification,'stderr_bytes':min(len(raw),2147483647),'diagnostic':diagnostic}
    publish_private(Path(evidence)/('failure-'+phase+'.json'),record)


def chrome_journey(context,base,evidence,deadline,driver):
    browser=None; node=None; err=b''; failure_class=None
    with tempfile.TemporaryDirectory(prefix='owned-chrome-',dir=evidence) as tmp:
        profile=Path(tmp)/'profile'
        try:
            browser=subprocess.Popen(['/opt/google/chrome/chrome','--headless','--no-sandbox','--disable-gpu',
                '--remote-debugging-address=127.0.0.1','--remote-debugging-port=0',f'--user-data-dir={profile}',
                '--no-first-run','--disable-background-networking','--no-proxy-server','about:blank'],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            active=profile/'DevToolsActivePort'
            while not active.exists() and time.monotonic()<deadline:
                require(browser.poll() is None,'owned Chrome exited'); time.sleep(.03)
            require(active.exists(),'owned Chrome readiness timeout')
            port=active.read_text().splitlines()[0]
            node=subprocess.Popen(['node',str(driver)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,start_new_session=True)
            out,err=node.communicate(encoded({**context,'debug_port':int(port),'base':base}),
                                     timeout=max(.01,deadline-time.monotonic()))
            if node.returncode!=0 or err or len(out)>LIMIT:
                failure_class='BROWSER_FAILED'
            require(node.returncode==0 and not err and len(out)<=LIMIT,'native Chrome journey failed (output withheld)')
            return json.loads(out)
        except subprocess.TimeoutExpired:
            failure_class='NODE_TIMEOUT';raise
        except (ValueError,OSError,KeyError,TypeError):
            if failure_class is None: failure_class='HOST_BROWSER_FAILED'
            raise
        finally:
            for p in (node,browser):
                if p is not None: stop_owned(p)
            if failure_class is not None:
                retain_failure(context,evidence,driver,node,err,failure_class)


def host(args):
    require(args.target=='admin@192.168.1.4','unassigned SSH target')
    require(Path(args.evidence).is_dir(),'private host evidence directory missing')
    driver=Path(__file__).with_name('feature_release_browser.mjs')
    driver_sha=sha(read_private(driver))
    wait_end=time.monotonic()+180
    context=None
    while time.monotonic()<wait_end:
        try: context=json.loads(remote(args,'--driver-context')); break
        except (CallerError,subprocess.TimeoutExpired): time.sleep(.1)
    require(context is not None,'bounded phase handshake unavailable')
    for key,value in [('binding_sha256',args.binding_sha256),('source_identity',args.source_identity),
                      ('spec_sha256',args.spec_sha256),('stage',args.stage),('phase',args.phase)]:
        require(context.get(key)==value,'host expected phase identity mismatch')
    require(context['driver_sha256']==driver_sha,'host driver pin differs')
    require(context.get('caller_sha256')==sha(read_private(Path(__file__))),'host/target caller bytes differ')
    deadline=time.monotonic()+min(40,max(0,44-(time.time()-context['issued_at'])))
    bridge=Bridge(('127.0.0.1',0),BridgeHandler)
    bridge.args=args; bridge.context=context; bridge.deadline=deadline
    bridge.owned=set(); bridge.owned_lock=threading.Lock(); bridge.failed=threading.Event()
    thread=threading.Thread(target=bridge.serve_forever,daemon=True); thread.start()
    try:
        proof=chrome_journey(context,'http://127.0.0.1:'+str(bridge.server_address[1]),args.evidence,deadline,driver)
        validate_browser_proof(context,proof)
        require(not bridge.failed.is_set(),'namespace SSH transport failed')
        proof['driver_sha256']=driver_sha
        # Sanitized metadata only; cookies/config bytes never retained on host.
        output=Path(args.evidence)/f"browser-{args.phase}-{context['nonce']}.json"
        publish_private(output,proof)
        remote(args,'--deliver',body=encoded(proof),timeout=max(.01,deadline-time.monotonic()))
        return {'status':'DELIVERED_REAL_BROWSER_RECEIPT','sha256':sha(read_private(output)),
                'human_visual':'pending'}
    finally:
        bridge.shutdown(); bridge.server_close(); thread.join(timeout=2)
        with bridge.owned_lock:
            for p in list(bridge.owned): stop_owned(p)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    modes=parser.add_mutually_exclusive_group()
    for flag in ('host','driver-context','deliver','pipe'): modes.add_argument('--'+flag,action='store_true')
    for key in ('stage','phase','program','target','identity','known-hosts','evidence','binding-sha256','source-identity','spec-sha256'):
        parser.add_argument('--'+key)
    parser.add_argument('--app-pid',type=int)
    args=parser.parse_args()
    if args.pipe: pipe_relay(args.app_pid,args.stage,args.phase); return
    if args.driver_context:
        print(json.dumps(driver_context(args.stage,args.phase))); return
    if args.deliver:
        request=phase_request(args.stage,args.phase)
        proof=json.loads(sys.stdin.buffer.read(LIMIT+1)); validate_browser_proof(request,proof)
        require(proof.get('driver_sha256')==request['driver_sha256'],'receipt driver differs')
        publish_private(Path(args.stage)/f'caller-receipt-{args.phase}.json',proof); return
    if args.host: result=host(args)
    else:
        raw=sys.stdin.buffer.read(8193); require(len(raw)<=8192,'stdin context oversized')
        result=target(json.loads(raw))
    print(json.dumps(result,sort_keys=True,separators=(',',':')))


if __name__=='__main__':
    try: main()
    except (ValueError,OSError,KeyError,TypeError,subprocess.SubprocessError):
        print('caller refused/failed; private evidence retained; no acceptance',file=sys.stderr)
        raise SystemExit(2)
