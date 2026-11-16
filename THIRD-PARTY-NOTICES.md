# Third-Party Notices

WheelHouse itself is licensed under the Apache License 2.0 (see `LICENSE`
and `NOTICE`).

## How dependencies are delivered

The WheelHouse release archive contains WheelHouse source code only. It
does **not** bundle or redistribute any third-party Python package. The
installer (`install-wheelhouse.ps1`) and the per-service `uv sync` step
resolve dependencies from the Python Package Index (PyPI) onto your
machine at install time, pinned by the checked-in `uv.lock` files. Every
package is installed unmodified, as published by its own maintainers, and
carries its own license inside its installed metadata
(`<service>/.venv/Lib/site-packages/<package>.dist-info/`).

Because the packages live as ordinary, separable files in a uv-managed
virtual environment, you can upgrade or replace any of them independently
of WheelHouse (for example with `uv pip install <package>==<version>` in
the owning service directory).

## Copyleft-family dependencies

Most WheelHouse dependencies use permissive licenses (MIT, BSD,
Apache-2.0, ISC, PSF). The following use weak-copyleft licenses. They are
consumed unmodified from PyPI under the terms named:

| Package | License | Role |
|---------|---------|------|
| PySide6 / PySide6-Essentials / PySide6-Addons / shiboken6 | LGPL-3.0-only (of its LGPL/GPL/commercial options) | GUI toolkit (Qt for Python) |
| pynput | LGPL-3.0 | Keyboard/mouse monitoring |
| pystray | LGPL-3.0 | System tray icon |
| certifi | MPL-2.0 | Mozilla CA certificate bundle |
| tqdm | MPL-2.0 AND MIT | Progress bars (dependency of ML libraries) |

WheelHouse does not modify, statically link, or vendor any of these
packages; they are dynamically imported Python libraries installed from
PyPI on the end user's machine and replaceable by the user as described
above. The corresponding license texts ship with each installed package.

## Speech model

The default offline speech model (Parakeet TDT 0.6b, int8) is downloaded
by the installer from the sherpa-onnx release assets. Model license
details are published with the model by its upstream maintainers
(NVIDIA NeMo / k2-fsa sherpa-onnx).

## Copied source code

One third-party file is copied into the Wheelhouse development
repository. It is a release tool: the help-document build uses it, and
the Wheelhouse application does not. The public release does not
include it, because the release export removes `scripts/release/`.

| File | Source | License |
|------|--------|---------|
| `scripts/release/helpdoc/ste_check.py` | [Dodaios/ste80-humanizer](https://github.com/Dodaios/ste80-humanizer), `scripts/ste_check.py` at commit `6a69c447a3eb24c7d403aea5fc04f965ad6dafcc`, unchanged | MIT |

The license of that file:

```
MIT License

Copyright (c) 2026 Dodaios

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Inventory

The dependency license audit backing this file was last run on
2026-07-11 against every shipping service's lockfile. If you find an
error in this file, please open an issue.
