"""Error-classification tests for the `claude -p` adapter.

Regression coverage for the bug where a Claude Code session-limit notice that
lands ONLY in an assistant text block (with an empty `result`) was misread as a
"malformed" attempt and, three in a row, ended an otherwise-healthy loop —
instead of being treated as a recoverable quota pause. See
`_classify_streams` / `_USAGE_LIMIT_RE` in goaloop/adapter.py.

Run: python -m unittest discover -s tests
"""

import unittest

from goaloop.adapter import (
    QuotaExhausted,
    TransientError,
    _classify_streams,
)

SID = "test-session-id"

# The exact string Claude Code emits when the subscription session limit is
# hit — the message that killed range-skew-error-rootcause and twice killed
# range-vs-hash-uniform attempt 023.
SESSION_LIMIT = "You've hit your session limit · resets 3:10pm (Asia/Shanghai)"


class ClassifyStreamsTest(unittest.TestCase):
    def _classify(self, result="", stderr="", assistant=""):
        return _classify_streams(result, stderr, assistant, SID)

    # ---- the fix: usage limit in assistant text only ---------------------

    def test_session_limit_in_assistant_text_only_is_quota(self):
        """The regression: limit notice in assistant text, empty result."""
        err = self._classify(result="", assistant=SESSION_LIMIT)
        self.assertIsInstance(err, QuotaExhausted)

    def test_session_limit_amid_analysis_is_quota(self):
        """Real turns interleave analysis then the abort notice at the end."""
        assistant = (
            "Let me add the retry helper.\n"
            "Editing client.py to wrap _stream_load.\n" + SESSION_LIMIT
        )
        err = self._classify(assistant=assistant)
        self.assertIsInstance(err, QuotaExhausted)
        # Surfaces the matched line, not the whole analysis blob.
        self.assertIn("session limit", str(err).lower())

    def test_resets_clock_phrasing_is_quota(self):
        err = self._classify(assistant="Paused. resets 11:30 am tomorrow.")
        self.assertIsInstance(err, QuotaExhausted)

    # ---- existing behavior preserved: result / stderr streams ------------

    def test_session_limit_in_result_still_quota(self):
        err = self._classify(result=SESSION_LIMIT)
        self.assertIsInstance(err, QuotaExhausted)

    def test_quota_in_stderr_is_quota(self):
        err = self._classify(stderr="Error: rate limit exceeded")
        self.assertIsInstance(err, QuotaExhausted)

    def test_transient_in_stderr_is_transient(self):
        err = self._classify(stderr="fetch failed: ECONNRESET")
        self.assertIsInstance(err, TransientError)
        self.assertEqual(err.session_id, SID)

    # ---- no false positives from a Runner's own analysis text ------------

    def test_perf_analysis_mentioning_overloaded_is_clean(self):
        """A completed turn whose analysis (intermediate assistant blocks)
        mentions broad quota/transient words must NOT be reclassified —
        assistant text is scanned only for the narrow usage-limit notice, not
        the broad _QUOTA_RE/_TRANSIENT_RE. The `result` field holds just the
        terminator, as in a real stream."""
        analysis = (
            "The CN was overloaded during the window; too many requests queued, "
            "and one node returned 503 / connection refused under the hot "
            "tablet. Query reached its timeout of 300 seconds."
        )
        terminator = '{"status": "advanced", "summary": "investigated the hotspot"}'
        self.assertIsNone(
            self._classify(result=terminator, assistant=analysis + "\n" + terminator)
        )

    def test_clean_turn_is_none(self):
        assistant = '{"status": "pass", "verification": "all 3 dims within 5%"}'
        self.assertIsNone(self._classify(result=assistant, assistant=assistant))


if __name__ == "__main__":
    unittest.main()
