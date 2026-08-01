#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Split the monolithic ``ccxt`` Python package into per-exchange distributions.

The upstream package has a single entry point: ``import ccxt`` executes
``python/ccxt/__init__.py``, which imports every exchange module. Applications
that talk to one venue therefore pay for all of them in import time, memory and
supply-chain surface.

This script mechanically rewrites the generated Python tree into:

* ``ccxt-core``  -> module ``ccxt_core``: base classes, errors, ws plumbing,
  vendored ``static_dependencies`` and ``protobuf``. No exchange code.
* ``ccxt-<id>``  -> module ``ccxt_<id>``: exactly one exchange, in whichever of
  the sync / ``async_support`` / ``pro`` / ``prediction`` flavours upstream
  ships it, plus its ``abstract`` endpoint table.

Exchanges that subclass another exchange (``binanceus`` -> ``binance``) declare a
dependency on the parent distribution instead of vendoring a second copy, so
``isinstance`` and ``except`` keep working across packages.

Nothing here is hand-maintained per exchange: the exchange list, the inheritance
edges and the package contents are all derived from the source tree, so the same
command works on every future ccxt release.

Usage::

    python python/split/split_packages.py --out python/split-dist
    python python/split/split_packages.py --out python/split-dist --only binance,okx
    python python/split/split_packages.py --out python/split-dist --build
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tokenize
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_PACKAGE = REPO_ROOT / 'python' / 'ccxt'

# Sub-namespaces of ``ccxt`` that hold one module per exchange.
FLAVOURS = ('sync', 'async_support', 'pro', 'prediction')

# Directories that belong to ccxt-core rather than to any single exchange.
CORE_TREES = ('base', 'static_dependencies', 'protobuf')

DEFAULT_DIST_PREFIX = 'ccxt-'
DEFAULT_MODULE_PREFIX = 'ccxt_'
CORE_SUFFIX = 'core'


# ---------------------------------------------------------------------------
# source-tree introspection
# ---------------------------------------------------------------------------


def _assignment_node(source: str, name: str) -> Optional[ast.Assign]:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return node
    return None


def read_string_list(path: Path, name: str) -> List[str]:
    """Return the value of a module-level ``name = ['a', 'b']`` assignment."""
    node = _assignment_node(path.read_text(encoding='utf-8'), name)
    if node is None:
        return []
    return [element.value for element in node.value.elts]  # type: ignore[attr-defined]


@dataclass
class Layout:
    """Which upstream files exist for every exchange id."""

    version: str
    # flavour -> ordered exchange ids, as listed in that flavour's __init__.py
    ids: Dict[str, List[str]]
    # every id that has at least one module
    all_ids: List[str] = field(default_factory=list)

    def flavours_of(self, exchange_id: str) -> List[str]:
        return [flavour for flavour in FLAVOURS if exchange_id in self.ids[flavour]]


def discover_layout(source: Path = SOURCE_PACKAGE) -> Layout:
    version = read_version(source / '__init__.py')
    ids = {
        'sync': read_string_list(source / '__init__.py', 'exchanges'),
        'async_support': read_string_list(source / 'async_support' / '__init__.py', 'exchanges'),
        'pro': read_string_list(source / 'pro' / '__init__.py', 'exchanges'),
        'prediction': read_string_list(source / 'prediction' / '__init__.py', 'exchanges'),
    }
    for flavour, listed in ids.items():
        subdir = source if flavour == 'sync' else source / flavour
        missing = [i for i in listed if not (subdir / (i + '.py')).is_file()]
        if missing:
            raise SystemExit('%s/__init__.py lists modules that do not exist: %s' % (flavour, missing))
    ordered: List[str] = []
    for flavour in FLAVOURS:
        for exchange_id in ids[flavour]:
            if exchange_id not in ordered:
                ordered.append(exchange_id)
    return Layout(version=version, ids=ids, all_ids=sorted(ordered))


def read_version(init_path: Path) -> str:
    node = _assignment_node(init_path.read_text(encoding='utf-8'), '__version__')
    if node is None:
        raise SystemExit('no __version__ in %s' % init_path)
    return node.value.value  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# import rewriting
# ---------------------------------------------------------------------------


