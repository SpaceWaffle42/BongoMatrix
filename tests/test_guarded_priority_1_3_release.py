import copy
import importlib
import json
import os
import re
import shlex
import shutil
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


def _discover_fake_final(publications, record):
    """Model identity-scoped, detached, exact external FINAL discovery."""
    if len(publications) > 1:
        raise ValueError("duplicate external FINAL authority")
    if not publications:
        return None
    publication = publications[0]
    if publication["record"] != record:
        raise ValueError("conflicting external FINAL authority")
    return copy.deepcopy(publication)


def _publish_fake_final(publications, record, note_id="900"):
    """Persist one exact FINAL and make duplicate/conflicting POSTs loud."""
    if publications:
        if publications[0]["record"] == record:
            raise AssertionError("duplicate FINAL POST")
        raise AssertionError("conflicting FINAL POST")
    publications.append({"note_id": str(note_id), "state": "FINAL",
                         "record": copy.deepcopy(record)})


class _FakeFinalAuthority:
    """Partition fake external FINAL notes by the bound transaction authority."""

    def bind_publication_authority(self, identity, transaction, **context):
        key = (identity.transaction_identity, transaction["transaction_id"],
               context["boundary"])
        stores = getattr(self, "_final_publications_by_authority", None)
        if stores is None:
            stores = {}
            self._final_publications_by_authority = stores
        self.final_publications = stores.setdefault(key, [])


def _fake_transaction_control(guarded, kind, request, holder, attempt_ids):
    """Exercise the real durable registry; never synthesize lifecycle successors."""
    del attempt_ids
    if kind not in guarded._TRANSACTION_HELPERS:
        return None
    if holder.get("record") is None:
        identity = guarded.canonical_release_identity(request["release_document"])
        registry_root = Path(tempfile.mkdtemp(prefix="guarded-transaction-authority-"))
        registry_root.chmod(0o700)
        holder["identity"] = identity
        holder["registry_root"] = registry_root
    authority = guarded.DurableTransactionRegistry(
        str(holder["registry_root"]), holder["identity"].release_key,
    )
    try:
        if kind == "remote-transaction-create-or-query":
            record = authority.create_or_query(request)
        elif kind == "remote-transaction-inspect":
            record = authority.inspect(request["transaction_id"])
        elif kind == "remote-transaction-claim":
            record = authority.claim(request)
        elif kind == "remote-transaction-transition":
            record = authority.transition(request)
        elif kind == "remote-transaction-terminal":
            record = authority.terminal(request)
        elif kind == "remote-transaction-advance":
            record = authority.advance(request)
        else:
            raise AssertionError(kind)
    finally:
        authority.close()
    holder["record"] = record
    terminal = json.loads(json.dumps(record))
    return guarded.ControlResult(0, terminal["record_digest"], terminal)


def _fake_commit_control(guarded, kind, request, terminals, *, committed_epoch=100.0):
    if kind != "remote-commit":
        return None
    if request.get("query") is True:
        terminal = terminals.get(request["attempt_id"])
        if terminal is None:
            return guarded.ControlResult(3, "")
        for field in guarded._COMMIT_QUERY_FIELDS - {"query"}:
            if request.get(field) != terminal["request"].get(field):
                return guarded.ControlResult(2, "")
        return guarded.ControlResult(0, terminal["result_digest"], terminal)
    success = {key: request[key] for key in (
        "schema", "boundary", "phase", "attempt_id", "operation_id",
        "evidence_digest", "pending_note_id",
    )}
    digest = __import__("hashlib").sha256(guarded._canonical_json_bytes(success) + b"\n").hexdigest()
    terminal = {"schema": 1, "request": json.loads(json.dumps(request)),
                "request_digest": guarded._request_digest("remote-commit", request),
                "result_digest": digest, "committed_epoch": committed_epoch}
    terminals[request["attempt_id"]] = terminal
    return guarded.ControlResult(0, digest, terminal)


