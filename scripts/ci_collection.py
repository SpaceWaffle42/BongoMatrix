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


NON_ESSENTIAL_MARKER = '__matrix_non_essential__'


def marked_non_essential(case):
    method = getattr(case, getattr(case, '_testMethodName', ''), None)
    return hasattr(type(case), NON_ESSENTIAL_MARKER) or hasattr(method, NON_ESSENTIAL_MARKER)


def validate_non_essential(inventory, required, declared, marked):
    """Quarantine must be explicit: marked tests == reviewed list, all still required."""
    if not isinstance(declared, dict) or not isinstance(declared.get('tests'), dict):
        raise CollectionError('invalid non-essential contract')
    listed = set()
    for module, names in declared['tests'].items():
        if not isinstance(names, list) or not names or len(names) != len(set(names)):
            raise CollectionError('invalid non-essential contract')
        for name in names:
            if name not in inventory.get(module, []):
                raise CollectionError(f'non-essential not collected: {module}: {name}')
            if name not in required.get(module, []):
                raise CollectionError(f'non-essential not required: {module}: {name}')
            listed.add((module, name))
    if listed != set(marked):
        drift = sorted(f'{m}.{n}' for m, n in listed ^ set(marked))
        raise CollectionError(f'non-essential marker/list mismatch: {drift}')
    return sorted(f'{m}.{n}' for m, n in listed)


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


def collect(tests, required, non_essential=None):
    tests = Path(tests).resolve()
    loader = unittest.TestLoader()
    suite = loader.discover(str(tests))
    if loader.errors:
        raise CollectionError('collection error (import/load_tests failed)')
    discovered = list(cases(suite))
    inventory = {}
    owned_ids = []
    marked = []
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
            if marked_non_essential(case):
                marked.append((path.stem, names[-1]))
        inventory[path.stem] = names
    if Counter(case.id() for case in discovered) != Counter(owned_ids):
        raise CollectionError('collection error: canonical/module discovery mismatch')
    report = validate_inventory(inventory, required)
    report['non_essential'] = validate_non_essential(
        inventory, required, non_essential if non_essential is not None else {'tests': {}}, marked)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument('--tests', type=Path, default=root / 'tests')
    parser.add_argument('--required', type=Path, default=root / 'ci/required-tests.json')
    parser.add_argument('--non-essential', type=Path, default=root / 'ci/non-essential-tests.json')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--print-non-essential', action='store_true',
                        help='print validated non-essential unittest ids (tests.<module>.<case>)')
    args = parser.parse_args()
    # Match unittest/xmlrunner discovery's repository import boundary.
    sys.path.insert(0, str(root))
    try:
        report = collect(args.tests, json.loads(args.required.read_text(), object_pairs_hook=required_object),
                         json.loads(args.non_essential.read_text(), object_pairs_hook=required_object))
        if args.print_non_essential:
            print(' '.join('tests.' + name for name in report['non_essential']))
            return 0
        encoded = json.dumps(report, indent=2) + '\n'
        if args.output:
            args.output.write_text(encoded, encoding='utf-8')
        print(json.dumps({'modules': report['modules'], 'tests': report['tests'],
                          'non_essential': len(report['non_essential'])}))
        return 0
    except (CollectionError, OSError, ValueError, TypeError) as error:
        print(f'collection preflight failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
