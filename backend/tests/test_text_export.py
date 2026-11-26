"""Exact export bytes, same-frame delivery and idempotent retry through the Lua runtime."""
import unittest
from unittest.mock import Mock

from backend.storage.errors import StorageError
from backend.tests import test_execution_runtime as runtime
from backend.vision.text_export import format_snapshot


def block(text, x, y, score=.9):
    return dict(text=text, confidence=score, polygon=[dict(x=x+dx, y=y+dy)
                for dx, dy in [(0, 0), (10, 0), (10, 10), (0, 10)]])


class Layout(unittest.TestCase):
    def test_exact_half_overlap_low_confidence_and_permuted_source(self):
        blocks = [block('乙', 30, 5), block('丙', 0, 18, .2), block('甲', 0, 0)]
        self.assertEqual(format_snapshot(blocks).encode(), bytes.fromhex('e794b220e4b9990d0ae4b8990d0a'))

    def test_stable_ties_unicode_whitespace_punctuation_and_inline_newlines(self):
        blocks = [block('A', 0, 0), block('A', 0, 0), block(' 12', 10, 0),
                  block('e\u0301。\r一\n二\r\n三\r\n', 20, 0), block('', 0, 20), block('한글', 0, 40)]
        self.assertEqual(format_snapshot(blocks).encode(), 'A A 12 e\u0301。\r\n一\r\n二\r\n三\r\n한글\r\n'.encode())
        self.assertEqual(format_snapshot([block('', 0, 0)]), '')
        self.assertEqual(format_snapshot([block(' ', 0, 0), block('x', 10, 0)]), ' x\r\n')


class ScriptDelivery(unittest.TestCase):
    setUp = runtime.ExecutionRuntime.setUp
    cleanup_roots = runtime.ExecutionRuntime.cleanup_roots
    save = runtime.ExecutionRuntime.save
    start = runtime.ExecutionRuntime.start
    await_state = runtime.ExecutionRuntime.await_state

    def test_cached_duplicate_blank_and_unacknowledged_commit(self):
        blocks = [block('A', 0, 0), block('A', 0, 0)]
        self.vision.run_sync.side_effect = [{'blocks': blocks}, {'blocks': blocks}, {'blocks': []}]
        self.capture.capture = Mock(wraps=self.capture.capture)
        dispatch = self.controller.text_output = Mock(side_effect=[{}, StorageError('INTERNAL_ERROR', 'timeout',
            details={'timeout_ms': 100}), {'bytes_written': 5}, {'bytes_written': 5}, {}])
        script = self.save('local image=capture_screen(); ocr(image); ocr(image); ocr(image); release_image(image)')
        root = self.start(script, output_token='fixture', client_run_id='one')
        self.assertEqual(root, self.start(script, output_token='fixture', client_run_id='one'))
        self.await_state(root, 'completed')
        self.assertEqual(self.capture.capture.call_count, 1)
        self.assertEqual([c.args[0] for c in dispatch.call_args_list], ['bind', 'commit', 'commit', 'commit', 'close'])
        first, retry, second = dispatch.call_args_list[1:4]
        self.assertEqual(first, retry)
        self.assertNotEqual(first.kwargs['action_id'], second.kwargs['action_id'])
        self.assertEqual(first.kwargs['text'], second.kwargs['text'])
        self.assertEqual(first.kwargs['text'].encode(), b'A A\r\n')
        self.assertEqual(len([e for e in self.events if e[0] == 'text_exported']), 2)

    def test_write_error_terminates_root_without_replaying(self):
        self.vision.run_sync.return_value = {'blocks': [block('B', 0, 0)]}
        dispatch = self.controller.text_output = Mock(side_effect=[{}, StorageError('STORAGE_ERROR', 'disk full'), {}])
        root = self.start(self.save('ocr(); ocr()'), output_token='fixture')
        state = self.await_state(root, 'failed')
        self.assertEqual(state['error']['code'], 'STORAGE_ERROR')
        self.assertEqual([c.args[0] for c in dispatch.call_args_list], ['bind', 'commit', 'close'])
        self.assertFalse(any(name == 'text_exported' for name, _ in self.events))
