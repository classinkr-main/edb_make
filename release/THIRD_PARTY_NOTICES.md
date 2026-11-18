# ClassInEDBMVP third-party notices

This notice index applies to the versions recorded in
`dependency-inventory.json` and the build-generated SPDX SBOM. The packaged
`release_metadata/license-files/` directory contains the license texts shipped
by each exact installed Python distribution. Absence of a component from this
summary does not override its copied license text.

## Review-required components

- **PyMuPDF 1.27.2.3** declares dual licensing under GNU AGPL v3 or an Artifex
  commercial license. Public distribution requires a release-specific legal or
  commercial-license approval.
- **pyhwp 0.1b15** declares GNU AGPL v3-or-later. Public distribution requires
  a documented compliance decision, including corresponding-source scope.
- **Upscayl**, only when explicitly bundled, is separately gated by
  `resources/upscayl/LICENSE`, `THIRD_PARTY_NOTICES.md`, and
  `CORRESPONDING_SOURCE.txt` for the exact binary and models.

## React and ReactDOM

Copyright (c) Facebook, Inc. and its affiliates.

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Bundled UI fonts

The desktop app runs offline, so its web fonts ship inside the package at
`ui_prototype/vendor/fonts/` instead of being fetched from a CDN. All three are
licensed under the SIL Open Font License, Version 1.1, and their full license
texts ship beside the font files.

- **Pretendard Variable** — Copyright (c) 2021 Kil Hyung-jin, with Reserved Font
  Name Pretendard. License text: `ui_prototype/vendor/fonts/OFL-Pretendard.txt`.
- **JetBrains Mono Variable** — Copyright (c) 2020 The JetBrains Mono Project
  Authors. License text: `ui_prototype/vendor/fonts/OFL-JetBrainsMono.txt`.
- **Caveat Variable** — Copyright (c) 2015 Impallari Type, with Reserved Font
  Name Caveat. License text: `ui_prototype/vendor/fonts/OFL-Caveat.txt`.

## Python dependencies

The complete inventory and exact versions are machine-readable. Their license
files are copied from installed wheel metadata during every release build and
covered by a SHA-256 manifest. This includes Pillow, NumPy, OpenCV, olefile,
HWP/HWPX helpers, cryptography, lxml, Pydantic, PyInstaller and its build-time
dependencies. PyInstaller's bundled bootloader is governed by its documented
exception in the copied PyInstaller license file.

This file is a technical notice bundle, not legal advice or a declaration that
all redistribution obligations have been satisfied.
