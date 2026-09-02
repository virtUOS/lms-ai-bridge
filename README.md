# LMS AI Bridge

**Gets course material out of a learning management system and prepares it for
an AI system to index.** Stud.IP, Moodle and ILIAS.

Built at [virtUOS](https://www.virtuos.uni-osnabrueck.de/), Osnabrück
University, as part of the **LernKI** project (Stiftung Innovation in der
Hochschullehre). Status: **working prototype**, not production software.

## What problem this solves

An AI assistant that can answer questions about a course needs the course's
material. Getting it out is the unglamorous part, and it is different on every
platform: Stud.IP has a JSON-API, Moodle has web services, ILIAS has a REST
plugin. Meanwhile the material itself is PDFs, slide decks, spreadsheets,
structured Courseware pages and audio recordings.

This component does that job and hands over text with the metadata that only
extraction can know:

- **Where a passage came from** — "S. 12", "Folie 25", "0:00", a chapter title.
  A citation nobody can check is barely a citation.
- **Under what licence**, and whether the account was even allowed to download
  it. Copying course material into a vector store is where a data-protection
  review lands, and that metadata exists at extraction time and nowhere else.

**It deliberately does not** try to be a RAG engine. Retrieval, chunking and
embedding are included so the adapters could be tested end to end, and as a
fallback for an institution that has no store of its own — not as a
contribution. Two funded German teams (ByCS/ISB's `local_ai_content` and
OSKI.nrw's LMS-RAG) are building engines for Moodle, and the LernKI project has
its own; the point here is to feed whichever one an institution runs.

```
  Moodle        ILIAS        Stud.IP          ← thin adapters, one per platform
     └────────────┼────────────┘
                  ▼
          LMS AI Bridge                       ← extraction + preparation
                  │
     ┌────────────┼─────────────┐
     ▼            ▼             ▼
   chat      retrieval    transcription       ← pluggable providers:
                                                 whatever the institution runs
```

## Everything is a provider slot

No AI infrastructure is bundled. Chat, embeddings and transcription are
interfaces configured from the environment, so an institution uses what it
already operates:

| Capability | Contract | Example in use here |
|---|---|---|
| Chat | any OpenAI-compatible endpoint | a LiteLLM gateway |
| Retrieval | `RetrievalProvider` | `bge-m3` embeddings, or a lexical fallback |
| Transcription | `TranscriptionProvider` | WhisperX/MurmurAI, or plain Whisper |

Where nothing is configured, the fallbacks keep working — worse, and honestly
labelled. `GET /v1/capabilities` reports which is live, so an LMS adapter can
offer only what a deployment can actually do.

## Try it

    ./demo.sh          # offline: fixtures, no credentials, no network
    ./demo-ui.sh       # against a live Stud.IP course (prompts for a password)
    ./demo-moodle.sh   # against a live Moodle course (needs MOODLE_URL + MOODLE_TOKEN)

Python 3.11+, **standard library only** — nothing to install.

Poppler (`pdftotext`) is optional and strongly recommended: PDF text extraction
uses it when present and falls back to a markedly weaker built-in reader when
not. [EXTRACTION.md](EXTRACTION.md) has the measurements.

    apt install poppler-utils      # Debian/Ubuntu
    dnf install poppler-utils      # RHEL/Fedora
    brew install poppler           # macOS

## What works

Verified against a production Stud.IP instance:

- **Documents** — PDF (per page), `.pptx`/`.odp` (per slide, including speaker
  notes), `.xlsx`/`.ods` (per sheet), `.docx`/`.odt`
- **Courseware** — per chapter, with the chapter title as the locator. On some
  courses this is *all* the content: of six courses surveyed, four had
  Courseware and three of those had no files at all
- **Audio** — transcribed in the background so indexing never blocks; timestamps
  as locators, speaker labels where the ASR server provides them
- **Governance metadata** — licence and download permission per file

Adapters: **Stud.IP** and **Moodle** both work against courses with content,
extracting file contents with page and slide locators; **ILIAS** is written but
has never run, for want of an instance.

## What does not work yet

- **Images.** Diagrams and scanned pages need a vision model or OCR. Not built.
- **Per-lecturer AI opt-in.** Everything reachable in a course is currently
  extracted. Only material a lecturer has explicitly released for AI should be —
  this is a precondition for real use and is the next piece of work.
- **Incremental updates.** A course is re-indexed wholesale. Stud.IP exposes an
  ETag per file, which would support syncing only what changed.
- **Legacy `.doc`/`.ppt`/`.xls`** — OLE compound files, refused with a message
  saying so.

## What it actually does, in three flows

The name is abstract, so concretely:

**1. Indexing — LMS to bridge.** An adapter pulls a course's material (files,
Courseware, recordings) and pushes the *text* to `/v1/index`. The bridge stores
chunks and, where an embedding model exists, their vectors. **That copy lives in
the bridge** — which is why `forget()` is in the contract and why licences are
recorded per file: it is a copy of someone's teaching material.

**2. Asking — LMS to bridge to LMS.** A student asks a question *in the LMS*.
The LMS calls `/v1/chat`. The bridge retrieves the relevant passages, calls the
institution's model, and returns **an answer plus citations**. The LMS renders
it. Nothing about the conversation is kept: no history, no user accounts, no
per-student state. **The answers belong to the LMS**, not to the bridge.

**3. Negotiating — before either.** The adapter asks `/v1/capabilities` what
this deployment can do, so one adapter serves an institution with a GPU cluster
and one with nothing.

So: **the index lives here, the answers go home.** The bridge never renders
anything to a student — Moodle's interface is Moodle's job.

Why a bridge rather than a plugin: without it, each of Moodle, ILIAS and Stud.IP
needs its own answer to *which model, where do embeddings come from, how do we
transcribe, who pays, what is the retention policy* — nine implementations
across three platforms, which is what the research found happening. With it: one
contract, three thin adapters, and every AI capability is a slot filled by
whatever the institution already runs.

## Seeing it work

    ./demo-ui.sh [course_id]

Indexes a course and opens a one-page demo surface showing what a terminal
cannot: citations as links back into Stud.IP, the capability handshake as a
status strip, and recordings transcribing in the background while questions are
already answerable.

**It is scaffolding, not a product.** The real front end is the LMS's. A chat
interface that grew nice enough would be exactly the mistake this prototype
argues against — see `bridge/demo_page.py`.

## The contract

Four endpoints. Everything else is one possible implementation of them.

| Endpoint | Purpose |
|---|---|
| `GET /v1/index/status` | Is a course indexed, and how many chunks — so an LMS can ask directly rather than infer it from an empty answer |
| `GET /v1/health` | Liveness and which providers are configured |
| `GET /v1/capabilities` | What this deployment can do — lets an adapter degrade instead of failing |
| `POST /v1/chat` | Ask; `course_ref` scopes the question to indexed material |
| `POST /v1/index` | Submit course content, per activity |
| `POST /v1/forget` | Delete a course's index |

Three decisions carry most of the weight:

- **`course_ref` is opaque** (`moodle:1234`, `studip:a1b2…`). The bridge scopes
  retrieval by it and never models courses, which avoids inventing a cross-LMS
  ontology.
- **The LMS pushes content; the bridge never reaches back into the LMS.** One
  trust direction, and per-activity opt-in becomes natural rather than bolted on.
- **`sources` is always present**, empty when retrieval is unavailable, so
  adapters need no special-casing.

## What is verified, and what is not

Verified against live instances on 2026-08-24:

| | Result |
|---|---|
| Bridge, end to end | **works** — index, grounded retrieval with citations, forget |
| Stud.IP adapter | **works** against `studip-test.uni-osnabrueck.de` (JSON-API, HTTP Basic, no admin rights needed) |
| Moodle adapter | **works** against `moodle-test-virtuos-openstack.uni-osnabrueck.de` (Moodle 5.1.3+): course description, module text and **file contents with page locators** — 36 documents and 97k characters from one course |
| ILIAS adapter | **not written.** No instance was available |

**Verified against a course with content, 2026-08-27.** The first three test
courses returned *zero* documents — not a bug: `core_course_get_contents`
exposes section summaries and module descriptions, and those courses had
neither. On a seeded course it returns the course description and one document
per module.

Two things worth knowing. `core_course_get_contents` does **not** return the
course-level summary, so the adapter fetches it separately — without that, the
one text field a lecturer reliably fills in produces no document at all. And
**the token must be appended to `fileurl` with `&`, not `?`**: that URL already
carries `?forcedownload=1`, and a second query string makes Moodle answer
**HTTP 200 with a JSON error body** saying the token is missing while one is
plainly being sent.

## File extraction

On a typical course the substance lives in the files, not the descriptions — so
the adapters download attachments and index their contents, **one document per
page or slide**. That is what fills `Source.locator`: a citation of
`Vorlesung 3.pdf` cannot be checked, `Vorlesung 3.pdf, S. 12` can.

| Format | Unit |
|---|---|
| PDF | per page |
| `.pptx`, `.odp` | per slide, including speaker notes |
| `.xlsx`, `.ods` | per sheet |
| `.docx`, `.odt` | whole document |
| Stud.IP Courseware | per chapter |

**Install `poppler-utils`.** PDF text comes from `pdftotext` when it is
installed and from a much weaker built-in reader when it is not — on real
documents the difference has been the whole file versus 12% of it. The built-in
reader exists so the demo runs with nothing installed, not because it is good
enough. An indexing run prints which one it used.

Scanned PDFs are refused rather than indexed as empty, so a course cannot
silently lose a file; reading them would need OCR, which this does not do.
Legacy `.doc`/`.ppt`/`.xls` are refused too. One unreadable file never fails an
indexing run.

> Details — measurements, per-platform download quirks, what Courseware
> deliberately does not index — are in **[EXTRACTION.md](EXTRACTION.md)**.

## An unexpected finding: Stud.IP is the easier platform

Contrary to what the platform research assumed, Stud.IP exposes far more
indexable text than Moodle:

- **Stud.IP** — `wiki-pages`, `courseware-units`, `file-refs`, `folders`, `news`,
  the full `forum-*` tree.
- **Moodle** — section summaries and module descriptions, via one call.

The Stud.IP JSON-API also turned out to carry more than content. See
The Stud.IP KI-Toolbox exposes **per-course AI rules
over REST** and runs an **OpenID Connect provider**. The rules being
machine-readable means a tool can honour a course's AI policy rather than
reimplement it — a governance primitive neither Moodle nor ILIAS has.

## Layout

```
DESIGN.md                     why it is shaped this way
EXTRACTION.md                 formats, locators, per-platform download quirks
bridge/
  contract.py                 the deliverable: types + provider interfaces
  server.py                   HTTP service (stdlib only)
  providers/
    openai_chat.py            any OpenAI-compatible endpoint, + offline echo
    builtin_retrieval.py      simple lexical retrieval, so the demo is honest
    external_stub.py          how ByCS / OSKI would plug in — deliberately unimplemented
adapters/
  moodle_adapter.py           Moodle web services → bridge
  studip_adapter.py           Stud.IP JSON-API → bridge
  studip_probe.py             what can this account see?
  studip_inspect.py           what is extractable from one course?
fixtures/demo-course.json     offline demo content
tests/                        contract and retrieval tests
```

## What this deliberately does not do

- **Not production-ready.** Shared-token auth at best, no rate limiting, no
  migrations, JSON-file storage.
- **No quotas or budgets.** Those belong in infrastructure — a gateway or
  `local_ai_manager`.
- **Not a better RAG engine.** The built-in retrieval is lexical, not vector, so
  the demo needs no embedding model or vector database. Real deployments should
  use one of the engines that already exist.
- **No data-protection work.** Indexing creates a *copy* of course material, so
  retention and deletion of that copy are live questions. `/v1/forget` exists
  because of that, but no DSFA has been done.

## Licence

[GPLv3](LICENSE). Chosen to match the ecosystem this is meant to work with —
Moodle itself and ByCS/ISB's `local_ai_content` are GPL — so that code can move
between them without a licence problem.

Copyright © 2026 [virtUOS](https://www.virtuos.uni-osnabrueck.de/), Osnabrück
University. Developed in the LernKI project, funded by the Stiftung Innovation
in der Hochschullehre.
