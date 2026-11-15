# Contributing

Thanks for looking. Issues and pull requests are both welcome, especially reports from people running other scanners.

## Running the tests

The tests are plain `unittest` and need numpy, Pillow, and tesseract. On Debian or Ubuntu:

```bash
sudo apt install python3-numpy python3-pil tesseract-ocr tesseract-ocr-osd
python3 -m unittest discover -v
```

New behaviour comes with a test that fails without it. The worker, scanner, and service tests use fakes, so nothing needs a real scanner or Paperless.

## Docs

Markdown is checked with [markdownlint-cli2](https://github.com/DavidAnson/markdownlint-cli2) using the repo's `.markdownlint-cli2.yaml`:

```bash
npx markdownlint-cli2 "**/*.md"
```

Docs and messages use UK spelling.

## Dependencies

The station installs only Debian packages, with no pip and no virtualenv, so it can't break on a Python package update. Please keep it that way.

## Scans

Never attach a scan of a real document to an issue or a test unless you're sure it's fine to publish.
