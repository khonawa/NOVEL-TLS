import queue
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from novel_translator.chrome import _ActiveClock, wait_for_translation
from novel_translator.translation import TranslationWatchdog, validate_translated_chunk
from novel_translator.pipeline import PipelineContext, TranslationRunner


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
        self.resets = 0

    def locator(self, selector):
        return self

    def inner_text(self, **kwargs):
        return self.text()

    def evaluate(self, script, argument=None):
        if argument is not None:
            self.scrolls.append((self.clock.now(), argument['cleanup']))
            return {'bottom': True, 'targeted': False}
        if 'scrollTo' in script:
            self.resets += 1
        return True

    def reload(self, **kwargs):
        self.reloads.append(self.clock.now())

    def wait_for_timeout(self, milliseconds):
        self.clock.sleep(milliseconds / 1000)


class WatchdogTests(unittest.TestCase):
    def test_startup_progress_and_stall(self):
        dog = TranslationWatchdog(1000, 0)
        self.assertIsNone(dog.observe(1000, 59)[0])
        self.assertIsNone(dog.observe(900, 60)[0])
        self.assertIsNone(dog.observe(899, 119)[0])
        self.assertEqual(dog.observe(899, 120)[0], 'reactivate')

    def test_observed_rollback_requires_spaced_confirmation(self):
        dog = TranslationWatchdog(46341, 0)
        dog.observe(12113, 35)
        self.assertIsNone(dog.observe(46341, 45)[0])
        self.assertIsNone(dog.observe(46341, 45.5)[0])
        self.assertIsNone(dog.observe(46341, 46)[0])
        self.assertEqual(dog.observe(46341, 47), ('reactivate', 'confirmed rollback'))

    def test_transient_rollback_and_minor_prior_progress_do_not_trigger(self):
        dog = TranslationWatchdog(1000, 0)
        for count, now in ((700, 10), (1000, 11), (700, 12), (1000, 13), (1000, 14)):
            self.assertIsNone(dog.observe(count, now)[0])
        dog = TranslationWatchdog(1000, 0)
        for count, now in ((990, 1), (1000, 2), (1000, 3), (1000, 4)):
            self.assertIsNone(dog.observe(count, now)[0])

    def test_cooldown_reload_reset_and_exhaustion(self):
        dog = TranslationWatchdog(1000, 0)
        self.assertEqual(dog.observe(1000, 60)[0], 'reactivate')
        self.assertIsNone(dog.observe(1000, 79)[0])
        self.assertEqual(dog.observe(1000, 80)[0], 'reload')
        dog.reset_page(80)
        self.assertEqual((dog.best, dog.rollback_samples), (1000, 0))
        self.assertIsNone(dog.observe(1000, 139)[0])
        self.assertEqual(dog.observe(1000, 140)[0], 'exhausted')

    def test_reactivation_improvement_cancels_pending_reload(self):
        dog = TranslationWatchdog(1000, 0)
        dog.observe(1000, 60)
        dog.cooldown_until = 80
        self.assertIsNone(dog.observe(700, 70)[0])
        self.assertIsNone(dog.observe(700, 80)[0])
        self.assertIsNone(dog.observe(700, 129)[0])
        self.assertEqual(dog.observe(700, 130)[0], 'reload')

    def run_page(self, text, retry=None):
        clock = FakeClock()
        page = FakePage(clock, lambda: text(clock, page))
        reports = []
        actions = []
        def reactivate():
            actions.append(clock.now())
            if retry:
                retry(clock, page)
            return True
        with patch('novel_translator.chrome.time.monotonic', clock.now), patch('novel_translator.chrome.time.sleep', clock.sleep):
            try:
                result = wait_for_translation(page, 1000, threading.Event(), reports.append, reactivate, lambda: 0.5, lambda: 1)
            except TimeoutError:
                result = None
        return clock, page, reports, actions, result

    def test_scrolls_cannot_hide_stall_and_recovery_is_sequential(self):
        clock, page, reports, actions, result = self.run_page(lambda *_: '中' * 1000)
        self.assertIsNone(result)
        self.assertEqual(len(actions), 1)
        self.assertEqual(len(page.reloads), 1)
        self.assertEqual(page.resets, 1)
        self.assertGreaterEqual(page.reloads[0] - actions[0], 20)
        self.assertFalse(any(actions[0] <= t < page.reloads[0] for t, _ in page.scrolls))
        self.assertFalse(next(cleanup for t, cleanup in page.scrolls if t >= page.reloads[0]))
        self.assertLess(clock.now(), 145)
        self.assertTrue(any('best=1,000' in message and 'action=exhausted' in message for message in reports))

    def test_success_after_reactivation_or_reload_still_requires_stability(self):
        for reload in (False, True):
            with self.subTest(reload=reload):
                _, page, _, actions, result = self.run_page(
                    lambda clock, page: 'English translation. ' * 100 if (bool(page.reloads) if reload else clock.now() >= 65) else '中' * 1000)
                self.assertTrue(validate_translated_chunk('中' * 1000, result, 0)[0])
                self.assertEqual(len(actions), 1)
                self.assertEqual(len(page.reloads), int(reload))

    def test_blank_page_does_not_count_as_progress_or_success(self):
        _, page, _, actions, result = self.run_page(lambda *_: '')
        self.assertIsNone(result)
        self.assertEqual((len(actions), len(page.reloads)), (1, 1))
        for candidate in ('', 'tiny', '中' * 1000):
            self.assertFalse(validate_translated_chunk('中' * 1000, candidate, 0)[0])

    def test_pause_excludes_time_and_cancel_interrupts_pause(self):
        clock = FakeClock()
        pause = threading.Event()
        cancel = threading.Event()
        active = _ActiveClock(pause, cancel)
        def resume(seconds):
            clock.sleep(100)
            pause.set()
        with patch('novel_translator.chrome.time.monotonic', clock.now), patch('novel_translator.chrome.time.sleep', resume):
            self.assertEqual(active.now(), 0)
            clock.sleep(60)
            self.assertEqual(active.now(), 60)
        pause.clear()
        def stop(seconds):
            cancel.set()
        with patch('novel_translator.chrome.time.sleep', stop):
            with self.assertRaises(InterruptedError):
                active.now()

    def test_cancel_during_reactivation_prevents_reload(self):
        clock = FakeClock()
        page = FakePage(clock, lambda: '中' * 1000)
        cancel = threading.Event()
        with patch('novel_translator.chrome.time.monotonic', clock.now), patch('novel_translator.chrome.time.sleep', clock.sleep):
            with self.assertRaises(InterruptedError):
                wait_for_translation(page, 1000, cancel, lambda _: None, lambda: cancel.set(), lambda: .5, lambda: 1)
        self.assertEqual(page.reloads, [])

    def test_pause_during_watchdog_wait_does_not_spend_budget(self):
        clock = FakeClock()
        pause = threading.Event()
        pause.set()
        cancel = threading.Event()
        page = FakePage(clock, lambda: '中' * 1000)
        actions = []
        paused = False
        def sleep(seconds):
            nonlocal paused
            if not paused and clock.now() >= 30:
                paused = True
                pause.clear()
            if not pause.is_set() and seconds == 0.1:
                clock.sleep(300)
                pause.set()
            else:
                clock.sleep(seconds)
        def retry():
            actions.append(clock.now())
            cancel.set()
        with patch('novel_translator.chrome.time.monotonic', clock.now), patch('novel_translator.chrome.time.sleep', sleep):
            with self.assertRaises(InterruptedError):
                wait_for_translation(page, 1000, cancel, lambda _: None, retry, lambda: .5, lambda: 1, pause_event=pause)
        self.assertGreaterEqual(actions[0], 360)
        self.assertLess(actions[0], 362)
        self.assertEqual(page.reloads, [])

    def test_reload_failure_is_reported_and_propagated(self):
        clock = FakeClock()
        page = FakePage(clock, lambda: '中' * 1000)
        reports = []
        with patch('novel_translator.chrome.time.monotonic', clock.now), patch('novel_translator.chrome.time.sleep', clock.sleep), patch.object(page, 'reload', side_effect=RuntimeError('Target closed')):
            with self.assertRaisesRegex(RuntimeError, 'Target closed'):
                wait_for_translation(page, 1000, threading.Event(), reports.append, lambda: False, lambda: .5, lambda: 1)
        self.assertTrue(any('reactivation returned False' in message for message in reports))
        self.assertTrue(any('reload failed (RuntimeError): Target closed' in message for message in reports))

    def test_browser_failure_evidence(self):
        page = FakePage(FakeClock(), lambda: '')
        reports = []
        with patch.object(page, 'inner_text', side_effect=RuntimeError('Target crashed')):
            with self.assertRaisesRegex(RuntimeError, 'Target crashed'):
                wait_for_translation(page, 1000, threading.Event(), reports.append, lambda: True, lambda: .5, lambda: 1)
        self.assertIn('Target crashed', reports[0])

    def test_pipeline_exhaustion_preserves_completed_checkpoints_and_encoding(self):
        # All I/O mocked: no browser, server, or temporary directory is created.
        pause = threading.Event()
        pause.set()
        ctx = PipelineContext(queue.Queue(), threading.Event(), pause, lambda: .5, lambda: 1,
                              request_validation=lambda *_: 'retry', source_encoding='gbk', source_digest='digest')
        completed = {1: 'saved translation'}
        browser = MagicMock()
        page = browser.chromium.launch_persistent_context.return_value.pages[0]
        page.is_closed.return_value = False
        clock = FakeClock()
        with ExitStack() as stack:
            def mock(name, **kwargs):
                return stack.enter_context(patch('novel_translator.pipeline.' + name, **kwargs))
            mock('tempfile.mkdtemp', return_value=str(Path.cwd() / 'unused-watchdog-work'))
            mock('shutil.rmtree')
            mock('seed_chrome_profile')
            mock('make_chunk_server', return_value=(MagicMock(), {}, 1234))
            mock('threading.Thread')
            mock('sync_playwright').return_value.__enter__.return_value = browser
            read = mock('read_text_file', return_value='source')
            mock('split_text', return_value=['中' * 1000, '中' * 1000])
            mock('load_checkpoint', return_value=completed.copy())
            mock('initialize_checkpoint')
            save = mock('save_checkpoint_chunk')
            epub = mock('make_epub')
            mock('wait_for_auto_translation', return_value=True)
            wait = mock('wait_for_translation', side_effect=TimeoutError('recovery exhausted'))
            mock('time.monotonic', side_effect=clock.now)
            mock('time.sleep', side_effect=clock.sleep)
            TranslationRunner(ctx).run(Path('source.txt'), Path('output.epub'))
        read.assert_called_once_with(Path('source.txt'), 'gbk', 'digest')
        self.assertEqual(wait.call_count, 3)
        save.assert_not_called()
        epub.assert_not_called()
        self.assertEqual(completed, {1: 'saved translation'})
        events = list(ctx.events.queue)
        self.assertTrue(any(event.kind == 'incomplete' for event in events))


if __name__ == '__main__':
    unittest.main()
