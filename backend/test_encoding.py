import codecs
import hashlib
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge import Bridge
from novel_translator.encoding import decode_source, ENCODINGS, PREVIEW_LIMIT
from novel_translator.text_processing import read_text_file, source_fingerprint, split_text
from novel_translator.checkpoint import checkpoint_dir_for, initialize_checkpoint, save_checkpoint_chunk


class EncodingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / 'source.txt'

    def decode(self, raw, encoding='auto'):
        self.source.write_bytes(raw)
        result = decode_source(self.source, encoding)
        self.assertEqual(self.source.read_bytes(), raw)
        return result

    def test_all_manual_encodings_preserve_whitespace(self):
        text = '中文文本'
        text += '\r\n  text\t123\n'
        for encoding in ENCODINGS:
            with self.subTest(encoding=encoding):
                actual, metadata = self.decode(text.encode(encoding), encoding)
                self.assertEqual(actual, text)
                self.assertEqual(metadata['encoding'], encoding)
                self.assertEqual(read_text_file(self.source, encoding, metadata['digest']), text)

    def test_boms_and_unicode_autodetection(self):
        text = 'Chapter 1\r\n中文文本\n'
        for bom, encoding in ((codecs.BOM_UTF8, 'utf-8'), (codecs.BOM_UTF16_LE, 'utf-16-le'),
                              (codecs.BOM_UTF16_BE, 'utf-16-be'), (codecs.BOM_UTF32_LE, 'utf-32-le'),
                              (codecs.BOM_UTF32_BE, 'utf-32-be')):
            with self.subTest(encoding=encoding):
                actual, meta = self.decode(bom + text.encode(encoding))
                self.assertEqual((actual, meta['encoding']), (text, encoding))
                with self.assertRaises(ValueError):
                    decode_source(self.source, 'big5')
        for encoding in ('utf-16-le', 'utf-16-be', 'utf-32-le', 'utf-32-be'):
            self.assertEqual(self.decode(('English chapter\n' * 10).encode(encoding))[1]['encoding'], encoding)

    def test_utf8_ascii_and_uncertain_legacy(self):
        self.assertEqual(self.decode('中文'.encode())[1]['encoding'], 'utf-8')
        self.assertTrue(self.decode(b'ASCII only')[1]['warnings'])
        for encoding in ('gb18030', 'big5'):
            self.assertTrue(self.decode('中文文本'.encode(encoding))[1]['warnings'])

    def test_invalid_full_file_and_binary(self):
        for raw in (b'', b'  \n', b'\x00\x01\x02', b'PK\x03\x04test', b'%PDF-1.4 test', b'hello\x01'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.decode(raw)
        with self.assertRaises(ValueError):
            self.decode(b'a' * 100000 + b'\xff', 'utf-8')
        with self.assertRaises(ValueError):
            self.decode(codecs.BOM_UTF16_LE + b'a', 'auto')
        with self.assertRaises(ValueError):
            self.decode(b'text', 'latin1')

    def test_preview_boundaries_and_changes(self):
        text = 'a' * 65535 + '中文' * 5000
        _, meta = self.decode(text.encode())
        self.assertEqual(len(meta['preview']), PREVIEW_LIMIT)
        self.assertTrue(meta['truncated'])
        self.assertEqual(meta['characters'], len(text))
        self.source.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'Source changed'):
            read_text_file(self.source, meta['encoding'], meta['digest'])

    def test_mojibake_is_warned_not_repaired(self):
        text = 'Ã¼ ÖÐ ???? \ufffd'
        actual, meta = self.decode(text.encode())
        self.assertEqual(actual, text)
        self.assertTrue(any('mojibake' in warning for warning in meta['warnings']))

    def test_bomless_chinese_utf16_is_detected(self):
        text = '第一章 他走进了房间，看着窗外的雨。\n' * 50
        for encoding in ('utf-16-le', 'utf-16-be'):
            with self.subTest(encoding=encoding):
                actual, meta = self.decode(text.encode(encoding))
                self.assertEqual((actual, meta['encoding']), (text, encoding))

    def test_text_starting_with_mz_is_not_binary(self):
        actual, _ = self.decode(b'MZ is the start of this English line.')
        self.assertTrue(actual.startswith('MZ'))

    def test_single_weak_mojibake_character_is_not_warned(self):
        _, meta = self.decode('Jürgen Österreich ¼ cup'.encode())
        self.assertFalse(any('mojibake' in warning for warning in meta['warnings']))

    def test_legacy_chinese_autodetection_prefers_real_text(self):
        simplified = '他说你好吗？我们还没有到那里去，这个时候还是要回来的。\n' * 3
        traditional = '他說你好嗎？我們還沒有到那裡去，這個時候還是要回來的。\n' * 3
        for text, encoding in ((simplified, 'gb18030'), (traditional, 'big5')):
            with self.subTest(encoding=encoding):
                actual, meta = self.decode(text.encode(encoding))
                self.assertEqual(actual, text)
                self.assertEqual(meta['encoding'], encoding)

    def test_manual_selection_rereads_original(self):
        self.source.write_bytes('中文文本'.encode('gbk'))
        first = decode_source(self.source, 'gbk')
        second = decode_source(self.source, 'big5')
        self.assertNotEqual(first[0], second[0])
        self.assertEqual(first[1]['digest'], second[1]['digest'])

    def test_large_source_and_pipeline_use_confirmed_text(self):
        text = '中文文本\r\n' * 1000000
        _, meta = self.decode(text.encode('gb18030'), 'gb18030')
        self.assertEqual(meta['characters'], 6000000)
        self.assertEqual(len(meta['preview']), PREVIEW_LIMIT)
        from novel_translator.pipeline import PipelineContext, TranslationRunner
        events = queue.Queue()
        context = PipelineContext(events=events, cancel_event=threading.Event(),
                                  pause_event=threading.Event(), get_scroll_interval=lambda: .5,
                                  get_required_sweeps=lambda: 2, request_validation=lambda *_: 'accept',
                                  source_encoding=meta['encoding'], source_digest=meta['digest'])
        work = self.source.parent / 'work'
        work.mkdir()
        with patch('novel_translator.pipeline.tempfile.mkdtemp', return_value=str(work)), \
             patch('novel_translator.pipeline.initialize_checkpoint', side_effect=RuntimeError('stop before Chrome')) as initialize:
            TranslationRunner(context).run(self.source, self.source.with_suffix('.epub'))
        self.assertEqual(initialize.call_args.args[1], source_fingerprint(f'275000\0{text}'))
        self.assertEqual(initialize.call_args.args[2], len(split_text(text, 275000)))

    def test_cancellation(self):
        self.source.write_bytes(b'a' * 100000)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(InterruptedError):
            decode_source(self.source, cancel=cancel)

    def test_bridge_preview_success_error_and_control_responsiveness(self):
        emitted = queue.Queue()
        bridge = Bridge(lambda kind, value: emitted.put((kind, value)))
        self.source.write_bytes('中文'.encode('gbk'))
        bridge.command({'kind': 'preview', 'requestId': 1, 'input': str(self.source), 'encoding': 'gbk'})
        kind, result = emitted.get(timeout=3)
        self.assertEqual((kind, result['requestId'], result['encoding']), ('preview_result', 1, 'gbk'))
        bridge.command({'kind': 'preview', 'requestId': 2, 'input': str(self.source), 'encoding': 'utf-8'})
        self.assertIn('error', emitted.get(timeout=3)[1])
        entered = threading.Event()
        release = threading.Event()
        def slow(*args, **kwargs):
            entered.set()
            release.wait(3)
            return '', {'encoding': 'utf-8'}
        with patch('bridge.decode_source', side_effect=slow):
            bridge.command({'kind': 'preview', 'requestId': 3, 'input': str(self.source)})
            self.assertTrue(entered.wait(3))
            bridge.command({'kind': 'pause'})
            self.assertFalse(bridge.pause.is_set())
            bridge.command({'kind': 'cancel_preview'})
            release.set()
            bridge.preview_thread.join(3)
        self.assertTrue(emitted.empty())

    def test_json_lines_preview_round_trip(self):
        import json
        import subprocess
        import sys
        self.source.write_bytes(codecs.BOM_UTF32_BE + '中文文本'.encode('utf-32-be'))
        process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('bridge.py'))],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding='utf-8')
        try:
            self.assertEqual(json.loads(process.stdout.readline())['kind'], 'ready')
            process.stdin.write(json.dumps({'kind': 'preview', 'requestId': 71, 'input': str(self.source)}) + '\n')
            process.stdin.flush()
            response = queue.Queue()
            reader = threading.Thread(target=lambda: response.put(process.stdout.readline()), daemon=True)
            reader.start()
            result = json.loads(response.get(timeout=10))
            self.assertEqual(result['kind'], 'preview_result')
            self.assertEqual(result['value']['requestId'], 71)
            self.assertEqual(result['value']['encoding'], 'utf-32-be')
            self.assertEqual(result['value']['preview'], '中文文本')
        finally:
            process.communicate(timeout=10)

    def test_selected_encoding_reaches_runner_and_checkpoint(self):
        text = '中文文本' * 100
        self.source.write_bytes(text.encode('gbk'))
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        output = self.source.with_suffix('.epub')
        received = []
        class Runner:
            def __init__(self, context):
                received.append(context)
            def run(self, source, output):
                pass
        bridge = Bridge(lambda *args: received.append(args), Runner)
        message = {'input': str(self.source), 'output': str(output), 'encoding': 'gbk', 'digest': digest}
        bridge.command({'kind': 'start', **message})
        bridge.thread.join(3)
        self.assertEqual(received[0].source_encoding, 'gbk')
        self.assertEqual(received[0].source_digest, digest)
        progress = checkpoint_dir_for(output)
        chunks = split_text(text, 275000)
        initialize_checkpoint(progress, source_fingerprint(f'275000\0{text}'), len(chunks))
        save_checkpoint_chunk(progress, 1, 'English text')
        bridge.command({'kind': 'checkpoint_status', **message})
        self.assertTrue(received[-1][1]['exists'])


if __name__ == '__main__':
    unittest.main()
