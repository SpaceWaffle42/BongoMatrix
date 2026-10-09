"""Diagnostic-only bounded failure custody. Never acceptance or journal authority."""
import json
import os
from pathlib import Path
import re
import sys

STAGES = frozenset(('OPERATOR_EXECUTE','OPERATOR_RECOVER','OPERATOR_LAUNCH',
    'TARGET_REQUEST','TARGET_PROCESS','TARGET_SOURCE','TARGET_STATE','TARGET_VERSION',
    'TARGET_DRIVER_PIN','TARGET_REQUEST_PUBLISH','TARGET_RECEIPT_WAIT','TARGET_RECEIPT_VALIDATE',
    'TARGET_PROCESS_RECHECK','TARGET_SOURCE_RECHECK','TARGET_STATE_RECHECK',
    'TARGET_NATIVE_DRIVER','TARGET_PROOF_PUBLISH','TARGET_PROOF_READBACK','OPERATOR_CALLER'))
SLOTS = frozenset(('primary','recovery-inline','recovery-stoppost','launcher','target','caller'))
CLASSES = frozenset(('ValueError','OperatorError','CallerError','OSError','FileNotFoundError',
    'PermissionError','TimeoutError','TimeoutExpired','CalledProcessError','KeyError','TypeError',
    'JSONDecodeError','RuntimeError','AssertionError','KeyboardInterrupt','SystemExit','BaseException'))
SITES = {'scoped_release_operator.py':frozenset(('execute','recover','recover_locked','run_supervised','accept','require','rollback_proof')),
         'feature_release_acceptance.py':frozenset(('target','_target','native_driver','require','publish_private','read_private','validate_browser_proof','verify_sources','state_evidence','namespace_identity','validate_request'))}
HASH_KEYS=frozenset(('binding_sha256','source_identity','spec_sha256','operator_sha256',
    'caller_sha256','driver_sha256','request_sha256','nonce'))
IDENTITY_KEYS=HASH_KEYS|{'stage','phase','app_pid'}
HEX=re.compile('[0-9a-f]{64}')

def identity(value):
    if type(value) is not dict or not set(value)<=IDENTITY_KEYS: raise ValueError('diagnosis identity unavailable')
    if value.get('phase') not in ('initial','rollback','final','abort'): raise ValueError('diagnosis phase unavailable')
    stage=value.get('stage')
    if type(stage) is not str or len(stage)>512 or not re.fullmatch('/[A-Za-z0-9_./-]+',stage) or '..' in Path(stage).parts:
        raise ValueError('diagnosis stage unavailable')
    for key in HASH_KEYS & value.keys():
        if type(value[key]) is not str or not HEX.fullmatch(value[key]): raise ValueError('diagnosis pin unavailable')
    if 'app_pid' in value and (type(value['app_pid']) is not int or not 1<value['app_pid']<2**31): raise ValueError('diagnosis PID unavailable')
    return dict(value)

def record(context,stage_name,exc,exit_code=None):
    pins=identity(context)
    if stage_name not in STAGES: raise ValueError('diagnosis stage unavailable')
    cls=type(exc).__name__
    if cls not in CLASSES: cls='BaseException'
    site=None; tb=exc.__traceback__
    while tb:
        code=tb.tb_frame.f_code; file=Path(code.co_filename).name
        if file in SITES and code.co_name in SITES[file]:
            site={'file':file,'function':code.co_name,'line':tb.tb_lineno}
        tb=tb.tb_next
    if exit_code is not None and (type(exit_code) is not int or not -255<=exit_code<=255): exit_code=None
    return {'schema':1,'kind':'MATRIX_FAILURE_CUSTODY','acceptance':False,
        'identity':pins,'stage_name':stage_name,'exception_class':cls,'site':site,'exit_code':exit_code}

def validate(value,context):
    if type(value) is not dict or set(value)!={'schema','kind','acceptance','identity','stage_name','exception_class','site','exit_code'}: raise ValueError('diagnosis malformed')
    if type(value['schema']) is not int or value['schema']!=1 or value['kind']!='MATRIX_FAILURE_CUSTODY' or value['acceptance'] is not False: raise ValueError('diagnosis malformed')
    pins=identity(value['identity']); expected=identity(context)
    if any(pins.get(k)!=v for k,v in expected.items()): raise ValueError('diagnosis identity mismatch')
    if value['stage_name'] not in STAGES or value['exception_class'] not in CLASSES: raise ValueError('diagnosis malformed')
    site=value['site']
    if site is not None:
        if type(site) is not dict or set(site)!={'file','function','line'} or site['file'] not in SITES or site['function'] not in SITES[site['file']] or type(site['line']) is not int or not 0<site['line']<100000: raise ValueError('diagnosis site malformed')
    code=value['exit_code']
    if code is not None and (type(code) is not int or not -255<=code<=255): raise ValueError('diagnosis exit malformed')
    return value

def parent_fd(stage):
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in Path(stage).parts[1:]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        return fd
    except BaseException:
        os.close(fd);raise

def filename(slot,phase):
    if slot not in SLOTS or phase not in ('initial','rollback','final','abort'): raise ValueError('diagnosis selector unavailable')
    return 'diagnostic-'+slot+'-'+phase+'.json'

def read(context,slot):
    pins=identity(context);fd=parent_fd(pins['stage'])
    try:
        child=os.open(filename(slot,pins['phase']),os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        try:
            import stat
            info=os.fstat(child)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>8192: raise ValueError('diagnosis unavailable')
            data=os.read(child,8193)
        finally:os.close(child)
    finally:os.close(fd)
    def unique(pairs):
        result={}
        for k,v in pairs:
            if k in result:raise ValueError('diagnosis duplicate')
            result[k]=v
        return result
    return validate(json.loads(data,object_pairs_hook=unique),pins)

def retain(context,slot,stage_name,exc,exit_code=None):
    try:
        return retain_value(record(context,stage_name,exc,exit_code),slot)
    except BaseException:
        print('MATRIX_FAILURE_CUSTODY_UNAVAILABLE; no acceptance',file=sys.stderr)
        return None


def retain_value(value,slot):
    # First failure in each slot is immutable; later recovery must not erase it.
    try:
        context=value['identity'];value=validate(value,context)
        fd=parent_fd(value['identity']['stage'])
        try:
            try:child=os.open(filename(slot,value['identity']['phase']),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
            except FileExistsError:return read(context,slot)
            try:
                data=json.dumps(value,sort_keys=True,separators=(',',':')).encode()+b'\n'
                with os.fdopen(child,'wb',closefd=False) as stream:stream.write(data);stream.flush()
                os.fsync(child)
            finally:os.close(child)
            os.fsync(fd)
        finally:os.close(fd)
        return value
    except BaseException:
        print('MATRIX_FAILURE_CUSTODY_UNAVAILABLE; no acceptance',file=sys.stderr)
        return None
