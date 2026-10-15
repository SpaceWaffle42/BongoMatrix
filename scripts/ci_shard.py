#!/usr/bin/env python3
"""Deterministic test sharding for the GitLab `full_test_suite` parallel job.

`plan`   prints the balanced assignment for N shards: whole TestCase classes (so
         setUpClass and intra-class order are unchanged), longest-processing-time first,
         weights from ci/test-weights.json, ties broken by name.
`run`    loads the suite with the canonical loader (`discover -s tests`, the same
         import boundary as the single-process suite) and executes only the test IDs
         assigned to this shard, writing JUnit XML and a shard manifest.
`verify` proves the union of every shard is exactly the collection inventory:
         no collected test missing, none foreign, none executed twice.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ShardError(ValueError):
    pass


def class_of(test_id):
    """`module.Class.test` -> `module.Class`; a class is never split across shards."""
    return test_id.rsplit('.', 1)[0]


def plan(test_ids, weights, total, default=1.0):
    """Longest-processing-time assignment of whole TestCase classes; ties broken by name."""
    if total < 1:
        raise ShardError('shard total must be >= 1')
    ids = sorted(set(test_ids))
    if len(ids) != len(test_ids):
        raise ShardError('duplicate test IDs')
    if not ids:
        raise ShardError('no tests collected')
    groups = {}
    for test_id in ids:
        groups.setdefault(class_of(test_id), []).append(test_id)
    cost = {name: sum(float(weights.get(t, default)) for t in members) for name, members in groups.items()}
    shards = [[] for _ in range(total)]
    load = [0.0] * total
    for name in sorted(groups, key=lambda g: (-cost[g], g)):
        target = min(range(total), key=lambda i: (load[i], i))
        shards[target].extend(groups[name])
        load[target] += cost[name]
    return [sorted(x) for x in shards], load


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def discover(tests):
    sys.path.insert(0, str(ROOT))
    loader = unittest.TestLoader()
    suite = loader.discover(str(Path(tests).resolve()))
    if loader.errors:
        raise ShardError('collection error (import/load_tests failed)')
    found = list(cases(suite))
    if not found:
        raise ShardError('no tests collected')
    return found


def load_weights(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    weights = data['tests']
    if not isinstance(weights, dict) or not all(isinstance(v, (int, float)) and v >= 0 for v in weights.values()):
        raise ShardError('invalid weights')
    return weights, float(data.get('default_seconds', 1.0))


def run(args):
    if not 1 <= args.index <= args.total:
        raise ShardError('shard index out of range')
    weights, default = load_weights(args.weights)
    found = discover(args.tests)
    shards, load = plan([c.id() for c in found], weights, args.total, default)
    owned = set(shards[args.index - 1])
    selected = unittest.TestSuite(c for c in found if c.id() in owned)
    manifest = {'index': args.index, 'total': args.total, 'tests': sorted(owned),
                'estimated_seconds': round(load[args.index - 1], 1)}
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'shard': f'{args.index}/{args.total}', 'tests': len(owned),
                      'estimated_seconds': manifest['estimated_seconds']}), flush=True)
    import xmlrunner  # provided by unittest-xml-reporting in the CI venv
    # Distinct suffix per shard: the same TestCase class may be split across shards and
    # GitLab merges artifacts of parallel jobs into one directory.
    runner = xmlrunner.XMLTestRunner(output=str(args.output), outsuffix=f'shard{args.index}', verbosity=1)
    return 0 if runner.run(selected).wasSuccessful() else 1


def junit_cases(results):
    from defusedxml import ElementTree as ET
    from defusedxml.common import DefusedXmlException

    files = sorted(Path(results).glob('*.xml'))
    if not files:
        raise ShardError('no JUnit reports')
    try:
        return [(case.get('classname') or '') + '.' + (case.get('name') or '')
                for path in files for case in ET.parse(path).getroot().iter('testcase')]
    except (DefusedXmlException, ET.ParseError) as error:
        raise ShardError(f'unsafe or invalid JUnit report: {error}') from error


def verify(args):
    collections = sorted(Path(args.collections).glob('collection-*.json'))
    inventories = [json.loads(p.read_text(encoding='utf-8'))['inventory'] for p in collections]
    if not inventories or any(i != inventories[0] for i in inventories):
        raise ShardError('shards collected different inventories')
    inventory = inventories[0]
    expected = {f'{module}.{name}' for module, names in inventory.items() for name in names}
    manifests = [json.loads(p.read_text(encoding='utf-8')) for p in sorted(Path(args.manifests).glob('shard-*.json'))]
    totals = {m['total'] for m in manifests}
    if len(totals) != 1 or sorted(m['index'] for m in manifests) != list(range(1, max(totals) + 1)):
        raise ShardError('shard manifests incomplete')
    if len(collections) != len(manifests):
        raise ShardError('collection/manifest count mismatch')
    assigned = [t for m in manifests for t in m['tests']]
    if len(assigned) != len(set(assigned)) or set(assigned) != expected:
        raise ShardError('shard assignment is not an exact partition of the collection')
    ran = junit_cases(args.results)
    # Subtests are reported as "<test> (<params>)" and belong to their collected parent.
    parents = [r if r in expected else r.split(' (', 1)[0] for r in ran]
    foreign = sorted({p for p in parents if p not in expected})
    missing = sorted(expected - set(parents))
    plain = sorted(r for r in ran if r in expected)
    duplicated = sorted({a for a, b in zip(plain, plain[1:]) if a == b})
    report = {'collected': len(expected), 'junit_cases': len(ran), 'shards': len(manifests),
              'missing': missing[:50], 'foreign': foreign[:50], 'duplicated': duplicated[:50]}
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in report.items()}))
    if missing or foreign or duplicated:
        raise ShardError(f'shard coverage mismatch: missing={len(missing)} '
                         f'foreign={len(foreign)} duplicated={len(duplicated)}')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('plan')
    p.add_argument('--total', type=int, required=True)
    r = sub.add_parser('run')
    r.add_argument('--index', type=int, required=True)
    r.add_argument('--total', type=int, required=True)
    r.add_argument('--output', type=Path, default=Path('test-results'))
    r.add_argument('--manifest', type=Path, required=True)
    for command in (p, r):
        command.add_argument('--tests', type=Path, default=ROOT / 'tests')
        command.add_argument('--weights', type=Path, default=ROOT / 'ci/test-weights.json')
    v = sub.add_parser('verify')
    v.add_argument('--collections', type=Path, required=True)
    v.add_argument('--manifests', type=Path, required=True)
    v.add_argument('--results', type=Path, required=True)
    v.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'run':
            return run(args)
        if args.command == 'plan':
            weights, default = load_weights(args.weights)
            shards, load = plan([c.id() for c in discover(args.tests)], weights, args.total, default)
            print(json.dumps([{'tests': len(s), 'estimated_seconds': round(x, 1)} for s, x in zip(shards, load)]))
            return 0
        return verify(args)
    except (ShardError, OSError, ValueError, KeyError, TypeError) as error:  # XML errors arrive as ShardError
        print(f'ci shard {args.command} failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
