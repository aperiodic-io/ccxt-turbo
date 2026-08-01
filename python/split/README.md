# Per-exchange Python packages

`import ccxt` runs `python/ccxt/__init__.py`, which imports all ~105 exchanges. An
application that talks to one venue pays for all of them — import time, resident
memory, and the review surface of every exchange module it will never call.

`split_packages.py` rewrites the generated Python tree into one distribution per
exchange, plus a shared core:

```
ccxt-core        ->  ccxt_core       base Exchange, errors, Precise, ws client,
                                     static_dependencies, protobuf. No exchanges.
ccxt-binance     ->  ccxt_binance    binance, in every flavour upstream ships it
ccxt-okx         ->  ccxt_okx        okx
...                                  one distribution per exchange id
```

```python
import ccxt_binance

exchange = ccxt_binance.binance()
print(exchange.fetch_ticker('BTC/USDT'))
```

```python
import ccxt_binance.pro as binance_pro       # websockets
import ccxt_binance.async_support as binance_async
```

The API inside each package is byte-for-byte the upstream implementation, so
`ccxt_binance.binance` behaves exactly like `ccxt.binance`.

## Running it

```console
$ python python/split/split_packages.py --out python/split-dist
generated 109 packages into python/split-dist

$ python python/split/verify_packages.py --out python/split-dist --compare-upstream
verified 108 exchange packages
```

Useful flags:

| Flag | Effect |
| --- | --- |
| `--only binance,okx` | generate a subset; parent exchanges are pulled in automatically |
| `--build` | also run `python -m build`, producing wheels and sdists |
| `--dist-prefix` / `--module-prefix` | rename `ccxt-`/`ccxt_` if PyPI names are taken |
| `--source` | split a different checkout of `python/ccxt` |

Publishing is the usual `twine` invocation over the built distributions:

```console
$ python python/split/split_packages.py --out python/split-dist --build
$ twine upload python/split-dist/dist/* -u __token__ -p "$PYPI_TOKEN"
```

`ccxt-core` must land on the index before the exchange packages, since each of
them pins `ccxt-core==<version>`.

## How the split works

Nothing is hand-maintained per exchange. Every input is read out of the source
tree, so the same command works on the next ccxt release without edits.

1. **Discovery.** The exchange list for each flavour (`sync`, `async_support`,
   `pro`, `prediction`) is read from that flavour's `__init__.py` `exchanges`
   list, and the version from `__version__`.
2. **Rewriting.** Every `ccxt.…` module path in the copied sources is rewritten
   to its new home. This is done over the *token stream*, not with a text regex,
   which is what keeps the ~6000 `ccxt.com` URLs inside docstrings and
   `describe()` blocks untouched. `ccxt.base.*`, `ccxt.static_dependencies.*`,
   `ccxt.protobuf.*` and `ccxt.async_support.base.*` go to `ccxt_core`;
   `ccxt.<id>`, `ccxt.abstract.<id>` and the flavour variants go to `ccxt_<id>`.
3. **Entry points.** Each generated `__init__.py` is the upstream one with the
   per-exchange import lines it does not own removed and `exchanges` shrunk to
   what it ships. The licence header, `__version__`, error re-exports and
   `__all__` carry over verbatim.
4. **Inheritance.** `binanceus` subclasses `binance`, so `ccxt-binanceus`
   *depends on* `ccxt-binance` rather than vendoring a second copy. Those edges
   are discovered from the imports, not from a hard-coded table. Eleven
   exchanges have a parent today.
5. **Metadata.** Each `pyproject.toml` inherits licence, authors, classifiers
   and `requires-python` from the root `pyproject.toml`; `ccxt-core` inherits
   the pinned runtime dependencies, and exchange packages depend only on
   `ccxt-core` plus any parent.

Because errors live in `ccxt_core`, `except ccxt_core.NetworkError` catches
failures raised by every installed exchange package, and `isinstance` still
works across them.

## Verifying it

`verify_packages.py` imports each package in a *fresh interpreter*, instantiates
the exchange in every flavour it ships, and asserts that the only `ccxt_*`
modules in `sys.modules` afterwards are the package itself, `ccxt_core`, and the
parents it declares. A leak surfaces as an extra module name rather than as an
import time nobody measures.

With `--compare-upstream` it additionally asserts, against the monolithic
package in the same interpreter, that `describe()`, the attribute surface and
the MRO of the split class match upstream exactly.

The pytest suite in `tests/` covers the rewriter's unit behaviour and runs the
same verification over a representative subset (`binance` for size, `binanceus`
for cross-package inheritance, `hyperliquid` for all four flavours, `kalshi` for
prediction-only, `bit2c` for the ordinary case):

```console
$ pytest python/split/tests                     # subset, a few seconds
$ CCXT_SPLIT_FULL=1 pytest python/split/tests   # every exchange, what CI runs
```

## Notes

- The generator reads only `python/ccxt`, and writes only to `--out`. It never
  modifies the monolithic package, which keeps shipping unchanged.
- `python/ccxt/test` is deliberately not packaged: the upstream test harness
  imports the whole library by design.
- Regenerate after every release. `python/split-dist/` is gitignored — the
  packages are build output, not source.
