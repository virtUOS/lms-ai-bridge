# LMS AI Bridge — design

- **Written:** 2026-08-20 · **Status:** design, before implementation
- **Context:** LernKI is asked to build an AI component/interface for Moodle,
  ILIAS and Stud.IP. Prototype promised in two weeks.

## The problem this solves, stated precisely

The research established two things that together determine what LernKI should
build:

1. **Moodle is taken.** Two funded teams are already building course-grounded RAG
   for Moodle — ByCS/ISB's `local_ai_content` and OSKI.nrw's LMS-RAG — both on the
   ISB stack. A third Moodle RAG would be waste.
2. **ILIAS and Stud.IP have nothing**, and no core AI layer to build one against.
   Every AI plugin on those platforms re-solves backends, credentials and
   governance for itself.

So the gap LernKI is uniquely placed to fill is not *another RAG engine*. It is
**the layer between an LMS and whatever AI capability an institution runs** —
defined once, implemented per platform, and deliberately shaped so that the
existing Moodle work can sit behind it rather than compete with it.

> **This is the "don't reinvent the wheel" answer made concrete.** Reuse where
> something exists (Moodle core's AI subsystem, `local_ai_manager`, the two RAG
> engines). Build only the part nobody is building: the common contract and the
> adapters for the two platforms that have nothing.

## What it is

A small **HTTP service** plus **thin per-LMS adapters**.

```
┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│   Moodle    │  │    ILIAS    │  │   Stud.IP   │      ← LMS adapters (thin)
└──────┬──────┘  └──────┬──────┘  └──────┬──────┘
       └────────────────┼────────────────┘
                        ▼
              ┌───────────────────┐
              │   LMS AI Bridge   │                    ← this prototype
              │  (one HTTP API)   │
              └─────────┬─────────┘
          ┌─────────────┼─────────────┐
          ▼             ▼             ▼
   ┌───────────┐ ┌────────────┐ ┌──────────┐
   │ chat      │ │ retrieval  │ │  future  │           ← capability providers
   │ (LiteLLM/ │ │ (pluggable:│ │          │              (pluggable)
   │  any      │ │  built-in, │ │          │
   │  OpenAI-  │ │  ByCS,     │ │          │
   │  compat)  │ │  OSKI, …)  │ │          │
   └───────────┘ └────────────┘ └──────────┘
```

Two design rules do the real work:

- **The bridge never talks to a model directly.** It speaks the project's standard
  contract — `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `MODEL` — so a gateway, a direct
  model server, GWDG, or an institution's existing AI layer all satisfy it
  unchanged. See `prototypes/README.md`.
- **Retrieval is a provider interface, not an implementation.** The prototype
  ships one simple built-in provider so the demo works end to end. But the
  interface is the deliverable: an institution running ByCS's `local_ai_content`
  or OSKI's stack should be able to put that behind the same endpoint without the
  LMS adapters knowing.

## The contract

Deliberately small. Four endpoints, versioned, JSON over HTTP.

| Endpoint | Purpose |
|---|---|
| `GET /v1/health` | Liveness plus which providers are configured — the first thing an admin checks |
| `GET /v1/capabilities` | What this deployment can do (`chat`, `retrieval`, …). Lets an LMS adapter degrade gracefully instead of failing |
| `POST /v1/chat` | Ask a question. Optional `course_ref` scopes it to a course's indexed material |
| `POST /v1/index` | Submit course content for indexing, per activity |

### Why these four and not more

- **`/capabilities` is the modularity mechanism.** An institution with no
  retrieval provider still gets chat; the adapter checks and hides the rest. This
  is how one contract serves institutions with very different infrastructure —
  institutions differ in what they run, and the handshake is how one
  interface serves all of them.
- **`/index` takes content, not credentials.** The LMS pushes what it chooses to
  share; the bridge never holds LMS credentials or reaches back into the LMS. That
  keeps the trust boundary in one direction and makes the per-activity opt-in
  model — which `local_ai_content` also uses — natural rather than bolted on.
- **`course_ref` is opaque to the bridge.** A namespaced string
  (`moodle:1234:activity:56`). The bridge does not model courses; it scopes
  retrieval. That avoids inventing a cross-LMS course ontology, which is where
  this kind of project usually dies.

### Request shape

```jsonc
// POST /v1/chat
{
  "course_ref": "studip:a1b2c3",      // optional; omit for ungrounded chat
  "messages": [{"role": "user", "content": "Was ist ein Monad?"}],
  "user_ref": "u:98765",              // opaque, for quota attribution
  "locale": "de"
}
```

```jsonc
// 200 response
{
  "answer": "…",
  "sources": [                         // empty when retrieval is unavailable
    {"title": "Vorlesung 3.pdf", "locator": "S. 12", "activity_ref": "…"}
  ],
  "usage": {"prompt_tokens": 812, "completion_tokens": 143},
  "provider": {"chat": "litellm", "retrieval": "builtin"}
}
```

**Citations are in the contract from the start**, because OSKI.nrw's version has
them and they are the feature that makes a grounded answer trustworthy. An
implementation without retrieval returns `sources: []` rather than omitting the
field, so adapters need no special-casing.

## What the prototype will and will not do

**Will:**

- Run the bridge with a working chat path against a real OpenAI-compatible
  endpoint (the LiteLLM gateway).
- Ship a naive but real retrieval provider — chunk, embed, store, retrieve — so
  the grounded path demonstrates end to end with citations.
- Include **Moodle and Stud.IP adapters** exercised against your test instances,
  and an **ILIAS adapter written but marked untested** if no instance is
  available.
- Ship a `providers/` interface with two retrieval implementations: the built-in
  one, and a **stub showing how `local_ai_content` or OSKI would plug in** — the
  point being to show the seam, not to implement someone else's engine.
- Be runnable by a colleague in one command, with fixtures, so the working group
  can try it without institutional access.

**Will not:**

- Be production-ready. No auth beyond a shared token, no rate limiting, no
  persistence guarantees, no migrations.
- Implement quotas or budgets. Those belong in the infrastructure layer — a
  gateway or the LMS's own AI layer.
- Be a better RAG engine than ByCS's or OSKI's. The built-in provider exists to
  make the demo honest, not to compete.
- Settle the data-protection questions. No DSFA, and indexing creates a copy of
  course material in a vector store — a real retention question flagged in
  the ByCS project.

## Scope, deliberately

- **Not production software.** No auth beyond a shared token, no rate limiting,
  no persistence guarantees, no migrations.
- **Not a quota or budget system.** That belongs in the infrastructure layer — a
  gateway, or the LMS's own AI layer — below every consumer of this.
- **Not a better retrieval engine** than the ones institutions already run. The
  built-in provider exists so the demo works end to end and so an institution
  with no store still gets something; it is a floor, not a competitor.
- **Not a settlement of the data-protection questions.** Indexing creates a copy
  of course material, which is a real retention question, and only material a
  lecturer has released for AI should ever be extracted.
