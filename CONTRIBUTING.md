# Working on this

## Running the tests

    python3 -m unittest discover -s tests

No dependencies. If a test needs one, the test is wrong.

## Conventions that are load-bearing

- **Standard library only.** A colleague must be able to clone this and run
  `./demo.sh` with nothing installed. That constraint is why the extractors are
  written by hand rather than pulling in `pypdf` and `python-pptx`, and it is
  worth keeping.
- **No AI infrastructure is bundled.** Chat, embeddings and transcription are
  provider interfaces configured from the environment. Adding a hardcoded host
  or a vendor SDK breaks the point of the component.
- **Locators are the product.** "S. 12", "Folie 25", "0:00" — anything that
  loses them makes an answer uncheckable. Extraction is the only place that
  knows them.
- **Failures are recorded, not hidden.** A file that cannot be read is skipped
  with a message; a question the material cannot answer gets an honest refusal.
  Silence reads as broken.
- **Corrections stay visible.** Several comments in this codebase document a
  wrong conclusion alongside the right one, because the wrong ones were
  plausible and got rediscovered. Keep doing that rather than tidying them away.
