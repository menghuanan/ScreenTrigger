"""Regressions at the P0 pipe and versioned configuration boundaries."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.config.service import _copy_file
from backend.ipc.envelope import MAX_FRAME_BYTES, MalformedFrameError, parse_frame
from backend.main import StdioTransport


class WireBoundaryTest(unittest.TestCase):
    def test_duplicate_nonfinite_and_non_string_discriminator(self):
        for fragment in ('{"value":1,"value":2}', '{"value":NaN}', '{"value":1e999}'):
            line = '{"protocol_version":1,"session_id":"s","type":"request","id":"u","command":"ping","params":' + fragment + '}'
            with self.subTest(fragment=fragment), self.assertRaises(MalformedFrameError):
                parse_frame(line)
        with self.assertRaises(MalformedFrameError):
            parse_frame('{"protocol_version":1,"type":[]}')
        with self.assertRaises(MalformedFrameError):
            parse_frame('{"protocol_version":1.0,"type":"request"}')

    def test_byte_reader_discards_whole_bad_frame_then_resumes(self):
        stream = io.TextIOWrapper(io.BytesIO(b'x' * (MAX_FRAME_BYTES + 1024) + b'\n\xff\n{}\n'), encoding='utf8')
        transport = StdioTransport(stdin=stream, stdout=io.StringIO(), stderr=io.StringIO())
        for expected in ("frame_too_large", "invalid_utf8"):
            with self.assertRaisesRegex(MalformedFrameError, expected):
                transport.read_line()
        self.assertEqual(transport.read_line(), '{}\n')
        self.assertIsNone(transport.read_line())

    def test_response_payload_matches_success_in_both_directions(self):
        for kind in ("response", "host_response"):
            for fields in ('"success":true,"error":{}', '"success":false,"data":null'):
                frame = '{"protocol_version":1,"session_id":"s","id":"py-1","type":"' + kind + '",' + fields + '}'
                with self.subTest(kind=kind, fields=fields), self.assertRaisesRegex(MalformedFrameError, "success_payload"):
                    parse_frame(frame)

    def test_saturated_output_reports_terminal_delivery_failure(self):
        failures = []
        transport = StdioTransport(stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO(),
                                   on_delivery_failure=lambda: failures.append(True))
        for _ in range(128):
            self.assertTrue(transport.send_raw('{}'))
        self.assertFalse(transport.send_raw('{}'))
        self.assertEqual(failures, [True])
        self.assertFalse(transport.send_raw('{}'))

    def test_partial_backup_copy_preserves_previous_recovery_point(self):
        with tempfile.TemporaryDirectory() as directory:
            current = Path(directory, 'config.json')
            backup = Path(directory, 'config.json.bak')
            current.write_bytes(b'current')
            backup.write_bytes(b'previous-good')
            def interrupted(source, staged):
                Path(staged).write_bytes(b'partial')
                raise OSError('injected write failure')
            with patch('backend.config.service.shutil.copyfile', side_effect=interrupted), self.assertRaises(OSError):
                _copy_file(str(current), str(backup))
            self.assertEqual(backup.read_bytes(), b'previous-good')
            self.assertEqual(current.read_bytes(), b'current')
            self.assertFalse(Path(str(backup) + '.tmp').exists())
