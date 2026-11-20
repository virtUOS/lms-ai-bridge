# Reading text from scans and images — design

- **Written:** 2026-11-20 · **Status:** approach chosen by the bridge's maintainer
  ("use your own calls"), built on branch `feat/image-text`
- **Context:** the bridge reads text layers only. Course material without one is
  invisible to the assistant today.

## Why

Three kinds of course material are lost today:

- **Scanned PDFs** raise "needs OCR, which this module does not do" and are
  skipped. The ILIAS test course 86 has one.
- **Pages without a text layer inside an otherwise normal PDF**, such as a
  screenshot slide, are dropped as too short, and nobody is told. The skip only
  speaks up when *every* page is empty.
- **Image files** in course folders are skipped as "no extractor for this format".

## Goals

- Read the text of those pages and files, for Stud.IP, Moodle and ILIAS.
- **Each deployment chooses** which model reads them and whether it is an OCR
  model (image in, text out) or a general vision model (needs an instruction).
  Through the same OpenAI-compatible gateway as chat, or a separate endpoint.
- **Off unless configured.** No fallback to the chat model: that would send
  course images to whatever provider chat uses, without anyone deciding.
- **Citations keep working.** Page 12 read from a scan cites "S. 12" in the same
  document as the PDF's text pages, so the `#page=12` link opens it.
- **Cost stays bounded:** only pages that need it, results remembered per file
  content and model, a cap per indexing run.

## Non-goals for this version

- **Describing figures and charts on pages that do have text.** Most slide
  decks have a picture on most pages (logos, photos), so this would send nearly
  every page, and a description is the model's interpretation, which must be
  labelled as such. It is the next step, below.
- Stud.IP Courseware images: they are `sendfile.php` URLs that need a web
  session the API credentials do not give.
- Opencast subtitles: no API route reaches them (checked 2026-11-20).
- The MCP `index_course` tool: it indexes text only and queues no transcription
  either. Scans go through `/v1/index`, as the adapters' `index` command uses.

## Anti-goals

- A scan that silently disappears. A failed page shows in `/v1/jobs`.
- An indexing run that waits for a GPU. Reading runs in the background, as
  transcription does.

## Approaches considered

1. **The OCR service's own `/v1/ocr`** (Mistral's OCR API shape): whole PDF in,
   Markdown per page out, text pages and scanned pages separated by the service.
   Least bridge code, but a second API shape beside the OpenAI-compatible ones,
   and it cannot use a vision model.
2. **OpenAI-compatible chat with one image per page — chosen.** The in-house OCR
   adapter talks to LightOnOCR exactly this way (`ocr_client.py`: one
   `image_url`, no prompt, temperature 0.2), and every production vision model
   answers the same call. One code path; the model name and the mode decide.
   The bridge renders pages with `pdftoppm`, which `poppler-utils` in the
   container already provides.
3. **Read synchronously inside the adapters.** Simplest wiring, but about 15 s
   per page on the production models (measured 2026-11-20) would block indexing
   for minutes. Rejected.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `IMAGE_TEXT_MODEL` | unset | Model that reads images. Unset: feature off |
| `IMAGE_TEXT_MODE` | — | `ocr` (image alone) or `vision` (image with a transcription instruction). Required when a model is set; the bridge cannot tell which kind a model is |
| `IMAGE_TEXT_BASE_URL`, `IMAGE_TEXT_API_KEY` | `OPENAI_BASE_URL`, `OPENAI_API_KEY` | For an OCR service that is not behind the gateway |
| `IMAGE_TEXT_MAX_PAGES` | 200 | Pages queued per `/v1/index` request; the rest is reported, not read |
| `IMAGE_TEXT_MAX_CONCURRENT` | 2 | Parallel model calls; a shared GPU is not ours to saturate |
| `IMAGE_TEXT_LONGEST_EDGE` | 2048 | Rendered page size in pixels, as the in-house OCR adapter uses |
| `IMAGE_TEXT_CACHE` | beside the index store | JSON file of results, keyed by file hash, page, model and mode |

## How it flows

1. **Adapter.** While extracting a PDF, it notes the pages whose text is too
   short to index (the existing 15-character floor). A scanned PDF counts as all
   pages. Image files (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.tif`, `.tiff`,
   `.bmp`) are collected too. Only when `/v1/capabilities` lists `image_text`
   does it send them, as a `scans` list in `/v1/index`: base64 content, title,
   `activity_ref`, licence, course name, folder and, for a PDF, the page numbers.
2. **Bridge.** Queues one job per file on the job runner, with its own
   concurrency limit next to transcription's. For PDF pages, it skips pages with
   no image on them (`pdfimages -list`), since those are dividers or blank.
3. **Job.** Render the page (`pdftoppm -scale-to`), send it to the model, keep
   the text, index it with `replace: false` and the page's locator. Results are
   cached, so re-indexing a course does not pay again.
4. **Status.** `/v1/jobs` counts these jobs. An answer given meanwhile says that
   files are still being read.

Modes:

- `ocr`: one `image_url`, no text, temperature 0.2, `max_tokens` 4096, as the
  in-house OCR adapter sends.
- `vision`: an instruction to transcribe the page exactly as Markdown (tables as
  tables), without summarising, translating or correcting, temperature 0.
  `<think>` blocks are stripped. A page with no text returns nothing.

## Next step: figures

A second setting, such as `IMAGE_DESCRIBE_MODEL`, for pages that have a text
layer *and* images. It needs a way to skip decoration, and its output must be
stored as an automatically generated description, separate from the page text,
so an answer never cites it as the lecturer's words.

## Facts this rests on (2026-11-20)

- Five production models on `litellm.uni-osnabrueck.de` (Gemma 4 31B, Gemma 4
  E4B, Qwen3.5-122B, Qwen3.8-27B, Ornith-1.5-35B) transcribed a clean test page
  without an error, in 14–16 s. Qwen3.5-9B-Base refuses images.
- LibreChat reaches the OCR service directly at
  `whisperx.virtuos.uni-osnabrueck.de`; LiteLLM also serves it, as
  `lightonai/LightOnOCR-3-4B`. Personal Keyosk keys do not list it.
- A clean page does not separate the models. Real scans do; ILIAS course 86 has
  one.

## Verified after building (2026-11-20)

- 321 tests pass (65 new), with `ResourceWarning` as an error.
- A local bridge with `IMAGE_TEXT_MODEL=RedHatAI/gemma-4-31B-it-FP8-Dynamic`,
  `IMAGE_TEXT_MODE=vision` indexed ILIAS course 86: `ocr_test.pdf` (no text
  layer) was queued, read, and filed as `ocr_test.pdf, S. 1`. Asked for the
  reference code and amount, the bridge answered "QWERTY-99 … 142.75 EUR [1]"
  citing that page. Re-indexing finished the scan job without a model call.
- On that page all five production vision models were word-for-word right in
  12–22 s. Still not a test that separates them.
- `ocr` mode against LightOnOCR also works through the gateway:
  `lightonai/LightOnOCR-3-4B` is a LiteLLM model on `litellm.uni-osnabrueck.de`
  (pointing at the OCR service on `whisperx.virtuos`), and a key that lists it
  read the same page word for word in 11.8 s.
