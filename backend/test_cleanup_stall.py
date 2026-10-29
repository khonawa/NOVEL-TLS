import queue
import threading
import unittest
from unittest.mock import patch

from novel_translator.chrome import wait_for_translation
from novel_translator.translation import validate_translated_chunk


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class FakePage:
    def __init__(self, clock, text):
        self.clock = clock
        self.text = text
        self.scrolls = []
        self.reloads = []
        self.reactivations = 0

    def locator(self, selector):
        return self

    def inner_text(self, **kwargs):
        return self.text()

    def evaluate(self, script, argument=None):
        if argument is not None:
            self.scrolls.append((self.clock.now(), argument['cleanup']))
            return {'bottom': True, 'targeted': False}
        if 'scrollTo' in script:
            pass  # reset after reload
        return True

    def reload(self, **kwargs):
        self.reloads.append(self.clock.now())

    def wait_for_timeout(self, milliseconds):
        self.clock.sleep(milliseconds / 1000)


class CleanupStallTests(unittest.TestCase):
    def test_cleanup_stall_sends_result_for_review(self):
        """Stalled targeted cleanup (61 chars remain across several scans) eventually sends result for review."""
        clock = FakeClock()
        # 45976 original → 61 remain after first sweep, then stuck at 61 for many scans.
        remaining = [61] * 100
        call_count = [0]

        def text():
            call_count[0] += 1
            return '中' * remaining[min(call_count[0] - 1, len(remaining) - 1)]

        page = FakePage(clock, text)
        reports = []
        reactivations = []

        def reactivate():
            reactivations.append(clock.now())
            page.reactivations += 1
            return True

        with patch('novel_translator.chrome.time.monotonic', clock.now), \
             patch('novel_translator.chrome.time.sleep', clock.sleep):
            result = wait_for_translation(
                page, 45976, threading.Event(), reports.append, reactivate,
                lambda: 0.5, lambda: 1, remaining_chinese_limit=50
            )

        # Should have triggered cleanup stall after 3 unchanged rounds, reactivated, then sent result.
        self.assertEqual(page.reactivations, 1)
        self.assertEqual(len(page.reloads), 0)  # stall does not reload
        self.assertTrue(any('sending result for review' in r.lower() for r in reports))
        self.assertEqual(validate_translated_chunk('中' * 45976, result, 50)[0], False)

    def test_cleanup_improvement_resets_stall_count(self):
        """If targeted cleanup makes progress, stall count resets."""
        clock = FakeClock()
        page = None

        def text():
            # Two equal scans, then a 10-character improvement, until within tolerance.
            scans = len(page.scrolls)
            return '中' * max(40, 100 - 10 * (scans // 2))

        page = FakePage(clock, text)
        reports = []

        with patch('novel_translator.chrome.time.monotonic', clock.now), \
             patch('novel_translator.chrome.time.sleep', clock.sleep):
            result = wait_for_translation(
                page, 1000, threading.Event(), reports.append, lambda: True,
                lambda: 0.5, lambda: 1, remaining_chinese_limit=50
            )

        self.assertFalse(any('sending result for review' in r.lower() for r in reports))
        self.assertTrue(any('within tolerance' in r.lower() for r in reports))
        self.assertLessEqual(len(result), 50)


if __name__ == '__main__':
    unittest.main()
