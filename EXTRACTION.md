# Extraction notes

Details of how course material is turned into indexable text. For using the
bridge, the README is enough; this is for changing the extractors or writing a
new adapter.

## Which formats, and what locator each carries

| Format | Unit | Locator |
|---|---|---|
| PDF | one document per page | `S. 12` |
| `.pptx`, `.odp` | one per slide, **including speaker notes** | `Folie 4` |
| `.xlsx`, `.ods` | one per sheet | sheet name |
| `.docx`, `.odt` | whole document | none |
| Stud.IP Courseware | one per chapter | chapter title |

Word and ODF text get no locator on purpose: those formats have no page
boundaries until they render, and inventing one would produce a citation that
cannot be checked.

Office formats are ZIPs of XML, so `zipfile` and the stdlib XML parser do the
work. Legacy binary `.doc`/`.ppt`/`.xls` are OLE compound files — a different
and much larger problem, refused with a message saying so.

## PDF text: Poppler, or a weaker built-in reader

`pdftotext` when it is installed, a built-in reader when it is not
(`PDF_EXTRACTOR=auto|poppler|builtin`). The same rule the bridge uses for
retrieval: **use what the institution already has, ship a fallback anyway, and
say which one ran.**

The fallback keeps the promise that the demo runs with nothing installed, but it
is genuinely worse. Measured against three arbitrary real PDFs (2026-08-27,
`pdftotext` as ground truth):

| File | Built-in reader | `pdftotext` |
|---|---|---|
| A 46-page policy report | **refused** as unreadable | 78,787 chars |
| A journal article | 16,781 chars, page 2 blank | 18,077 chars |
| A scanned document | refused | refused — correctly, it is a scan |

**The partial success is the more dangerous case.** Returning 93% of a document
with one page silently missing reads as success; a refusal does not. That is why
an indexing run prints which extractor produced its text, and why
`PDF_EXTRACTOR=poppler` raises rather than quietly falling back — the cost of
guessing wrong is whole pages missing from a citation.

Two known limitations of the built-in reader are documented and deliberately not
fixed: its text-operator pattern truncates arrays containing brackets, and it
can capture `/Lang` dictionary values as if they were text. It is the fallback;
chasing parity with Poppler in a hand-rolled PDF parser is not the point.

Scanned PDFs raise `ScannedPdfError` rather than returning empty text, so a
course cannot silently lose a file. When the bridge can read images, the
adapters send them to it instead (next section).

## Scans and images: read in the background

Configured with `IMAGE_TEXT_MODEL` and `IMAGE_TEXT_MODE`; the design and the
approaches weighed are in `docs/superpowers/specs/2026-11-20-image-text-design.md`.

- **What is sent.** A scanned PDF (all pages), the pages of a normal PDF whose
  text is under the 15-character floor the adapters index at, and image files
  (`.png`, `.jpg`, `.gif`, `.webp`, `.tif`, `.bmp`; not `.svg`). Only when
  `/v1/capabilities` lists `image_text` — otherwise images are not even
  downloaded. Limits: 100 MB per PDF, 20 MB per image.
- **What the bridge skips.** Short pages with no image on them
  (`pdfimages -list`): dividers and blank pages, nothing to read. Pages over
  `IMAGE_TEXT_MAX_PAGES` per request, reported as `image_text_skipped`.
- **How a page is read.** Rendered with `pdftoppm -scale-to 2048`, sent as one
  `image_url` to `/chat/completions`. In `ocr` mode the image goes alone at
  temperature 0.2, as the in-house OCR adapter sends LightOnOCR; in `vision`
  mode with an instruction to transcribe literally, at temperature 0.
- **Where the text goes.** Same `activity_ref` and title as the file, locator
  `S. N`, so a citation and its `#page=N` link work as for any PDF page.
  Indexed with `replace: false`, after the course's text.
- **Cost.** Results are cached by file hash, page, model and mode
  (`image-text-cache.json` beside the index), so re-indexing a course reads
  nothing twice.

**Verified 2026-11-20** against ILIAS course 86, whose `ocr_test.pdf` has no
text layer: read by Gemma 4 31B on the production gateway, indexed as
`ocr_test.pdf, S. 1`, and a question about its reference code was answered
with that citation. A second indexing run took no model call. All five
production vision models (Gemma 4 31B and E4B, Qwen3.5-122B, Qwen3.8-27B,
Ornith-1.5-35B) read that page without an error in 12–22 s, and so did
LightOnOCR in `ocr` mode (`lightonai/LightOnOCR-3-4B` on the same gateway,
11.8 s). A clean scan does not tell them apart.

## Stud.IP: fetching files

`GET /file-refs/{id}/content`, HTTP Basic, same credentials as the rest of the
JSON-API.

**Send no `Accept` header.** With `Accept: */*` the route returns HTTP 500; with
none it returns 200 and `application/pdf`. Stud.IP negotiates content there and
does not treat `*/*` as "anything" — so do not add one to be tidy.

`meta.download-url` points at `sendfile.php` in the **web front end**, which is a
different application needing a session: it answers Basic auth with HTTP 200
carrying the login page. That is not the API's download route. The adapter guards
against receiving HTML, because a wrong base URL or a redirect lands there and
"200 with 23 KB of HTML" would otherwise be reported as a corrupt PDF.

HEAD on the content route returns an `ETag` of `"<id> <mtime> <size>"` — what an
incremental indexer should use to skip unchanged files.

## Moodle: fetching files

`fileurl` from `core_course_get_contents`, with the web-service token appended.

**Append the token with `&`, not `?`.** That URL already ends in
`?forcedownload=1`, so `?token=…` builds a second query string and Moodle answers
**HTTP 200 with a JSON error body** — "A required parameter (token) was missing"
— while a token is plainly being sent. Same shape as the Stud.IP `Accept` trap:
the status code cannot be trusted to mean what it says, so the adapter checks
whether it received a document rather than assuming.

`core_course_get_contents` does **not** return the course-level summary. The
adapter fetches it with a separate `core_course_get_courses` call; without it,
the one text field a lecturer reliably fills in produces no document at all.

## Stud.IP Courseware is often the whole course

A survey of six real courses found four with Courseware and **three of those with
no files at all** — the Mikromodule are Courseware end to end, so file extraction
retrieves nothing there. It is walked properly: `descendants` returns the tree in
one call, one document per chapter.

Two things it deliberately does not index:

- **Quiz content.** A `test` block carries only an assignment reference. Putting
  a course's answers where a student can ask an assistant for them is a
  governance decision, not a default — the block's title is recorded so the
  assistant knows a self-test exists, nothing more.
- **Images.** Embedded as `sendfile.php` URLs needing a web session the JSON-API
  credentials do not provide. The bridge can read images now, but not these
  until that is solved.
