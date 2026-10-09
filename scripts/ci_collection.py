#!/usr/bin/env python3
"""Fail-closed unittest collection preflight; never executes test methods.

Required behavior IDs are independent reviewed input, not a generated count.
Adding owned tests is allowed; deleting requirements needs explicit review.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import unittest


class CollectionError(ValueError):
    pass


def required_object(pairs):
    required = {}
    for key, value in pairs:
        if key in required:
            raise CollectionError(f'duplicate required key: {key}')
        required[key] = value
    return required


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def validate_inventory(inventory, required):
    if not inventory or not any(inventory.values()):
        raise CollectionError('empty collection')
    if not isinstance(required, dict) or not required:
        raise CollectionError('empty required contract')
    for module, names in required.items():
        if not isinstance(names, list) or not names or len(names) != len(set(names)):
            raise CollectionError('invalid required contract')
        missing = set(names) - set(inventory.get(module, []))
        if missing:
            raise CollectionError(f'missing required: {module}: {sorted(missing)}')
    for module, names in inventory.items():
        if len(names) != len(set(names)):
            raise CollectionError(f'duplicate discovery: {module}')
    return {'modules': len(inventory), 'tests': sum(map(len, inventory.values())),
            'inventory': inventory}


def collect(tests, required):
    tests = Path(tests).resolve()
    loader = unittest.TestLoader()
    suite = loader.discover(str(tests))
    if loader.errors:
        raise CollectionError('collection error (import/load_tests failed)')
    discovered = list(cases(suite))
    inventory = {}
    owned_ids = []
    for path in sorted(tests.glob('test_*.py')):
        module = sys.modules.get(path.stem)
        if module is None or not module.__file__ or Path(module.__file__).resolve() != path:
            raise CollectionError(f'collection error: module not loaded: {path.stem}')
        module_suite = loader.loadTestsFromModule(module)
        if loader.errors:
            raise CollectionError('collection error (module loader failed)')
        names = []
        for case in cases(module_suite):
            if type(case).__module__ != module.__name__:
                raise CollectionError(f'foreign TestCase: {path.stem}: {case.id()}')
            names.append(case.id().removeprefix(module.__name__ + '.'))
            owned_ids.append(case.id())
        inventory[path.stem] = names
    if Counter(case.id() for case in discovered) != Counter(owned_ids):
        raise CollectionError('collection error: canonical/module discovery mismatch')
    return validate_inventory(inventory, required)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument('--tests', type=Path, default=root / 'tests')
    parser.add_argument('--required', type=Path, default=root / 'ci/required-tests.json')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    # Match unittest/xmlrunner discovery's repository import boundary.
    sys.path.insert(0, str(root))
    try:
        report = collect(args.tests, json.loads(args.required.read_text(), object_pairs_hook=required_object))
        encoded = json.dumps(report, indent=2) + '\n'
        if args.output:
            args.output.write_text(encoded, encoding='utf-8')
        print(json.dumps({'modules': report['modules'], 'tests': report['tests']}))
        return 0
    except (CollectionError, OSError, ValueError, TypeError) as error:
        print(f'collection preflight failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
