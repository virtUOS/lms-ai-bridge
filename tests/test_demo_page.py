"""The demo surface: it must stay a demo.

The bridge's argument is that institutions should not build a fourth AI
component, so a chat UI that quietly became a product would be that mistake in
a different costume. These tests pin the properties that keep it scaffolding —
no build step, no framework, and a visible statement of what it is not.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.demo_page import PAGE  # noqa: E402


class TestDemoPageStaysADemo(unittest.TestCase):
    def test_it_says_it_is_not_a_product(self):
        # The framing is the point: whoever sees this must not mistake it for
        # the thing being proposed.
        self.assertIn("kein Produkt", PAGE)
        self.assertIn("Moodle", PAGE)
        self.assertIn("Stud.IP", PAGE)

    def test_no_external_dependency(self):
        # No CDN, no framework, no build step. It must run from the bridge on a
        # laptop with nothing installed — the same promise as the rest.
        for forbidden in ("cdn.", "unpkg", "jsdelivr", "googleapis",
                          "react", "vue.", "jquery", "import "):
            self.assertNotIn(forbidden, PAGE.lower(),
                             f"demo page pulled in {forbidden!r}")

    def test_it_is_one_self_contained_file(self):
        self.assertIn("<style>", PAGE)
        self.assertIn("<script>", PAGE)
        self.assertNotIn('<link rel="stylesheet"', PAGE)
        self.assertNotIn("<script src=", PAGE)


class TestItShowsWhatTheTerminalCannot(unittest.TestCase):
    """Three things justify a UI at all; if any is missing, it is just chat."""

    def test_it_renders_the_capability_handshake(self):
        # /v1/capabilities IS the reuse argument — which chat model, which
        # retrieval provider, whether transcription exists.
        self.assertIn("/v1/capabilities", PAGE)
        self.assertIn("Transkription", PAGE)
        self.assertIn("Retrieval", PAGE)

    def test_it_makes_citations_clickable(self):
        # Printed, a citation is a claim; linked, it is checkable.
        self.assertIn("sourceLink", PAGE)
        self.assertIn("activity_ref", PAGE)
        self.assertIn("sendfile.php", PAGE)

    def test_it_shows_background_transcription(self):
        # The design decision hardest to convey in prose: answers work while
        # recordings are still being processed.
        self.assertIn("/v1/jobs", PAGE)
        self.assertIn("transkribiert", PAGE)

    def test_it_states_where_the_data_lives(self):
        # The question this whole page exists to answer: the index is in the
        # bridge, the answers belong to the LMS, and it can all be deleted.
        self.assertIn("/v1/forget", PAGE)


class TestNoSecretsInThePage(unittest.TestCase):
    def test_no_credentials_are_embedded(self):
        for leak in ("sk-", "Bearer ", "api_key", "password"):
            self.assertNotIn(leak, PAGE, f"demo page contains {leak!r}")


class TestItAcceptsWhatPeopleActuallyType(unittest.TestCase):
    """A demo that requires exact syntax looks broken when it is working.

    Found live: the course field was given a bare id, so course_ref was
    "ad2ae0…" rather than "studip:ad2ae0…". Retrieval matched nothing, the
    answer was an honest "nothing found", and the Status button reported
    nothing — three symptoms of one input-format mismatch.
    """

    def test_it_normalises_the_input(self):
        # Bare id, pasted URL and full ref must all reach the same course.
        self.assertIn("function courseRef", PAGE)
        self.assertIn("[0-9a-f]{32}", PAGE)
        self.assertIn("studip:${m[1]}", PAGE)

    def test_it_shows_the_resolved_reference(self):
        # Silence is the enemy here: the page must say what it actually sent.
        self.assertIn("showResolved", PAGE)
        self.assertIn("nichts unter dieser", PAGE)

    def test_the_placeholder_does_not_demand_one_format(self):
        self.assertIn("Kurs-ID, Stud.IP-URL oder", PAGE)

    def test_no_shadowed_identifier(self):
        # courseRef() the function and courseRef the parameter would shadow.
        self.assertNotIn("function sourceLink(src, courseRef)", PAGE)


class TestStatusAlwaysSaysSomething(unittest.TestCase):
    """A button that does nothing visible is worse than no button.

    Found live: on a course with no audio, every job counter is zero and the
    panel stayed hidden, so Status appeared broken. "No recordings" is a real
    answer — and the common one, since most courses have none.
    """

    def test_every_state_renders_a_message(self):
        for message in ("werden transkribiert",          # pending
                        "Transkript(e) im Index",        # finished
                        "Keine Aufnahmen in diesem Kurs",  # none — the common case
                        "Status nicht abrufbar"):        # request failed
            self.assertIn(message, PAGE, f"no rendering for {message!r}")

    def test_the_panel_only_hides_when_no_course_is_entered(self):
        # The single legitimate silent path.
        self.assertEqual(PAGE.count('box.className = "jobs";'), 1)


class TestSuggestedQuestions(unittest.TestCase):
    """A blank box stalls a live demo, and the starters are chosen to show the
    three things worth showing rather than just to fill the space."""

    def test_suggestions_exist_and_are_clickable(self):
        self.assertIn("const SUGGESTIONS", PAGE)
        self.assertIn("renderSuggestions", PAGE)

    def test_one_suggestion_has_no_answer_anywhere(self):
        # The honest refusal is a feature, so the demo should provoke it on
        # purpose rather than hope someone asks the right thing.
        self.assertIn("Studiengeb", PAGE)
        self.assertIn("zeigt, dass nicht geraten wird", PAGE)

    def test_one_suggestion_targets_a_recording(self):
        self.assertIn("Was wird in der Aufnahme gesagt?", PAGE)

    def test_no_suggestion_depends_on_a_previous_question(self):
        # A starter question has no conversation behind it, so a referring word
        # like "dazu" points at nothing. Observed live: "Was steht dazu in den
        # Folien?" retrieved a slide holding only a heading and reported that
        # heading — correct behaviour, meaningless question.
        start = PAGE.index("const SUGGESTIONS")
        block = PAGE[start:PAGE.index("]", start)]
        for referring in ("dazu", "davon", "darüber", "dieser Punkt"):
            self.assertNotIn(f'"{referring}', block)
            self.assertNotIn(f" {referring} ", block)


if __name__ == "__main__":
    unittest.main()
