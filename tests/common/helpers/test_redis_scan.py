"""
Unit tests for redis_scan.py

These tests verify the parsing logic for redis-cli SCAN output format.
Run with: pytest tests/common/helpers/test_redis_scan.py -v
"""

import pytest
from unittest.mock import MagicMock


class TestRedisScanOutputParsing:
    """
    Tests for redis-cli SCAN output parsing.

    redis-cli SCAN returns output in the format:
        Line 1: cursor (integer)
        Lines 2+: keys (one per line, may be empty)

    Example output for SCAN 0 MATCH "*" COUNT 100:
        42
        key1
        key2
        key3

    Example output when no keys match:
        0

    These tests pin the expected format to protect against regressions
    if the output format changes in future Redis/sonic versions.
    """

    @pytest.fixture
    def mock_host(self):
        """Create a mock host object for testing."""
        return MagicMock()

    @pytest.fixture
    def scanner(self, mock_host):
        """Create a RedisScan instance with mock host."""
        from tests.common.helpers.redis_scan import RedisScan
        return RedisScan(mock_host)

    def test_parse_scan_output_with_keys(self, scanner, mock_host):
        """Test parsing SCAN output with multiple keys."""
        # Simulate redis-cli output: cursor on first line, keys on subsequent lines
        mock_host.shell.return_value = {
            'rc': 0,
            'stdout': '42\nkey1\nkey2\nkey3\n',
            'stderr': ''
        }

        cursor, keys = scanner._run_scan_iteration('ASIC_DB', '*', 0, 100)

        assert cursor == 42
        assert keys == ['key1', 'key2', 'key3']

    def test_parse_scan_output_no_keys(self, scanner, mock_host):
        """Test parsing SCAN output when no keys match (cursor only)."""
        mock_host.shell.return_value = {
            'rc': 0,
            'stdout': '0\n',
            'stderr': ''
        }

        cursor, keys = scanner._run_scan_iteration('ASIC_DB', '*', 0, 100)

        assert cursor == 0
        assert keys == []

    def test_parse_scan_output_iteration_complete(self, scanner, mock_host):
        """Test parsing SCAN output when iteration is complete (cursor=0 with keys)."""
        mock_host.shell.return_value = {
            'rc': 0,
            'stdout': '0\nlast_key1\nlast_key2\n',
            'stderr': ''
        }

        cursor, keys = scanner._run_scan_iteration('ASIC_DB', '*', 123, 100)

        assert cursor == 0  # 0 means iteration complete
        assert keys == ['last_key1', 'last_key2']

    def test_parse_scan_output_empty_stdout(self, scanner, mock_host):
        """Test handling of empty stdout (edge case)."""
        mock_host.shell.return_value = {
            'rc': 0,
            'stdout': '',
            'stderr': ''
        }

        cursor, keys = scanner._run_scan_iteration('ASIC_DB', '*', 0, 100)

        assert cursor == 0
        assert keys == []

    def test_parse_scan_output_with_blank_lines(self, scanner, mock_host):
        """Test that blank lines in output are filtered out."""
        mock_host.shell.return_value = {
            'rc': 0,
            'stdout': '42\nkey1\n\nkey2\n  \nkey3\n',
            'stderr': ''
        }

        cursor, keys = scanner._run_scan_iteration('ASIC_DB', '*', 0, 100)

        assert cursor == 42
        assert keys == ['key1', 'key2', 'key3']

    def test_parse_scan_output_keys_with_special_chars(self, scanner, mock_host):
        """Test parsing keys containing special characters."""
        mock_host.shell.return_value = {
            'rc': 0,
            'stdout': '0\nASIC_STATE:SAI_OBJECT_TYPE_ROUTE_ENTRY:{"dest":"10.0.0.0/24"}\nCOUNTERS:oid:0x1234\n',
            'stderr': ''
        }

        cursor, keys = scanner._run_scan_iteration('ASIC_DB', '*', 0, 100)

        assert cursor == 0
        assert keys == [
            'ASIC_STATE:SAI_OBJECT_TYPE_ROUTE_ENTRY:{"dest":"10.0.0.0/24"}',
            'COUNTERS:oid:0x1234'
        ]

    def test_scan_error_raises_exception(self, scanner, mock_host):
        """Test that non-zero return code raises RedisScanError."""
        from tests.common.helpers.redis_scan import RedisScanError

        mock_host.shell.return_value = {
            'rc': 1,
            'stdout': '',
            'stderr': 'ERR unknown command'
        }

        with pytest.raises(RedisScanError) as exc_info:
            scanner._run_scan_iteration('ASIC_DB', '*', 0, 100)

        assert 'SCAN command failed' in str(exc_info.value)

    def test_scan_invalid_cursor_raises_exception(self, scanner, mock_host):
        """Test that non-integer cursor raises RedisScanError."""
        from tests.common.helpers.redis_scan import RedisScanError

        mock_host.shell.return_value = {
            'rc': 0,
            'stdout': 'not_a_number\nkey1\n',
            'stderr': ''
        }

        with pytest.raises(RedisScanError) as exc_info:
            scanner._run_scan_iteration('ASIC_DB', '*', 0, 100)

        assert 'Failed to parse SCAN output' in str(exc_info.value)


class TestKeysetHash:
    """Tests for keyset hashing used in stability detection."""

    @pytest.fixture
    def scanner(self):
        """Create a RedisScan instance with mock host."""
        from tests.common.helpers.redis_scan import RedisScan
        mock_host = MagicMock()
        return RedisScan(mock_host)

    def test_same_keys_same_hash(self, scanner):
        """Test that identical keysets produce the same hash."""
        keys1 = {'key1', 'key2', 'key3'}
        keys2 = {'key3', 'key1', 'key2'}  # Same keys, different order

        hash1 = scanner._compute_keyset_hash(keys1)
        hash2 = scanner._compute_keyset_hash(keys2)

        assert hash1 == hash2

    def test_different_keys_different_hash(self, scanner):
        """Test that different keysets produce different hashes."""
        keys1 = {'key1', 'key2', 'key3'}
        keys2 = {'key1', 'key2', 'key4'}

        hash1 = scanner._compute_keyset_hash(keys1)
        hash2 = scanner._compute_keyset_hash(keys2)

        assert hash1 != hash2

    def test_empty_keyset_hash(self, scanner):
        """Test that empty keyset produces a valid hash."""
        keys = set()
        hash_value = scanner._compute_keyset_hash(keys)

        assert isinstance(hash_value, int)
