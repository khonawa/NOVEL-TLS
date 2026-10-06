import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from bridge import Bridge
from novel_translator.chrome import wait_for_translation
from novel_translator.translation import validate_translated_chunk
from novel_translator.text_processing import chinese_count


class BridgeTests(unittest.TestCase):
    def test_chinese_tolerance_boundaries(self):
        source = "\u4e2d" * 1000
        for count, limit, expected in ((49, 50, True), (50, 50, True), (51, 50, False), (1, 0, False), (80, 100, True)):
            with self.subTest(count=count, limit=limit):
                self.assertEqual(validate_translated_chunk(source, "Translated text. " * 100 + "\u4e2d" * count, limit)[0], expected)
        self.assertFalse(validate_translated_chunk(source, "", 50)[0])
        self.assertFalse(validate_translated_chunk(source, "tiny", 50)[0])

    def test_targeted_cleanup_stalls_and_retries_once(self):
        states = []
        retries = []

        class Page:
            def locator(self, selector):
                return self

            def inner_text(self, **kwargs):
                return "English text " + "\u4e2d" * 20

            def evaluate(self, script, argument=None):
                if argument is None:
                    self_test.assertNotIn("scrollTo", script)
                    return True
                states.append(argument["cleanup"])
                return {"bottom": True, "targeted": False}

        self_test = self
        with patch("novel_translator.chrome.time.monotonic", side_effect=range(0, 1000, 10)), patch("novel_translator.chrome.time.sleep"):
            result = wait_for_translation(Page(), 100, threading.Event(), lambda message: None, lambda: retries.append(True), lambda: 0.1, lambda: 10)
        self.assertEqual(states, [False] + [True] * 6)
        self.assertEqual(len(retries), 1)
        self.assertEqual(chinese_count(result), 20)

    def test_extended_chinese_count(self):
        self.assertEqual(chinese_count("English \u4e2d\u3007\uf900\U00020000\U00030000"), 5)
        self.assertEqual(chinese_count("English 123"), 0)

    def test_utf8_paths_over_windows_pipe(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "\u4e2d\u6587 novel.txt"
            source.touch()
            output = Path(directory) / "\u82f1\u6587.epub"
            message = {"kind": "start", "input": str(source), "output": str(output), "chunkLimit": 1}
            result = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("bridge.py"))],
                input=(json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"),
                capture_output=True,
                env={**os.environ, "PYTHONIOENCODING": "cp1252"},
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            events = [json.loads(line) for line in result.stdout.decode("utf-8").splitlines()]
            self.assertIn({"kind": "command_error", "value": "Unknown chunk preset."}, events)

    def test_controls(self):
        bridge = Bridge(lambda *args: None)
        bridge.command({"kind": "pause"})
        self.assertFalse(bridge.pause.is_set())
        bridge.command({"kind": "resume"})
        self.assertTrue(bridge.pause.is_set())
        bridge.command({"kind": "decision", "value": "retry"})
        self.assertEqual(bridge.decide(1, "test"), "retry")
        bridge.command({"kind": "cancel"})
        self.assertTrue(bridge.cancel.is_set())
        with self.assertRaises(InterruptedError):
            bridge.decide(1, "test")

    def test_reject_invalid_command(self):
        bridge = Bridge(lambda *args: None)
        with self.assertRaises(ValueError):
            bridge.command({"kind": "decision", "value": "invalid"})
        with self.assertRaises(ValueError):
            bridge.command({"kind": "unknown"})

    def test_runner_receives_context_and_drains(self):
        idle = threading.Event()
        received = []

        class FakeRunner:
            def __init__(self, context):
                received.append(context)

            def run(self, source, output):
                received.extend([source, output])

        bridge = Bridge(lambda kind, value: idle.set() if kind == "idle" else None, FakeRunner)
        threading.Thread(target=bridge.forward, daemon=True).start()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "novel.txt"
            source.write_text("Test novel", encoding="utf-8")
            output = Path(directory) / "novel.epub"
            bridge.command({"kind": "start", "input": str(source), "output": str(output), "scrollInterval": 0.2, "sweeps": 1})
            self.assertTrue(idle.wait(3))
            self.assertEqual(received[0].chunk_limit, 275000)
            self.assertEqual(received[0].remaining_chinese_limit, 50)
            self.assertEqual(received[0].get_scroll_interval(), 0.2)
            self.assertEqual(received[0].get_required_sweeps(), 1)
            self.assertEqual(received[1:], [source, output])
            bridge.command({"kind": "settings", "scrollInterval": 1.2})
            self.assertEqual(received[0].get_scroll_interval(), 1.2)

    def test_invalid_live_interval_preserves_settings(self):
        emitted = []
        bridge = Bridge(lambda kind, value: emitted.append(kind))
        for value in (0, 6, float("nan"), None, "invalid"):
            bridge.command({"kind": "settings", "scrollInterval": value})
            self.assertEqual(bridge.scroll_interval, 0.5)
        self.assertEqual(emitted, ["settings_error"] * 5)

    def test_invalid_scroll_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.txt"
            source.touch()
            message = {"kind": "start", "input": str(source), "output": str(Path(directory) / "book.epub")}
            bridge = Bridge(lambda *args: None)
            for settings in ({"scrollInterval": 0}, {"scrollInterval": float("nan")}, {"sweeps": 0}, {"sweeps": 1.5}):
                with self.subTest(settings=settings), self.assertRaises(ValueError):
                    bridge.command({**message, **settings})

    def test_zero_chinese_finishes_before_scroll(self):
        class Page:
            def locator(self, selector):
                return self

            def inner_text(self, **kwargs):
                return "Translated chapter. " * 20

            def wait_for_timeout(self, milliseconds):
                pass

            def evaluate(self, *args):
                raise AssertionError("Already translated page should not scroll")

        result = wait_for_translation(Page(), 100, threading.Event(), lambda message: None, lambda: False, lambda: 0.5, lambda: 2)
        self.assertIn("Translated chapter", result)
        self.assertFalse(validate_translated_chunk("\u4e2d" * 100, "")[0])
        self.assertFalse(validate_translated_chunk("\u4e2d" * 100, "tiny")[0])

    def test_cancel_during_zero_chinese_check(self):
        cancel = threading.Event()

        class Page:
            def locator(self, selector):
                return self

            def inner_text(self, **kwargs):
                return "Translated chapter"

            def wait_for_timeout(self, milliseconds):
                cancel.set()

        with self.assertRaises(InterruptedError):
            wait_for_translation(Page(), 100, cancel, lambda message: None, lambda: False, lambda: 0.5, lambda: 2)


if __name__ == "__main__":
    unittest.main()