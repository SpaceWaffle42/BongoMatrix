"""Offline caller/transport evidence only, never production acceptance."""
import copy
import base64
import struct
import zlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('caller', ROOT / 'deploy/feature_release_acceptance.py')
assert spec and spec.loader
caller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(caller)

def request(phase='initial'):
    return dict(binding_sha256='a'*64, source_identity='b'*64,
                spec_sha256='c'*64, stage='/private/stage', phase=phase,
                app_pid=123, nonce='d'*64, version='offline-version')

def fixture_png():
    def chunk(k,b): return struct.pack('>I',len(b))+k+b+struct.pack('>I',zlib.crc32(k+b)&0xffffffff)
    rgba=bytes([10,20,30,255])*64*32
    scan=b''.join(b'\0'+rgba[y*256:(y+1)*256] for y in range(32))
    raw=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',64,32,8,6,0,0,0))+chunk(b'IDAT',zlib.compress(scan))+chunk(b'IEND',b'')
    return base64.b64encode(raw).decode(),caller.sha(rgba)

def proof(phase='initial'):
    png,digest=fixture_png()
    role = dict(native_png=png,inspector_png=png,selected_id='splash_row', source='built-in/example.gif',
                library_native=True, checkbox=True, layout_x=7,
                independent=True, inspect_selected=True, inspect_http=200,
                png_dimensions=[64,32], inspector_pixels_sha256=digest,
                frame_pixels_sha256=digest, frame_revision_before=1,
                frame_revision_after=2, native_http=202,
                heartbeat_http=202, foreign_heartbeat_http=409,
                cancel_http=202, generation=1, owner='f'*32,
                current_module='device-draft:splash', singleton=True)
    sleep = dict(role, selected_id='sleep_row', generation=2, current_module='device-draft:sleep')
    return dict(request(phase), browser=True, framebuffer=True, offline=False,
                version_visible='offline-version', page_errors=0,
                saved_config_unchanged=True, roles={'splash':role,'sleep':sleep},
                limits=['logical SwapOnVSync frame, not photographic LED proof',
                        'owner lease + singleton source + matching pixels, not per-frame owner metadata'])

class CallerContract(unittest.TestCase):
    def test_reject_stale_phase_nonce_or_identity(self):
        for key in ('nonce','phase','source_identity','spec_sha256','binding_sha256','app_pid','stage'):
            with self.subTest(key=key):
                p = proof(); p[key]='wrong'
                with self.assertRaises(ValueError): caller.validate_browser_proof(request(),p)

    def test_stale_or_wrong_pixels_rejected(self):
        for field,value in [('frame_revision_after',1),('frame_pixels_sha256','0'*64)]:
            p=proof(); p['roles']['splash'][field]=value
            with self.assertRaises(ValueError): caller.validate_browser_proof(request(),p)

    def test_offline_proof_cannot_be_production(self):
        p=proof(); p['offline']=True
        with self.assertRaises(ValueError): caller.validate_browser_proof(request(),p)

    def test_raw_native_png_is_required(self):
        p=proof(); p['roles']['splash']['native_png']='not PNG'
        with self.assertRaises(ValueError): caller.validate_browser_proof(request(),p)

    def test_valid_structural_proof(self):
        # Shape-only fixture; no release acceptance claim.
        self.assertEqual(caller.validate_browser_proof(request(),proof()),proof())

    def test_rollback_does_not_expect_grouped_candidate(self):
        p=proof('rollback'); p['roles']={}; p['predecessor_dashboard']=True
        png,digest=fixture_png()
        p['predecessor_frame']={'dimensions':[64,32],'revision':2,'sha256':digest,'png':png,'http':200}
        self.assertEqual(caller.validate_browser_proof(request('rollback'),p),p)