class GuardedPriority13ReleaseTests(unittest.TestCase):
    def test_exported_run_help_is_a_real_cli_subcommand(self):
        script = Path(__file__).parents[1] / "deploy" / "guarded_priority_1_3_release.py"
        interpreter = Path(sys.executable)
        completed = subprocess.run(
            [str(interpreter), "-I", "-B", str(script), "run", "--help"],
            check=False, text=True, capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        for option in (
            "--boundary", "--ssh-target", "--ssh-identity-file", "--ssh-known-hosts-file",
            "--stage", "--bundle", "--destination-root", "--service",
            "--initial-service-state", "--health-url", "--health-status",
            "--health-timeout-seconds", "--attempt-budget-seconds",
            "--commit-reserve-seconds", "--watchdog-seconds", "--expected-sha",
            "--expected-version", "--previous-sha", "--previous-version",
            "--session-cookie-file", "--evidence-dir", "--gitlab-project",
            "--gitlab-mr", "--gitlab-token-file", "--resume",
        ):
            self.assertIn(option, completed.stdout)

    def test_exact_exported_full_run_parser_dispatches_production_orchestrator_in_isolated_subprocess(self):
        source = Path(__file__).parents[1]
        root = Path(tempfile.mkdtemp(prefix="guarded-export-subprocess-"))
        try:
            exported = root / "export"
            shutil.copytree(source / "deploy", exported / "deploy")
            shutil.copy2(source / "release_argparse.py", exported / "release_argparse.py")
            for script in exported.joinpath("deploy").glob("*.py"):
                if script.name in {"guarded_priority_1_3_release.py", "verify_priority_1_3_acceptance.py", "release_bundle.py"}:
                    script.chmod(0o755)
            for directory in (root / "bundle", root / "destination", root / "evidence", root / "transactions"):
                directory.mkdir()
            (root / "transactions").chmod(0o700)
            for name in ("identity", "known-hosts", "cookie", "token"):
                path = root / name; path.write_text("session=value" if name == "cookie" else "fixture", encoding="ascii"); path.chmod(0o600)
            harness = r"""
import argparse,hashlib,json,pathlib,sys,types
root=pathlib.Path(sys.argv[1]);sys.path.insert(0,str(root/'export'))
import deploy.guarded_priority_1_3_release as guarded
records=[];final_publications=[]
class Executor:
 def __init__(self): self.transaction=None;self.terminals={}
 def run_remote(self,attempt_id,operation_id,argv,*,registry,run_as,operation_timeout):
  op=hashlib.sha256(f'{attempt_id}\0{operation_id}'.encode()).hexdigest()[:32]
  request={'schema':1,'attempt_id':attempt_id,'operation_id':op,'registry':registry,'run_as':run_as,'argv':argv}
  assert guarded.decode_privileged_request('remote-start',guarded.encode_privileged_request('remote-start',request))==request
  unit=f'matrix-p13-op-{attempt_id}-{op}.service';return guarded.OperationResult(0,unit,'/system.slice/'+unit)
 def control_remote(self,kind,request,*,operation_timeout):
  immutable=json.loads(json.dumps(request,sort_keys=True));assert guarded.decode_privileged_request(kind,guarded.encode_privileged_request(kind,immutable))==immutable
  records.append((kind,immutable))
  if kind=='remote-transaction-create-or-query':
   identity=guarded.canonical_release_identity(immutable['release_document'])
   authority=guarded.DurableTransactionRegistry(str(root/'transactions'),identity.release_key)
   try:self.transaction=authority.create_or_query(immutable)
   finally:authority.close()
   terminal=json.loads(json.dumps(self.transaction));return guarded.ControlResult(0,terminal['record_digest'],terminal)
  if kind=='remote-transaction-inspect':
   terminal=json.loads(json.dumps(self.transaction));return guarded.ControlResult(0,terminal['record_digest'],terminal)
  if kind=='remote-transaction-claim':
   authority=guarded.DurableTransactionRegistry(str(root/'transactions'),immutable['release_key'])
   try:self.transaction=authority.claim(immutable)
   finally:authority.close()
   terminal=json.loads(json.dumps(self.transaction));return guarded.ControlResult(0,terminal['record_digest'],terminal)
  if kind in {'remote-transaction-transition','remote-transaction-terminal','remote-transaction-advance'}:
   authority=guarded.DurableTransactionRegistry(str(root/'transactions'),immutable['release_key'])
   try:self.transaction=getattr(authority,kind.removeprefix('remote-transaction-'))(immutable)
   finally:authority.close()
   terminal=json.loads(json.dumps(self.transaction));return guarded.ControlResult(0,terminal['record_digest'],terminal)
  if kind=='remote-commit' and immutable.get('query') is True:
   terminal=self.terminals.get(immutable['attempt_id'])
   if terminal is None:return guarded.ControlResult(3,'')
   if any(immutable.get(field)!=terminal['request'].get(field) for field in guarded._COMMIT_QUERY_FIELDS-{'query'}):return guarded.ControlResult(2,'')
   return guarded.ControlResult(0,terminal['result_digest'],terminal)
  if kind=='remote-commit':
   success={key:immutable[key] for key in ('schema','boundary','phase','attempt_id','operation_id','evidence_digest','pending_note_id')}
   digest=hashlib.sha256(guarded._canonical_json_bytes(success)+b'\n').hexdigest()
   terminal={'schema':1,'request':immutable,'request_digest':guarded._request_digest('remote-commit',immutable),'result_digest':digest,'committed_epoch':100.0}
   self.terminals[immutable['attempt_id']]=terminal;return guarded.ControlResult(0,digest,terminal)
  return guarded.ControlResult(0,'d'*64)
class Notes:
 def pending(self,record): records.append(('PENDING',dict(record)));return str(100+sum(x[0]=='PENDING' for x in records))
 def reconcile(self,note,state,record): records.append((state,dict(record)))
 def discover_final(self,record):
  if len(final_publications)>1: raise ValueError('duplicate external FINAL authority')
  if not final_publications:return None
  if final_publications[0]['record']!=record:raise ValueError('conflicting external FINAL authority')
  return json.loads(json.dumps(final_publications[0]))
 def final(self,record):
  if final_publications:raise AssertionError('duplicate or conflicting FINAL POST')
  final_publications.append({'note_id':'900','state':'FINAL','record':json.loads(json.dumps(record))})
  records.append(('FINAL',dict(record)))
def accept(args,timeout):
 assert timeout>0 and args.mode in {'deploy','rollback'}
 pathlib.Path(args.evidence_file).write_text(json.dumps({'schema':2,'attempt_id':args.attempt_id})+'\n')
 pathlib.Path(args.evidence_file).chmod(0o600)
 return 0
fake=types.ModuleType('deploy.verify_priority_1_3_acceptance');fake.PinnedRemoteExecutor=lambda *a,**k:Executor();sys.modules[fake.__name__]=fake
guarded.GitLabEvidenceStore=lambda *a,**k:Notes();guarded._production_acceptance_runner=accept
a=[
'run','--boundary','b1','--ssh-target','fixture@host','--ssh-identity-file',str(root/'identity'),'--ssh-known-hosts-file',str(root/'known-hosts'),
'--stage',str(root/'export'),'--bundle',str(root/'bundle'),'--destination-root',str(root/'destination'),'--service','matrix-web.service','--initial-service-state','active',
'--health-url','http://127.0.0.1:5000/','--health-status','302','--health-timeout-seconds','30','--attempt-budget-seconds','6600','--commit-reserve-seconds','60','--watchdog-seconds','7200',
'--expected-sha','a'*40,'--expected-version','0.3.58-preview','--previous-sha','b'*40,'--previous-version','0.3.57-preview','--destination-identity','matrix-production','--release-stamp','2026-08-23T02:51:53Z','--release-manifest-digest','c'*64,'--backup-manifest-digest','d'*64,'--release-spec-digest','e'*64,'--session-cookie-file',str(root/'cookie'),
'--evidence-dir',str(root/'evidence'),'--gitlab-project','2','--gitlab-mr','73','--gitlab-token-file',str(root/'token')]
rc=guarded.main(a);assert rc==0,rc
assert [x[0] for x in records if x[0] in {'PENDING','ACCEPTED','ROLLED_BACK','FINAL'}]==['PENDING','ACCEPTED','PENDING','ROLLED_BACK','PENDING','ACCEPTED','FINAL']
assert len([x for x in records if x[0]=='remote-commit' and x[1].get('query') is not True])==2
assert len([x for x in records if x[0]=='remote-commit' and x[1].get('query') is True])==0
assert not hasattr(guarded,'run_three_attempts')
print('EXPORTED_FULL_RUN_OK')
"""
            completed = subprocess.run(
                [sys.executable, "-I", "-B", "-c", harness, str(root)],
                check=False, text=True, capture_output=True, timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            self.assertEqual(completed.stdout.strip(), "EXPORTED_FULL_RUN_OK")
            self.assertEqual(stat.S_IMODE((exported / "deploy/guarded_priority_1_3_release.py").stat().st_mode), 0o755)
        finally:
            shutil.rmtree(root)

    def test_exported_real_parser_codec_pinned_transport_fixed_service_and_gitlab_evidence_chain(self):
        source = Path(__file__).parents[1]
        root = Path(tempfile.mkdtemp(prefix="guarded-real-chain-"))
        try:
            exported = root / "export"
            shutil.copytree(source / "deploy", exported / "deploy")
            for production_source in source.glob("*.py"):
                shutil.copy2(production_source, exported / production_source.name)
            shutil.copy2(source / "rss_presets.json", exported / "rss_presets.json")
            for production_tree in ("app", "modules"):
                shutil.copytree(source / production_tree, exported / production_tree)
            shutil.copytree(source / "resources" / "fonts", exported / "resources" / "fonts")
            built_in_media = exported / "resources" / "media" / "built-in"
            built_in_media.mkdir(parents=True)
            for media_name in (
                "sleep.gif", "squares_tb.gif",
                "rubik_squares.gif", "waterfall_squares.gif",
                "loading_squares.gif", "splash_loading_squares.gif",
                "zigzag_squares.gif",
            ):
                shutil.copy2(source / "resources" / "media" / "built-in" / media_name,
                             built_in_media / media_name)
            (exported / "scripts").mkdir()
            shutil.copy2(
                source / "scripts/verify_priority_1_3_remote.py",
                exported / "scripts/verify_priority_1_3_remote.py",
            )
            stage = root / "stage"
            (stage / "venv/bin").mkdir(parents=True)
            (stage / "deploy").symlink_to(exported / "deploy", target_is_directory=True)
            for directory in (root / "bin", root / "bundle", root / "destination", root / "evidence", root / "transactions"):
                directory.mkdir()
            shutil.copytree(
                built_in_media,
                root / "destination" / "resources" / "media" / "built-in",
            )
            (root / "transactions").chmod(0o700)
            for name in ("identity", "known-hosts", "cookie", "token"):
                path = root / name
                path.write_text("session=value" if name == "cookie" else "fixture", encoding="ascii")
                path.chmod(0o600)
            shutil.copy2(source / "tests/fixtures/priority_1_3_fake_ssh.py", root / "bin/ssh")
            fake_python = root / "bin/fake-python.py"
            shutil.copy2(source / "tests/fixtures/priority_1_3_fake_python.py", fake_python)
            (stage / "venv/bin/python").write_text(
                "#!/bin/sh\nexec "
                + shlex.quote(sys.executable)
                + " "
                + shlex.quote(str(fake_python))
                + " \"$@\"\n",
                encoding="utf-8",
            )
            (root / "bin/ssh").chmod(0o755)
            (stage / "venv/bin/python").chmod(0o755)
            harness = r'''import hashlib, json, pathlib, subprocess, sys, urllib.parse, urllib.request
root=pathlib.Path(sys.argv[1]);sys.path.insert(0,str(root/'export'))
import deploy.guarded_priority_1_3_release as guarded
import deploy.verify_priority_1_3_acceptance as acceptance
real_executor=acceptance.PinnedRemoteExecutor
def fixture_executor(*args,**kwargs):
 def runner(argv,**run_kwargs):
  return subprocess.run([sys.executable,str(root/'bin/ssh'),*argv[1:]],**run_kwargs)
 return real_executor(*args,runner=runner,**kwargs)
acceptance.PinnedRemoteExecutor=fixture_executor
events=[];notes=[];next_id=100
release_spec={'version':1,'files':[{'path':'app.py','sha256':'e'*64,'uid':0,'gid':0,'mode':'0644'}]}
release_spec_bytes=(json.dumps(release_spec,sort_keys=True,separators=(',',':'))+'\n').encode()
(root/'stage/release-spec.json').write_bytes(release_spec_bytes);(root/'stage/release-spec.json').chmod(0o400)
release_spec_digest=hashlib.sha256(release_spec_bytes).hexdigest()
class Response:
 def __init__(self,payload,headers=None):
  self.payload=json.dumps(payload).encode();self.headers=headers or {}
 def __enter__(self): return self
 def __exit__(self,*args): pass
 def read(self,size): return self.payload
def urlopen(request,timeout):
 global next_id
 assert timeout==10
 if request.method=='GET':
  assert request.data is None
  query=urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
  assert query['per_page']==['100'] and query['order_by']==['updated_at'] and query['sort']==['desc']
  page=int(query['page'][0]);ordered=sorted(notes,key=lambda item:item['id'],reverse=True)
  start=(page-1)*100;items=ordered[start:start+100]
  next_page=str(page+1) if start+100<len(ordered) else ''
  return Response(items,{'X-Next-Page':next_page})
 assert request.data is not None
 body=urllib.parse.parse_qs(request.data.decode())['body'][0]
 document=json.loads(body.split(' ',1)[1]);state=document['state'];record=document['record']
 assert set(document)=={'schema','kind','state','record'} and type(document['schema']) is int and document['schema']==1
 evidence=root/'evidence'/f"{record.get('attempt_id','')}-candidate.json"
 events.append({'method':request.method,'state':state,'evidence_exists':evidence.is_file()})
 if request.method=='POST':
  if state=='FINAL' and any(json.loads(item['body'].split(' ',1)[1])['state']=='FINAL' for item in notes):
   raise AssertionError('duplicate or conflicting FINAL POST')
  next_id+=1;notes.append({'id':next_id,'body':body});return Response({'id':next_id})
 assert request.method=='PUT'
 note_id=int(request.full_url.rstrip('/').rsplit('/',1)[1])
 match=[item for item in notes if item['id']==note_id];assert len(match)==1
 match[0]['body']=body
 return Response({})
urllib.request.urlopen=urlopen
argv=['run','--transaction-registry-root',str(root/'transactions'),'--boundary','b1','--ssh-target','fixture@host','--ssh-identity-file',str(root/'identity'),'--ssh-known-hosts-file',str(root/'known-hosts'),'--stage',str(root/'stage'),'--bundle',str(root/'bundle'),'--destination-root',str(root/'destination'),'--service','matrix-web.service','--initial-service-state','active','--health-url','http://127.0.0.1:5000/','--health-status','302','--health-timeout-seconds','30','--attempt-budget-seconds','6600','--commit-reserve-seconds','60','--watchdog-seconds','7200','--expected-sha','a'*40,'--expected-version','0.3.58-preview','--previous-sha','b'*40,'--previous-version','0.3.57-preview','--destination-identity','matrix-production','--release-stamp','2026-08-23T02:51:53Z','--release-manifest-digest','c'*64,'--backup-manifest-digest','d'*64,'--release-spec-digest',release_spec_digest,'--session-cookie-file',str(root/'cookie'),'--evidence-dir',str(root/'evidence'),'--gitlab-project','2','--gitlab-mr','73','--gitlab-token-file',str(root/'token')]
original_write=guarded._write_reconciliation
armed=True
def interrupt_after_remote_terminal(path,document):
 global armed
 if armed and document['attempts']:
  armed=False
  raise KeyboardInterrupt('after remote commit response before local journal')
 original_write(path,document)
guarded._write_reconciliation=interrupt_after_remote_terminal
try: guarded.main(argv)
except KeyboardInterrupt: pass
assert not (root/'evidence/guarded-release-reconciliation.json').exists()
replay_start=len((root/'transport.jsonl').read_text().splitlines())
guarded._write_reconciliation=original_write
rc=guarded.main(argv)
(root/'replay-start').write_text(str(replay_start));(root/'gitlab.json').write_text(json.dumps(events));raise SystemExit(rc)
'''
            environment = dict(os.environ)
            environment.update({
                "PATH": str(root / "bin") + os.pathsep + environment["PATH"],
                "FIXTURE_ROOT": str(root), "FIXTURE_EXPORT": str(exported),
                "SSH_AUTH_SOCK": "/poison/agent", "SSH_AGENT_PID": "999",
                "GIT_SSH": "/poison/git-ssh", "GIT_SSH_COMMAND": "poison",
                "HTTP_PROXY": "http://poison", "HTTPS_PROXY": "http://poison",
                "ALL_PROXY": "http://poison",
            })
            completed = subprocess.run(
                [sys.executable, "-I", "-B", "-c", harness, str(root)],
                check=False, text=True, capture_output=True, timeout=180, env=environment, shell=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            transport = [json.loads(line) for line in (root / "transport.jsonl").read_text().splitlines()]
            mutations = [event for event in transport if event["lifecycle"] == "MUTATION"]
            commits = [event for event in transport if event["lifecycle"] == "DISARMED_COMMITTED"]
            queries = [event for event in transport if event["lifecycle"] == "COMMIT_QUERY"]
            replays = [event for event in transport if event["lifecycle"] == "COMMIT_REPLAY"]
            watchdogs = [event for event in transport if event["lifecycle"] == "ARMED"]
            self.assertEqual((len(mutations), len(commits), len(queries), len(replays), len(watchdogs)),
                             (3, 2, 1, 1, 3))
            phases = ("initial-deploy", "rollback-proof", "final-deploy")
            for mutation, phase in zip(mutations, phases):
                operation = mutation["request"]["operation_id"]
                start_index = transport.index(mutation)
                running_index = next(index for index, event in enumerate(transport)
                                     if index > start_index and event["lifecycle"] == "INSPECT_RUNNING"
                                     and event["request"]["operation_id"] == operation)
                terminal_index = next(index for index, event in enumerate(transport)
                                      if index > running_index and event["lifecycle"] == "INSPECT_TERMINAL"
                                      and event["request"]["operation_id"] == operation)
                mutation_done_index = next(index for index, event in enumerate(transport)
                                           if index > terminal_index and event["lifecycle"] == "TRANSACTION"
                                           and event["request"].get("phase") == phase
                                           and event["request"].get("to_state") == "MUTATION_DONE")
                acceptance_index = next(index for index, event in enumerate(transport)
                                        if index > mutation_done_index and event["lifecycle"] == "CHECK"
                                        and event["request"]["attempt_id"] == mutation["request"]["attempt_id"])
                self.assertLess(start_index, running_index)
                self.assertLess(running_index, terminal_index)
                self.assertLess(terminal_index, mutation_done_index)
                self.assertLess(mutation_done_index, acceptance_index)
            replay_start = int((root / "replay-start").read_text())
            replay_lifecycle = [event["lifecycle"] for event in transport[replay_start:]
                                if event["lifecycle"] != "TRANSACTION"]
            self.assertEqual(replay_lifecycle[:2], ["COMMIT_QUERY", "COMMIT_REPLAY"])
            self.assertEqual([event["request"]["attempt_id"] for event in commits],
                             [mutations[index]["request"]["attempt_id"] for index in (0, 2)])
            armed_units = {event["request"]["argv"][2] for event in watchdogs}
            self.assertTrue(all(event["request"]["guard_unit"] in armed_units for event in commits))
            gitlab = json.loads((root / "gitlab.json").read_text())
            self.assertEqual([event["state"] for event in gitlab],
                             ["PENDING", "ACCEPTED", "PENDING", "ROLLED_BACK", "PENDING", "ACCEPTED", "FINAL"])
            self.assertTrue(all(event["evidence_exists"] for event in gitlab if event["state"] != "FINAL"))
            journal = json.loads((root / "evidence/guarded-release-reconciliation.json").read_text())
            self.assertEqual(journal["final"]["state"], "AUTHORITATIVE_FINAL")
            self.assertEqual([entry["state"] for entry in journal["attempts"]], ["ACCEPTED"] * 3)
        finally:
            shutil.rmtree(root)

    def _module(self):
        return importlib.import_module("deploy.guarded_priority_1_3_release")

    def _tracer_b_arguments(self, guarded, root):
        for name in ("identity", "known-hosts", "cookie", "token"):
            path = root / name
            path.write_text("session=value" if name == "cookie" else "fixture", encoding="ascii")
            path.chmod(0o600)
        stage, bundle, evidence = root / "stage", root / "bundle", root / "evidence"
        stage.mkdir(); bundle.mkdir(); evidence.mkdir()
        arguments = guarded.build_parser().parse_args([
            "run", "--boundary", "b1", "--ssh-target", "fixture@host",
            "--ssh-identity-file", str(root / "identity"),
            "--ssh-known-hosts-file", str(root / "known-hosts"),
            "--stage", str(stage), "--bundle", str(bundle),
            "--destination-root", str(root / "destination"), "--service", "matrix-web.service",
            "--initial-service-state", "active", "--health-url", "http://127.0.0.1:5000/",
            "--health-status", "302", "--health-timeout-seconds", "30",
            "--attempt-budget-seconds", "6600", "--commit-reserve-seconds", "60",
            "--watchdog-seconds", "7200", "--expected-sha", "a" * 40,
            "--expected-version", "0.3.58-preview", "--previous-sha", "b" * 40,
            "--previous-version", "0.3.57-preview", "--destination-identity", "matrix-production",
            "--release-stamp", "2026-08-23T02:51:53Z", "--release-manifest-digest", "c" * 64,
            "--backup-manifest-digest", "d" * 64, "--release-spec-digest", "e" * 64,
            "--session-cookie-file", str(root / "cookie"), "--evidence-dir", str(evidence),
            "--gitlab-project", "2", "--gitlab-mr", "73", "--gitlab-token-file", str(root / "token"),
        ])
        return arguments, stage, bundle, evidence

    def _tracer_b_authority(self, guarded, arguments):
        class Executor:
            def __init__(self): self.transaction = {"record": None}; self.terminals = {}; self.controls = []
            def control_remote(inner, kind, request, *, operation_timeout):
                immutable = json.loads(json.dumps(request, sort_keys=True))
                inner.controls.append((kind, immutable))
                result = _fake_transaction_control(guarded, kind, immutable, inner.transaction, ())
                if result is not None: return result
                result = _fake_commit_control(guarded, kind, immutable, inner.terminals)
                if result is not None: return result
                raise AssertionError(kind)
        executor = Executor()
        identity, transaction = guarded._prepare_active_transaction(arguments, executor, created_epoch=100)
        authority = guarded._claim_active_transaction(
            arguments, executor, identity, transaction, coordinator_epoch=100,
        )
        return executor, identity, authority

    def _tracer_b_pending_authority(self, guarded, arguments, executor, authority, phase):
        for from_state, to_state in (
            ("READY", "GUARD_ARMING"), ("GUARD_ARMING", "GUARDED"),
            ("GUARDED", "MUTATION_RUNNING"), ("MUTATION_RUNNING", "MUTATION_DONE"),
            ("MUTATION_DONE", "ACCEPTANCE_RUNNING"),
            ("ACCEPTANCE_RUNNING", "CANDIDATE_DURABLE"),
            ("CANDIDATE_DURABLE", "PENDING_BOUND"),
        ):
            authority = guarded._active_transition(
                arguments, executor, authority, phase, from_state, to_state, lambda: 100.0,
            )
        return authority

    def test_commit_terminal_validator_enforces_exact_finite_positive_epoch_contract(self):
        guarded = self._module()
        attempt = "a" * 32
        request = {
            "schema": 1, "boundary": "b1", "phase": "initial-deploy",
            "attempt_id": attempt,
            "operation_id": __import__("hashlib").sha256(
                f"{attempt}\0commit".encode()
            ).hexdigest()[:32],
            "registry": "/run/matrix-priority-1-3/attempt.json",
            "evidence_digest": "b" * 64, "pending_note_id": "17",
            "guard_unit": f"matrix-p13-recover-{attempt}.service",
            "success_record": "/run/matrix-priority-1-3/success.json",
            "control_unit": f"matrix-p13-commit-{attempt}.service",
            "control_cgroup": f"/system.slice/matrix-p13-commit-{attempt}.service",
        }
        terminal = {
            "schema": 1, "request": request,
            "request_digest": guarded._request_digest("remote-commit", request),
            "result_digest": "c" * 64, "committed_epoch": 100.25,
        }
        invalid = (
            ("nan", float("nan")), ("positive infinity", float("inf")),
            ("negative infinity", float("-inf")), ("true", True), ("false", False),
            ("integer zero", 0), ("float zero", 0.0),
            ("negative integer", -1), ("negative float", -1.25),
        )
        for label, value in invalid:
            with self.subTest(label=label):
                candidate = dict(terminal, committed_epoch=value)
                with self.assertRaisesRegex(ValueError, "remote commit terminal time is invalid"):
                    guarded._validated_commit_terminal(candidate)
        for label, value in (("positive float", 100.25), ("positive integer", 100)):
            with self.subTest(label=label):
                candidate = dict(terminal, committed_epoch=value)
                self.assertIs(guarded._validated_commit_terminal(candidate), candidate)

    def test_canonical_release_identity_is_typed_domain_separated_and_path_independent(self):
        guarded = self._module()
        immutable = {
            "schema": 1, "transaction_version": 1, "boundary": "b1",
            "expected_sha": "a" * 40, "expected_version": "0.3.58-preview",
            "previous_sha": "b" * 40, "previous_version": "0.3.57-preview",
            "release_stamp": "2026-08-23T02:51:53Z",
            "release_manifest_digest": "c" * 64, "backup_manifest_digest": "d" * 64,
            "release_spec_digest": "e" * 64,
            "destination_identity": {"root": "matrix-production", "service": "matrix-web.service"},
            "gitlab": {"project": "2", "mr": "73"},
        }
        local_a = {"stage": "/tmp/export-a/stage", "bundle": "/tmp/export-a/bundle",
                   "ssh_target": "alice@alias-a", "ssh_identity_file": "/tmp/export-a/id",
                   "ssh_known_hosts_file": "/tmp/export-a/known-hosts", "evidence_dir": "/tmp/export-a/evidence",
                   "candidate_file": "/tmp/export-a/evidence/candidate.json", "temp_root": "/tmp/export-a"}
        local_b = {"stage": "/different/root/stage", "bundle": "/different/root/bundle",
                   "ssh_target": "bob@alias-b", "ssh_identity_file": "/different/root/id",
                   "ssh_known_hosts_file": "/different/root/known-hosts", "evidence_dir": "/different/root/evidence",
                   "candidate_file": "/different/root/evidence/missing.json", "temp_root": "/different/root"}
        previous = os.environ.get("MATRIX_RELEASE_TEMP_ROOT")
        try:
            os.environ["MATRIX_RELEASE_TEMP_ROOT"] = local_a["temp_root"]
            first = guarded.canonical_release_identity(immutable, local_context=local_a)
            os.environ["MATRIX_RELEASE_TEMP_ROOT"] = local_b["temp_root"]
            second = guarded.canonical_release_identity(immutable, local_context=local_b)
        finally:
            if previous is None: os.environ.pop("MATRIX_RELEASE_TEMP_ROOT", None)
            else: os.environ["MATRIX_RELEASE_TEMP_ROOT"] = previous
        self.assertEqual(first, second)
        self.assertRegex(first.release_key, r"^[0-9a-f]{64}$")
        self.assertRegex(first.transaction_identity, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first.release_key, first.transaction_identity)
        self.assertEqual(first.document, immutable)
        collision_fields = {
            "expected_sha": "f" * 40, "release_manifest_digest": "1" * 64,
            "backup_manifest_digest": "2" * 64, "release_spec_digest": "3" * 64,
            "destination_identity": {"root": "matrix-canary", "service": "matrix-web.service"},
            "release_stamp": "2026-08-23T02:52:00Z", "transaction_version": 2,
        }
        for field, replacement in collision_fields.items():
            changed = json.loads(json.dumps(immutable)); changed[field] = replacement
            with self.subTest(field=field):
                identity = guarded.canonical_release_identity(changed, local_context=local_b)
                self.assertNotEqual(identity.release_key, first.release_key)
                self.assertNotEqual(identity.transaction_identity, first.transaction_identity)
        changed_service = json.loads(json.dumps(immutable))
        changed_service["destination_identity"]["service"] = "matrix-preview.service"
        self.assertNotEqual(guarded.canonical_release_identity(changed_service).release_key, first.release_key)

    def test_shipped_privileged_cli_rejects_omitted_malformed_and_unsupported_requests(self):
        script = Path(__file__).parents[1] / "deploy" / "guarded_priority_1_3_release.py"
        interpreter = Path(sys.executable)
        invocations = (
            (),
            ("remote-recover", "--request-b64", "definitely-invalid"),
            ("remote-start", "--request-b64", "invalid"),
            ("remote-stop",),
            ("unsupported", "--request-b64", "invalid"),
        )
        for arguments in invocations:
            with self.subTest(arguments=arguments):
                completed = subprocess.run(
                    [str(interpreter), str(script), *arguments],
                    check=False, text=True, capture_output=True,
                )
                self.assertNotEqual(completed.returncode, 0)

    def test_durable_attempt_registry_is_mode_0600_atomic_and_persistent_across_processes(self):
        root = Path(tempfile.mkdtemp(prefix="guarded-registry-"))
        try:
            os.chmod(root, 0o700)
            registry = root / "attempt.json"
            attempt = "a" * 32
            operation = "b" * 32
            code = (
                "from deploy.guarded_priority_1_3_release import DurableAttemptRegistry; "
                f"r=DurableAttemptRegistry({str(registry)!r},{attempt!r},create=True); "
                f"assert r.register_operation({operation!r},'d'*64,'matrix-p13-op-{attempt}-{operation}.service',"
                f"'/system.slice/matrix-p13-op-{attempt}-{operation}.service',"
                f"{str(registry)!r}+'.outputs/{operation}.stdout',{str(registry)!r}+'.outputs/{operation}.stderr')"
            )
            first = subprocess.run(
                [sys.executable, "-I", "-B", "-c",
                 "import sys;sys.path.insert(0," + repr(str(Path(__file__).parents[1])) + ");" + code],
                check=False, text=True, capture_output=True,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(stat.S_IMODE(registry.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((root / "attempt.json.lock").stat().st_mode), 0o600)

            reader = (
                "import json;from deploy.guarded_priority_1_3_release import DurableAttemptRegistry;"
                f"r=DurableAttemptRegistry({str(registry)!r},{attempt!r});"
                "print(json.dumps(r.snapshot(),sort_keys=True,separators=(',',':')))"
            )
            second = subprocess.run(
                [sys.executable, "-I", "-B", "-c",
                 "import sys;sys.path.insert(0," + repr(str(Path(__file__).parents[1])) + ");" + reader],
                check=False, text=True, capture_output=True,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            record = json.loads(second.stdout)
            self.assertEqual(record["state"], "OPEN")
            self.assertEqual(record["attempt_id"], attempt)
            self.assertEqual(record["operations"][operation]["unit"],
                             f"matrix-p13-op-{attempt}-{operation}.service")
            self.assertFalse(any(path.name.startswith(".attempt.json.") for path in root.iterdir()))
        finally:
            for child in root.iterdir():
                child.unlink()
            root.rmdir()

    def test_legacy_release_identity_wrapper_is_path_independent_observer_cache_only(self):
        guarded = self._module()
        self.assertFalse(hasattr(guarded, "_release_identity"))

    def test_remote_transaction_registry_commands_are_durable_fresh_process_authority(self):
        guarded = self._module()
        root = Path(tempfile.mkdtemp(prefix="guarded-transaction-registry-"))
        try:
            os.chmod(root, 0o700)
            document = {
                "schema": 1, "transaction_version": 1, "boundary": "b1",
                "expected_sha": "a" * 40, "expected_version": "0.3.58-preview",
                "previous_sha": "b" * 40, "previous_version": "0.3.57-preview",
                "release_stamp": "2026-08-23T02:51:53Z",
                "release_manifest_digest": "c" * 64, "backup_manifest_digest": "d" * 64,
                "release_spec_digest": "e" * 64,
                "destination_identity": {"root": "matrix-production", "service": "matrix-web.service"},
                "gitlab": {"project": "2", "mr": "73"},
            }
            release_key = guarded.canonical_release_identity(document).release_key
            execution = {
                "schema": 1, "stage": "/var/lib/matrix/releases/stage-immutable",
                "bundle": "/var/lib/matrix/releases/bundle-immutable",
                "destination_root": "/home/admin/matrix", "service": "matrix-web.service",
            }
            common = {"schema": 1, "registry_root": str(root), "release_key": release_key}
            create = {**common, "release_document": document, "execution_contract": execution,
                      "created_epoch": 1000, "original_deadline_epoch": 8200}
            script = Path(__file__).parents[1] / "deploy/guarded_priority_1_3_release.py"
            python = Path(sys.executable)

            def payload(helper, request):
                return guarded.encode_privileged_request(helper, request)

            def command(helper, request):
                return [str(python), "-B", str(script), helper, "--request-b64", payload(helper, request)]

            # Two unrelated fresh processes race the same semantic release.  The
            # stable flock inode yields one durable random transaction topology.
            first = subprocess.Popen(command("remote-transaction-create-or-query", create),
                                     text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            second = subprocess.Popen(command("remote-transaction-create-or-query", create),
                                      text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            first_out, first_err = first.communicate(timeout=10)
            second_out, second_err = second.communicate(timeout=10)
            self.assertEqual((first.returncode, second.returncode), (0, 0), first_err + second_err)
            first_record = json.loads(first_out)["terminal"]
            second_record = json.loads(second_out)["terminal"]
            self.assertEqual(first_record, second_record)
            self.assertEqual(first_record["release_key"], release_key)
            self.assertRegex(first_record["transaction_id"], r"^[0-9a-f]{32}$")
            self.assertEqual([a["phase"] for a in first_record["attempts"]],
                             ["initial-deploy", "rollback-proof", "final-deploy"])
            self.assertEqual([a["operation"] for a in first_record["attempts"]],
                             ["deploy", "rollback", "deploy"])
            self.assertEqual(len({a["attempt_id"] for a in first_record["attempts"]}), 3)
            self.assertTrue(all(re.fullmatch(r"[0-9a-f]{32}", a["attempt_id"])
                                for a in first_record["attempts"]))
            self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", a["attempt_identity"])
                                for a in first_record["attempts"]))
            record_path = root / f"{release_key}.json"
            lock_path = root / f"{release_key}.lock"
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(record_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)
            on_disk = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(on_disk, first_record)
            self.assertEqual(on_disk["record_digest"], guarded.transaction_record_digest(on_disk))
            mismatched_create = dict(create, execution_contract=dict(execution, stage="/different/remote/stage"))
            before_mismatch = record_path.read_bytes()
            mismatch_begin = subprocess.run(command("remote-transaction-create-or-query", mismatched_create),
                                            check=False, text=True, capture_output=True)
            self.assertNotEqual(mismatch_begin.returncode, 0)
            self.assertEqual(record_path.read_bytes(), before_mismatch)
            with self.assertRaises(ValueError):
                payload("remote-transaction-create-or-query",
                        dict(create, original_deadline_epoch=create["created_epoch"] + 86401))

            transaction_id = first_record["transaction_id"]
            claims = [
                {**common, "transaction_id": transaction_id, "claimant_id": claimant_id,
                 "request_digest": digest, "claimed_epoch": 1001}
                for claimant_id, digest in (("1" * 32, "2" * 64), ("3" * 32, "4" * 64))
            ]
            claimers = [subprocess.Popen(command("remote-transaction-claim", claim), text=True,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE) for claim in claims]
            claim_results = [(process, *process.communicate(timeout=10)) for process in claimers]
            self.assertEqual(sorted(process.returncode for process, _out, _err in claim_results), [0, 2])
            winner_index = next(index for index, (process, _out, _err) in enumerate(claim_results)
                                if process.returncode == 0)
            claim = claims[winner_index]
            claimant = claim["claimant_id"]
            replay_claim = subprocess.run(command("remote-transaction-claim", claim), check=False,
                                          text=True, capture_output=True)
            self.assertEqual(replay_claim.returncode, 0, replay_claim.stderr)
            self.assertEqual(json.loads(claim_results[winner_index][1]), json.loads(replay_claim.stdout))

            inspect = {**common, "transaction_id": transaction_id}
            inspected = subprocess.run(command("remote-transaction-inspect", inspect), check=False,
                                       text=True, capture_output=True)
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            self.assertEqual(json.loads(inspected.stdout)["terminal"]["claim"]["claimant_id"], claimant)

            states = ["GUARD_ARMING", "GUARDED", "MUTATION_RUNNING", "MUTATION_DONE",
                      "ACCEPTANCE_RUNNING", "CANDIDATE_DURABLE", "PENDING_BOUND", "SEALED_COMMITTING"]
            current = "READY"
            for index, target in enumerate(states):
                transition = {**common, "transaction_id": transaction_id, "claimant_id": claimant,
                              "phase": "initial-deploy", "from_state": current, "to_state": target,
                              "request_digest": f"{index + 4:064x}", "transition_epoch": 1010 + index}
                moved = subprocess.run(command("remote-transaction-transition", transition), check=False,
                                       text=True, capture_output=True)
                self.assertEqual(moved.returncode, 0, moved.stderr)
                if index == 0:
                    replay = subprocess.run(command("remote-transaction-transition", transition), check=False,
                                            text=True, capture_output=True)
                    self.assertEqual(replay.returncode, 0, replay.stderr)
                current = target
            before_bad = record_path.read_bytes()
            backward = dict(transition, from_state="SEALED_COMMITTING", to_state="READY",
                            request_digest="9" * 64)
            bad = subprocess.run(command("remote-transaction-transition", backward), check=False,
                                 text=True, capture_output=True)
            self.assertNotEqual(bad.returncode, 0)
            self.assertEqual(record_path.read_bytes(), before_bad)

            terminal = {**common, "transaction_id": transaction_id, "claimant_id": claimant,
                        "phase": "initial-deploy", "kind": "commit",
                        "from_state": "SEALED_COMMITTING", "to_state": "COMMITTED_UNRECONCILED",
                        "request_digest": "a" * 64, "result_digest": "b" * 64,
                        "terminal_epoch": 1020}
            published = subprocess.run(command("remote-transaction-terminal", terminal), check=False,
                                       text=True, capture_output=True)
            replayed = subprocess.run(command("remote-transaction-terminal", terminal), check=False,
                                      text=True, capture_output=True)
            self.assertEqual((published.returncode, replayed.returncode), (0, 0))
            self.assertEqual(json.loads(published.stdout), json.loads(replayed.stdout))
            mismatch_terminal = dict(terminal, result_digest="c" * 64)
            mismatch = subprocess.run(command("remote-transaction-terminal", mismatch_terminal), check=False,
                                      text=True, capture_output=True)
            self.assertNotEqual(mismatch.returncode, 0)
            accepted = {**common, "transaction_id": transaction_id, "claimant_id": claimant,
                        "phase": "initial-deploy", "from_state": "COMMITTED_UNRECONCILED",
                        "to_state": "ACCEPTED", "request_digest": "d" * 64,
                        "transition_epoch": 1021}
            self.assertEqual(subprocess.run(command("remote-transaction-transition", accepted),
                                            check=False, text=True, capture_output=True).returncode, 0)
            advance = {**common, "transaction_id": transaction_id, "claimant_id": claimant,
                       "phase": "initial-deploy", "request_digest": "e" * 64,
                       "transition_epoch": 1022}
            advanced = subprocess.run(command("remote-transaction-advance", advance), check=False,
                                      text=True, capture_output=True)
            replay_advance = subprocess.run(command("remote-transaction-advance", advance), check=False,
                                            text=True, capture_output=True)
            self.assertEqual((advanced.returncode, replay_advance.returncode), (0, 0))
            advanced_record = json.loads(advanced.stdout)["terminal"]
            self.assertEqual(advanced_record["current_phase_index"], 1)
            self.assertEqual(advanced_record["attempts"][1]["state"], "READY")
            self.assertEqual(json.loads(replay_advance.stdout)["terminal"], advanced_record)

            alias = root.parent / f"{root.name}-symlink"
            alias.symlink_to(root, target_is_directory=True)
            try:
                unsafe_inspect = dict(inspect, registry_root=str(alias))
                unsafe = subprocess.run(command("remote-transaction-inspect", unsafe_inspect), check=False,
                                        text=True, capture_output=True)
                self.assertNotEqual(unsafe.returncode, 0)
            finally:
                alias.unlink()
            corrupted = json.loads(record_path.read_text(encoding="utf-8"))
            corrupted["state"] = "CORRUPTED"
            record_path.write_text(json.dumps(corrupted, sort_keys=True, separators=(",", ":")) + "\n",
                                   encoding="utf-8")
            corrupt_inspect = subprocess.run(command("remote-transaction-inspect", inspect), check=False,
                                             text=True, capture_output=True)
            self.assertNotEqual(corrupt_inspect.returncode, 0)
        finally:
            shutil.rmtree(root)

    def test_recovery_schema_is_complete_and_terminal_replay_uses_shipped_cli_across_processes(self):
        guarded = self._module()
        root = Path(tempfile.mkdtemp(prefix="guarded-replay-"))
        try:
            os.chmod(root, 0o700)
            attempt = "c" * 32
            operation = "d" * 32
            registry_path = root / "attempt.json"
            request = {
                "schema": 1, "boundary": "b1", "phase": "final-deploy",
                "attempt_id": attempt, "operation_id": operation,
                "registry": str(registry_path), "stage": "/trusted/stage",
                "release_root": "/trusted/stage/release",
                "destination_root": "/home/admin/matrix",
                "bundle": "/var/lib/matrix/releases/bundle",
                "release_manifest": "/trusted/stage/release-manifest.json",
                "backup_manifest": "/var/lib/matrix/releases/bundle/backup-manifest.json",
                "release_spec": "/trusted/stage/release-spec.json",
                "provenance_digest": "1" * 64, "evidence_digest": "2" * 64,
                "pending_note_id": "255", "service": "matrix-web.service",
                "initial_service_state": "active",
                "health_url": "http://127.0.0.1:5000/", "health_status": 302,
                "health_timeout_seconds": 30,
                "guard_unit": f"matrix-p13-recover-{attempt}.service",
            }
            payload = guarded.encode_privileged_request("remote-recover", request)
            self.assertEqual(guarded.decode_privileged_request("remote-recover", payload), request)
            for field in tuple(request):
                broken = dict(request)
                broken.pop(field)
                with self.subTest(missing=field), self.assertRaises(ValueError):
                    guarded.validate_privileged_request("remote-recover", broken)
            with self.assertRaises(ValueError):
                guarded.validate_privileged_request("remote-recover", dict(request, surprise=True))

            digest = guarded.canonical_recovery_request_digest(request)
            result_digest = "3" * 64
            recovered = {
                "state": "SEALED_RECOVERED",
                "recovery": {"request_digest": digest, "request": request,
                             "result_digest": result_digest, "recovered_epoch": 100.0},
            }
            seed = (
                "from deploy.guarded_priority_1_3_release import DurableAttemptRegistry;"
                f"r=DurableAttemptRegistry({str(registry_path)!r},{attempt!r},create=True);"
                + "r.mutate(lambda x:x.update(%r))" % recovered
            )
            prefix = "import sys;sys.path.insert(0," + repr(str(Path(__file__).parents[1])) + ");"
            seeded = subprocess.run(
                [sys.executable, "-I", "-B", "-c", prefix + seed],
                check=False, text=True, capture_output=True,
            )
            self.assertEqual(seeded.returncode, 0, seeded.stderr)
            replay = subprocess.run(
                [sys.executable,
                 str(Path(__file__).parents[1] / "deploy/guarded_priority_1_3_release.py"),
                 "remote-recover", "--request-b64", payload],
                check=False, text=True, capture_output=True,
            )
            self.assertEqual(replay.returncode, 0, replay.stderr)
            self.assertEqual(json.loads(replay.stdout),
                             {"digest": result_digest, "returncode": 0})
            self.assertEqual(json.loads(registry_path.read_text())["state"], "SEALED_RECOVERED")
        finally:
            for child in root.iterdir(): child.unlink()
            root.rmdir()

    def test_systemd_runner_rejects_path_resolution_and_uses_a_fixed_environment(self):
        guarded = self._module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            poison = root / "bin"
            poison.mkdir()
            marker = root / "poisoned"
            for executable in ("systemctl", "systemd-run"):
                script = poison / executable
                script.write_text(f"#!/bin/sh\nprintf '%s' poisoned > {marker}\n")
                script.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": str(poison)}, clear=False):
                for executable in ("systemctl", "systemd-run"):
                    with self.subTest(executable=executable), self.assertRaises(ValueError):
                        guarded.SystemdController._run([executable, "--version"], timeout=1)
            self.assertFalse(marker.exists())

        completed = subprocess.CompletedProcess(["/usr/bin/systemctl"], 0, "", "")
        with mock.patch.object(guarded.subprocess, "run", return_value=completed) as run:
            self.assertIs(guarded.SystemdController._run(
                ["/usr/bin/systemctl", "--version"], timeout=1,
            ), completed)
        self.assertEqual(
            run.call_args.kwargs["env"],
            {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )

    def test_systemd_runner_contains_operations_and_proves_cgroup_extinct_before_reset(self):
        guarded = self._module()
        root = Path(tempfile.mkdtemp(prefix="guarded-systemd-"))
        try:
            os.chmod(root, 0o700)
            attempt, operation = "4" * 32, "5" * 32
            registry = guarded.DurableAttemptRegistry(root / "attempt.json", attempt, create=True)
            unit = f"matrix-p13-op-{attempt}-{operation}.service"
            cgroup = f"/system.slice/{unit}"
            cgroup_dir = root / cgroup.removeprefix("/")
            cgroup_dir.mkdir(parents=True)
            (cgroup_dir / "cgroup.events").write_text("populated 1\n")
            (cgroup_dir / "cgroup.procs").write_text("999\n")
            events = []

            class Completed:
                def __init__(self, returncode=0, stdout="", stderr=""):
                    self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

            def runner(argv, *, timeout):
                events.append((tuple(argv), timeout))
                if argv[0] == "/usr/bin/systemd-run":
                    return Completed()
                if argv[:2] == ["/usr/bin/systemctl", "show"] and "--value" in argv:
                    return Completed(stdout=cgroup + "\n")
                if argv[:3] == ["/usr/bin/systemctl", "kill", "--signal=KILL"]:
                    (cgroup_dir / "cgroup.events").write_text("populated 0\n")
                    (cgroup_dir / "cgroup.procs").write_text("")
                    return Completed()
                if argv[:2] == ["/usr/bin/systemctl", "show"]:
                    killed = any(command[:3] == ("/usr/bin/systemctl", "kill", "--signal=KILL") for command, _ in events)
                    return Completed(stdout=("MainPID=0\nActiveState=failed\nSubState=failed\n"
                                             if killed else "MainPID=999\nActiveState=deactivating\nSubState=stop-sigterm\n"))
                return Completed()

            controller = guarded.SystemdController(runner=runner, cgroup_root=root,
                                                    sleep=lambda _seconds: None)
            request = {
                "schema": 1, "attempt_id": attempt, "operation_id": operation,
                "registry": str(root / "attempt.json"), "run_as": "root",
                "argv": ["/bin/true", "--literal"],
            }
            result = controller.start(request, registry)
            self.assertEqual(result, guarded.OperationResult(0, unit, cgroup))
            start_argv = events[0][0]
            self.assertEqual(start_argv[0], "/usr/bin/systemd-run")
            self.assertIn("--property=Type=exec", start_argv)
            self.assertIn("--property=KillMode=control-group", start_argv)
            self.assertIn("--property=TimeoutStopSec=5s", start_argv)
            self.assertIn("--property=SendSIGKILL=yes", start_argv)
            self.assertIn("--property=User=root", start_argv)
            self.assertEqual(start_argv[-3:], ("--", "/bin/true", "--literal"))
            self.assertNotIn("sh", start_argv)

            self.assertTrue(controller.quiesce(unit, cgroup))
            commands = [command for command, _ in events]
            term_index = next(i for i, command in enumerate(commands)
                              if command[:3] == ("/usr/bin/systemctl", "kill", "--signal=TERM"))
            kill_index = next(i for i, command in enumerate(commands)
                              if command[:3] == ("/usr/bin/systemctl", "kill", "--signal=KILL"))
            reset_index = next(i for i, command in enumerate(commands)
                               if command[:2] == ("/usr/bin/systemctl", "reset-failed"))
            stop_index = next(i for i, command in enumerate(commands)
                              if command[:2] == ("/usr/bin/systemctl", "stop"))
            self.assertLess(term_index, stop_index)
            self.assertLess(stop_index, kill_index)
            self.assertLess(kill_index, reset_index)
            self.assertEqual((cgroup_dir / "cgroup.events").read_text(), "populated 0\n")
            self.assertEqual((cgroup_dir / "cgroup.procs").read_text(), "")
            operation_record = registry.snapshot()["operations"][operation]
            self.assertTrue(Path(operation_record["stdout_path"]).is_file())
            self.assertTrue(Path(operation_record["stderr_path"]).is_file())
            self.assertTrue(all(timeout <= 5 for _, timeout in events))
        finally:
            for child in sorted(root.rglob("*"), reverse=True):
                child.rmdir() if child.is_dir() else child.unlink()
            root.rmdir()

    def test_durable_dispatch_start_stop_recover_commit_and_replay_are_real_and_typed(self):
        guarded = self._module()
        root = Path(tempfile.mkdtemp(prefix="guarded-dispatch-"))
        try:
            os.chmod(root, 0o700)
            events = []
            attempt, op = "6" * 32, "7" * 32
            registry_path = root / "attempt.json"

            class Controller:
                def start(self, request, registry):
                    unit = f"matrix-p13-op-{request['attempt_id']}-{request['operation_id']}.service"
                    cgroup = "/system.slice/" + unit
                    output_root = str(registry.path) + ".outputs/" + request["operation_id"]
                    self.assert_registered = registry.register_operation(
                        request["operation_id"], guarded._request_digest("remote-start", request), unit, cgroup,
                        output_root + ".stdout", output_root + ".stderr",
                    )
                    events.append(("start", tuple(request["argv"])))
                    return guarded.OperationResult(0, unit, cgroup)
                def quiesce(self, unit, cgroup):
                    events.append(("quiesce", unit, cgroup))
                    return True

            start = {"schema": 1, "attempt_id": attempt, "operation_id": op,
                     "registry": str(registry_path), "run_as": "root", "argv": ["/bin/true"]}
            started = guarded.dispatch_privileged("remote-start", start, controller=Controller())
            self.assertEqual(started.returncode, 0)
            unit, cgroup = started.unit, started.cgroup
            stop = {"schema": 1, "attempt_id": attempt, "operation_id": op,
                    "registry": str(registry_path), "unit": unit, "cgroup": cgroup}
            stopped = guarded.dispatch_privileged("remote-stop", stop, controller=Controller())
            self.assertEqual(stopped.returncode, 0)

            commit_op = "8" * 32
            success = root / "success.json"
            commit = {
                "schema": 1, "boundary": "b1", "phase": "final-deploy",
                "attempt_id": attempt, "operation_id": commit_op,
                "registry": str(registry_path), "evidence_digest": "9" * 64,
                "pending_note_id": "255", "guard_unit": f"matrix-p13-recover-{attempt}.service",
                "success_record": str(success),
                "control_unit": f"matrix-p13-commit-{attempt}.service",
                "control_cgroup": f"/system.slice/matrix-p13-commit-{attempt}.service",
            }
            committed = guarded.dispatch_privileged("remote-commit", commit, controller=Controller())
            self.assertEqual(committed.returncode, 0)
            self.assertEqual(stat.S_IMODE(success.stat().st_mode), 0o400)
            self.assertEqual(json.loads(registry_path.read_text())["state"], "SEALED_COMMITTED")

            recover_attempt, recover_op = "a" * 32, "b" * 32
            recover_registry = root / "recover.json"
            guarded.DurableAttemptRegistry(recover_registry, recover_attempt, create=True)
            recovery = {
                "schema": 1, "boundary": "b1", "phase": "initial-deploy",
                "attempt_id": recover_attempt, "operation_id": recover_op,
                "registry": str(recover_registry), "stage": "/trusted/stage",
                "release_root": "/trusted/stage/release", "destination_root": "/home/admin/matrix",
                "bundle": "/var/lib/matrix/releases/bundle",
                "release_manifest": "/trusted/stage/release-manifest.json",
                "backup_manifest": "/var/lib/matrix/releases/bundle/backup-manifest.json",
                "release_spec": "/trusted/stage/release-spec.json",
                "provenance_digest": "c" * 64, "evidence_digest": "d" * 64,
                "pending_note_id": "255", "service": "matrix-web.service",
                "initial_service_state": "inactive", "health_url": None,
                "health_status": None, "health_timeout_seconds": None,
                "guard_unit": f"matrix-p13-recover-{recover_attempt}.service",
            }
            recovered = guarded.dispatch_privileged(
                "remote-recover", recovery, controller=Controller(),
                recovery_runner=lambda argv: events.append(("recover", tuple(argv))) or guarded.ControlResult(0, "e" * 64),
                release_lock=lambda: events.append(("lock",)) or 0,
            )
            self.assertEqual(recovered.returncode, 0)
            before = list(events)
            replayed = guarded.dispatch_privileged(
                "remote-recover", recovery, controller=Controller(),
                recovery_runner=lambda argv: events.append(("unexpected-recover",)) or guarded.ControlResult(2, ""),
                release_lock=lambda: events.append(("unexpected-lock",)) or 2,
            )
            self.assertEqual(replayed, recovered)
            self.assertEqual(events, before)
            self.assertEqual(json.loads(recover_registry.read_text())["state"], "SEALED_RECOVERED")
        finally:
            for child in root.iterdir(): child.unlink()
            root.rmdir()

    def test_run_dispatches_three_guarded_deadline_bound_attempts_and_reconciles_authority(self):
        guarded = self._module()
        root = Path(tempfile.mkdtemp(prefix="guarded-production-run-"))
        try:
            for name in ("identity", "known-hosts", "cookie", "token"):
                path = root / name
                path.write_text("session=value" if name == "cookie" else "fixture", encoding="ascii")
                path.chmod(0o600)
            stage, bundle, evidence = root / "stage", root / "bundle", root / "evidence"
            stage.mkdir(); bundle.mkdir(); evidence.mkdir()
            argv = [
                "run", "--boundary", "b1", "--ssh-target", "fixture@host",
                "--ssh-identity-file", str(root / "identity"),
                "--ssh-known-hosts-file", str(root / "known-hosts"),
                "--stage", str(stage), "--bundle", str(bundle),
                "--destination-root", str(root / "destination"), "--service", "matrix-web.service",
                "--initial-service-state", "active", "--health-url", "http://127.0.0.1:5000/",
                "--health-status", "302", "--health-timeout-seconds", "30",
                "--attempt-budget-seconds", "6600", "--commit-reserve-seconds", "60",
                "--watchdog-seconds", "7200", "--expected-sha", "a" * 40,
                "--expected-version", "0.3.58-preview", "--previous-sha", "b" * 40,
                "--previous-version", "0.3.57-preview", "--destination-identity", "matrix-production", "--release-stamp", "2026-08-23T02:51:53Z", "--release-manifest-digest", "c" * 64, "--backup-manifest-digest", "d" * 64, "--release-spec-digest", "e" * 64, "--session-cookie-file", str(root / "cookie"),
                "--evidence-dir", str(evidence), "--gitlab-project", "2", "--gitlab-mr", "73",
                "--gitlab-token-file", str(root / "token"),
            ]
            arguments = guarded.build_parser().parse_args(argv)

            class Clock:
                now = 100.0
                def monotonic(self): return self.now
            clock = Clock()

            class Executor:
                def __init__(self, ids=("1" * 32, "2" * 32, "3" * 32)):
                    self.requests = []; self.operations = []; self.transaction = {"record": None}; self.ids = ids
                    self.terminals = {}
                def run_remote(self, attempt_id, operation_id, command, *, registry, run_as, operation_timeout):
                    self.operations.append((attempt_id, operation_id, tuple(command), run_as, operation_timeout))
                    self.assert_timeout(operation_timeout)
                    op_hash = __import__("hashlib").sha256(f"{attempt_id}\0{operation_id}".encode()).hexdigest()[:32]
                    request = {"schema": 1, "attempt_id": attempt_id, "operation_id": op_hash,
                               "registry": registry,
                               "run_as": run_as, "argv": command}
                    guarded.decode_privileged_request("remote-start", guarded.encode_privileged_request("remote-start", request))
                    return guarded.OperationResult(0, f"matrix-p13-op-{attempt_id}-{op_hash}.service",
                                                   f"/system.slice/matrix-p13-op-{attempt_id}-{op_hash}.service")
                def control_remote(self, kind, request, *, operation_timeout):
                    immutable = json.loads(json.dumps(request, sort_keys=True))
                    guarded.decode_privileged_request(kind, guarded.encode_privileged_request(kind, immutable))
                    self.requests.append((kind, immutable, operation_timeout))
                    self.assert_timeout(operation_timeout)
                    transaction = _fake_transaction_control(guarded, kind, immutable, self.transaction, self.ids)
                    if transaction is not None:
                        return transaction
                    if (kind == "remote-commit" and immutable["phase"] == "final-deploy"
                            and immutable.get("query") is not True):
                        clock.now = 183.0
                    committed = _fake_commit_control(guarded, kind, immutable, self.terminals)
                    return committed if committed is not None else guarded.ControlResult(0, "d" * 64)
                def assert_timeout(self, value):
                    if type(value) is not int or not 1 <= value <= 6540: raise AssertionError(value)

            class Notes(_FakeFinalAuthority):
                def __init__(self): self.events = []; self.next_id = 10; self.final_publications = []
                def pending(self, record):
                    self.next_id += 1; self.events.append(("PENDING", self.next_id, dict(record))); return str(self.next_id)
                def reconcile(self, note_id, state, record):
                    self.events.append((state, int(note_id), dict(record)))
                def discover_final(self, record):
                    return _discover_fake_final(self.final_publications, record)
                def final(self, record):
                    _publish_fake_final(self.final_publications, record)
                    self.events.append(("FINAL", None, dict(record)))

            executor, notes, acceptance = Executor(), Notes(), []
            final_timeout = {"armed": True}
            def accept(args, timeout):
                self.assertGreater(timeout, 0)
                acceptance.append((args.phase, args.mode, args.attempt_id, timeout))
                if len(acceptance) == 5 and final_timeout["armed"]:
                    final_timeout["armed"] = False
                    raise subprocess.TimeoutExpired(["acceptance"], timeout)
                if len(acceptance) == 6:
                    clock.now += 190.0
                Path(args.evidence_file).write_text(json.dumps({"schema": 2, "attempt_id": args.attempt_id}) + "\n")
                Path(args.evidence_file).chmod(0o600)
                return 0

            timed_out = guarded.run_release(
                arguments, executor=executor, acceptance_runner=accept, evidence_store=notes,
                clock=clock.monotonic, attempt_ids=iter(("1" * 32, "2" * 32, "3" * 32)),
            )
            self.assertNotEqual(timed_out, 0)
            self.assertNotIn("FINAL", [event[0] for event in notes.events])
            self.assertEqual(notes.final_publications, [])
            (evidence / "guarded-release-reconciliation.json").unlink()
            clock.now = 100.0
            arguments.release_stamp = "2026-08-23T02:51:54Z"
            executor, notes, acceptance = Executor(("4" * 32, "5" * 32, "6" * 32)), Notes(), []
            def accept_healthy(args, timeout):
                self.assertEqual(timeout, guarded._AGGREGATE_ACCEPTANCE_BUDGET_SECONDS)
                acceptance.append((args.phase, args.mode, args.attempt_id, timeout))
                if len(acceptance) == 4:
                    clock.now = 183.0
                if len(acceptance) == 5:
                    clock.now += 190.0
                Path(args.evidence_file).write_text(json.dumps({"schema": 2, "attempt_id": args.attempt_id}) + "\n")
                Path(args.evidence_file).chmod(0o600)
                return 0
            result = guarded.run_release(
                arguments, executor=executor, acceptance_runner=accept_healthy, evidence_store=notes,
                clock=clock.monotonic, attempt_ids=iter(("4" * 32, "5" * 32, "6" * 32)),
            )
            self.assertEqual(result, 0)
            self.assertEqual([(p, m) for p, m, _, _ in acceptance],
                             [("initial-deploy", "deploy"), ("rollback-proof", "rollback"),
                              ("rollback-proof", "rollback"), ("final-deploy", "deploy"),
                              ("final-deploy", "deploy")])
            self.assertEqual([event[0] for event in notes.events],
                             ["PENDING", "ACCEPTED", "PENDING", "ROLLED_BACK", "PENDING", "ACCEPTED", "FINAL"])
            self.assertEqual(len({item[0] for item in acceptance}), 3)
            self.assertEqual(acceptance[-1][3], guarded._AGGREGATE_ACCEPTANCE_BUDGET_SECONDS)
            commit_requests = [request for kind, request, _ in executor.requests if kind == "remote-commit"]
            self.assertEqual(len([request for request in commit_requests if request.get("query") is not True]), 2)
            self.assertEqual(len([request for request in commit_requests if request.get("query") is True]), 0)
            recover_requests = [request for kind, request, _ in executor.requests if kind == "remote-recover"]
            self.assertEqual(len(recover_requests), 1)
            self.assertEqual(recover_requests[0]["phase"], "rollback-proof")
            watchdogs = [op for op in executor.operations if op[1] == "watchdog-arm"]
            self.assertEqual(len(watchdogs), 3)
            for attempt_id, _, command, _, _ in watchdogs:
                self.assertEqual(command[:2], ("/usr/bin/systemd-run", "--unit"))
                self.assertIn("remote-recover", command)
                payload = command[command.index("--request-b64") + 1]
                recovered = guarded.decode_privileged_request("remote-recover", payload)
                self.assertEqual(recovered["attempt_id"], attempt_id)
            self.assertEqual([op[1] for op in executor.operations if op[1] in {"deploy", "rollback"}],
                             ["deploy", "rollback", "deploy"])

            class CommitFailureExecutor(Executor):
                def control_remote(self, kind, request, *, operation_timeout):
                    result = super().control_remote(kind, request, operation_timeout=operation_timeout)
                    return guarded.ControlResult(2, "") if kind == "remote-commit" else result

            # A fresh transaction is required for the independent pre-commit recovery case.
            (evidence / "guarded-release-reconciliation.json").unlink()
            clock.now = 100.0
            arguments.release_stamp = "2026-08-23T02:52:00Z"
            failed_executor, failed_notes = CommitFailureExecutor(("4" * 32, "5" * 32, "6" * 32)), Notes()
            calls = []
            def accept_then_verify(args, timeout):
                calls.append((args.mode, args.evidence_file))
                Path(args.evidence_file).write_text(json.dumps({"schema": 2, "attempt_id": args.attempt_id}) + "\n")
                Path(args.evidence_file).chmod(0o600)
                return 0
            failed = guarded.run_release(
                arguments, executor=failed_executor, acceptance_runner=accept_then_verify,
                evidence_store=failed_notes, clock=clock.monotonic,
                attempt_ids=iter(("4" * 32,)),
            )
            self.assertNotEqual(failed, 0)
            self.assertEqual([event[0] for event in failed_notes.events], ["PENDING"])
            self.assertEqual([mode for mode, _ in calls], ["deploy"])
            recovery_controls = [request for kind, request, _ in failed_executor.requests if kind == "remote-recover"]
            self.assertEqual(recovery_controls, [])
            self.assertEqual(failed_executor.transaction["record"]["attempts"][0]["state"],
                             "SEALED_COMMITTING")
        finally:
            import shutil
            shutil.rmtree(root)

    def test_committed_gitlab_reconciliation_is_bounded_durable_and_resume_safe(self):
        guarded = self._module()
        root = Path(tempfile.mkdtemp(prefix="guarded-reconcile-resume-"))
        try:
            for name in ("identity", "known-hosts", "cookie", "token"):
                path = root / name
                path.write_text("session=value" if name == "cookie" else "fixture", encoding="ascii")
                path.chmod(0o600)
            stage, bundle, evidence = root / "stage", root / "bundle", root / "evidence"
            stage.mkdir(); bundle.mkdir(); evidence.mkdir()
            argv = [
                "run", "--boundary", "b1", "--ssh-target", "fixture@host",
                "--ssh-identity-file", str(root / "identity"),
                "--ssh-known-hosts-file", str(root / "known-hosts"),
                "--stage", str(stage), "--bundle", str(bundle),
                "--destination-root", str(root / "destination"), "--service", "matrix-web.service",
                "--initial-service-state", "active", "--health-url", "http://127.0.0.1:5000/",
                "--health-status", "302", "--health-timeout-seconds", "30",
                "--attempt-budget-seconds", "6600", "--commit-reserve-seconds", "60",
                "--watchdog-seconds", "7200", "--expected-sha", "a" * 40,
                "--expected-version", "0.3.58-preview", "--previous-sha", "b" * 40,
                "--previous-version", "0.3.57-preview", "--destination-identity", "matrix-production", "--release-stamp", "2026-08-23T02:51:53Z", "--release-manifest-digest", "c" * 64, "--backup-manifest-digest", "d" * 64, "--release-spec-digest", "e" * 64, "--session-cookie-file", str(root / "cookie"),
                "--evidence-dir", str(evidence), "--gitlab-project", "2", "--gitlab-mr", "73",
                "--gitlab-token-file", str(root / "token"),
            ]
            arguments = guarded.build_parser().parse_args(argv)

            class Executor:
                def __init__(self):
                    self.operations = []; self.controls = []; self.transaction = {"record": None}
                    self.terminals = {}

                def run_remote(self, attempt_id, operation_id, command, *, registry, run_as, operation_timeout):
                    self.operations.append((attempt_id, operation_id))
                    operation = __import__("hashlib").sha256(
                        f"{attempt_id}\0{operation_id}".encode()
                    ).hexdigest()[:32]
                    unit = f"matrix-p13-op-{attempt_id}-{operation}.service"
                    return guarded.OperationResult(0, unit, "/system.slice/" + unit)
                def control_remote(self, kind, request, *, operation_timeout):
                    transaction = _fake_transaction_control(
                        guarded, kind, request, self.transaction, ("1" * 32, "2" * 32, "3" * 32)
                    )
                    if transaction is not None:
                        return transaction
                    self.controls.append((kind, request["attempt_id"], request.get("query") is True))
                    committed = _fake_commit_control(guarded, kind, request, self.terminals)
                    return committed if committed is not None else guarded.ControlResult(0, "d" * 64)

            class Notes(_FakeFinalAuthority):
                def __init__(self, failures):
                    self.failures = failures; self.events = []; self.next_id = 40
                    self.final_publications = []
                def pending(self, record):
                    self.next_id += 1; self.events.append(("PENDING", record["attempt_id"])); return str(self.next_id)
                def reconcile(self, note_id, state, record):
                    self.events.append(("ACCEPTED_ATTEMPT", record["attempt_id"]))
                    if self.failures:
                        self.failures -= 1
                        raise OSError("injected GitLab PUT failure")
                    self.events.append((state, record["attempt_id"]))
                def discover_final(self, record):
                    return _discover_fake_final(self.final_publications, record)
                def final(self, record):
                    _publish_fake_final(self.final_publications, record)
                    self.events.append(("FINAL", record["attempt_id"]))

            def accept(args, timeout):
                Path(args.evidence_file).write_text(
                    json.dumps({"schema": 2, "attempt_id": args.attempt_id}) + "\n", encoding="utf-8"
                )
                Path(args.evidence_file).chmod(0o600)
                return 0

            executor = Executor()
            persistent = Notes(99)
            red = guarded.run_release(
                arguments, executor=executor, acceptance_runner=accept, evidence_store=persistent,
                clock=lambda: 100.0, attempt_ids=iter(("1" * 32, "2" * 32, "3" * 32)),
            )
            self.assertNotEqual(red, 0)
            self.assertEqual([event[0] for event in persistent.events],
                             ["PENDING", "ACCEPTED_ATTEMPT", "ACCEPTED_ATTEMPT", "ACCEPTED_ATTEMPT"])
            canonical_ids = tuple(
                attempt["attempt_id"] for attempt in executor.transaction["record"]["attempts"]
            )
            first_commits = [item for item in executor.controls if item[0] == "remote-commit"]
            self.assertEqual(first_commits,
                             [("remote-commit", canonical_ids[0], False)])
            self.assertEqual([item for item in executor.controls if item[0] == "remote-recover"], [])
            journal_path = evidence / "guarded-release-reconciliation.json"
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            self.assertEqual(journal["attempts"][0]["state"], "COMMITTED_UNRECONCILED")
            self.assertGreater(journal["attempts"][0]["remaining_seconds"], 0)
            immutable_deadline = journal["attempts"][0]["deadline_epoch"]

            transient = Notes(2)
            green = guarded.run_release(
                arguments, executor=executor, acceptance_runner=accept, evidence_store=transient,
                clock=lambda: 100.0, attempt_ids=iter(("2" * 32, "3" * 32)),
            )
            self.assertEqual(green, 0)
            mutations = [item for item in executor.operations if item[1] in {"deploy", "rollback"}]
            self.assertEqual(mutations, [(canonical_ids[0], "deploy"),
                                         (canonical_ids[1], "rollback"),
                                         (canonical_ids[2], "deploy")])
            commits = [item for item in executor.controls if item[0] == "remote-commit"]
            self.assertEqual([item[1] for item in commits if item[2] is False],
                             [canonical_ids[0], canonical_ids[2]])
            self.assertEqual([item[1] for item in commits if item[2] is True],
                             [canonical_ids[0]])
            self.assertIn("ROLLED_BACK", [event[0] for event in persistent.events + transient.events])
            final = json.loads(journal_path.read_text(encoding="utf-8"))
            self.assertEqual([item["state"] for item in final["attempts"]], ["ACCEPTED"] * 3)
            self.assertEqual(final["attempts"][0]["deadline_epoch"], immutable_deadline)
            self.assertEqual(final["final"]["state"], "AUTHORITATIVE_FINAL")
            self.assertEqual([event[0] for event in transient.events].count("ACCEPTED_ATTEMPT"), 5)
            self.assertEqual(transient.events[-1][0], "FINAL")
        finally:
            shutil.rmtree(root)

    def test_tracer_b_lost_terminal_transition_and_advance_responses_use_exact_inspect(self):
        guarded = self._module(); root = Path(tempfile.mkdtemp(prefix="guarded-tracer-b-lost-response-"))
        try:
            arguments, _stage, _bundle, _evidence = self._tracer_b_arguments(guarded, root)
            executor, _identity, authority = self._tracer_b_authority(guarded, arguments)
            phase = guarded._TRANSACTION_TOPOLOGY[0][0]
            authority = self._tracer_b_pending_authority(
                guarded, arguments, executor, authority, phase,
            )
            authority = guarded._active_transition(
                arguments, executor, authority, phase,
                "PENDING_BOUND", "SEALED_COMMITTING", lambda: 100.0,
            )
            original_control = executor.control_remote
            losses = []
            def lose_once(target_kind):
                armed = {"value": True}
                def control(kind, request, *, operation_timeout):
                    result = original_control(kind, request, operation_timeout=operation_timeout)
                    if kind == target_kind and armed["value"]:
                        armed["value"] = False; losses.append(kind)
                        raise OSError(f"lost {kind} response after durable write")
                    return result
                executor.control_remote = control

            terminal_digest = __import__("hashlib").sha256(b"commit terminal").hexdigest()
            lose_once("remote-transaction-terminal")
            authority = guarded._active_terminal(
                arguments, executor, authority, phase, "commit", "SEALED_COMMITTING",
                "COMMITTED_UNRECONCILED", terminal_digest, lambda: 100.0,
            )
            self.assertEqual(authority.record["attempts"][0]["state"], "COMMITTED_UNRECONCILED")
            executor.control_remote = original_control
            lose_once("remote-transaction-transition")
            authority = guarded._active_transition(
                arguments, executor, authority, phase,
                "COMMITTED_UNRECONCILED", "ACCEPTED", lambda: 100.0,
            )
            self.assertEqual(authority.record["attempts"][0]["state"], "ACCEPTED")
            executor.control_remote = original_control
            lose_once("remote-transaction-advance")
            authority = guarded._active_advance(
                arguments, executor, authority, phase, lambda: 100.0,
            )
            self.assertEqual(authority.record["current_phase_index"], 1)
            self.assertEqual(authority.record["attempts"][1]["state"], "READY")
            self.assertEqual(losses, ["remote-transaction-terminal",
                                      "remote-transaction-transition",
                                      "remote-transaction-advance"])
            inspections = [request for kind, request in executor.controls
                           if kind == "remote-transaction-inspect"]
            self.assertEqual(len(inspections), 4)  # prepare inspection plus one per lost response
            self.assertFalse(hasattr(executor, "run_remote"), "lifecycle replay must perform no mutation")
        finally:
            shutil.rmtree(root)

    def test_tracer_b_accepted_local_commit_syncs_unreconciled_remote_before_next_phase(self):
        guarded = self._module(); root = Path(tempfile.mkdtemp(prefix="guarded-tracer-b-local-first-"))
        try:
            arguments, stage, bundle, evidence = self._tracer_b_arguments(guarded, root)
            executor, identity, authority = self._tracer_b_authority(guarded, arguments)
            phase, operation = guarded._TRANSACTION_TOPOLOGY[0]
            authority = self._tracer_b_pending_authority(
                guarded, arguments, executor, authority, phase,
            )
            remote = authority.record["attempts"][0]; attempt_id = remote["attempt_id"]
            raw = (json.dumps({"schema": 2, "attempt_id": attempt_id}) + "\n").encode()
            evidence_path = evidence / f"{attempt_id}-candidate.json"
            evidence_path.write_bytes(raw); evidence_path.chmod(0o600)
            evidence_digest = __import__("hashlib").sha256(raw).hexdigest(); note_id = "17"
            commit_request = {
                "schema": 1, "boundary": "b1", "phase": phase, "attempt_id": attempt_id,
                "operation_id": __import__("hashlib").sha256(f"{attempt_id}\0commit".encode()).hexdigest()[:32],
                "registry": remote["registry"], "evidence_digest": evidence_digest,
                "pending_note_id": note_id, "guard_unit": f"matrix-p13-recover-{attempt_id}.service",
                "success_record": f"/run/matrix-priority-1-3/{attempt_id}/success.json",
                "control_unit": f"matrix-p13-commit-{attempt_id}.service",
                "control_cgroup": f"/system.slice/matrix-p13-commit-{attempt_id}.service",
            }
            terminal = _fake_commit_control(guarded, "remote-commit", commit_request, executor.terminals)
            authority = guarded._active_transition(
                arguments, executor, authority, phase, "PENDING_BOUND", "SEALED_COMMITTING", lambda: 100.0,
            )
            authority = guarded._active_terminal(
                arguments, executor, authority, phase, "commit", "SEALED_COMMITTING",
                "COMMITTED_UNRECONCILED", terminal.digest, lambda: 100.0,
            )
            accepted = {"boundary": "b1", "phase": phase, "attempt_id": attempt_id,
                        "stage": str(stage), "bundle": str(bundle), "evidence_digest": evidence_digest,
                        "remote_record_digest": terminal.digest}
            journal = {"schema": 1, "identity": identity.transaction_identity, "boundary": "b1",
                       "attempts": [{"phase": phase, "operation": operation, "attempt_id": attempt_id,
                           "note_id": note_id, "accepted_record": accepted, "evidence_file": str(evidence_path),
                           "state": "ACCEPTED", "remaining_seconds": 60, "deadline_epoch": 160.0}],
                       "final": None}
            wrong_note = json.loads(json.dumps(journal))
            wrong_note["attempts"][0]["note_id"] = "18"
            before_rejection = json.loads(json.dumps(authority.record))
            with self.assertRaisesRegex(ValueError, "authoritative remote commit"):
                guarded._synchronize_reconciliation_authority(
                    arguments, executor, authority, wrong_note, wall_clock=lambda: 100.0,
                    commit_terminals={attempt_id: terminal.terminal},
                )
            self.assertEqual(executor.transaction["record"], before_rejection)
            synchronized = guarded._synchronize_reconciliation_authority(
                arguments, executor, authority, journal, wall_clock=lambda: 100.0,
            )
            self.assertEqual(synchronized.record["attempts"][0]["state"], "ACCEPTED")
            self.assertEqual(synchronized.record["current_phase_index"], 1)
            self.assertEqual(synchronized.record["attempts"][1]["state"], "READY")
            before = json.loads(json.dumps(synchronized.record))
            repeated = guarded._synchronize_reconciliation_authority(
                arguments, executor, synchronized, journal, wall_clock=lambda: 100.0,
            )
            self.assertEqual(repeated.record, before)
        finally:
            shutil.rmtree(root)

    def test_tracer_b_accepted_rollback_syncs_recovered_remote_before_final_phase(self):
        guarded = self._module(); root = Path(tempfile.mkdtemp(prefix="guarded-tracer-b-rollback-first-"))
        try:
            arguments, stage, bundle, evidence = self._tracer_b_arguments(guarded, root)
            executor, identity, authority = self._tracer_b_authority(guarded, arguments)
            entries = []
            phase, operation = guarded._TRANSACTION_TOPOLOGY[0]
            authority = self._tracer_b_pending_authority(guarded, arguments, executor, authority, phase)
            remote = authority.record["attempts"][0]; attempt_id = remote["attempt_id"]
            raw = b'{"schema":2,"phase":"initial"}\n'; path = evidence / f"{attempt_id}-candidate.json"
            path.write_bytes(raw); path.chmod(0o600); digest = __import__("hashlib").sha256(raw).hexdigest()
            request = {"schema": 1, "boundary": "b1", "phase": phase, "attempt_id": attempt_id,
                "operation_id": __import__("hashlib").sha256(f"{attempt_id}\0commit".encode()).hexdigest()[:32],
                "registry": remote["registry"], "evidence_digest": digest, "pending_note_id": "17",
                "guard_unit": f"matrix-p13-recover-{attempt_id}.service",
                "success_record": f"/run/matrix-priority-1-3/{attempt_id}/success.json",
                "control_unit": f"matrix-p13-commit-{attempt_id}.service",
                "control_cgroup": f"/system.slice/matrix-p13-commit-{attempt_id}.service"}
            terminal = _fake_commit_control(guarded, "remote-commit", request, executor.terminals)
            authority = guarded._active_transition(arguments, executor, authority, phase,
                "PENDING_BOUND", "SEALED_COMMITTING", lambda: 100.0)
            authority = guarded._active_terminal(arguments, executor, authority, phase, "commit",
                "SEALED_COMMITTING", "COMMITTED_UNRECONCILED", terminal.digest, lambda: 100.0)
            authority = guarded._active_transition(arguments, executor, authority, phase,
                "COMMITTED_UNRECONCILED", "ACCEPTED", lambda: 100.0)
            authority = guarded._active_advance(arguments, executor, authority, phase, lambda: 100.0)
            accepted = {"boundary": "b1", "phase": phase, "attempt_id": attempt_id,
                "stage": str(stage), "bundle": str(bundle), "evidence_digest": digest,
                "remote_record_digest": terminal.digest}
            entries.append({"phase": phase, "operation": operation, "attempt_id": attempt_id,
                "note_id": "17", "accepted_record": accepted, "evidence_file": str(path),
                "state": "ACCEPTED", "remaining_seconds": 60, "deadline_epoch": 160.0})

            phase, operation = guarded._TRANSACTION_TOPOLOGY[1]
            authority = self._tracer_b_pending_authority(guarded, arguments, executor, authority, phase)
            remote = authority.record["attempts"][1]; attempt_id = remote["attempt_id"]
            raw = b'{"schema":2,"phase":"rollback"}\n'; path = evidence / f"{attempt_id}-candidate.json"
            path.write_bytes(raw); path.chmod(0o600); digest = __import__("hashlib").sha256(raw).hexdigest()
            recovery_digest = __import__("hashlib").sha256(b"authoritative recovery terminal").hexdigest()
            authority = guarded._active_transition(arguments, executor, authority, phase,
                "PENDING_BOUND", "SEALED_RECOVERING", lambda: 100.0)
            authority = guarded._active_terminal(arguments, executor, authority, phase, "recovery",
                "SEALED_RECOVERING", "RECOVERED_UNVERIFIED", recovery_digest, lambda: 100.0)
            accepted = {"boundary": "b1", "phase": phase, "attempt_id": attempt_id,
                "stage": str(stage), "bundle": str(bundle), "evidence_digest": digest,
                "remote_record_digest": recovery_digest}
            entries.append({"phase": phase, "operation": operation, "attempt_id": attempt_id,
                "note_id": "18", "accepted_record": accepted, "evidence_file": str(path),
                "state": "ACCEPTED", "remaining_seconds": 60, "deadline_epoch": 160.0})
            journal = {"schema": 1, "identity": identity.transaction_identity,
                       "boundary": "b1", "attempts": entries, "final": None}
            synchronized = guarded._synchronize_reconciliation_authority(
                arguments, executor, authority, journal, wall_clock=lambda: 100.0,
            )
            self.assertEqual(synchronized.record["attempts"][1]["state"], "ROLLBACK_VERIFIED")
            self.assertEqual(synchronized.record["current_phase_index"], 2)
            self.assertEqual(synchronized.record["attempts"][2]["state"], "READY")
            before = json.loads(json.dumps(synchronized.record))
            repeated = guarded._synchronize_reconciliation_authority(
                arguments, executor, synchronized, journal, wall_clock=lambda: 100.0,
            )
            self.assertEqual(repeated.record, before)
        finally:
            shutil.rmtree(root)

    def test_tracer_b_sealed_commit_with_external_terminal_reconstructs_real_registry_before_accept_and_advance(self):
        guarded = self._module()
        root = Path(tempfile.mkdtemp(prefix="guarded-tracer-b-sealed-"))
        try:
            for name in ("identity", "known-hosts", "cookie", "token"):
                path = root / name
                path.write_text("session=value" if name == "cookie" else "fixture", encoding="ascii")
                path.chmod(0o600)
            stage, bundle, evidence = root / "stage", root / "bundle", root / "evidence"
            stage.mkdir(); bundle.mkdir(); evidence.mkdir()
            arguments = guarded.build_parser().parse_args([
                "run", "--boundary", "b1", "--ssh-target", "fixture@host",
                "--ssh-identity-file", str(root / "identity"),
                "--ssh-known-hosts-file", str(root / "known-hosts"),
                "--stage", str(stage), "--bundle", str(bundle),
                "--destination-root", str(root / "destination"), "--service", "matrix-web.service",
                "--initial-service-state", "active", "--health-url", "http://127.0.0.1:5000/",
                "--health-status", "302", "--health-timeout-seconds", "30",
                "--attempt-budget-seconds", "6600", "--commit-reserve-seconds", "60",
                "--watchdog-seconds", "7200", "--expected-sha", "a" * 40,
                "--expected-version", "0.3.58-preview", "--previous-sha", "b" * 40,
                "--previous-version", "0.3.57-preview", "--destination-identity", "matrix-production",
                "--release-stamp", "2026-08-23T02:51:53Z", "--release-manifest-digest", "c" * 64,
                "--backup-manifest-digest", "d" * 64, "--release-spec-digest", "e" * 64,
                "--session-cookie-file", str(root / "cookie"), "--evidence-dir", str(evidence),
                "--gitlab-project", "2", "--gitlab-mr", "73", "--gitlab-token-file", str(root / "token"),
            ])

            class Executor:
                def __init__(self):
                    self.transaction = {"record": None}; self.terminals = {}; self.operations = []
                    self.lose_commit_response = True; self.stop_next_phase = False
                def run_remote(self, attempt_id, operation_id, command, *, registry, run_as, operation_timeout):
                    self.operations.append((attempt_id, operation_id))
                    first = self.transaction["record"]["attempts"][0]["attempt_id"]
                    if self.stop_next_phase and attempt_id != first:
                        raise KeyboardInterrupt("after prior phase advance before next mutation")
                    operation = __import__("hashlib").sha256(
                        f"{attempt_id}\0{operation_id}".encode()
                    ).hexdigest()[:32]
                    unit = f"matrix-p13-op-{attempt_id}-{operation}.service"
                    return guarded.OperationResult(0, unit, "/system.slice/" + unit)
                def control_remote(self, kind, request, *, operation_timeout):
                    immutable = json.loads(json.dumps(request, sort_keys=True))
                    transaction = _fake_transaction_control(
                        guarded, kind, immutable, self.transaction, (),
                    )
                    if transaction is not None:
                        return transaction
                    if kind == "remote-commit":
                        result = _fake_commit_control(
                            guarded, kind, immutable, self.terminals, committed_epoch=100.0,
                        )
                        assert result is not None
                        if immutable.get("query") is not True and self.lose_commit_response:
                            self.lose_commit_response = False
                            raise OSError("external commit terminal persisted; response lost")
                        return result
                    return guarded.ControlResult(0, "d" * 64)

            class Notes(_FakeFinalAuthority):
                def __init__(self): self.events = []; self.next_id = 16; self.final_publications = []
                def pending(self, record):
                    self.next_id += 1; self.events.append(("PENDING", record["attempt_id"])); return str(self.next_id)
                def reconcile(self, note_id, state, record): self.events.append((state, record["attempt_id"]))
                def discover_final(self, record):
                    return _discover_fake_final(self.final_publications, record)
                def final(self, record):
                    _publish_fake_final(self.final_publications, record)
                    self.events.append(("FINAL", record["attempt_id"]))

            def accept(args, timeout):
                Path(args.evidence_file).write_text(
                    json.dumps({"schema": 2, "attempt_id": args.attempt_id}) + "\n", encoding="utf-8",
                )
                Path(args.evidence_file).chmod(0o600)
                return 0

            executor, notes = Executor(), Notes()
            self.assertEqual(guarded.run_release(
                arguments, executor=executor, acceptance_runner=accept, evidence_store=notes,
                clock=lambda: 100.0, wall_clock=lambda: 100.0,
            ), 2)
            first = executor.transaction["record"]["attempts"][0]
            first_id = first["attempt_id"]
            terminal = executor.terminals[first_id]
            self.assertEqual(first["state"], "SEALED_COMMITTING")
            self.assertIsNone(first["commit"])
            self.assertFalse((evidence / "guarded-release-reconciliation.json").exists())
            first_mutations = [event for event in executor.operations if event == (first_id, "deploy")]
            self.assertEqual(first_mutations, [(first_id, "deploy")])

            executor.stop_next_phase = True
            with self.assertRaisesRegex(KeyboardInterrupt, "after prior phase advance"):
                guarded.run_release(
                    arguments, executor=executor, acceptance_runner=accept, evidence_store=notes,
                    clock=lambda: 100.0, wall_clock=lambda: 100.0,
                )
            record = executor.transaction["record"]
            first = record["attempts"][0]
            self.assertEqual(first["state"], "ACCEPTED")
            self.assertEqual(first["commit"]["result_digest"], terminal["result_digest"])
            self.assertEqual(record["current_phase_index"], 1)
            self.assertEqual(record["attempts"][1]["state"], "GUARD_ARMING")
            self.assertEqual([event for event in executor.operations if event == (first_id, "deploy")],
                             first_mutations, "restart must never repeat the committed mutation")
            self.assertEqual([event[0] for event in notes.events].count("ACCEPTED"), 1)
        finally:
            shutil.rmtree(root)

    def test_no_local_journal_replay_discovers_remote_commit_before_any_mutation(self):
        guarded = self._module()
        root = Path(tempfile.mkdtemp(prefix="guarded-remote-commit-gap-"))
        original_write = guarded._write_reconciliation
        try:
            for name in ("identity", "known-hosts", "cookie", "token"):
                path = root / name
                path.write_text("session=value" if name == "cookie" else "fixture", encoding="ascii")
                path.chmod(0o600)
            stage, bundle, evidence = root / "stage", root / "bundle", root / "evidence"
            stage.mkdir(); bundle.mkdir(); evidence.mkdir()
            arguments = guarded.build_parser().parse_args([
                "run", "--boundary", "b1", "--ssh-target", "fixture@host",
                "--ssh-identity-file", str(root / "identity"),
                "--ssh-known-hosts-file", str(root / "known-hosts"),
                "--stage", str(stage), "--bundle", str(bundle),
                "--destination-root", str(root / "destination"), "--service", "matrix-web.service",
                "--initial-service-state", "active", "--health-url", "http://127.0.0.1:5000/",
                "--health-status", "302", "--health-timeout-seconds", "30",
                "--attempt-budget-seconds", "6600", "--commit-reserve-seconds", "60",
                "--watchdog-seconds", "7200", "--expected-sha", "a" * 40,
                "--expected-version", "0.3.58-preview", "--previous-sha", "b" * 40,
                "--previous-version", "0.3.57-preview", "--destination-identity", "matrix-production", "--release-stamp", "2026-08-23T02:51:53Z", "--release-manifest-digest", "c" * 64, "--backup-manifest-digest", "d" * 64, "--release-spec-digest", "e" * 64, "--session-cookie-file", str(root / "cookie"),
                "--evidence-dir", str(evidence), "--gitlab-project", "2", "--gitlab-mr", "73",
                "--gitlab-token-file", str(root / "token"),
            ])

            class Executor:
                def __init__(self):
                    self.events = []; self.terminals = {}; self.transaction = {"record": None}

                def run_remote(self, attempt_id, operation_id, command, *, registry, run_as, operation_timeout):
                    self.events.append(("operation", attempt_id, operation_id))
                    operation = __import__("hashlib").sha256(
                        f"{attempt_id}\0{operation_id}".encode()
                    ).hexdigest()[:32]
                    unit = f"matrix-p13-op-{attempt_id}-{operation}.service"
                    return guarded.OperationResult(0, unit, "/system.slice/" + unit)
                def control_remote(self, kind, request, *, operation_timeout):
                    immutable = json.loads(json.dumps(request, sort_keys=True))
                    self.events.append(("control", kind, immutable))
                    transaction = _fake_transaction_control(
                        guarded, kind, immutable, self.transaction, ("1" * 32, "2" * 32, "3" * 32)
                    )
                    if transaction is not None:
                        return transaction
                    if kind != "remote-commit":
                        return guarded.ControlResult(0, "c" * 64)
                    committed = _fake_commit_control(
                        guarded, kind, immutable, self.terminals, committed_epoch=150.0,
                    )
                    assert committed is not None
                    return committed

            class Notes(_FakeFinalAuthority):
                def __init__(self): self.events = []; self.next_id = 20; self.final_publications = []
                def pending(self, record):
                    self.next_id += 1; self.events.append(("PENDING", record["attempt_id"])); return str(self.next_id)
                def reconcile(self, note_id, state, record): self.events.append((state, record["attempt_id"]))
                def discover_final(self, record):
                    return _discover_fake_final(self.final_publications, record)
                def final(self, record):
                    _publish_fake_final(self.final_publications, record)
                    self.events.append(("FINAL", record["attempt_id"]))

            accept_calls = []
            def accept(args, timeout):
                accept_calls.append((args.phase, args.attempt_id))
                Path(args.evidence_file).write_text(
                    json.dumps({"schema": 2, "attempt_id": args.attempt_id}) + "\n", encoding="utf-8"
                )
                Path(args.evidence_file).chmod(0o600)
                return 0

            executor, notes = Executor(), Notes()
            interrupted = {"armed": True}
            def interrupt_first_local_journal(target, document):
                if interrupted["armed"] and document["attempts"]:
                    interrupted["armed"] = False
                    raise KeyboardInterrupt("after remote terminal before local reconciliation journal")
                original_write(target, document)
            guarded._write_reconciliation = interrupt_first_local_journal
            with self.assertRaises(KeyboardInterrupt):
                guarded.run_release(
                    arguments, executor=executor, acceptance_runner=accept, evidence_store=notes,
                    clock=lambda: 100.0, wall_clock=lambda: 100.0,
                    attempt_ids=iter(("1" * 32, "2" * 32, "3" * 32)),
                )
            journal_path = evidence / "guarded-release-reconciliation.json"
            self.assertFalse(journal_path.exists())
            canonical_ids = tuple(
                attempt["attempt_id"] for attempt in executor.transaction["record"]["attempts"]
            )
            first_terminal = executor.terminals[canonical_ids[0]]

            guarded._write_reconciliation = original_write
            before = len(executor.events)
            self.assertEqual(guarded.run_release(
                arguments, executor=executor, acceptance_runner=accept, evidence_store=notes,
                clock=lambda: 100.0, wall_clock=lambda: 100.0,
                attempt_ids=iter(("1" * 32, "2" * 32, "3" * 32)),
            ), 0)
            replay = executor.events[before:]
            authoritative = [event for event in replay if not (
                event[0] == "control" and event[1].startswith("remote-transaction-")
            )]
            self.assertEqual(authoritative[0][0:2], ("control", "remote-commit"))
            self.assertTrue(authoritative[0][2]["query"])
            first_mutation = next(event for event in authoritative if event[0] == "operation")
            self.assertEqual(first_mutation[1], canonical_ids[1])
            first_attempt_replays = [event for event in replay if event[0] == "control"
                                     and event[1] == "remote-commit"
                                     and event[2]["attempt_id"] == canonical_ids[0]]
            self.assertEqual(first_attempt_replays[1][2], first_terminal["request"])
            self.assertEqual([call for call in accept_calls if call[1] == canonical_ids[0]],
                             [("initial-deploy", canonical_ids[0])])
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            self.assertEqual([entry["state"] for entry in journal["attempts"]], ["ACCEPTED"] * 3)
            self.assertEqual(journal["final"]["state"], "AUTHORITATIVE_FINAL")

            # Stale candidate evidence alone is not commit authority.  An explicit
            # typed "missing" response permits the ordinary guarded path.
            journal_path.unlink()
            executor = Executor()
            before_missing = len(executor.events)
            self.assertEqual(guarded.run_release(
                arguments, executor=executor, acceptance_runner=accept, evidence_store=notes,
                clock=lambda: 100.0, wall_clock=lambda: 100.0,
                attempt_ids=iter(("1" * 32, "2" * 32, "3" * 32)),
            ), 0)
            missing_replay = executor.events[before_missing:]
            missing_authoritative = [event for event in missing_replay if not (
                event[0] == "control" and event[1].startswith("remote-transaction-")
            )]
            self.assertEqual(missing_authoritative[0][0:2], (
                "operation", executor.transaction["record"]["attempts"][0]["attempt_id"],
            ))
        finally:
            guarded._write_reconciliation = original_write
            shutil.rmtree(root)

    def test_success_record_publication_never_overwrites_a_raced_target(self):
        guarded = self._module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            target = root / "success.json"
            victim = b'{"victim":true}\n'
            request = {
                "boundary": "b1", "phase": "initial-deploy",
                "attempt_id": "a" * 32, "operation_id": "b" * 32,
                "evidence_digest": "c" * 64, "pending_note_id": "17",
            }
            real_rename = guarded.os.rename
            real_noreplace = getattr(guarded, "_rename_noreplace", None)

            def raced_rename(*args, **kwargs):
                target.write_bytes(victim)
                target.chmod(0o400)
                return real_rename(*args, **kwargs)

            def raced_noreplace(*args, **kwargs):
                target.write_bytes(victim)
                target.chmod(0o400)
                if real_noreplace is None:
                    raise AssertionError("no no-replace publication primitive")
                return real_noreplace(*args, **kwargs)

            with mock.patch.object(guarded.os, "rename", side_effect=raced_rename), \
                    mock.patch.object(guarded, "_rename_noreplace", create=True,
                                      side_effect=raced_noreplace):
                with self.assertRaises((FileExistsError, PermissionError)):
                    guarded._publish_success_record(str(target), request)
            self.assertEqual(target.read_bytes(), victim)

    def test_success_record_validation_rejects_symlink_hardlink_and_inode_replacement(self):
        guarded = self._module()
        request = {
            "boundary": "b1", "phase": "initial-deploy",
            "attempt_id": "a" * 32, "operation_id": "b" * 32,
            "evidence_digest": "c" * 64, "pending_note_id": "17",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            target = root / "success.json"
            guarded._publish_success_record(str(target), request)
            approved = target.read_bytes()
            source = root / "source.json"
            source.write_bytes(approved)
            source.chmod(0o400)

            target.unlink()
            target.symlink_to(source)
            with self.assertRaises(PermissionError):
                guarded._publish_success_record(str(target), request)
            self.assertEqual(source.read_bytes(), approved)

            target.unlink()
            os.link(source, target)
            self.assertEqual(source.stat().st_nlink, 2)
            with self.assertRaises(PermissionError):
                guarded._publish_success_record(str(target), request)
            self.assertEqual(source.stat().st_nlink, 2)

            target.unlink()
            target.write_bytes(approved)
            target.chmod(0o400)
            opened_inode = target.stat().st_ino
            real_stat = guarded.os.stat
            replaced = False

            def replace_before_path_identity(path, *args, **kwargs):
                nonlocal replaced
                if path == target.name and kwargs.get("dir_fd") is not None and not replaced:
                    replaced = True
                    target.unlink()
                    target.write_bytes(approved)
                    target.chmod(0o400)
                return real_stat(path, *args, **kwargs)

            with mock.patch.object(guarded.os, "stat", side_effect=replace_before_path_identity):
                with self.assertRaises(PermissionError):
                    guarded._publish_success_record(str(target), request)
            self.assertTrue(replaced)
            self.assertNotEqual(target.stat().st_ino, opened_inode)
            self.assertEqual(target.read_bytes(), approved)

    def test_remote_commit_rejects_sparse_registry_before_quiescence_or_publication(self):
        guarded = self._module()
        root = Path(tempfile.mkdtemp(prefix="guarded-sparse-attempt-registry-"))
        try:
            attempt = "a" * 32
            registry_path = root / "registry.json"
            registry_path.write_text(json.dumps({
                "schema": 1, "attempt_id": attempt, "state": "OPEN",
            }, sort_keys=True, separators=(",", ":")) + "\n", encoding="ascii")
            registry_path.chmod(0o600)
            success_parent = root / "success"
            success_parent.mkdir()
            request = {
                "schema": 1, "boundary": "b1", "phase": "initial-deploy",
                "attempt_id": attempt,
                "operation_id": __import__("hashlib").sha256(
                    f"{attempt}\0commit".encode()
                ).hexdigest()[:32],
                "registry": str(registry_path), "evidence_digest": "b" * 64,
                "pending_note_id": "17", "guard_unit": f"matrix-p13-recover-{attempt}.service",
                "success_record": str(success_parent / "success.json"),
                "control_unit": f"matrix-p13-commit-{attempt}.service",
                "control_cgroup": f"/system.slice/matrix-p13-commit-{attempt}.service",
            }
            quiesced = []
            class Controller:
                def quiesce(self, unit, cgroup):
                    quiesced.append((unit, cgroup))
                    return True

            before = registry_path.read_bytes()
            with self.assertRaisesRegex(ValueError, "registry"):
                guarded.dispatch_privileged("remote-commit", request, controller=Controller())
            self.assertEqual(quiesced, [])
            self.assertFalse((success_parent / "success.json").exists())
            self.assertEqual(registry_path.read_bytes(), before)
        finally:
            shutil.rmtree(root)

    def test_remote_commit_terminal_survives_lost_transport_response_and_queries_fail_closed(self):
        guarded = self._module()
        root = Path(tempfile.mkdtemp(prefix="guarded-remote-terminal-"))
        original_result = guarded.ControlResult
        try:
            attempt = "a" * 32
            registry_path = root / "registry.json"
            success_parent = root / "success"
            success_parent.mkdir()
            request = {
                "schema": 1, "boundary": "b1", "phase": "initial-deploy",
                "attempt_id": attempt,
                "operation_id": __import__("hashlib").sha256(
                    f"{attempt}\0commit".encode()
                ).hexdigest()[:32],
                "registry": str(registry_path), "evidence_digest": "b" * 64,
                "pending_note_id": "17", "guard_unit": f"matrix-p13-recover-{attempt}.service",
                "success_record": str(success_parent / "success.json"),
                "control_unit": f"matrix-p13-commit-{attempt}.service",
                "control_cgroup": f"/system.slice/matrix-p13-commit-{attempt}.service",
            }
            guarded.DurableAttemptRegistry(registry_path, attempt, create=True)
            quiesced = []
            class Controller:
                def quiesce(self, unit, cgroup): quiesced.append((unit, cgroup)); return True

            def interrupt_terminal_response(returncode, digest, terminal=None):
                if terminal is not None:
                    raise KeyboardInterrupt("remote terminal durable before transport response")
                return original_result(returncode, digest, terminal)
            guarded.ControlResult = interrupt_terminal_response
            with self.assertRaises(KeyboardInterrupt):
                guarded.dispatch_privileged("remote-commit", request, controller=Controller())
            guarded.ControlResult = original_result

            query = {
                "schema": 1, "query": True, "boundary": request["boundary"], "phase": request["phase"],
                "attempt_id": attempt, "operation_id": request["operation_id"],
                "registry": str(registry_path), "evidence_digest": request["evidence_digest"],
                "success_record": request["success_record"],
            }
            discovered = guarded.dispatch_privileged("remote-commit", query, controller=Controller())
            self.assertEqual(discovered.returncode, 0)
            self.assertEqual(discovered.terminal["request"], request)
            before_replay = list(quiesced)
            replayed = guarded.dispatch_privileged("remote-commit", request, controller=Controller())
            self.assertEqual(replayed, discovered)
            self.assertEqual(quiesced, before_replay)

            for mismatch in (
                dict(query, attempt_id="c" * 32),
                dict(query, evidence_digest="d" * 64),
            ):
                with self.subTest(mismatch=mismatch):
                    with self.assertRaises(ValueError):
                        guarded.dispatch_privileged("remote-commit", mismatch, controller=Controller())

            registry = guarded.DurableAttemptRegistry(registry_path, attempt)
            registry.mutate(lambda state: state["commit"].__setitem__("result_digest", "e" * 64))
            with self.assertRaises(ValueError):
                guarded.dispatch_privileged("remote-commit", query, controller=Controller())

            missing = dict(query, attempt_id="f" * 32,
                           operation_id=__import__("hashlib").sha256(
                               f"{'f' * 32}\0commit".encode()).hexdigest()[:32],
                           registry=str(root / "missing.json"),
                           success_record=str(root / "missing-success.json"))
            self.assertEqual(
                guarded.dispatch_privileged("remote-commit", missing, controller=Controller()).returncode,
                3,
            )
        finally:
            guarded.ControlResult = original_result
            shutil.rmtree(root)

    def test_sparse_authoritative_final_cannot_return_false_release_success(self):
        guarded = self._module()
        root = Path(tempfile.mkdtemp(prefix="guarded-sparse-authority-"))
        original_prepare = guarded._prepare_active_transaction
        try:
            stage, bundle, evidence = root / "stage", root / "bundle", root / "evidence"
            stage.mkdir(); bundle.mkdir(); evidence.mkdir()
            arguments = guarded.build_parser().parse_args([
                "run", "--boundary", "b1", "--ssh-target", "fixture@host",
                "--ssh-identity-file", str(root / "identity"),
                "--ssh-known-hosts-file", str(root / "known-hosts"),
                "--stage", str(stage), "--bundle", str(bundle),
                "--destination-root", str(root / "destination"), "--service", "matrix-web.service",
                "--initial-service-state", "active", "--health-url", "http://127.0.0.1:5000/",
                "--health-status", "302", "--health-timeout-seconds", "30",
                "--attempt-budget-seconds", "6600", "--commit-reserve-seconds", "60",
                "--watchdog-seconds", "7200", "--expected-sha", "a" * 40,
                "--expected-version", "0.3.58-preview", "--previous-sha", "b" * 40,
                "--previous-version", "0.3.57-preview", "--destination-identity", "matrix-production",
                "--release-stamp", "2026-08-23T02:51:53Z", "--release-manifest-digest", "c" * 64,
                "--backup-manifest-digest", "d" * 64, "--release-spec-digest", "e" * 64,
                "--session-cookie-file", str(root / "cookie"), "--evidence-dir", str(evidence),
                "--gitlab-project", "2", "--gitlab-mr", "73", "--gitlab-token-file", str(root / "token"),
            ])
            identity = "e" * 64
            canonical = guarded.CanonicalReleaseIdentity("f" * 64, identity, {"boundary": "b1"})
            transaction = {"attempts": [
                {"attempt_id": str(index) * 32,
                 "registry": f"/run/matrix-priority-1-3/attempts/{str(index) * 32}.json"}
                for index in (1, 2, 3)
            ]}
            guarded._prepare_active_transaction = lambda *args, **kwargs: (canonical, transaction)
            journal = {"schema": 1, "identity": identity, "boundary": "b1", "attempts": [],
                       "final": {"state": "AUTHORITATIVE_FINAL"}}
            (evidence / "guarded-release-reconciliation.json").write_text(
                json.dumps(journal) + "\n", encoding="utf-8",
            )
            self.assertEqual(guarded.run_release(
                arguments, executor=object(), evidence_store=object(),
            ), 2, "a sparse AUTHORITATIVE_FINAL must not produce release success")
        finally:
            guarded._prepare_active_transaction = original_prepare
            shutil.rmtree(root)

    def test_complete_authoritative_final_requires_bound_evidence_and_remote_commit_authority(self):
        guarded = self._module()
        root = Path(tempfile.mkdtemp(prefix="guarded-forged-complete-authority-"))
        try:
            for name in ("identity", "known-hosts", "cookie", "token"):
                path = root / name; path.write_text("fixture", encoding="ascii"); path.chmod(0o600)
            stage, bundle, evidence = root / "stage", root / "bundle", root / "evidence"
            stage.mkdir(); bundle.mkdir(); evidence.mkdir()
            arguments = guarded.build_parser().parse_args([
                "run", "--resume", "--boundary", "b1", "--ssh-target", "fixture@host",
                "--ssh-identity-file", str(root / "identity"), "--ssh-known-hosts-file", str(root / "known-hosts"),
                "--stage", str(stage), "--bundle", str(bundle), "--destination-root", str(root / "destination"),
                "--service", "matrix-web.service", "--initial-service-state", "active",
                "--health-url", "http://127.0.0.1:5000/", "--health-status", "302", "--health-timeout-seconds", "30",
                "--attempt-budget-seconds", "6600", "--commit-reserve-seconds", "60", "--watchdog-seconds", "7200",
                "--expected-sha", "a" * 40, "--expected-version", "0.3.58-preview", "--previous-sha", "b" * 40,
                "--previous-version", "0.3.57-preview", "--destination-identity", "matrix-production",
                "--release-stamp", "2026-08-23T02:51:53Z", "--release-manifest-digest", "c" * 64,
                "--backup-manifest-digest", "d" * 64, "--release-spec-digest", "e" * 64,
                "--session-cookie-file", str(root / "cookie"), "--evidence-dir", str(evidence),
                "--gitlab-project", "2", "--gitlab-mr", "73", "--gitlab-token-file", str(root / "token"),
            ])
            class Executor:
                def __init__(self): self.transaction = {"record": None}; self.terminals = {}; self.events = []
                def control_remote(self, kind, request, *, operation_timeout):
                    immutable = json.loads(json.dumps(request, sort_keys=True)); self.events.append((kind, immutable))
                    result = _fake_transaction_control(guarded, kind, immutable, self.transaction, ())
                    if result is not None: return result
                    if kind != "remote-commit" or immutable.get("query") is not True:
                        raise AssertionError("resume authenticity may only query remote commit authority")
                    terminal = self.terminals[immutable["attempt_id"]]
                    for field in guarded._COMMIT_QUERY_FIELDS - {"query"}:
                        if immutable[field] != terminal["request"][field]:
                            return guarded.ControlResult(2, "")
                    return guarded.ControlResult(0, terminal["result_digest"], terminal)
            executor = Executor()
            canonical, transaction = guarded._prepare_active_transaction(arguments, executor, created_epoch=100)
            attempts, evidence_bytes = [], {}
            for index, (phase, operation) in enumerate(guarded._TRANSACTION_TOPOLOGY):
                remote = transaction["attempts"][index]; attempt_id, registry = remote["attempt_id"], remote["registry"]
                raw = (json.dumps({"schema": 2, "attempt_id": attempt_id}, sort_keys=True) + "\n").encode()
                candidate = evidence / f"{attempt_id}-candidate.json"; candidate.write_bytes(raw); candidate.chmod(0o600)
                evidence_bytes[attempt_id] = raw; digest = __import__("hashlib").sha256(raw).hexdigest()
                note_id = str(17 + index)
                operation_id = __import__("hashlib").sha256(f"{attempt_id}\0commit".encode()).hexdigest()[:32]
                request = {"schema": 1, "boundary": "b1", "phase": phase, "attempt_id": attempt_id,
                           "operation_id": operation_id, "registry": registry, "evidence_digest": digest,
                           "pending_note_id": note_id, "guard_unit": f"matrix-p13-recover-{attempt_id}.service",
                           "success_record": f"/run/matrix-priority-1-3/{attempt_id}/success.json",
                           "control_unit": f"matrix-p13-commit-{attempt_id}.service",
                           "control_cgroup": f"/system.slice/matrix-p13-commit-{attempt_id}.service"}
                success = {"schema": 1, "boundary": "b1", "phase": phase, "attempt_id": attempt_id,
                           "operation_id": operation_id, "evidence_digest": digest, "pending_note_id": note_id}
                remote_digest = __import__("hashlib").sha256(guarded._canonical_json_bytes(success) + b"\n").hexdigest()
                executor.terminals[attempt_id] = {"schema": 1, "request": request,
                    "request_digest": guarded._request_digest("remote-commit", request),
                    "result_digest": remote_digest, "committed_epoch": 100.0}
                accepted = {"boundary": "b1", "phase": phase, "attempt_id": attempt_id,
                    "stage": str(stage), "bundle": str(bundle), "evidence_digest": digest,
                    "remote_record_digest": remote_digest}
                attempts.append({"phase": phase, "operation": operation, "attempt_id": attempt_id,
                    "note_id": note_id, "accepted_record": accepted, "evidence_file": str(candidate),
                    "state": "ACCEPTED", "remaining_seconds": 60, "deadline_epoch": 160.0})

            # A legal accepted journal is only an observer of the matching canonical
            # progressed remote authority.  Build that authority through the exact
            # transition/terminal/advance graph rather than forging READY records.
            authority = guarded._claim_active_transaction(
                arguments, executor, canonical, transaction, coordinator_epoch=100,
            )
            ordinary = (
                ("READY", "GUARD_ARMING"), ("GUARD_ARMING", "GUARDED"),
                ("GUARDED", "MUTATION_RUNNING"), ("MUTATION_RUNNING", "MUTATION_DONE"),
                ("MUTATION_DONE", "ACCEPTANCE_RUNNING"),
                ("ACCEPTANCE_RUNNING", "CANDIDATE_DURABLE"),
                ("CANDIDATE_DURABLE", "PENDING_BOUND"),
            )
            for index, (phase, _operation) in enumerate(guarded._TRANSACTION_TOPOLOGY):
                for from_state, to_state in ordinary:
                    authority = guarded._active_transition(
                        arguments, executor, authority, phase, from_state, to_state, lambda: 100.0,
                    )
                if phase == "rollback-proof":
                    recovery_digest = __import__("hashlib").sha256(
                        f"{attempts[index]['attempt_id']}\0recovery".encode()
                    ).hexdigest()
                    authority = guarded._active_transition(
                        arguments, executor, authority, phase,
                        "PENDING_BOUND", "SEALED_RECOVERING", lambda: 100.0,
                    )
                    authority = guarded._active_terminal(
                        arguments, executor, authority, phase, "recovery",
                        "SEALED_RECOVERING", "RECOVERED_UNVERIFIED",
                        recovery_digest, lambda: 100.0,
                    )
                    authority = guarded._active_transition(
                        arguments, executor, authority, phase,
                        "RECOVERED_UNVERIFIED", "ROLLBACK_VERIFIED", lambda: 100.0,
                    )
                    attempts[index]["accepted_record"]["remote_record_digest"] = recovery_digest
                else:
                    authority = guarded._active_transition(
                        arguments, executor, authority, phase,
                        "PENDING_BOUND", "SEALED_COMMITTING", lambda: 100.0,
                    )
                    authority = guarded._active_terminal(
                        arguments, executor, authority, phase, "commit",
                        "SEALED_COMMITTING", "COMMITTED_UNRECONCILED",
                        executor.terminals[attempts[index]["attempt_id"]]["result_digest"], lambda: 100.0,
                    )
                    authority = guarded._active_transition(
                        arguments, executor, authority, phase,
                        "COMMITTED_UNRECONCILED", "ACCEPTED", lambda: 100.0,
                    )
                authority = guarded._active_advance(
                    arguments, executor, authority, phase, lambda: 100.0,
                )
            last = attempts[-1]
            final_bytes = (json.dumps({"schema": 2, "attempt_id": last["attempt_id"], "final": True}) + "\n").encode()
            final_path = evidence / f"{last['attempt_id']}-final-verification.json"
            final_path.write_bytes(final_bytes); final_path.chmod(0o600)
            legal = {"schema": 1, "identity": canonical.transaction_identity, "boundary": "b1", "attempts": attempts,
                "final": {"state": "AUTHORITATIVE_FINAL", "record": {"boundary": "b1",
                    "attempt_id": last["attempt_id"], "note_id": last["note_id"],
                    "candidate_evidence_digest": last["accepted_record"]["evidence_digest"],
                    "remote_record_digest": last["accepted_record"]["remote_record_digest"],
                    "accepted_record_digest": __import__("hashlib").sha256(
                        guarded._canonical_json_bytes(last["accepted_record"])).hexdigest(),
                    "verification_digest": __import__("hashlib").sha256(final_bytes).hexdigest()},
                    "evidence_file": str(final_path), "remaining_seconds": 60, "deadline_epoch": 160.0}}
            journal_path = evidence / "guarded-release-reconciliation.json"
            class Notes(_FakeFinalAuthority):
                def __init__(self): self.events = []; self.final_publications = []
                def reconcile(self, *args): self.events.append("ACCEPTED")
                def discover_final(self, record):
                    return _discover_fake_final(self.final_publications, record)
                def final(self, record):
                    _publish_fake_final(self.final_publications, record)
                    self.events.append("FINAL")
            for forgery in ("nonexistent-arbitrary", "mismatched-evidence", "wrong-remote", "wrong-final"):
                with self.subTest(forgery=forgery):
                    forged = json.loads(json.dumps(legal))
                    for entry in forged["attempts"]:
                        path = Path(entry["evidence_file"]); path.write_bytes(evidence_bytes[entry["attempt_id"]]); path.chmod(0o600)
                    final_path.write_bytes(final_bytes); final_path.chmod(0o600)
                    if forgery == "nonexistent-arbitrary":
                        for index, entry in enumerate(forged["attempts"]):
                            Path(entry["evidence_file"]).unlink(); entry["accepted_record"]["evidence_digest"] = str(index + 4) * 64
                            entry["accepted_record"]["remote_record_digest"] = str(index + 7) * 64
                        final_path.unlink()
                        forged["final"]["record"]["accepted_record_digest"] = __import__("hashlib").sha256(
                            guarded._canonical_json_bytes(forged["attempts"][-1]["accepted_record"])).hexdigest()
                        forged["final"]["record"]["verification_digest"] = "f" * 64
                    elif forgery == "mismatched-evidence":
                        Path(forged["attempts"][0]["evidence_file"]).write_bytes(b"forged bytes\n")
                    elif forgery == "wrong-remote": forged["attempts"][1]["accepted_record"]["remote_record_digest"] = "f" * 64
                    else: forged["final"]["record"]["verification_digest"] = "f" * 64
                    journal_path.write_text(json.dumps(forged) + "\n", encoding="utf-8")
                    before, notes = journal_path.read_bytes(), Notes()
                    self.assertEqual(guarded.run_release(arguments, executor=executor, evidence_store=notes,
                        clock=lambda: 100.0, wall_clock=lambda: 100.0), 2)
                    self.assertEqual(notes.events, [], "forgery must fail before publication")
                    self.assertEqual(journal_path.read_bytes(), before, "forgery must fail before mutation")

            # Candidate observer files are reconstructible from sealed remote commit
            # terminals; the independent final-verification observer remains mandatory.
            for entry in legal["attempts"]:
                path = Path(entry["evidence_file"])
                if path.exists(): path.unlink()
            final_path.write_bytes(final_bytes); final_path.chmod(0o600)
            journal_path.write_text(json.dumps(legal) + "\n", encoding="utf-8")
            before, notes = journal_path.read_bytes(), Notes()
            self.assertEqual(guarded.run_release(
                arguments, executor=executor, evidence_store=notes,
                clock=lambda: 100.0, wall_clock=lambda: 100.0,
            ), 0)
            self.assertEqual(notes.events, [])
            self.assertEqual(journal_path.read_bytes(), before)
        finally:
            shutil.rmtree(root)

    def test_cached_commit_terminal_remains_authoritative_after_reconciliation_retry_deadline(self):
        guarded = self._module()
        attempt_id = "1" * 32
        evidence_digest = "a" * 64
        registry = f"/run/matrix-priority-1-3/attempts/{attempt_id}.json"
        commit_request = {
            "schema": 1, "boundary": "b1", "phase": "initial-deploy",
            "attempt_id": attempt_id,
            "operation_id": __import__("hashlib").sha256(
                f"{attempt_id}\0commit".encode("utf-8")
            ).hexdigest()[:32],
            "registry": registry, "evidence_digest": evidence_digest,
            "pending_note_id": "17",
            "guard_unit": f"matrix-p13-recover-{attempt_id}.service",
            "success_record": f"/run/matrix-priority-1-3/{attempt_id}/success.json",
            "control_unit": f"matrix-p13-commit-{attempt_id}.service",
            "control_cgroup": f"/system.slice/matrix-p13-commit-{attempt_id}.service",
        }
        success = {key: commit_request[key] for key in (
            "schema", "boundary", "phase", "attempt_id", "operation_id",
            "evidence_digest", "pending_note_id",
        )}
        terminal = {
            "schema": 1, "request": commit_request,
            "request_digest": guarded._request_digest("remote-commit", commit_request),
            "result_digest": __import__("hashlib").sha256(
                guarded._canonical_json_bytes(success) + b"\n"
            ).hexdigest(),
            "committed_epoch": 100.0,
        }
        journal = {
            "boundary": "b1", "attempts": [{
                "phase": "initial-deploy", "operation": "deploy",
                "attempt_id": attempt_id, "note_id": "17",
                "accepted_record": {
                    "boundary": "b1", "phase": "initial-deploy",
                    "attempt_id": attempt_id, "stage": "/stage", "bundle": "/bundle",
                    "evidence_digest": evidence_digest,
                    "remote_record_digest": terminal["result_digest"],
                },
                "evidence_file": "/nonexistent/cached-terminal-evidence.json",
                "state": "ACCEPTED", "remaining_seconds": 1,
                "deadline_epoch": 101.0,
            }],
            "final": None,
        }
        remote_attempts = [
            {"attempt_id": attempt_id, "registry": registry},
            {"attempt_id": "2" * 32, "registry": "/run/matrix-priority-1-3/attempts/" + "2" * 32 + ".json"},
            {"attempt_id": "3" * 32, "registry": "/run/matrix-priority-1-3/attempts/" + "3" * 32 + ".json"},
        ]
        executor = mock.Mock()

        result = guarded._validate_reconciliation_authenticity(
            journal, mock.Mock(), executor, remote_attempts,
            wall_clock=lambda: 200.0,
            cached_commit_terminals={attempt_id: terminal},
        )

        self.assertEqual(result, {attempt_id: terminal})
        executor.control_remote.assert_not_called()

    def test_reconciliation_journal_rejects_malformed_bound_records_before_load_or_publication(self):
        guarded = self._module()
        root = Path(tempfile.mkdtemp(prefix="guarded-reconciliation-schema-"))
        original_prepare = guarded._prepare_active_transaction
        try:
            stage, bundle, evidence = root / "stage", root / "bundle", root / "evidence"
            stage.mkdir(); bundle.mkdir(); evidence.mkdir()
            arguments = guarded.build_parser().parse_args([
                "run", "--boundary", "b1", "--ssh-target", "fixture@host",
                "--ssh-identity-file", str(root / "identity"),
                "--ssh-known-hosts-file", str(root / "known-hosts"),
                "--stage", str(stage), "--bundle", str(bundle),
                "--destination-root", str(root / "destination"), "--service", "matrix-web.service",
                "--initial-service-state", "active", "--health-url", "http://127.0.0.1:5000/",
                "--health-status", "302", "--health-timeout-seconds", "30",
                "--attempt-budget-seconds", "6600", "--commit-reserve-seconds", "60",
                "--watchdog-seconds", "7200", "--expected-sha", "a" * 40,
                "--expected-version", "0.3.58-preview", "--previous-sha", "b" * 40,
                "--previous-version", "0.3.57-preview", "--destination-identity", "matrix-production",
                "--release-stamp", "2026-08-23T02:51:53Z", "--release-manifest-digest", "c" * 64,
                "--backup-manifest-digest", "d" * 64, "--release-spec-digest", "e" * 64,
                "--session-cookie-file", str(root / "cookie"), "--evidence-dir", str(evidence),
                "--gitlab-project", "2", "--gitlab-mr", "73", "--gitlab-token-file", str(root / "token"),
            ])
            identity = "e" * 64
            attempt_ids = ("1" * 32, "2" * 32, "3" * 32)
            canonical = guarded.CanonicalReleaseIdentity("f" * 64, identity, {"boundary": "b1"})
            transaction = {"attempts": [
                {"attempt_id": attempt_id, "registry": f"/run/matrix-priority-1-3/attempts/{attempt_id}.json"}
                for attempt_id in attempt_ids
            ]}
            guarded._prepare_active_transaction = lambda *args, **kwargs: (canonical, transaction)

            journal_path = evidence / "guarded-release-reconciliation.json"
            accepted = {
                "boundary": "b1", "phase": "initial-deploy", "attempt_id": attempt_ids[0],
                "stage": str(stage), "bundle": str(bundle), "evidence_digest": "a" * 64,
                "remote_record_digest": "b" * 64,
            }
            entry = {
                "phase": "initial-deploy", "operation": "deploy", "attempt_id": attempt_ids[0],
                "note_id": "17", "accepted_record": accepted,
                "evidence_file": str(evidence / f"{attempt_ids[0]}-candidate.json"),
                "state": "COMMITTED_UNRECONCILED", "remaining_seconds": 10,
                "deadline_epoch": 110.0,
            }
            legal = {"schema": 1, "identity": identity, "boundary": "b1",
                     "attempts": [entry], "final": None}
            malformed = []
            value = json.loads(json.dumps(legal)); value["extra"] = None; malformed.append(value)
            value = json.loads(json.dumps(legal)); value["attempts"][0]["accepted_record"]["extra"] = 1; malformed.append(value)
            value = json.loads(json.dumps(legal)); value["attempts"][0]["remaining_seconds"] = True; malformed.append(value)
            value = json.loads(json.dumps(legal)); value["attempts"][0]["accepted_record"]["attempt_id"] = attempt_ids[1]; malformed.append(value)
            value = json.loads(json.dumps(legal)); value["attempts"][0]["accepted_record"]["evidence_digest"] = True; malformed.append(value)
            value = json.loads(json.dumps(legal)); value["attempts"][0]["accepted_record"]["boundary"] = "b2"; malformed.append(value)
            for index, value in enumerate(malformed):
                with self.subTest(load_malformed_attempt=index):
                    journal_path.write_text(json.dumps(value) + "\n", encoding="utf-8")
                    with self.assertRaises(ValueError):
                        guarded._load_reconciliation(journal_path, identity)

            all_attempts = []
            for index, (phase, operation) in enumerate(guarded._TRANSACTION_TOPOLOGY):
                record = dict(accepted, phase=phase, attempt_id=attempt_ids[index])
                item = dict(entry, phase=phase, operation=operation, attempt_id=attempt_ids[index],
                            accepted_record=record, note_id=str(17 + index), state="ACCEPTED",
                            evidence_file=str(evidence / f"{attempt_ids[index]}-candidate.json"))
                all_attempts.append(item)
            accepted_digest = __import__("hashlib").sha256(json.dumps(
                all_attempts[-1]["accepted_record"], sort_keys=True, separators=(",", ":")
            ).encode("utf-8")).hexdigest()
            final = {
                "state": "FINAL_UNRECONCILED",
                "record": {"boundary": "b1", "attempt_id": attempt_ids[-1],
                           "accepted_record_digest": accepted_digest,
                           "verification_digest": "c" * 64},
                "evidence_file": str(evidence / f"{attempt_ids[-1]}-final-verification.json"),
                "remaining_seconds": 10, "deadline_epoch": 110.0,
            }
            legal_final = {"schema": 1, "identity": identity, "boundary": "b1",
                           "attempts": all_attempts, "final": final}
            malformed_final = []
            value = json.loads(json.dumps(legal_final)); value["final"]["record"]["extra"] = 1; malformed_final.append(value)
            value = json.loads(json.dumps(legal_final)); value["final"]["remaining_seconds"] = False; malformed_final.append(value)
            value = json.loads(json.dumps(legal_final)); value["final"]["record"]["attempt_id"] = attempt_ids[0]; malformed_final.append(value)
            value = json.loads(json.dumps(legal_final)); value["final"]["record"]["accepted_record_digest"] = "d" * 64; malformed_final.append(value)
            value = json.loads(json.dumps(legal_final)); value["final"]["record"]["verification_digest"] = True; malformed_final.append(value)
            for index, value in enumerate(malformed_final):
                with self.subTest(load_malformed_final=index):
                    journal_path.write_text(json.dumps(value) + "\n", encoding="utf-8")
                    with self.assertRaises(ValueError):
                        guarded._load_reconciliation(journal_path, identity)

            class Notes(_FakeFinalAuthority):
                def __init__(self): self.events = []; self.final_publications = []
                def reconcile(self, *args): self.events.append(("ACCEPTED", args))
                def discover_final(self, record):
                    return _discover_fake_final(self.final_publications, record)
                def final(self, record):
                    _publish_fake_final(self.final_publications, record)
                    self.events.append(("FINAL", (record,)))
            notes = Notes()
            bad_attempt = json.loads(json.dumps(legal))
            bad_attempt["attempts"][0]["accepted_record"]["attempt_id"] = attempt_ids[1]
            with self.assertRaises(ValueError):
                guarded._bounded_reconcile_attempt(
                    notes, bad_attempt["attempts"][0], bad_attempt, journal_path,
                    lambda: 100.0, lambda: 100.0,
                )
            bad_final = json.loads(json.dumps(legal_final))
            bad_final["final"]["record"]["accepted_record_digest"] = "d" * 64
            with self.assertRaises(ValueError):
                guarded._bounded_publish_final(
                    notes, bad_final["final"], bad_final, journal_path,
                    lambda: 100.0, lambda: 100.0,
                )
            self.assertEqual(notes.events, [], "invalid records must not reach publication")
        finally:
            guarded._prepare_active_transaction = original_prepare
            shutil.rmtree(root)

    def test_post_commit_reconciliation_boundary_interruptions_replay_idempotently(self):
        guarded = self._module()
        root = Path(tempfile.mkdtemp(prefix="guarded-reconcile-boundaries-"))
        try:
            path = root / "guarded-release-reconciliation.json"
            deadline_epoch = __import__("time").time() + 10
            accepted = {
                "boundary": "b1", "phase": "initial-deploy", "attempt_id": "1" * 32,
                "stage": str(root / "stage"), "bundle": str(root / "bundle"),
                "evidence_digest": "a" * 64, "remote_record_digest": "b" * 64,
            }
            entry = {
                "phase": "initial-deploy", "operation": "deploy", "attempt_id": "1" * 32,
                "note_id": "17", "accepted_record": accepted,
                "evidence_file": str(root / f"{'1' * 32}-candidate.json"),
                "state": "COMMITTED_UNRECONCILED", "remaining_seconds": 10,
                "deadline_epoch": deadline_epoch,
            }
            journal = {"schema": 1, "identity": "e" * 64, "boundary": "b1",
                       "attempts": [entry], "final": None}
            original_write = guarded._write_reconciliation
            guarded._write_reconciliation(path, journal)

            class Notes(_FakeFinalAuthority):
                def __init__(self):
                    self.accepted = 0; self.finals = 0; self.final_publications = []
                def reconcile(self, note_id, state, record): self.accepted += 1
                def discover_final(self, record):
                    return _discover_fake_final(self.final_publications, record)
                def final(self, record):
                    _publish_fake_final(self.final_publications, record)
                    self.finals += 1
            notes = Notes()
            interrupted = {"accepted": True, "final": True}
            def interrupt_after_authority(target, document):
                final = document.get("final")
                if entry["state"] == "ACCEPTED" and interrupted["accepted"]:
                    interrupted["accepted"] = False
                    raise KeyboardInterrupt("after GitLab ACCEPTED before durable ACCEPTED")
                if type(final) is dict and final.get("state") == "AUTHORITATIVE_FINAL" and interrupted["final"]:
                    interrupted["final"] = False
                    raise KeyboardInterrupt("after GitLab FINAL before durable FINAL")
                original_write(target, document)
            guarded._write_reconciliation = interrupt_after_authority
            with self.assertRaises(KeyboardInterrupt):
                guarded._bounded_reconcile_attempt(notes, entry, journal, path, lambda: 1.0)
            self.assertEqual(json.loads(path.read_text())["attempts"][0]["state"], "COMMITTED_UNRECONCILED")
            guarded._write_reconciliation = original_write
            disk = guarded._load_reconciliation(path, "e" * 64)
            self.assertTrue(guarded._bounded_reconcile_attempt(notes, disk["attempts"][0], disk, path, lambda: 1.0))
            self.assertEqual(notes.accepted, 2)

            for index, (phase, operation) in enumerate(guarded._TRANSACTION_TOPOLOGY[1:], start=2):
                attempt_id = str(index) * 32
                accepted_record = dict(
                    accepted, phase=phase, attempt_id=attempt_id,
                    evidence_digest=str(index) * 64, remote_record_digest=str(index + 3) * 64,
                )
                disk["attempts"].append({
                    "phase": phase, "operation": operation, "attempt_id": attempt_id,
                    "note_id": str(16 + index), "accepted_record": accepted_record,
                    "evidence_file": str(root / f"{attempt_id}-candidate.json"),
                    "state": "ACCEPTED", "remaining_seconds": 10,
                    "deadline_epoch": deadline_epoch,
                })
            final_record = {
                "boundary": "b1", "attempt_id": "3" * 32,
                "note_id": disk["attempts"][-1]["note_id"],
                "candidate_evidence_digest": disk["attempts"][-1]["accepted_record"]["evidence_digest"],
                "remote_record_digest": disk["attempts"][-1]["accepted_record"]["remote_record_digest"],
                "accepted_record_digest": __import__("hashlib").sha256(json.dumps(
                    disk["attempts"][-1]["accepted_record"], sort_keys=True, separators=(",", ":")
                ).encode("utf-8")).hexdigest(),
                "verification_digest": "f" * 64,
            }
            final = {"state": "FINAL_UNRECONCILED", "record": final_record,
                     "evidence_file": str(root / f"{'3' * 32}-final-verification.json"),
                     "remaining_seconds": 10, "deadline_epoch": deadline_epoch}
            disk["final"] = final
            original_write(path, disk)
            guarded._write_reconciliation = interrupt_after_authority
            with self.assertRaises(KeyboardInterrupt):
                guarded._bounded_publish_final(notes, final, disk, path, lambda: 1.0)
            self.assertEqual(json.loads(path.read_text())["final"]["state"], "FINAL_UNRECONCILED")
            guarded._write_reconciliation = original_write
            disk = guarded._load_reconciliation(path, "e" * 64)
            self.assertTrue(guarded._bounded_publish_final(notes, disk["final"], disk, path, lambda: 1.0))
            self.assertEqual(notes.finals, 1)
            self.assertEqual(len(notes.final_publications), 1)
            self.assertEqual(json.loads(path.read_text())["final"]["state"], "AUTHORITATIVE_FINAL")
        finally:
            guarded._write_reconciliation = original_write
            shutil.rmtree(root)

    def test_successful_final_post_with_lost_response_is_rediscovered_without_duplicate(self):
        guarded = self._module()
        root = Path(tempfile.mkdtemp(prefix="guarded-final-lost-response-"))
        try:
            deadline_epoch = __import__("time").time() + 30
            attempts = []
            for index, (phase, operation) in enumerate(guarded._TRANSACTION_TOPOLOGY):
                attempt_id = str(index + 1) * 32
                accepted = {
                    "boundary": "b1", "phase": phase, "attempt_id": attempt_id,
                    "stage": str(root / "stage"), "bundle": str(root / "bundle"),
                    "evidence_digest": str(index + 1) * 64,
                    "remote_record_digest": str(index + 4) * 64,
                }
                attempts.append({
                    "phase": phase, "operation": operation, "attempt_id": attempt_id,
                    "note_id": str(41 + index), "accepted_record": accepted,
                    "evidence_file": str(root / f"{attempt_id}-candidate.json"),
                    "state": "ACCEPTED", "remaining_seconds": 30,
                    "deadline_epoch": deadline_epoch,
                })
            accepted = attempts[-1]["accepted_record"]
            final_record = {
                "boundary": "b1", "attempt_id": attempts[-1]["attempt_id"],
                "note_id": attempts[-1]["note_id"],
                "candidate_evidence_digest": accepted["evidence_digest"],
                "remote_record_digest": accepted["remote_record_digest"],
                "accepted_record_digest": __import__("hashlib").sha256(
                    guarded._canonical_json_bytes(accepted)).hexdigest(),
                "verification_digest": "f" * 64,
            }
            final = {"state": "FINAL_UNRECONCILED", "record": final_record,
                     "evidence_file": str(root / f"{attempts[-1]['attempt_id']}-final-verification.json"),
                     "remaining_seconds": 30, "deadline_epoch": deadline_epoch}
            journal = {"schema": 1, "identity": "e" * 64, "boundary": "b1",
                       "attempts": attempts, "final": final}
            path = root / guarded._RECONCILIATION_FILE
            guarded._write_reconciliation(path, journal)

            class Notes(_FakeFinalAuthority):
                def __init__(self): self.publications = []; self.lost = True
                def discover_final(self, record):
                    matches = [item for item in self.publications if item == record]
                    if len(matches) > 1: raise ValueError("duplicate final")
                    return copy.deepcopy(matches[0]) if matches else None
                def final(self, record):
                    self.publications.append(copy.deepcopy(record))
                    if self.lost:
                        self.lost = False
                        raise OSError("response lost after successful POST")
            notes = Notes()

            self.assertTrue(guarded._bounded_publish_final(
                notes, final, journal, path, lambda: 1.0,
            ))
            self.assertEqual(notes.publications, [final_record])
            self.assertEqual(json.loads(path.read_text())["final"]["state"], "AUTHORITATIVE_FINAL")
        finally:
            shutil.rmtree(root)

    def test_fake_clock_attempt_deadline_precedes_watchdog_and_forces_pending_recovery(self):
        guarded = self._module()

        class Clock:
            def __init__(self): self.now = 100.0
            def monotonic(self): return self.now

        clock = Clock()
        events = []

        def arm_watchdog(deadline):
            events.append(("armed", deadline))
            return "guard"

        def work(deadline):
            events.append(("work", deadline))
            clock.now = deadline + 0.001
            return 0

        result = guarded.run_guarded_attempt(
            clock=clock.monotonic,
            attempt_budget_seconds=6600,
            commit_reserve_seconds=60,
            watchdog_seconds=7200,
            arm_watchdog=arm_watchdog,
            work=work,
            recover=lambda reason: events.append(("recover", reason)) or 0,
            commit=lambda: events.append(("commit",)) or 0,
        )

        self.assertEqual(events[0], ("armed", 7300.0))
        self.assertEqual(events[1], ("work", 6640.0))
        self.assertLess(events[1][1], events[0][1])
        self.assertEqual(result.state, "PENDING")
        self.assertEqual(result.reason, "attempt-deadline")
        self.assertIn(("recover", "attempt-deadline"), events)
        self.assertNotIn(("commit",), events)

    def test_remote_descendants_and_start_race_are_sealed_term_killed_and_extinct_before_recovery(self):
        guarded = self._module()
        registry = guarded.AttemptRegistry("attempt-a")
        self.assertTrue(registry.register_operation("op-a", "unit-a", "/cg/a"))
        self.assertTrue(registry.register_operation("op-b", "unit-b", "/cg/b"))
        events = []

        class Controller:
            def __init__(self): self.killed = set()
            def term(self, unit):
                events.append(("TERM", unit))
                if unit == "unit-a":
                    events.append(("racing-start", registry.register_operation("late", "unit-late", "/cg/late")))
            def is_extinct(self, cgroup):
                events.append(("extinct?", cgroup))
                return cgroup == "/cg/a" or cgroup.removeprefix("/cg/") in self.killed
            def kill(self, unit):
                events.append(("KILL", unit))
                self.killed.add(unit.removeprefix("unit-"))

        result = guarded.quiesce_then_recover(
            registry,
            request_digest="recovery-digest",
            controller=Controller(),
            recover=lambda: events.append(("RECOVER",)) or 0,
        )

        self.assertEqual(result, 0)
        self.assertEqual(registry.state, "SEALED_RECOVERING")
        self.assertIn(("racing-start", False), events)
        self.assertIn(("TERM", "unit-a"), events)
        self.assertIn(("TERM", "unit-b"), events)
        self.assertNotIn(("KILL", "unit-a"), events)
        self.assertIn(("KILL", "unit-b"), events)
        recover_index = events.index(("RECOVER",))
        self.assertLess(events.index(("extinct?", "/cg/a")), recover_index)
        self.assertLess(events.index(("extinct?", "/cg/b"), events.index(("KILL", "unit-b"))), recover_index)
        self.assertFalse(registry.register_operation("after", "unit-after", "/cg/after"))

    def test_privileged_request_framing_is_canonical_bounded_and_exact_argv(self):
        import base64
        import json
        guarded = self._module()
        helpers = ("remote-start", "remote-stop", "remote-recover", "remote-commit")
        argv = ["/stage/deploy/release_bundle.py", "recover", "--service", "matrix.service", "snowman-\u2603"]
        canonical = json.dumps(argv, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        expected = base64.urlsafe_b64encode(canonical).rstrip(b"=").decode("ascii")
        self.assertEqual(guarded.encode_privileged_argv(argv), expected)
        for helper in helpers:
            self.assertEqual(guarded.decode_privileged_argv(helper, expected), argv)
            self.assertEqual(
                guarded.build_privileged_execution(helper, expected, executable="/trusted/helper"),
                ["/trusted/helper", helper, "--", *argv],
            )

        frame = lambda value: base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
        invalid = [
            expected + "=",                         # padding
            "+" + expected[1:],                     # alternate alphabet
            "A" * 65537,                            # encoded bound
            frame(b"\xff"),                       # malformed UTF-8
            frame(b"["),                            # malformed JSON
            frame(b"[ \"x\" ]"),                # noncanonical whitespace
            frame(b"\"scalar\""),              # wrong scalar shape
            frame(b"[]"),                           # empty
            frame(b"[1]"),                          # non-string
            frame(b"[[\"nested\"]]"),           # nested
            frame(json.dumps(["bad\x00arg"], ensure_ascii=True, separators=(",", ":")).encode()),
            frame(json.dumps(["x"] * 257, ensure_ascii=True, separators=(",", ":")).encode()),
            frame(json.dumps(["x" * 4097], ensure_ascii=True, separators=(",", ":")).encode()),
            frame(json.dumps(["x" * 4096] * 13, ensure_ascii=True, separators=(",", ":")).encode()),
        ]
        for helper in helpers:
            for payload in invalid:
                with self.subTest(helper=helper, payload_size=len(payload)):
                    with self.assertRaises(ValueError):
                        guarded.decode_privileged_argv(helper, payload)
        for bad_helper in ("start", "recover", "", "REMOTE-START"):
            with self.assertRaises(ValueError):
                guarded.decode_privileged_argv(bad_helper, expected)

    def test_remote_commit_is_idempotent_self_exempt_and_wins_start_disarm_races(self):
        guarded = self._module()
        registry = guarded.AttemptRegistry("attempt-c")
        self.assertTrue(registry.register_operation("deploy", "unit-deploy", "/cg/deploy"))
        events = []

        def quiesce(targets):
            events.append(("quiesce", tuple(targets)))
            events.append(("start-race", registry.register_operation("late-a", "unit-late-a", "/cg/late-a")))
            return 0

        def write_success():
            events.append(("write-success",))
            return "success-record-digest"

        def disarm(unit):
            events.append(("disarm", unit))
            events.append(("disarm-start-race", registry.register_operation("late-b", "unit-late-b", "/cg/late-b")))
            return 0

        result = guarded.remote_commit(
            registry,
            request_digest="commit-request-digest",
            control_unit="matrix-p13-commit-attempt-c.service",
            control_cgroup="/cg/commit",
            guard_unit="matrix-p13-recover-attempt-c.service",
            quiesce_operations=quiesce,
            write_success=write_success,
            disarm_guard=disarm,
        )
        self.assertEqual(result, guarded.ControlResult(0, "success-record-digest"))
        self.assertEqual(registry.state, "SEALED_COMMITTED")
        self.assertIn(("start-race", False), events)
        self.assertIn(("disarm-start-race", False), events)
        quiesced = next(event[1] for event in events if event[0] == "quiesce")
        self.assertIn(("unit-deploy", "/cg/deploy"), quiesced)
        self.assertNotIn(("matrix-p13-commit-attempt-c.service", "/cg/commit"), quiesced)

        before_retry = list(events)
        retry = guarded.remote_commit(
            registry,
            request_digest="commit-request-digest",
            control_unit="matrix-p13-commit-attempt-c.service",
            control_cgroup="/cg/commit",
            guard_unit="matrix-p13-recover-attempt-c.service",
            quiesce_operations=quiesce,
            write_success=write_success,
            disarm_guard=disarm,
        )
        self.assertEqual(retry, result)
        self.assertEqual(events, before_retry)
        with self.assertRaises(ValueError):
            guarded.remote_commit(
                registry,
                request_digest="mismatch",
                control_unit="matrix-p13-commit-attempt-c.service",
                control_cgroup="/cg/commit",
                guard_unit="matrix-p13-recover-attempt-c.service",
                quiesce_operations=quiesce,
                write_success=write_success,
                disarm_guard=disarm,
            )

    def test_immediate_and_watchdog_execstart_are_identical_remote_recover_only(self):
        guarded = self._module()
        request_b64 = guarded.encode_privileged_argv([
            "b1", "final-deploy", "attempt-r", "evidence-digest", "pending-note", "guard-unit"
        ])
        expected = [
            "/home/admin/matrix/venv/bin/python", "-I", "-B",
            "/trusted/stage/deploy/guarded_priority_1_3_release.py",
            "remote-recover", "--request-b64", request_b64,
        ]
        immediate = guarded.immediate_recovery_execstart("/trusted/stage", request_b64)
        watchdog = guarded.watchdog_recovery_execstart("/trusted/stage", request_b64)
        self.assertEqual(immediate, expected)
        self.assertEqual(watchdog, expected)
        self.assertEqual(immediate, watchdog)
        self.assertNotIn("release_bundle.py", " ".join(immediate))
        self.assertFalse(any(part in {"sh", "bash", "-c"} for part in immediate))
        for bad_stage in ("trusted/stage", "/trusted/../stage", "/trusted/stage;sh", "/trusted/stage\n"):
            with self.assertRaises(ValueError):
                guarded.immediate_recovery_execstart(bad_stage, request_b64)

    def test_matching_sealed_recovery_replays_every_gateway_boundary_and_rejects_mismatch(self):
        guarded = self._module()
        attempt = "f" * 32
        request = {
            "schema": 1, "boundary": "b1", "phase": "final-deploy",
            "attempt_id": attempt, "operation_id": "e" * 32,
            "registry": "/run/matrix-priority-1-3/attempt.json", "stage": "/trusted/stage",
            "release_root": "/trusted/stage/release", "destination_root": "/home/admin/matrix",
            "bundle": "/var/lib/matrix/releases/bundle",
            "release_manifest": "/trusted/stage/release-manifest.json",
            "backup_manifest": "/var/lib/matrix/releases/bundle/backup-manifest.json",
            "release_spec": "/trusted/stage/release-spec.json",
            "provenance_digest": "d" * 64, "evidence_digest": "e" * 64,
            "pending_note_id": "7", "service": "matrix-web.service",
            "initial_service_state": "active", "health_url": "http://127.0.0.1:5000/",
            "health_status": 302, "health_timeout_seconds": 30,
            "guard_unit": f"matrix-p13-recover-{attempt}.service",
        }
        boundaries = (
            "after-seal",
            "after-extinction:unit-commit",
            "after-extinction:unit-a",
            "after-extinction:unit-b",
            "before-lock",
            "after-lock",
            "before-inner-recovery",
            "after-inner-recovery",
            "before-terminal-record",
            "after-terminal-record",
        )

        class Crash(RuntimeError): pass

        for crash_at in boundaries:
            with self.subTest(crash_at=crash_at):
                registry = guarded.AttemptRegistry(attempt)
                registry.register_operation("a", "unit-a", "/cg/a")
                registry.register_operation("b", "unit-b", "/cg/b")
                registry.state = "SEALED_COMMITTING"
                registry.commit_request_digest = "old-commit"
                registry.commit_cgroup = ("unit-commit", "/cg/commit")
                events = []

                class Controller:
                    def term(self, unit): events.append(("TERM", unit))
                    def is_extinct(self, cgroup): events.append(("extinct", cgroup)); return True
                    def kill(self, unit): events.append(("KILL", unit))

                def checkpoint(name):
                    events.append(("checkpoint", name))
                    if name == crash_at:
                        raise Crash(name)

                with self.assertRaises(Crash):
                    guarded.remote_recover_gateway(
                        registry,
                        request=request,
                        controller=Controller(),
                        acquire_release_lock=lambda: events.append(("lock",)) or 0,
                        inner_recover=lambda: events.append(("inner",)) or guarded.ControlResult(0, "inner-digest"),
                        checkpoint=checkpoint,
                    )
                self.assertIn(registry.state, {"SEALED_RECOVERING", "SEALED_RECOVERED"})

                mismatch = dict(request, evidence_digest="f" * 64)
                with self.assertRaises(ValueError):
                    guarded.remote_recover_gateway(
                        registry,
                        request=mismatch,
                        controller=Controller(),
                        acquire_release_lock=lambda: 0,
                        inner_recover=lambda: guarded.ControlResult(0, "wrong"),
                    )

                result = guarded.remote_recover_gateway(
                    registry,
                    request=request,
                    controller=Controller(),
                    acquire_release_lock=lambda: events.append(("lock-replay",)) or 0,
                    inner_recover=lambda: events.append(("inner-replay",)) or guarded.ControlResult(0, "inner-digest"),
                )
                self.assertEqual(result.returncode, 0)
                self.assertTrue(result.digest)
                self.assertEqual(registry.state, "SEALED_RECOVERED")
                self.assertEqual(registry.recovery_digest, guarded.canonical_recovery_request_digest(request))
                before_terminal_replay = list(events)
                self.assertEqual(
                    guarded.remote_recover_gateway(
                        registry,
                        request=request,
                        controller=Controller(),
                        acquire_release_lock=lambda: events.append(("unexpected-lock",)) or 0,
                        inner_recover=lambda: events.append(("unexpected-inner",)) or guarded.ControlResult(0, "wrong"),
                    ),
                    result,
                )
                self.assertEqual(events, before_terminal_replay)
                self.assertFalse(hasattr(result, "rolled_back"))


    def test_canonical_acceptance_operation_ids_are_domain_separated_by_phase_topology_and_invocation(self):
        guarded = self._module()
        attempt = "a" * 32
        candidate = guarded._canonical_acceptance_operation_id(
            attempt, "final-deploy", "deploy", 2, "candidate",
        )
        recovery = guarded._canonical_acceptance_operation_id(
            attempt, "final-deploy", "deploy", 2, "recovery-verification",
        )
        final = guarded._canonical_acceptance_operation_id(
            attempt, "final-deploy", "deploy", 2, "final-verification",
        )
        self.assertEqual(len({candidate, recovery, final}), 3)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", value) for value in (candidate, recovery, final)))
        self.assertEqual(
            candidate,
            guarded._canonical_acceptance_operation_id(
                attempt, "final-deploy", "deploy", 2, "candidate",
            ),
        )
        with self.assertRaises(ValueError):
            guarded._canonical_acceptance_operation_id(
                attempt, "initial-deploy", "deploy", 2, "candidate",
            )
        with self.assertRaises(ValueError):
            guarded._canonical_acceptance_operation_id(
                attempt, "initial-deploy", "deploy", 0, "final-verification",
            )

    def test_final_acceptance_budget_exact_boundary_and_prestart_rejection(self):
        guarded = self._module()
        now = {"value": 100.0}
        calls = []
        runner = lambda arguments, timeout: calls.append((arguments, timeout)) or 0
        arguments = object()
        deadline = now["value"] + guarded._AGGREGATE_ACCEPTANCE_BUDGET_SECONDS
        self.assertEqual(
            guarded._invoke_funded_acceptance(
                runner, arguments, deadline, lambda: now["value"],
            ),
            0,
        )
        self.assertEqual(calls[0][1], guarded._AGGREGATE_ACCEPTANCE_BUDGET_SECONDS)
        calls.clear()
        now["value"] += 0.001
        with self.assertRaisesRegex(TimeoutError, "insufficient immutable attempt budget"):
            guarded._invoke_funded_acceptance(
                runner, arguments, deadline, lambda: now["value"],
            )
        self.assertEqual(calls, [], "underfunded final verifier was started")
        now["value"] = deadline
        with self.assertRaisesRegex(TimeoutError, "insufficient immutable attempt budget"):
            guarded._invoke_funded_acceptance(
                runner, arguments, deadline, lambda: now["value"],
            )
        self.assertEqual(calls, [])

    def test_production_acceptance_runner_passes_required_exact_operation_identity(self):
        import argparse
        guarded = self._module()
        arguments = argparse.Namespace(
            stage="/stage", boundary="b1", mode="deploy",
            ssh_target="admin@example", ssh_identity_file="/pins/id",
            ssh_known_hosts_file="/pins/known", destination_root="/srv/matrix",
            accepted_sha="a" * 40, accepted_version="test",
            session_cookie_file="/pins/cookie", evidence_file="/evidence/result.json",
            attempt_id="b" * 32, acceptance_operation_id="c" * 64,
            attempt_registry="/run/matrix-priority-1-3/attempts/" + "b" * 32 + ".json",
            release_spec_digest="d" * 64,
            chromium=None,
        )
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(guarded.subprocess, "run", return_value=completed) as run:
            self.assertEqual(guarded._production_acceptance_runner(arguments, 6480), 0)
        argv = run.call_args.args[0]
        self.assertEqual(
            argv[argv.index("--acceptance-operation-id") + 1], "c" * 64,
        )
        self.assertEqual(argv.count("--acceptance-operation-id"), 1)
        self.assertEqual(argv[argv.index("--release-spec") + 1], "/stage/release-spec.json")
        self.assertEqual(argv[argv.index("--release-spec-digest") + 1], "d" * 64)
        self.assertEqual(argv.count("--release-spec"), 1)
        self.assertEqual(argv.count("--release-spec-digest"), 1)

    def test_production_acceptance_timeout_kills_and_reaps_process(self):
        import argparse
        guarded = self._module()
        root = Path(tempfile.mkdtemp(prefix="acceptance-timeout-kill-"))
        try:
            stage = root / "stage"
            (stage / "venv/bin").mkdir(parents=True)
            (stage / "deploy").mkdir()
            (stage / "venv/bin/python").symlink_to("/usr/bin/python3")
            pid_file = root / "acceptance.pid"
            script = stage / "deploy/verify_priority_1_3_acceptance.py"
            script.write_text(
                "import os,pathlib,time\n"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            arguments = argparse.Namespace(
                stage=str(stage), boundary="b1", mode="deploy",
                ssh_target="admin@example", ssh_identity_file=str(root / "identity"),
                ssh_known_hosts_file=str(root / "known-hosts"),
                destination_root=str(root / "destination"), accepted_sha="a" * 40,
                accepted_version="test", session_cookie_file=str(root / "cookie"),
                evidence_file=str(root / "evidence"), attempt_id="b" * 32,
                acceptance_operation_id="c" * 64,
                attempt_registry=str(root / "attempt.json"), release_spec_digest="d" * 64,
                chromium=None,
            )
            with self.assertRaises(subprocess.TimeoutExpired):
                guarded._production_acceptance_runner(arguments, 0.2)
            self.assertTrue(pid_file.is_file(), "acceptance process never started")
            pid = int(pid_file.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            shutil.rmtree(root)
