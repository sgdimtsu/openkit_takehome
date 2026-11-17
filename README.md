# openkit_takehome
openkit take home test

# Meridian prototype — component 1

Implemented: inbox loading and source identification only. Python 3.9+;
uses the Python standard library. Set up a virtual environment as below.
No API credentials are needed for this component.

## Repository layout

```text
src/meridian/   Python application code
tests/         Unit and supplied-data integration checks
data/          Supplied input pack, including PDFs and Orbit snapshot
requirements.txt Virtual-environment installation entry point
pyproject.toml Package configuration and runtime dependencies
```

The input pack is included for independent local runs. Attachment paths remain
relative to `data/`, preserving their original directory structure. Discovery
notes describe requirements; they are not live operational records. Generated
HTML mocks and macOS metadata from Downloads are not copied into the input pack.

## Run and verify

From the repository directory, create and activate a virtual environment, then
install the requirements:

```sh
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m meridian
python3 -m unittest discover -s tests -v
```

`requirements.txt` installs the local package in editable mode, making its `src/`
package importable without setting `PYTHONPATH`. Runtime dependencies are declared
in `pyproject.toml` and installed through the same command as components are added.
There are no third-party runtime dependencies yet. The build backend is setuptools,
which pip installs into an isolated build environment when needed.

The default input root is `./data`, relative to the working directory.
`--input-root /path/to/pack` selects another pack; `--inbox` specifies an inbox
JSON path relative to that root. The command prints a summary and warnings and
does not write files. Tests use repository data by default; optionally set
`MERIDIAN_TEST_INPUT_ROOT` to test another pack.

## Interface and boundaries

`meridian.inbox.load_inbox(root, inbox_path)` returns `LoadedInbox` containing:

- `emails`: original fields preserved, with IDs assigned if absent, an email
  `source_id`, and attachment `source_id` / `available` fields added.
- `sources`: an in-memory dictionary mapping source IDs to readable labels,
  relative paths, locators, original timestamps and email body text.
- `warnings`: missing attachments and unavailable reply references.

Existing email IDs and timestamp strings are preserved exactly. Missing IDs use
SHA-256 of canonical record content, excluding the ID field. Identical records
without IDs are rejected as duplicate IDs rather than silently merged. IDs remain
stable across record reordering; changing content changes a generated email ID.
Email bodies are not rewritten or interpreted, including literal escape sequences
in supplied text. Received timestamps must include a timezone.

Email source IDs identify the email body. File source IDs identify an attachment
path, shared across emails that attach that file. Attachment contents are not read
in this component. File timestamps are unknown: filesystem modification times are
not treated as document freshness. Page-level identification belongs to the later
PDF extraction component. IDs identify locations, not content versions.

The source catalogue exists only during the run. A later renderer will embed cited
evidence and readable source locations into retained HTML reports. No catalogue
persistence or report rendering is implemented yet.

Malformed records, duplicate IDs and paths escaping the input root fail explicitly.
Missing attachments remain visible with `available: false` and a warning.

No classification, customer matching, Orbit access, PDF/OCR extraction, OpenAI
calls, calculations or HTML generation is implemented. Source files and the
existing HTML mock remain untouched. Source content is data, never executable
instructions. This is snapshot processing with no live connections, reconciliation,
entry tracking, operational database, email sending, stock guarantees or delivery
promises. Later recommendations will depend on source completeness and matching.