class Resolver:
    """Maps a dotted ``ccxt.…`` path onto its post-split module path.

    ``resolve`` receives the attribute chain that follows the ``ccxt`` name and
    returns ``(replacement, consumed)`` where ``consumed`` is how many of those
    attributes the replacement already accounts for, or ``None`` when the chain
    is not a module path we own (a local variable called ``ccxt``, say).
    """

    def __init__(self, layout: Layout, module_prefix: str, self_module: str) -> None:
        self.layout = layout
        self.module_prefix = module_prefix
        self.self_module = self_module
        self.core = module_prefix + CORE_SUFFIX
        self.referenced: Set[str] = set()

    def _module_for(self, exchange_id: str) -> str:
        return self.module_prefix + exchange_id

    def _record(self, module: str) -> None:
        self.referenced.add(module.split('.')[0])

    def resolve(self, parts: Sequence[str]) -> Optional[Tuple[str, int]]:
        known = self.layout.all_ids

        if not parts:
            self._record(self.self_module)
            return self.self_module, 0

        head = parts[0]

        if head in CORE_TREES:
            self._record(self.core)
            return '%s.%s' % (self.core, head), 1

        if head == 'async_support' and len(parts) > 1 and parts[1] == 'base':
            self._record(self.core)
            return '%s.async_support.base' % self.core, 2

        if head == 'abstract':
            if len(parts) > 2 and parts[1] == 'prediction' and parts[2] in known:
                module = self._module_for(parts[2])
                self._record(module)
                return '%s.abstract.prediction.%s' % (module, parts[2]), 3
            if len(parts) > 1 and parts[1] in known:
                module = self._module_for(parts[1])
                self._record(module)
                return '%s.abstract.%s' % (module, parts[1]), 2
            self._record(self.self_module)
            return '%s.abstract' % self.self_module, 1

        if head in ('async_support', 'pro', 'prediction'):
            if len(parts) > 1 and parts[1] in known:
                module = self._module_for(parts[1])
                self._record(module)
                return '%s.%s.%s' % (module, head, parts[1]), 2
            self._record(self.self_module)
            return '%s.%s' % (self.self_module, head), 1

        if head in known:
            module = self._module_for(head)
            self._record(module)
            return '%s.%s' % (module, head), 1

        # `ccxt.Exchange`, `ccxt.NetworkError`, … - attributes of the top-level
        # package, which each generated package re-exports.
        self._record(self.self_module)
        return self.self_module, 0


def _rewrite_tokens(source: str, resolve: Callable[[Sequence[str]], Optional[Tuple[str, int]]]) -> str:
    """Replace ``ccxt.…`` module paths in real code, never inside strings.

    Tokenising rather than running a regex over the text is what keeps the 5981
    ``ccxt.com`` URLs in docstrings and describe() blocks untouched.
    """
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    lines = source.splitlines(keepends=True)
    edits: List[Tuple[Tuple[int, int], Tuple[int, int], str]] = []

    for index, token in enumerate(tokens):
        if token.type != tokenize.NAME or token.string != 'ccxt':
            continue
        previous = tokens[index - 1] if index else None
        if previous is not None and previous.type == tokenize.OP and previous.string == '.':
            continue  # `something.ccxt`, not our package

        parts: List[str] = []
        ends: List[Tuple[int, int]] = [token.end]
        cursor = index
        while (
            cursor + 2 < len(tokens)
            and tokens[cursor + 1].type == tokenize.OP
            and tokens[cursor + 1].string == '.'
            and tokens[cursor + 2].type == tokenize.NAME
        ):
            parts.append(tokens[cursor + 2].string)
            ends.append(tokens[cursor + 2].end)
            cursor += 2

        tail = tokens[cursor + 1] if cursor + 1 < len(tokens) else None
        if tail is not None and tail.type == tokenize.OP and tail.string == '.':
            # the chain continues past a line break, so `parts` is incomplete and
            # resolving it would silently point at the wrong module
            raise ValueError('module path wraps onto the next line at line %d' % token.start[0])

        resolved = resolve(parts)
        if resolved is None:
            continue
        replacement, consumed = resolved
        end = ends[consumed]
        if end[0] != token.start[0]:
            raise ValueError('module path split across lines at line %d' % token.start[0])
        edits.append((token.start, end, replacement))

    for (start_row, start_col), (_, end_col), replacement in reversed(edits):
        line = lines[start_row - 1]
        lines[start_row - 1] = line[:start_col] + replacement + line[end_col:]
    return ''.join(lines)


