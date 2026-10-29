import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from bridge import Bridge
from novel_translator.constants import sanitize_filename
from novel_translator.title import validate_title, translate_page, suggest_title
from novel_translator.epub_builder import make_epub


class TitleTests(unittest.TestCase):
    def test_native_failure_is_not_an_untranslated_title_stall(self):
        page = Mock()
        page._novel_chrome_pid = 123
        page.locator.return_value.inner_text.return_value = '山'
        desktop = Mock()
        desktop.return_value.window.return_value.child_window.return_value.exists.return_value = True
        retry = Mock()
        with patch('novel_translator.chrome._get_desktop_class', return_value=desktop):
            with self.assertRaisesRegex(RuntimeError, 'could not be translated'):
                translate_page(page, '山', threading.Event(), retry)
        retry.assert_not_called()

    def test_native_title_activation_requires_english_even_with_retry_button(self):
        from novel_translator.chrome import automate_chrome_translation
        page = Mock()
        page.title.return_value = 'Synthetic title'
        page.viewport_size = {'width': 800, 'height': 600}
        desktop = Mock()
        chrome = desktop.return_value.window.return_value
        with patch('novel_translator.chrome._get_desktop_class', return_value=desktop):
            self.assertTrue(automate_chrome_translation(page, Mock(), 123, ensure_english=True))
        page.mouse.click.assert_called_once()
        self.assertTrue(any(call.kwargs.get('title_re') == r'(?i)^English$' for call in chrome.child_window.call_args_list))

    def test_slow_native_activation_does_not_reset_translation_budget(self):
        page = Mock()
        page.locator.return_value.inner_text.return_value = '山'
        retry = Mock(return_value=True)
        with patch('novel_translator.title.time.monotonic', side_effect=[0, 5, 5, 26]):
            with self.assertRaisesRegex(RuntimeError, 'stalled'):
                translate_page(page, '山', threading.Event(), retry)
        retry.assert_called_once()

    def test_worker_failure_reason_survives_cleanup_and_bridge(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            source = Path(directory) / 'synthetic.txt'
            source.touch()
            process = Mock()
            process.stdout.readline.return_value = b'{"kind":"ready","version":1}\n'
            process.poll.return_value = 1
            process.returncode = 1
            process.communicate.return_value = (b'{"error":"failed"}', b'private stderr')
            events = []
            bridge = Bridge(lambda *args: events.append(args))
            with patch('novel_translator.title.subprocess.Popen', return_value=process):
                bridge.run_title({'input': str(source), 'requestId': 9}, threading.Event())
            self.assertIn('could not be translated', events[-1][1]['error'])
            self.assertNotIn('private', events[-1][1]['error'])

    def test_server_idle_preconnect_does_not_block_title_or_cleanup(self):
        import socket
        from urllib.request import urlopen
        from novel_translator.pipeline import make_chunk_server
        server, pages, port = make_chunk_server()
        pages['title.html'] = b'<p id="novel-title">Synthetic title</p>'
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        idle = socket.create_connection(('127.0.0.1', port))
        shutdown = None
        try:
            with urlopen(f'http://127.0.0.1:{port}/title.html', timeout=1) as response:
                self.assertEqual(response.read(), pages['title.html'])
            shutdown = threading.Thread(target=server.shutdown, daemon=True)
            shutdown.start()
            shutdown.join(2)
            self.assertFalse(shutdown.is_alive(), 'Idle Chrome preconnect blocked server shutdown')
        finally:
            idle.close()
            server.shutdown()
            serving.join(2)
            if shutdown:
                shutdown.join(2)
            server.server_close()

    def test_one_character_title_success_requires_stable_english(self):
        page = Mock()
        page.locator.return_value.inner_text.side_effect = ['山', 'Mountain', 'Mountain', 'Mountain']
        with patch('novel_translator.title.time.monotonic', side_effect=[0, 0, 0, 1, 1, 2, 2, 3]):
            self.assertEqual(translate_page(page, '山', threading.Event(), Mock()), 'Mountain')

    def test_cancellation_during_native_activation_is_not_failure(self):
        page = Mock()
        page.locator.return_value.inner_text.return_value = '山'
        cancel = threading.Event()
        def activate():
            cancel.set()
            return False
        with patch('novel_translator.title.time.monotonic', side_effect=[0, 5, 5]):
            with self.assertRaises(InterruptedError):
                translate_page(page, '山', cancel, activate)

    def test_one_character_auto_translation_detected(self):
        from novel_translator.chrome import wait_for_auto_translation
        page = Mock()
        page.evaluate.return_value = False
        page.locator.return_value.inner_text.return_value = 'Mountain'
        with patch('novel_translator.chrome.time.monotonic', side_effect=[0, 0, 6]):
            self.assertTrue(wait_for_auto_translation(page, 1, threading.Event()))

    def test_native_menu_click_uses_page_coordinates(self):
        from novel_translator.chrome import automate_chrome_translation
        page = Mock()
        page.title.return_value = 'Synthetic title'
        page.viewport_size = {'width': 800, 'height': 600}
        desktop = Mock()
        chrome = desktop.return_value.window.return_value
        chrome.child_window.return_value.exists.return_value = False
        chrome.rectangle.return_value = Mock(left=1200, top=200, width=lambda: 800, height=lambda: 600)
        with patch('novel_translator.chrome._get_desktop_class', return_value=desktop):
            self.assertTrue(automate_chrome_translation(page, Mock(), 123))
        page.mouse.click.assert_called_once_with(400, 300, button='right')
        chrome.click_input.assert_not_called()

    def test_unchanged_title_retries_even_with_translated_page_marker(self):
        page = Mock()
        page.evaluate.return_value = True
        page.locator.return_value.inner_text.side_effect = ['中文', 'A New World', 'A New World', 'A New World']
        retry = Mock(return_value=True)
        with patch('novel_translator.title.time.monotonic', side_effect=[0, 1, 5, 5, 6, 6, 7, 7, 8]):
            self.assertEqual(translate_page(page, '中文', threading.Event(), retry), 'A New World')
        retry.assert_called_once()

    def test_failed_short_title_trigger_fails_without_waiting_full_deadline(self):
        page = Mock()
        page.locator.return_value.inner_text.return_value = '中文'
        with patch('novel_translator.title.time.monotonic', side_effect=[0, 5, 5]):
            with self.assertRaisesRegex(RuntimeError, 'could not activate'):
                translate_page(page, '中文', threading.Event(), Mock(return_value=False))

    def test_empty_body_is_not_auto_translation_progress(self):
        from novel_translator.chrome import wait_for_auto_translation
        page = Mock()
        page.evaluate.return_value = False
        page.locator.return_value.inner_text.return_value = ''
        with patch('novel_translator.chrome.time.monotonic', side_effect=[0, 0, 6]):
            self.assertFalse(wait_for_auto_translation(page, 10, threading.Event()))

    def test_timeout_reports_only_allowlisted_stage(self):
        import subprocess
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            source = Path(directory) / 'synthetic.txt'
            source.touch()
            process = Mock()
            process.poll.return_value = 0
            process.stdout.readline.return_value = b'{"kind":"ready","version":1}\n'
            def launch(args, **kwargs):
                from novel_translator.title import report_stage, last_stage
                work = Path(args[-1])
                report_stage(work, 'private title')
                self.assertEqual(last_stage(work, 'unknown'), 'unknown')
                report_stage(work, 'closing Chrome')
                return process
            process.communicate.side_effect = subprocess.TimeoutExpired('worker', .1)
            with patch('novel_translator.title.subprocess.Popen', side_effect=launch), patch('novel_translator.title.time.monotonic', side_effect=[0, 0, 0, 91]):
                with self.assertRaisesRegex(TimeoutError, 'during closing Chrome'):
                    suggest_title(source, threading.Event())

    def test_titles(self):
        self.assertEqual(validate_title('中文', 'A New World'), 'A New World')
        self.assertEqual(validate_title('中文', 'CON.txt'), '_CON.txt')
        for value in ('', '中文', '123', 'line\nbreak', 'x' * 501, "This site can't be reached", 'Bad \ufffd title'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_title('中文', value)

    def test_windows_names(self):
        for value, expected in [('CON', '_CON'), ('con.txt', '_con.txt'), ('LPT¹.foo', '_LPT¹.foo'), ('a:b?. ', 'a_b_'), ('...', 'untitled')]:
            self.assertEqual(sanitize_filename(value), expected)
        for value in ('a' * 500, '😀' * 200, 'CON.' + 'a' * 300):
            self.assertLessEqual(len(sanitize_filename(value).encode('utf-16-le')), 360)

    def test_page_success_and_cancel(self):
        page = Mock()
        page.locator.return_value.inner_text.return_value = 'A New World'
        with patch('novel_translator.chrome.wait_for_auto_translation', return_value=False):
            retry = Mock()
            self.assertEqual(translate_page(page, '中文', threading.Event(), retry), 'A New World')
            retry.assert_not_called()
            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(InterruptedError):
                translate_page(page, '中文', cancel, retry)

    def test_invalid_page_times_out(self):
        page = Mock()
        page.locator.return_value.inner_text.return_value = '中文'
        with patch('novel_translator.chrome.wait_for_auto_translation', return_value=True), patch('novel_translator.title.time.monotonic', side_effect=[0, 1, 1, 26]):
            with self.assertRaisesRegex(RuntimeError, 'stalled'):
                translate_page(page, '中文', threading.Event(), Mock())

    def test_final_collision_and_metadata(self):
        from ebooklib import epub
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            output = Path(directory) / 'Chosen Title.epub'
            with patch('novel_translator.epub_builder._generate_text_cover', return_value=b'cover') as cover:
                make_epub('Chapter 1\nHello world', output, output.stem)
                cover.assert_called_once_with('Chosen Title', '#ffffff', '#0f131c')
            book = epub.read_epub(str(output))
            self.assertEqual(book.get_metadata('DC', 'title')[0][0], 'Chosen Title')
            original = output.read_bytes()
            with patch('novel_translator.epub_builder._generate_text_cover', return_value=b'cover'):
                with self.assertRaises(FileExistsError):
                    make_epub('Different content', output, 'Different')
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_bridge_concurrency_and_failure(self):
        events = []
        entered = threading.Event()
        release = threading.Event()
        def fake(source, cancel):
            entered.set()
            release.wait(2)
            raise ValueError('Synthetic failure')
        bridge = Bridge(lambda *args: events.append(args))
        with patch('novel_translator.title.suggest_title', side_effect=fake):
            bridge.command({'kind': 'suggest_title', 'input': 'synthetic.txt', 'requestId': 7})
            self.assertTrue(entered.wait(1))
            for kind in ('suggest_title', 'start', 'fix_epub'):
                with self.assertRaises(ValueError):
                    bridge.command({'kind': kind})
            bridge.command({'kind': 'cancel_title'})
            self.assertTrue(bridge.title_cancel.is_set())
            thread = bridge.title_thread
            release.set()
            thread.join(3)
        self.assertEqual(events[-1], ('title_result', {'requestId': 7, 'error': 'Synthetic failure'}))

    def test_resumed_final_collision_retains_checkpoint(self):
        import queue
        from contextlib import ExitStack
        from novel_translator.pipeline import PipelineContext, TranslationRunner
        from novel_translator.checkpoint import checkpoint_dir_for, initialize_checkpoint, save_checkpoint_chunk
        from novel_translator.text_processing import source_fingerprint
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            source = Path(directory) / 'source.txt'
            source.write_text('source', encoding='utf-8')
            output = Path(directory) / 'Chosen.epub'
            progress = checkpoint_dir_for(output)
            fingerprint = source_fingerprint('275000\0source')
            initialize_checkpoint(progress, fingerprint, 1)
            save_checkpoint_chunk(progress, 1, 'Saved translated chapter')
            pause = threading.Event()
            pause.set()
            ctx = PipelineContext(queue.Queue(), threading.Event(), pause, lambda: .5, lambda: 1, request_validation=lambda *_: 'accept')
            from novel_translator.epub_builder import make_epub as real_make
            def collide(text, destination, title, **kwargs):
                self.assertEqual(destination, output)
                self.assertEqual(text, 'Saved translated chapter')
                destination.write_bytes(b'concurrent book')
                real_make(text, destination, title, **kwargs)
            with ExitStack() as stack:
                stack.enter_context(patch('novel_translator.pipeline.sync_playwright'))
                stack.enter_context(patch('novel_translator.pipeline.make_epub', side_effect=collide))
                stack.enter_context(patch('novel_translator.epub_builder._generate_text_cover', return_value=b'cover'))
                TranslationRunner(ctx).run(source, output)
            self.assertEqual(output.read_bytes(), b'concurrent book')
            self.assertTrue(progress.is_dir())
            self.assertTrue(any(event.kind == 'error' for event in ctx.events.queue))
            self.assertFalse(any(event.kind == 'done' for event in ctx.events.queue))

    def test_subprocess_timeout_and_invalid_response(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            source = Path(directory) / 'synthetic.txt'
            source.touch()
            process = Mock()
            process.poll.return_value = 0
            process.returncode = 0
            process.stdout.readline.return_value = b'{"kind":"ready","version":1}\n'
            process.communicate.return_value = (b'""', b'')
            with patch('novel_translator.title.subprocess.Popen', return_value=process):
                with self.assertRaises(TimeoutError):
                    suggest_title(source, threading.Event(), timeout=0)
                with self.assertRaises(ValueError):
                    suggest_title(source, threading.Event())
                process.communicate.return_value = (b'"Synthetic English Title"', b'')
                self.assertEqual(suggest_title(source, threading.Event()), 'Synthetic English Title')

    def test_subprocess_cancel_cleanup(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            source = Path(directory) / 'synthetic.txt'
            source.write_text('unchanged')
            cancel = threading.Event()
            cancel.set()
            process = Mock()
            process.poll.return_value = None
            with patch('novel_translator.title.subprocess.Popen', return_value=process), patch('novel_translator.title.subprocess.run') as kill:
                with self.assertRaises(InterruptedError):
                    suggest_title(source, cancel)
                kill.assert_called_once()
                process.communicate.assert_called_once_with(timeout=10)
            self.assertEqual(source.read_text(), 'unchanged')


if __name__ == '__main__':
    unittest.main()
