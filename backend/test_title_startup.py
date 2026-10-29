import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from novel_translator.title import await_ready, launch_title_worker, suggest_title


class TitleStartupTests(unittest.TestCase):
    def test_readiness_precedes_request_and_is_not_result(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            source = Path(directory) / 'synthetic.txt'
            source.touch()
            process = Mock()
            process.poll.return_value = 0
            process.returncode = 0
            ready = threading.Event()

            def handshake(_):
                process.communicate.assert_not_called()
                ready.set()
                return b'{"kind":"ready","version":1}\n'

            def communicate(**kwargs):
                self.assertTrue(ready.is_set())
                self.assertEqual(json.loads(kwargs['input']), {'kind': 'translate', 'title': 'synthetic'})
                return b'"Synthetic English Title"', b''

            process.stdout.readline.side_effect = handshake
            process.communicate.side_effect = communicate
            with patch('novel_translator.title.launch_title_worker', return_value=process):
                self.assertEqual(suggest_title(source, threading.Event()), 'Synthetic English Title')

    def test_invalid_and_exit_readiness_never_send_title(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            work = Path(directory)
            for line in [b'', b'not json\n', b'[]\n', b'{"kind":"result"}\n', b'{"kind":"ready","version":2}\n', b'\xff\n']:
                with self.subTest(line=line):
                    process = Mock(stdout=io.BytesIO(line))
                    with self.assertRaisesRegex(ValueError, 'readiness'):
                        await_ready(process, threading.Event(), time.monotonic() + 3, work)
                    process.communicate.assert_not_called()

    def test_cancel_at_readiness_never_sends_request(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            cancel = threading.Event()
            process = Mock()

            def handshake(_):
                cancel.set()
                return b'{"kind":"ready","version":1}\n'

            process.stdout.readline.side_effect = handshake
            with self.assertRaises(InterruptedError):
                await_ready(process, cancel, time.monotonic() + 3, Path(directory))
            process.communicate.assert_not_called()

    def test_real_worker_cancel_after_ready_cleans_process_and_work(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            source = Path(directory) / 'synthetic.txt'
            source.touch()
            cancel = threading.Event()
            launched = []

            def launch(work):
                process = launch_title_worker(work)
                launched.append((process, work))
                return process

            def ready_then_cancel(process, event, deadline, work):
                await_ready(process, event, deadline, work)
                cancel.set()

            with patch('novel_translator.title.launch_title_worker', side_effect=launch), patch('novel_translator.title.await_ready', side_effect=ready_then_cancel):
                with self.assertRaises(InterruptedError):
                    suggest_title(source, cancel)
            process, work = launched[0]
            self.assertIsNotNone(process.poll())
            self.assertFalse(work.exists())
            self.assertTrue(process.stdin.closed)
            self.assertTrue(process.stdout.closed)
            self.assertTrue(process.stderr.closed)

    def test_real_production_worker_ready_then_eof_without_chrome(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            work = Path(directory).resolve()
            process = launch_title_worker(work)
            try:
                await_ready(process, threading.Event(), time.monotonic() + 15, work)
                self.assertEqual((work / 'stage').read_text(), 'waiting for title request')
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stderr.decode('utf-8', errors='replace'))
                self.assertEqual(json.loads(stdout), None)
                self.assertEqual(stderr, b'')
                self.assertFalse((work / 'profile' / 'Default' / 'History').exists())
            finally:
                if process.poll() is None:
                    import subprocess
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=10)
                    process.communicate(timeout=10)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()


if __name__ == '__main__':
    unittest.main()