def rewrite_source(source: str, resolver: Resolver) -> str:
    return _rewrite_tokens(source, resolver.resolve)


# ---------------------------------------------------------------------------
# __init__.py surgery
# ---------------------------------------------------------------------------


EXCHANGE_IMPORT = re.compile(
    r'^from ccxt\.(?:(?P<sub>async_support|pro|prediction)\.)?(?P<id>[a-z0-9_]+) import (?P=id)\b.*$'
)


def filter_init(source: str, keep: Sequence[str], known_ids: Iterable[str]) -> str:
    """Drop the per-exchange imports we do not ship and shrink ``exchanges``.

    Everything else - the licence header, ``__version__``, the base/error
    re-exports, ``__all__`` - is carried over verbatim, so a generated
    ``__init__`` tracks upstream automatically.
    """
    known = set(known_ids)
    keep_set = set(keep)

    kept_lines = []
    for line in source.splitlines(keepends=True):
        match = EXCHANGE_IMPORT.match(line.rstrip('\n'))
        if match and match.group('id') in known and match.group('id') not in keep_set:
            continue
        kept_lines.append(line)
    filtered = ''.join(kept_lines)

    node = _assignment_node(filtered, 'exchanges')
    if node is not None:
        lines = filtered.splitlines(keepends=True)
        body = 'exchanges = [\n' + ''.join("    '%s',\n" % i for i in keep) + ']\n'
        lines[node.lineno - 1:node.end_lineno] = [body]
        filtered = ''.join(lines)
    return filtered


# ---------------------------------------------------------------------------
# package emission
# ---------------------------------------------------------------------------


@dataclass
class Package:
    dist_name: str
    module_name: str
    exchange_id: Optional[str]
    flavours: List[str]
    requires: Set[str] = field(default_factory=set)

    @property
    def is_core(self) -> bool:
        return self.exchange_id is None


