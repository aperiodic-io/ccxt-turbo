#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify a tree produced by ``split_packages.py``.

For every generated package this imports each flavour it ships in a *fresh
interpreter*, instantiates the exchange, and asserts that the only ``ccxt_*``
modules that ended up in ``sys.modules`` are the package itself, ``ccxt_core``
and the parent exchanges it declares. That last assertion is the whole point of
the split: a leak shows up as an extra module name, not as a slow import nobody
notices.

Usage::

    python python/split/verify_packages.py --out python/split-dist
    python python/split/verify_packages.py --out python/split-dist --only binance,okx
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

REPO_ROOT = Path(__file__).resolve().parents[2]

# Runs inside the child interpreter: import, instantiate, report what got loaded.
PROBE = r'''
import json, sys

module_name = sys.argv[1]
exchange_id = sys.argv[2]
flavours = sys.argv[3].split(",") if sys.argv[3] else []

result = {"module": module_name, "checks": []}
for flavour in flavours:
    dotted = module_name if flavour == "sync" else module_name + "." + flavour
    namespace = __import__(dotted, fromlist=["*"])
    cls = getattr(namespace, exchange_id)
    instance = cls()
    described = instance.describe()
    assert described["id"] == exchange_id, (dotted, described["id"])
    assert instance.id == exchange_id, (dotted, instance.id)
    result["checks"].append(dotted)

result["loaded"] = sorted(m for m in sys.modules if m == "ccxt" or m.startswith("ccxt_") or m.startswith("ccxt."))
print(json.dumps(result))
'''

# Runs inside the child interpreter with both the split tree and the monolithic
# package importable: proves the split class is the upstream class.
EQUIVALENCE_PROBE = r'''
import json, re, sys

module_name = sys.argv[1]
exchange_id = sys.argv[2]
flavours = sys.argv[3].split(",") if sys.argv[3] else []

def normalise(value):
    # the only legitimate difference is where a class object lives now
    return re.sub(r"\bccxt_[a-z0-9_]+\.", "ccxt.", json.dumps(value, sort_keys=True, default=repr))

differences = []
for flavour in flavours:
    split_dotted = module_name if flavour == "sync" else module_name + "." + flavour
    upstream_dotted = "ccxt" if flavour == "sync" else "ccxt." + flavour
    split_cls = getattr(__import__(split_dotted, fromlist=["*"]), exchange_id)
    upstream_cls = getattr(__import__(upstream_dotted, fromlist=["*"]), exchange_id)
    if normalise(split_cls().describe()) != normalise(upstream_cls().describe()):
        differences.append(split_dotted + ": describe() differs from " + upstream_dotted)
    missing = sorted(set(dir(upstream_cls)) - set(dir(split_cls)))
    if missing:
        differences.append(split_dotted + ": missing attributes " + repr(missing[:10]))
    upstream_bases = [c.__name__ for c in upstream_cls.__mro__]
    split_bases = [c.__name__ for c in split_cls.__mro__]
    if upstream_bases != split_bases:
        differences.append(split_dotted + ": mro " + repr(split_bases) + " != " + repr(upstream_bases))

print(json.dumps({"differences": differences}))
'''


def load_manifest(out_dir: Path) -> dict:
    manifest_path = out_dir / 'manifest.json'
    if not manifest_path.is_file():
        raise SystemExit('no manifest.json in %s - run split_packages.py first' % out_dir)
    return json.loads(manifest_path.read_text(encoding='utf-8'))


def module_closure(manifest: dict, dist_name: str) -> Set[str]:
    """The modules a package is allowed to load: itself plus its dependencies."""
    by_dist = {package['dist']: package for package in manifest['packages']}
    pending = [dist_name]
    seen: Set[str] = set()
    while pending:
        current = pending.pop()
        if current in seen or current not in by_dist:
            continue
        seen.add(current)
        pending.extend(by_dist[current]['requires'])
    return {by_dist[d]['module'] for d in seen if d in by_dist}


def verify(out_dir: Path, only: Optional[Sequence[str]] = None, jobs: int = 8,
           python: str = sys.executable, compare_upstream: bool = False) -> List[str]:
    """Returns a list of failure descriptions; empty means everything passed."""
    manifest = load_manifest(out_dir)
    packages = [p for p in manifest['packages'] if p['exchange']]
    if only:
        wanted = set(only)
        packages = [p for p in packages if p['exchange'] in wanted]

    environment = dict(os.environ)
    roots = [str(out_dir / p['dist']) for p in manifest['packages']]
    environment['PYTHONPATH'] = os.pathsep.join(roots)
    environment['PYTHONDONTWRITEBYTECODE'] = '1'

    upstream_environment = dict(environment)
    upstream_environment['PYTHONPATH'] = os.pathsep.join(roots + [str(REPO_ROOT / 'python')])

    def run(probe: str, package: dict, env: Dict[str, str]) -> subprocess.CompletedProcess:
        command = [python, '-c', probe, package['module'], package['exchange'], ','.join(package['flavours'])]
        return subprocess.run(command, capture_output=True, text=True, env=env)

    def check(package: dict) -> Optional[str]:
        completed = run(PROBE, package, environment)
        if completed.returncode != 0:
            return '%s: import failed\n%s' % (package['dist'], completed.stderr.strip()[-1500:])
        report = json.loads(completed.stdout.strip().splitlines()[-1])
        allowed = module_closure(manifest, package['dist'])
        leaked = sorted({m.split('.')[0] for m in report['loaded']} - allowed)
        if leaked:
            return '%s: leaked unrelated packages %s' % (package['dist'], leaked)
        if len(report['checks']) != len(package['flavours']):
            return '%s: only checked %s of %s' % (package['dist'], report['checks'], package['flavours'])
        if not compare_upstream:
            return None
        completed = run(EQUIVALENCE_PROBE, package, upstream_environment)
        if completed.returncode != 0:
            return '%s: upstream comparison failed\n%s' % (package['dist'], completed.stderr.strip()[-1500:])
        differences = json.loads(completed.stdout.strip().splitlines()[-1])['differences']
        if differences:
            return '%s: %s' % (package['dist'], '; '.join(differences))
        return None

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        return [failure for failure in pool.map(check, packages) if failure]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', type=Path, default=REPO_ROOT / 'python' / 'split-dist')
    parser.add_argument('--only', help='comma-separated exchange ids to verify')
    parser.add_argument('--jobs', type=int, default=min(8, (os.cpu_count() or 2)))
    parser.add_argument('--compare-upstream', action='store_true',
                        help='also assert describe(), the attribute surface and the MRO match python/ccxt')
    args = parser.parse_args(argv)

    only = [i.strip() for i in args.only.split(',') if i.strip()] if args.only else None
    failures = verify(args.out, only, args.jobs, compare_upstream=args.compare_upstream)
    for failure in failures:
        print(failure, file=sys.stderr)
    if failures:
        print('\n%d package(s) failed verification' % len(failures), file=sys.stderr)
        return 1
    manifest = load_manifest(args.out)
    print('verified %d exchange packages' % len([p for p in manifest['packages'] if p['exchange']]))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
