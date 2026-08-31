# Local Testing Guide

## Do You Need An API Key?

No API key is required for the current local MVP flow.

The project can be tested fully in local mode with:

- image or PDF input
- local preprocessing
- local segmentation
- optional local OCR
- local `.edb` generation
- local preview prototype

## What You Do Need

### Python version

Use Python 3.11 or newer. The app code uses standard-library features such as
`enum.StrEnum`, so older virtual environments such as Python 3.9 will fail before
tests can collect.

If `.venv` already exists, check its version first:

```powershell
.\.venv\Scripts\python --version
```

```bash
.venv/bin/python --version
```

If it reports a version older than Python 3.11, recreate `.venv` with a newer
interpreter before installing test dependencies.

### Core Python packages

Install the local requirements:

```powershell
python -m pip install -r requirements-local.txt
```

### Automated test setup

For repeatable local tests, create the virtual environment with Python 3.11+ and
install the development requirements:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest -q
```

On macOS or Linux, the same test path is:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

### Optional OCR

You can run without OCR by using `--ocr noop`.

If you want OCR later:

- `pytesseract` needs the Windows Tesseract binary installed separately
- `paddleocr` is optional and heavier

## Fastest Test Path

### 1. Build one local test EDB

This command does all of the following:

- preprocesses the source
- builds `pages.json`
- crops problem images
- places them onto the board layout
- writes a test `.edb`
- leaves project `ui_prototype` untouched; legacy UI bridge files are written under the selected output folder

```powershell
python build_problem_board_edb.py out_images_sample4\record_0001_img_0.jpg --output-dir local_test_output\sample_run --ocr noop --subject korean --record-mode mixed
```

Main outputs:

- `local_test_output\sample_run\pages.json`
- `local_test_output\sample_run\board_run_summary.json`
- `local_test_output\sample_run\record_0001_img_0.edb`

### Record modes

`build_problem_board_edb.py` now supports two export modes:

- `--record-mode image-only`
  - one image record per placed problem crop
  - most stable fallback
- `--record-mode mixed`
  - text-capable blocks become text records when OCR confidence is high enough
  - figures, formulas, and low-confidence blocks remain image records

Recommended first comparison:

```powershell
python build_problem_board_edb.py out_images_sample4\record_0001_img_0.jpg --output-dir local_test_output\image_only --ocr noop --subject korean --record-mode image-only
python build_problem_board_edb.py out_images_sample4\record_0001_img_0.jpg --output-dir local_test_output\mixed --ocr noop --subject korean --record-mode mixed
```

### 2. Inspect the generated EDB structure

```powershell
python inspect_edb.py .\local_test_output\sample_run\record_0001_img_0.edb
```

For a mixed export, check whether text and image records both appear in the summary.

### 3. Open the local app

Run the local app and open the printed URL:

```powershell
.\run_local_app.ps1
```

The app serves `ui_prototype\board.html` and the current `ui_prototype\app.bundle.js` bundle.

### Clean stale local app builds

If an older packaged UI appears during launch checks, first remove ignored local
packaging outputs so only the current source run remains visible:

```bash
python scripts/clean_local_artifacts.py
python scripts/clean_local_artifacts.py --yes
```

By default this removes root-level `dist*`, `build`, and `tmp_validation_*`
artifacts, plus stale legacy UI bridge files under `ui_prototype`. The local
`.app_runtime` folder and everything inside it are always protected; generated
EDB exports require `--include-edb-exports`. The command is a dry run unless
`--yes` is present, and it refuses targets outside the Git worktree root,
Git-tracked content, or paths that are not ignored by Git.

## Structured JSON Only

If you want only the page analysis output without building an EDB:

```powershell
python build_structured_page_json.py out_images_sample4\record_0001_img_0.jpg --output-dir local_test_output\inspect_only --ocr noop --subject korean
```

This writes:

- `pages.json`
- `run_summary.json`

## Testing With Your Own File

Replace the sample source with your own image or PDF path:

```powershell
python build_problem_board_edb.py C:\path\to\your_file.pdf --output-dir local_test_output\my_run --ocr noop --subject unknown --record-mode mixed
```

## Current Recommended Defaults

- use `--ocr noop` first
- test with one representative input first
- open the prototype and the generated `.edb` together
- use Korean subject hint for long reading passages

## Notes

- Current pipeline is local-first and offline-capable
- OCR quality is still optional and not required for fallback image export
- `mixed` mode is now available, but real ClassIn verification should be used before treating it as stable