class Splitter:
    def __init__(
        self,
        out_dir: Path,
        layout: Layout,
        source: Path = SOURCE_PACKAGE,
        dist_prefix: str = DEFAULT_DIST_PREFIX,
        module_prefix: str = DEFAULT_MODULE_PREFIX,
    ) -> None:
        self.out_dir = out_dir
        self.layout = layout
        self.source = source
        self.dist_prefix = dist_prefix
        self.module_prefix = module_prefix
        self.core_module = module_prefix + CORE_SUFFIX
        self.core_dist = dist_prefix + CORE_SUFFIX
        self.metadata = tomllib.loads((REPO_ROOT / 'pyproject.toml').read_text(encoding='utf-8'))

    # -- helpers ----------------------------------------------------------

    def _copy_rewritten(self, relative: Path, destination: Path, resolver: Resolver) -> None:
        text = (self.source / relative).read_text(encoding='utf-8')
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative.suffix == '.py':
            try:
                text = rewrite_source(text, resolver)
            except (tokenize.TokenError, ValueError) as error:
                raise SystemExit('cannot rewrite %s: %s' % (relative, error))
        destination.write_text(text, encoding='utf-8')

    def _copy_tree(self, relative: Path, destination: Path, resolver: Resolver) -> None:
        for path in sorted((self.source / relative).rglob('*')):
            if path.is_dir() or '__pycache__' in path.parts:
                continue
            child = path.relative_to(self.source)
            self._copy_rewritten(child, destination / child.relative_to(relative), resolver)

    # -- core -------------------------------------------------------------

    def emit_core(self) -> Package:
        package = Package(self.core_dist, self.core_module, None, [])
        root = self.out_dir / self.core_dist / self.core_module
        resolver = Resolver(self.layout, self.module_prefix, self.core_module)

        for tree in CORE_TREES:
            self._copy_tree(Path(tree), root / tree, resolver)
        self._copy_tree(Path('async_support') / 'base', root / 'async_support' / 'base', resolver)

        for source_name, target in (
            ('__init__.py', root / '__init__.py'),
            (Path('async_support') / '__init__.py', root / 'async_support' / '__init__.py'),
        ):
            text = (self.source / source_name).read_text(encoding='utf-8')
            text = filter_init(text, [], self.layout.all_ids)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rewrite_source(text, resolver), encoding='utf-8')

        self._write_project(package, root)
        return package

    # -- exchanges --------------------------------------------------------

    def emit_exchange(self, exchange_id: str) -> Package:
        flavours = self.layout.flavours_of(exchange_id)
        module_name = self.module_prefix + exchange_id
        package = Package(self.dist_prefix + exchange_id, module_name, exchange_id, flavours)
        root = self.out_dir / package.dist_name / module_name
        resolver = Resolver(self.layout, self.module_prefix, module_name)

        for flavour in flavours:
            relative = Path(exchange_id + '.py') if flavour == 'sync' else Path(flavour) / (exchange_id + '.py')
            target = root / relative
            self._copy_rewritten(relative, target, resolver)

            init_source = '__init__.py' if flavour == 'sync' else str(Path(flavour) / '__init__.py')
            text = (self.source / init_source).read_text(encoding='utf-8')
            text = filter_init(text, [exchange_id], self.layout.all_ids)
            init_target = root / '__init__.py' if flavour == 'sync' else root / flavour / '__init__.py'
            init_target.parent.mkdir(parents=True, exist_ok=True)
            init_target.write_text(rewrite_source(text, resolver), encoding='utf-8')

        # an exchange with no sync flavour (prediction-only venues) still needs a
        # top-level __init__ so `import ccxt_kalshi.prediction` resolves
        if 'sync' not in flavours:
            text = filter_init((self.source / '__init__.py').read_text(encoding='utf-8'), [], self.layout.all_ids)
            (root / '__init__.py').write_text(rewrite_source(text, resolver), encoding='utf-8')

        for abstract in (Path('abstract') / (exchange_id + '.py'), Path('abstract') / 'prediction' / (exchange_id + '.py')):
            if (self.source / abstract).is_file():
                self._copy_rewritten(abstract, root / abstract, resolver)
                for marker in (root / 'abstract' / '__init__.py', root / abstract.parent / '__init__.py'):
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.touch()

        package.requires = {name for name in resolver.referenced if name != module_name}
        self._write_project(package, root)
        return package

    # -- packaging metadata ----------------------------------------------

    def dist_for_module(self, module_name: str) -> str:
        return self.dist_prefix + module_name[len(self.module_prefix):]

    def _write_project(self, package: Package, root: Path) -> None:
        project = self.metadata['project']
        version = self.layout.version
        if package.is_core:
            description = 'ccxt base classes, errors and vendored dependencies - shared by every ccxt-<exchange> package'
            requirements = list(project['dependencies'])
        else:
            description = 'ccxt API for the %s exchange, without the other %d exchanges' % (
                package.exchange_id,
                len(self.layout.all_ids) - 1,
            )
            requirements = ['%s==%s' % (self.dist_for_module(m), version) for m in sorted(package.requires)]

        lines = [
            '# Generated by python/split/split_packages.py - do not edit by hand.',
            '[build-system]',
            'requires = ["setuptools>=77"]',
            'build-backend = "setuptools.build_meta"',
            '',
            '[project]',
            'name = %s' % json.dumps(package.dist_name),
            'version = %s' % json.dumps(version),
            'description = %s' % json.dumps(description),
            'readme = "README.md"',
            'license = %s' % json.dumps(project['license']),
            'requires-python = %s' % json.dumps(project['requires-python']),
            'authors = [',
            '    { name = %s, email = %s },' % (
                json.dumps(project['authors'][0]['name']),
                json.dumps(project['authors'][0]['email']),
            ),
            ']',
            'keywords = [',
        ]
        keywords = list(project['keywords'])
        if package.exchange_id:
            keywords = [package.exchange_id] + keywords
        lines += ['    %s,' % json.dumps(keyword) for keyword in keywords]
        lines += [']', 'classifiers = [']
        lines += ['    %s,' % json.dumps(classifier) for classifier in project['classifiers']]
        lines += [']', 'dependencies = [']
        lines += ['    %s,' % json.dumps(requirement) for requirement in requirements]
        lines += [
            ']',
            '',
            '[project.urls]',
        ]
        for key, value in project['urls'].items():
            lines.append('%s = %s' % (key, json.dumps(value)))
        lines += [
            '',
            '[tool.setuptools.packages.find]',
            'where = ["."]',
            'include = [%s, %s]' % (json.dumps(package.module_name), json.dumps(package.module_name + '.*')),
            '',
            '[tool.setuptools.package-data]',
            '# static_dependencies vendors grammars, wordlists and Cython sources alongside the modules',
            '"*" = ["**/*"]',
            '',
        ]
        (root.parent / 'pyproject.toml').write_text('\n'.join(lines), encoding='utf-8')
        (root.parent / 'README.md').write_text(self._readme(package), encoding='utf-8')

    def _readme(self, package: Package) -> str:
        if package.is_core:
            return (
                '# %s\n\n'
                'Shared runtime for the per-exchange [ccxt](https://github.com/ccxt/ccxt) packages: `Exchange`,\n'
                '`Precise`, the error hierarchy, the WebSocket client and the vendored `static_dependencies`.\n\n'
                'It contains no exchange implementations. Install `%s<exchange>` instead - it pulls this in.\n\n'
                'Generated from ccxt %s by `python/split/split_packages.py`.\n'
                % (package.dist_name, self.dist_prefix, self.layout.version)
            )
        entry_points = []
        if 'sync' in package.flavours:
            entry_points.append(
                'import %s\n\nexchange = %s.%s()\nprint(exchange.fetch_ticker("BTC/USDT"))'
                % (package.module_name, package.module_name, package.exchange_id)
            )
        if 'async_support' in package.flavours:
            entry_points.append(
                'import %s.async_support as %s_async\n\nexchange = %s_async.%s()'
                % (package.module_name, package.exchange_id, package.exchange_id, package.exchange_id)
            )
        if 'pro' in package.flavours:
            entry_points.append(
                'import %s.pro as %s_pro\n\nexchange = %s_pro.%s()'
                % (package.module_name, package.exchange_id, package.exchange_id, package.exchange_id)
            )
        if 'prediction' in package.flavours:
            entry_points.append(
                'import %s.prediction as %s_prediction\n\nexchange = %s_prediction.%s()'
                % (package.module_name, package.exchange_id, package.exchange_id, package.exchange_id)
            )
        blocks = '\n\n'.join('```python\n%s\n```' % block for block in entry_points)
        siblings = sorted(self.dist_for_module(m) for m in package.requires if m != self.core_module)
        extra = ''
        if siblings:
            extra = '\nThis exchange subclasses another one, so it also installs %s.\n' % ', '.join(
                '`%s`' % s for s in siblings
            )
        return (
            '# %s\n\n'
            '[ccxt](https://github.com/ccxt/ccxt) for **%s** only. Importing it loads one exchange instead\n'
            'of all %d, which keeps import time, memory and dependency surface proportional to what you use.\n\n'
            '```console\n$ pip install %s\n```\n\n'
            '%s\n%s\n'
            'API and behaviour are identical to `ccxt.%s` upstream.\n\n'
            'Generated from ccxt %s by `python/split/split_packages.py`.\n'
            % (
                package.dist_name,
                package.exchange_id,
                len(self.layout.all_ids),
                package.dist_name,
                blocks,
                extra,
                package.exchange_id,
                self.layout.version,
            )
        )


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------


