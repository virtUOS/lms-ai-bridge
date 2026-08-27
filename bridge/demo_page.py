"""A single-page demo surface, served by the bridge itself.

**This is scaffolding, not a product.** The bridge's whole argument is that
institutions should not build a fourth AI component — so a chat interface that
grew nice enough would be that mistake wearing a different hat. The real front
end is Moodle's, ILIAS's, Stud.IP's. This page exists so a human can watch the
contract work, and it says so on the page rather than leaving it implied.

It shows the three things a terminal cannot:

1. **Citations as links.** "Folie 25", "S. 38", "0:00" are the trust mechanism.
   Printed they are claims; clickable they are checkable.
2. **The capability handshake.** `/v1/capabilities` returns which chat model,
   which retrieval provider, whether transcription exists — that *is* the reuse
   argument, and a status strip renders it.
3. **Background work.** A recording still transcribing while questions are
   already answerable is the design decision hardest to convey in prose.

No build step, no framework, no dependency: one string of HTML, served from
`/demo`. Deliberately unglamorous.
"""

from __future__ import annotations

PAGE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LMS AI Bridge — Demo</title>
<style>
  :root {
    --bg: #fbfaf8; --panel: #fff; --ink: #1b1a18; --muted: #6b6862;
    --line: #e5e1da; --accent: #7a1d2e; --ok: #2c6e49; --warn: #9a6700;
    --mono: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#16151a; --panel:#1e1d24; --ink:#eceaf0; --muted:#9a97a3;
            --line:#312f3a; --accent:#e08a9b; --ok:#7fd1a4; --warn:#e5b567; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
    font:16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
  .wrap { max-width: 860px; margin: 0 auto; padding: 28px 20px 80px; }
  header { border-bottom: 1px solid var(--line); padding-bottom: 18px; margin-bottom: 22px; }
  h1 { font-size: 21px; margin: 0 0 4px; letter-spacing: -0.01em; }
  .sub { color: var(--muted); font-size: 14px; margin: 0; }
  .scaffold { margin-top: 14px; padding: 10px 13px; border-left: 3px solid var(--accent);
    background: var(--panel); font-size: 13.5px; color: var(--muted); border-radius: 0 6px 6px 0; }
  .scaffold b { color: var(--ink); }

  .strip { display:flex; flex-wrap:wrap; gap:8px; margin: 18px 0 22px; }
  .cap { font: 12px/1 var(--mono); padding: 7px 10px; border-radius: 999px;
    border:1px solid var(--line); background:var(--panel); color:var(--muted); }
  .cap b { color: var(--ink); font-weight: 600; }
  .cap.on { border-color: color-mix(in srgb, var(--ok) 45%, var(--line)); }
  .cap.off { opacity: .55; text-decoration: line-through; }

  .panel { background:var(--panel); border:1px solid var(--line); border-radius:10px;
    padding:16px; margin-bottom:16px; }
  label { display:block; font-size:13px; color:var(--muted); margin-bottom:6px; }
  input, select, textarea, button { font: inherit; }
  input[type=text] { width:100%; padding:9px 11px; border:1px solid var(--line);
    border-radius:7px; background:var(--bg); color:var(--ink); }
  .row { display:flex; gap:10px; align-items:flex-end; }
  .row > div { flex:1; }
  button { padding:9px 16px; border-radius:7px; border:1px solid var(--accent);
    background:var(--accent); color:#fff; cursor:pointer; font-weight:500; }
  button.ghost { background:transparent; color:var(--accent); }
  button:disabled { opacity:.5; cursor:default; }

  .suggest { display:flex; flex-wrap:wrap; gap:7px; margin-top:11px; }
  .suggest button { font-size:13px; padding:5px 11px; border-radius:999px;
    background:transparent; border:1px solid var(--line); color:var(--muted);
    font-weight:400; }
  .suggest button:hover { border-color:var(--accent); color:var(--ink); }
  .resolved { font: 11px var(--mono); color:var(--muted); margin-top:6px; min-height:14px; }
  .resolved b { color: var(--ok); }
  .jobs { font-size:13px; color:var(--warn); margin-top:10px; display:none; }
  .jobs.on { display:block; }

  .qa { margin-top:22px; }
  .q { font-weight:600; margin: 22px 0 8px; }
  .q::before { content:"▸ "; color:var(--accent); }
  .a { white-space:pre-wrap; }
  .a code { font: 13px var(--mono); }
  .sources { margin-top:12px; padding-top:10px; border-top:1px dashed var(--line); }
  .sources h4 { margin:0 0 7px; font-size:12px; text-transform:uppercase;
    letter-spacing:.06em; color:var(--muted); font-weight:600; }
  .src { display:flex; gap:9px; align-items:baseline; font-size:14px; margin:4px 0; }
  .src .n { font: 12px var(--mono); color:var(--accent); }
  .src a { color:var(--ink); text-decoration:none; border-bottom:1px solid var(--line); }
  .src a:hover { border-color:var(--accent); }
  .src .loc { font: 12px var(--mono); color:var(--muted); }
  .none { color:var(--muted); font-size:14px; font-style:italic; }
  .meta { font: 11px var(--mono); color:var(--muted); margin-top:9px; }
  .err { color:var(--accent); }
  footer { margin-top:40px; padding-top:16px; border-top:1px solid var(--line);
    font-size:13px; color:var(--muted); }
</style>
</head>
<body>
<div class="wrap">

<header>
  <h1>LMS AI Bridge</h1>
  <p class="sub">Eine Schnittstelle zwischen einem Lernmanagementsystem und den
     KI-Diensten, die eine Hochschule ohnehin betreibt.</p>
  <div class="scaffold">
    <b>Dies ist kein Produkt.</b> Die eigentliche Oberfläche ist die von Moodle,
    ILIAS oder Stud.IP. Diese Seite existiert nur, damit sichtbar wird, was der
    Vertrag tut: Kursmaterial hinein, belegte Antwort heraus — mit den Diensten,
    die die Hochschule bereits hat.
  </div>
</header>

<div class="strip" id="caps"><span class="cap">lade …</span></div>

<div class="panel">
  <div class="row">
    <div>
      <label for="course">Kurs (course_ref)</label>
      <input type="text" id="course"
             placeholder="Kurs-ID, Stud.IP-URL oder studip:…">
      <div class="resolved" id="resolved"></div>
    </div>
    <button class="ghost" id="reload">Status</button>
  </div>
  <div class="jobs" id="jobs"></div>
</div>

<div class="panel">
  <label for="q">Frage an die Kursmaterialien</label>
  <div class="row">
    <div><input type="text" id="q" placeholder="Worum geht es in dieser Veranstaltung?"></div>
    <button id="ask">Fragen</button>
  </div>
  <div class="suggest" id="suggest"></div>
</div>

<div class="qa" id="qa"></div>

<footer>
  Der Index liegt in der Bridge; die Antworten gehören dem LMS. Nichts wird
  gespeichert, was nicht über <code>/v1/index</code> hereingegeben wurde, und
  <code>/v1/forget</code> löscht es wieder.
</footer>

</div>
<script>
const $ = s => document.querySelector(s);

// Accept what a person actually types: a bare course id, a pasted Stud.IP URL,
// or a full course_ref. Demanding "studip:<32 hex>" exactly is the kind of
// detail that makes a demo look broken when it is working fine.
function courseRef() {
  const raw = $("#course").value.trim();
  if (!raw) return "";
  if (raw.includes(":") && !raw.startsWith("http")) return raw;   // already a ref
  const m = raw.match(/([0-9a-f]{32})/i);
  return m ? `studip:${m[1]}` : raw;
}
const esc = s => String(s).replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

// A citation should be checkable, not just printed. Stud.IP refs carry the
// course id, so they can point back into the LMS the material came from.
function sourceLink(src, ref_) {
  const ref = src.activity_ref || "";
  const m = ref_.match(/^studip:([0-9a-f]{32})/);
  if (m && ref.includes(":file:")) {
    const fid = ref.split(":file:")[1];
    return `https://studip.uni-osnabrueck.de/sendfile.php?type=0&file_id=${fid}`;
  }
  if (m) return `https://studip.uni-osnabrueck.de/dispatch.php/course/overview?cid=${m[1]}`;
  return null;
}

async function caps() {
  const el = $("#caps");
  try {
    const r = await fetch("/v1/capabilities");
    const d = await r.json();
    const has = n => d.capabilities.includes(n);
    const p = d.providers || {};
    el.innerHTML = [
      `<span class="cap on">Vertrag <b>${esc(d.contract)}</b></span>`,
      `<span class="cap ${has("chat") ? "on" : "off"}">Chat <b>${esc(p.chat || "—")}</b></span>`,
      `<span class="cap ${has("retrieval") ? "on" : "off"}">Retrieval <b>${esc(p.retrieval || "—")}</b></span>`,
      `<span class="cap ${has("transcription") ? "on" : "off"}">Transkription <b>${esc(p.transcription || "nicht konfiguriert")}</b></span>`,
    ].join("");
  } catch (e) {
    el.innerHTML = `<span class="cap err">Bridge nicht erreichbar</span>`;
  }
}

async function jobs() {
  const course = courseRef();
  const box = $("#jobs");
  if (!course) { box.className = "jobs"; return; }
  try {
    const r = await fetch(`/v1/jobs?course_ref=${encodeURIComponent(course)}`);
    const d = await r.json();
    box.className = "jobs on";
    if (d.pending > 0) {
      box.style.color = "var(--warn)";
      box.textContent = `⏳ ${d.pending} Aufnahme(n) werden transkribiert — `
        + `Fragen sind trotzdem schon möglich, die Antwort stützt sich `
        + `solange nur auf die Textmaterialien.`;
    } else if (d.done > 0 || d.failed > 0) {
      box.style.color = "var(--ok)";
      box.textContent = `✓ ${d.done} Transkript(e) im Index`
        + (d.failed ? `, ${d.failed} fehlgeschlagen` : "");
    } else {
      // Silence here reads as a broken button. "No recordings" is a real
      // answer and worth saying — most courses have none.
      box.style.color = "var(--muted)";
      box.textContent = "Keine Aufnahmen in diesem Kurs — nichts zu transkribieren.";
    }
  } catch (e) {
    box.className = "jobs on";
    box.style.color = "var(--accent)";
    box.textContent = "Status nicht abrufbar.";
  }
  showResolved();
}

// Tell the user what the bridge actually has for this course.
//
// This asks the index directly. An earlier version probed with a nonsense
// question and inferred emptiness from the lack of results — which reported a
// fully indexed course as empty, because embeddings correctly found nothing
// similar to "__probe__". Inferring state from a query is unreliable: a course
// can be indexed and still have nothing relevant to a given question.
async function showResolved() {
  const ref = courseRef();
  const el = $("#resolved");
  if (!ref) { el.textContent = ""; return; }
  try {
    const r = await fetch(`/v1/index/status?course_ref=${encodeURIComponent(ref)}`);
    const d = await r.json();
    el.innerHTML = d.indexed
      ? `→ <b>${esc(ref)}</b> — ${d.chunks} Abschnitte im Index`
      : `→ ${esc(ref)} — <span style="color:var(--warn)">nichts unter dieser `
        + `Referenz indexiert</span>`;
  } catch (e) {
    el.textContent = `→ ${ref}`;
  }
}

async function ask() {
  const course = courseRef();
  const question = $("#q").value.trim();
  if (!question) return;
  $("#ask").disabled = true;

  const qa = $("#qa");
  const block = document.createElement("div");
  block.innerHTML = `<div class="q">${esc(question)}</div><div class="a">…</div>`;
  qa.prepend(block);

  try {
    const r = await fetch("/v1/chat", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({course_ref: course, locale: "de",
                            messages: [{role: "user", content: question}]}),
    });
    const d = await r.json();
    if (d.error) throw new Error(d.error.message);

    let html = `<div class="a">${esc(d.answer)}</div>`;
    if (d.sources && d.sources.length) {
      html += `<div class="sources"><h4>Quellen</h4>`;
      d.sources.forEach((s, i) => {
        const url = sourceLink(s, course);
        const title = esc(s.title);
        const name = url ? `<a href="${url}" target="_blank" rel="noopener">${title}</a>`
                         : title;
        const loc = s.locator ? `<span class="loc">${esc(s.locator)}</span>` : "";
        html += `<div class="src"><span class="n">[${i + 1}]</span>
                 <span>${name} ${loc}</span></div>`;
      });
      html += `</div>`;
    } else {
      html += `<div class="sources"><span class="none">Keine Quelle —
               im Kursmaterial steht dazu nichts.</span></div>`;
    }
    const u = d.usage || {};
    html += `<div class="meta">${esc(d.provider.retrieval || "")} ·
             ${esc(d.provider.chat || "")} ·
             ${u.prompt_tokens || 0}+${u.completion_tokens || 0} tokens</div>`;
    block.innerHTML = `<div class="q">${esc(question)}</div>` + html;
  } catch (e) {
    block.innerHTML = `<div class="q">${esc(question)}</div>
                       <div class="a err">Fehler: ${esc(e.message)}</div>`;
  } finally {
    $("#ask").disabled = false;
    $("#q").value = "";
    jobs();
  }
}

// Starter questions. A blank box stalls a live demo, and these are chosen to
// show the three things worth showing: a grounded answer with citations across
// formats, an answer that must come from a recording, and an honest refusal.
// Each has to stand on its own. "Was steht dazu in den Folien?" was here and
// was a bad starter: "dazu" refers to nothing when it is the first question, so
// it retrieved a slide carrying only a heading and honestly reported just the
// heading — the system working correctly on a meaningless question.
const SUGGESTIONS = [
  "Worum geht es in dieser Veranstaltung?",
  "Welche Themen werden behandelt?",
  "Was wird über KI-Kompetenzen gesagt?",
  "Was wird in der Aufnahme gesagt?",
  "Wie hoch ist die Studiengebühr?",     // deliberately not in any course
];

function renderSuggestions() {
  const box = $("#suggest");
  box.innerHTML = "";
  SUGGESTIONS.forEach(text => {
    const b = document.createElement("button");
    b.textContent = text;
    b.title = text === "Wie hoch ist die Studiengebühr?"
      ? "Steht in keinem Kurs — zeigt, dass nicht geraten wird"
      : "";
    b.addEventListener("click", () => { $("#q").value = text; ask(); });
    box.appendChild(b);
  });
}
renderSuggestions();

$("#ask").addEventListener("click", ask);
$("#q").addEventListener("keydown", e => { if (e.key === "Enter") ask(); });
$("#reload").addEventListener("click", () => { caps(); jobs(); });
$("#course").addEventListener("change", () => { jobs(); showResolved(); });
$("#course").addEventListener("input", () => {
  const ref = courseRef();
  $("#resolved").textContent = ref ? `→ ${ref}` : "";
});
caps();
setInterval(jobs, 5000);
</script>
</body>
</html>
"""
