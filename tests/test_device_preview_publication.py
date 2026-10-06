"""Deterministic semantic publication boundary; real shipped lifecycle JS."""
import subprocess
from pathlib import Path
import unittest


class DevicePreviewPublicationTests(unittest.TestCase):
    def test_duplicate_events_preserve_pending_and_explicit_replacement_epochs(self):
        root = Path(__file__).parents[1]
        source = (root / 'app/templates/dashboard.html').read_text()
        lifecycle = source[source.index('let devicePreviewEnabled='):source.index('\nfunction editPreviewCurrent')]
        harness = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const timers=new Map(),listeners={},starts=[],pending=[];let timerId=0,name='first',enabled=true;
const toggle={checked:true,addEventListener(){}},status={dataset:{}};
const context={console,AbortController,Date,Number,Promise,Set,Map,Math,JSON,encodeURIComponent,
 setTimeout(fn,delay){const id=++timerId;timers.set(id,{fn,delay});return id},clearTimeout(id){timers.delete(id)},
 localStorage:{getItem:()=> 'true',setItem(){}},matrixSyncStopped:false,matrixEditorsReady:true,
 previewMode:'edit',previewModule:'weather',previewInstance:'a'.repeat(32),PAIRED_ACTION_SIGNAL:'paired-request',
 configBody:()=>({device:{name},enabled}),selectedLavaPaletteDescriptor:()=>null,devicePreviewDelay:()=>150,
 selectEditPreviewModule(module){context.previewModule=module},selectEditedPreviewModule(){},
 document:{querySelector:s=>s==='#device-preview-enabled'?toggle:status,addEventListener(n,h){listeners[n]=h}},
 window:{addEventListener(){}},fetch:async()=>({ok:true}),
 api:async(path,options)=>{const body=JSON.parse(options.body);if(path.endsWith('/cancel'))return{cancelled:true};starts.push({path,body});return new Promise(resolve=>pending.push(()=>resolve({generation:body.generation,heartbeat_after_seconds:60})))}
};vm.createContext(context);vm.runInContext(process.argv[1],context);
const run=s=>vm.runInContext(s,context),settle=()=>new Promise(r=>setImmediate(r));
const expire=async()=>{const entry=[...timers].find(([,t])=>t.delay<1000);assert.ok(entry,'publication debounce must remain pending');timers.delete(entry[0]);entry[1].fn();await settle()};
const accept=async()=>{assert.ok(pending.length);pending.shift()();await settle()};
(async()=>{
 listeners.input({target:{}});await expire();assert.equal(starts.length,1);
 listeners.change({target:{}}); // input timer expired, request still pending
 await accept();assert.equal([...timers.values()].filter(t=>t.delay<1000).length,0,'same-value change must not queue duplicate');assert.equal(starts.length,1);
 run('scheduleDevicePreview(150,true)'); // intentional same-value replacement, accepted request settled
 const timer=[...timers].find(([,t])=>t.delay===150)[0];
 listeners.input({target:{}});listeners.change({target:{}});
 assert.ok(timers.has(timer),'duplicate coalescing erased or restarted explicit replacement work');
 await expire();assert.equal(starts.length,2,'explicit same-value epoch must publish');await accept();
 name='newest';listeners.input({target:{}});name='newest final';listeners.input({target:{}});await expire();assert.equal(starts.length,3);assert.equal(starts[2].body.config.device.name,'newest final');await accept();
 enabled=false;listeners.change({target:{}});await expire();assert.equal(starts.length,4,'changed checkbox/select-only change must publish');await accept();
 run("publishDeviceDraftMutation('sleep',150)");await expire();assert.equal(starts.length,5);assert.equal(starts[4].path,'/api/device-preview/sleep');await accept();
 await run('stopDevicePreview()');run('resumeStickyDevicePreview()');await expire();assert.equal(starts.length,6,'re-enable same draft must publish');await accept();
 console.log('SEMANTIC_PUBLICATION_PASS');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run(['node', '-e', harness, lifecycle], cwd=root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('SEMANTIC_PUBLICATION_PASS', result.stdout)