def build_distributions(out_dir: Path, packages: Sequence[Package], dist_dir: Path, jobs: int,
                        isolation: bool = True) -> None:
    dist_dir.mkdir(parents=True, exist_ok=True)
    flags = [] if isolation else ['--no-isolation']

    def build(package: Package) -> Tuple[str, int, str]:
        result = subprocess.run(
            [sys.executable, '-m', 'build', '--outdir', str(dist_dir), *flags, str(out_dir / package.dist_name)],
            capture_output=True,
            text=True,
        )
        return package.dist_name, result.returncode, result.stderr or result.stdout

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        failures = [(name, log) for name, code, log in pool.map(build, packages) if code != 0]
    for name, log in failures:
        print('build failed: %s\n%s' % (name, log[-2000:]), file=sys.stderr)
    if failures:
        raise SystemExit('%d distribution(s) failed to build' % len(failures))
    print('built %d distributions into %s' % (len(packages), dist_dir))


def split(
    out_dir: Path,
    only: Optional[Sequence[str]] = None,
    dist_prefix: str = DEFAULT_DIST_PREFIX,
    module_prefix: str = DEFAULT_MODULE_PREFIX,
    clean: bool = True,
    source: Path = SOURCE_PACKAGE,
) -> List[Package]:
    """Generate ``ccxt-core`` plus one distribution per exchange. Returns them."""
    layout = discover_layout(source)
    if clean and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    splitter = Splitter(out_dir, layout, source, dist_prefix, module_prefix)
    packages = [splitter.emit_core()]

    selected = list(layout.all_ids)
    if only:
        unknown = sorted(set(only) - set(layout.all_ids))
        if unknown:
            raise SystemExit('unknown exchange id(s): %s' % ', '.join(unknown))
        selected = [i for i in layout.all_ids if i in set(only)]
        selected = _with_parents(splitter, selected)

    packages += [splitter.emit_exchange(exchange_id) for exchange_id in selected]

    manifest = {
        'ccxt_version': layout.version,
        'dist_prefix': dist_prefix,
        'module_prefix': module_prefix,
        'packages': [
            {
                'dist': package.dist_name,
                'module': package.module_name,
                'exchange': package.exchange_id,
                'flavours': package.flavours,
                'requires': sorted(splitter.dist_for_module(m) for m in package.requires),
            }
            for package in packages
        ],
    }
    (out_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    return packages


def _with_parents(splitter: Splitter, selected: Sequence[str]) -> List[str]:
    """Add the exchanges the selection inherits from, transitively."""
    resolved: List[str] = []
    pending = list(selected)
    seen: Set[str] = set()
    while pending:
        exchange_id = pending.pop()
        if exchange_id in seen:
            continue
        seen.add(exchange_id)
        resolved.append(exchange_id)
        probe = Resolver(splitter.layout, splitter.module_prefix, splitter.module_prefix + exchange_id)
        for flavour in splitter.layout.flavours_of(exchange_id):
            relative = exchange_id + '.py' if flavour == 'sync' else '%s/%s.py' % (flavour, exchange_id)
            rewrite_source((splitter.source / relative).read_text(encoding='utf-8'), probe)
        for module in probe.referenced:
            parent = module[len(splitter.module_prefix):]
            if parent in splitter.layout.all_ids and parent not in seen:
                pending.append(parent)
    return [i for i in splitter.layout.all_ids if i in seen]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', type=Path, default=REPO_ROOT / 'python' / 'split-dist',
                        help='directory to write the generated source distributions into')
    parser.add_argument('--source', type=Path, default=SOURCE_PACKAGE, help='the monolithic ccxt package to split')
    parser.add_argument('--only', help='comma-separated exchange ids; parents are pulled in automatically')
    parser.add_argument('--dist-prefix', default=DEFAULT_DIST_PREFIX, help='PyPI name prefix (default: ccxt-)')
    parser.add_argument('--module-prefix', default=DEFAULT_MODULE_PREFIX, help='import name prefix (default: ccxt_)')
    parser.add_argument('--no-clean', action='store_true', help='keep whatever is already in --out')
    parser.add_argument('--build', action='store_true', help='also run `python -m build` for every package')
    parser.add_argument('--dist-dir', type=Path, default=None, help='where --build puts wheels (default: <out>/dist)')
    parser.add_argument('--jobs', type=int, default=min(8, (os.cpu_count() or 2)), help='parallel builds')
    parser.add_argument('--no-isolation', action='store_true',
                        help='reuse the current environment for --build instead of creating one per package')
    args = parser.parse_args(argv)

    only = [i.strip() for i in args.only.split(',') if i.strip()] if args.only else None
    packages = split(
        out_dir=args.out,
        only=only,
        dist_prefix=args.dist_prefix,
        module_prefix=args.module_prefix,
        clean=not args.no_clean,
        source=args.source,
    )
    print('generated %d packages into %s' % (len(packages), args.out))
    if args.build:
        build_distributions(args.out, packages, args.dist_dir or args.out / 'dist', args.jobs,
                            isolation=not args.no_isolation)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
