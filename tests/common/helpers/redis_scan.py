"""
Redis SCAN-based utilities for non-blocking key enumeration.

The Redis KEYS command blocks the entire database while scanning, which
can cause BUSY errors when FlexCounter Lua scripts timeout during
long-running operations. These utilities use SCAN instead, which returns
results incrementally without blocking.

SCAN Consistency Notes:
- SCAN provides weak iteration guarantees during concurrent modifications
- Keys that exist throughout iteration are guaranteed to be returned
- Keys added/deleted during iteration may or may not appear
- Duplicates are possible if rehashing occurs during scan

The functions in this module handle these edge cases by:
1. Collecting results into sets (deduplication)
2. Waiting for keyspace stability before returning (by default)
3. Using hash-based comparison for memory-efficient stability checks

Usage:
    from tests.common.helpers.redis_scan import RedisScan

    # Scan keys with stability check (default, recommended for tests)
    scanner = RedisScan(duthost)
    keys = scanner.scan_keys(
        "ASIC_DB", "ASIC_STATE:SAI_OBJECT_TYPE_ROUTE_ENTRY*")

    # Count keys with stability check (default)
    count = scanner.count_keys(
        "ASIC_DB", "ASIC_STATE:SAI_OBJECT_TYPE_ROUTE_ENTRY*")

    # Quick scan without stability (for debug/info only)
    keys = scanner.scan_keys(
        "ASIC_DB", "ASIC_STATE:SAI_OBJECT_TYPE_ROUTE_ENTRY*",
        skip_stability=True)

    # Wait for specific key count
    keys = scanner.wait_for_key_count(
        "ASIC_DB", "ASIC_STATE:SAI_OBJECT_TYPE_ROUTE_ENTRY*",
        expected_count=0)
"""

import logging
import time
from typing import Set, Tuple, Optional

logger = logging.getLogger(__name__)


class RedisScanTimeout(Exception):
    """Raised when keyspace does not stabilize within timeout."""
    pass


class RedisScanError(Exception):
    """Raised when Redis SCAN command fails."""
    pass


class RedisScan:
    """
    Non-blocking Redis key scanning utilities.

    Uses SCAN instead of KEYS to avoid blocking Redis during enumeration.
    Provides stability checking for use during state transitions.
    """

    # Default parameters for SCAN operations
    DEFAULT_SCAN_COUNT = 1000       # Keys per SCAN batch
    DEFAULT_STABILITY_WINDOW = 2    # Consecutive identical scans required
    DEFAULT_STABILITY_TIMEOUT = 30  # Max seconds to wait for stability
    DEFAULT_STABILITY_INTERVAL = 0.2  # Seconds between stability checks

    # Database name to redis-cli -n mapping
    # sonic-db-cli uses names, redis-cli uses numeric IDs
    DB_NAME_TO_ID = {
        "APPL_DB": 0,
        "ASIC_DB": 1,
        "COUNTERS_DB": 2,
        "LOGLEVEL_DB": 3,
        "CONFIG_DB": 4,
        "PFC_WD_DB": 5,
        "STATE_DB": 6,
        "SNMP_OVERLAY_DB": 7,
    }

    def __init__(self, host, namespace: Optional[str] = None):
        """
        Initialize RedisScan with a host connection.

        Args:
            host: A SonicHost, SonicAsic, or similar object with shell() method
            namespace: Optional namespace for multi-ASIC platforms
        """
        self.host = host
        self.namespace = namespace

    def _build_cli_prefix(self, database: str) -> str:
        """Build the sonic-db-cli command prefix."""
        ns_arg = ""
        if self.namespace:
            ns_arg = f"-n {self.namespace} "
        return f"sonic-db-cli {ns_arg}{database}"

    def _build_redis_cli_prefix(self, database: str) -> str:
        """Build the redis-cli command prefix with numeric DB ID."""
        db_id = self.DB_NAME_TO_ID.get(database, 0)
        ns_arg = ""
        if self.namespace:
            # For multi-ASIC, redis-cli needs to connect to namespace-specific socket
            ns_arg = f"-n {self.namespace} "
        return f"redis-cli -n {db_id} {ns_arg}"

    def _run_scan_iteration(self, database: str, pattern: str,
                            cursor: int, count: int) -> Tuple[int, list]:
        """
        Execute a single SCAN iteration using redis-cli.

        Note: We use redis-cli instead of sonic-db-cli because sonic-db-cli
        has a bug on some platforms (e.g., Cisco-8000) where it returns
        cursor=0 prematurely, causing SCAN to stop after one iteration.

        Args:
            database: Redis database name (e.g., "ASIC_DB", "CONFIG_DB")
            pattern: Key pattern to match (supports Redis glob patterns)
            cursor: Current cursor position (0 to start)
            count: Hint for number of keys to return per batch

        Returns:
            Tuple of (new_cursor, list_of_keys)
        """
        # Validate database name - don't silently fall back to wrong DB
        if database not in self.DB_NAME_TO_ID:
            raise RedisScanError(
                f"Unknown database '{database}'. Valid databases: {list(self.DB_NAME_TO_ID.keys())}")
        db_id = self.DB_NAME_TO_ID[database]

        # Build namespace-aware command for multi-ASIC platforms
        if self.namespace:
            cmd = f'ip netns exec {self.namespace} redis-cli -n {db_id} SCAN {cursor} MATCH "{pattern}" COUNT {count}'
        else:
            cmd = f'redis-cli -n {db_id} SCAN {cursor} MATCH "{pattern}" COUNT {count}'

        result = self.host.shell(cmd, module_ignore_errors=True, verbose=False)
        if result['rc'] != 0:
            err = result.get('stderr', 'unknown error')
            raise RedisScanError(f"SCAN command failed (rc={result['rc']}): {err}")

        stdout = result.get('stdout', '').strip()
        if not stdout:
            # Empty stdout with rc=0 means no keys matched (cursor=0, empty list)
            return 0, []

        # redis-cli SCAN returns:
        # Line 1: cursor
        # Lines 2+: keys (one per line)
        lines = stdout.split('\n')
        if len(lines) < 1:
            return 0, []

        try:
            new_cursor = int(lines[0].strip())
            keys = [line.strip() for line in lines[1:] if line.strip()]
            return new_cursor, keys
        except (ValueError, IndexError) as e:
            raise RedisScanError(f"Failed to parse SCAN output: {e}, output: {stdout[:200]}")

    def _scan_once(self, database: str, pattern: str, *,
                   count: int = DEFAULT_SCAN_COUNT) -> Set[str]:
        """
        Perform a single complete SCAN without stability checking.

        This is an internal method. Use scan_keys() for most use cases.

        Args:
            database: Redis database name (e.g., "ASIC_DB", "CONFIG_DB")
            pattern: Key pattern to match (supports Redis glob patterns)
            count: Hint for number of keys per SCAN batch

        Returns:
            Set of matching keys (deduplicated)
        """
        keys = set()
        cursor = 0

        while True:
            cursor, batch = self._run_scan_iteration(
                database, pattern, cursor, count)
            keys.update(batch)

            if cursor == 0:
                break

        return keys

    def _compute_keyset_hash(self, keys: Set[str]) -> int:
        """
        Compute a hash of a key set for change detection.

        Uses Python's built-in hash on a frozenset, which is efficient
        and avoids security scanner flags that MD5/SHA1 might trigger.
        Note: Only suitable for same-process comparisons (hash() is not
        deterministic across Python invocations).

        Args:
            keys: Set of key strings

        Returns:
            Integer hash of the frozen keyset
        """
        return hash(frozenset(keys))

    def scan_keys(self, database: str, pattern: str, *,
                  skip_stability: bool = False,
                  stability_window: int = DEFAULT_STABILITY_WINDOW,
                  timeout: float = DEFAULT_STABILITY_TIMEOUT,
                  interval: float = DEFAULT_STABILITY_INTERVAL,
                  count: int = DEFAULT_SCAN_COUNT) -> Set[str]:
        """
        Scan for keys matching a pattern without blocking Redis.

        By default, waits for the keyspace to stabilize before returning.
        This handles SCAN's weak consistency guarantees during concurrent
        modifications. Use skip_stability=True only for debug/info purposes
        where exact results don't matter.

        Args:
            database: Redis database name (e.g., "ASIC_DB", "CONFIG_DB")
            pattern: Key pattern to match (supports Redis glob patterns)
            skip_stability: If True, return immediately without stability
                check. Only use for debug/informational purposes.
            stability_window: Number of consecutive identical scans required
            timeout: Maximum seconds to wait for stability
            interval: Seconds between scan attempts
            count: Hint for number of keys per SCAN batch

        Returns:
            Set of matching keys (deduplicated)

        Raises:
            RedisScanTimeout: If keyspace does not stabilize within timeout
                (only when skip_stability=False)
        """
        if skip_stability:
            keys = self._scan_once(database, pattern, count=count)
            logger.debug(
                f"SCAN {database} '{pattern}': found {len(keys)} keys "
                "(no stability check)"
            )
            return keys

        # Stability checking mode
        start_time = time.time()
        prev_hash = None
        stable_count = 0
        last_keys = set()

        while time.time() - start_time < timeout:
            current_keys = self._scan_once(database, pattern, count=count)
            current_hash = self._compute_keyset_hash(current_keys)

            if current_hash == prev_hash:
                stable_count += 1
                if stable_count >= stability_window:
                    elapsed = time.time() - start_time
                    logger.debug(
                        f"SCAN {database} '{pattern}': stabilized "
                        f"with {len(current_keys)} keys after "
                        f"{elapsed:.2f}s ({stable_count} matches)"
                    )
                    return current_keys
            else:
                if prev_hash is not None:
                    # Log the change for debugging
                    added = current_keys - last_keys
                    removed = last_keys - current_keys
                    logger.debug(
                        f"SCAN {database} '{pattern}': keyspace changed "
                        f"(+{len(added)}/-{len(removed)} keys), resetting"
                    )
                stable_count = 0
                prev_hash = current_hash
                last_keys = current_keys

            time.sleep(interval)

        # Timeout reached
        logger.warning(
            f"SCAN {database} '{pattern}': did not stabilize within "
            f"{timeout}s (last count: {len(last_keys)} keys), raising timeout"
        )
        raise RedisScanTimeout(
            f"Keyspace '{pattern}' in {database} did not stabilize "
            f"within {timeout}s"
        )

    def count_keys(self, database: str, pattern: str, *,
                   skip_stability: bool = False,
                   stability_window: int = DEFAULT_STABILITY_WINDOW,
                   timeout: float = DEFAULT_STABILITY_TIMEOUT,
                   interval: float = DEFAULT_STABILITY_INTERVAL,
                   count: int = DEFAULT_SCAN_COUNT) -> int:
        """
        Count keys matching a pattern without blocking Redis.

        By default, waits for the keyspace to stabilize before returning.
        Use skip_stability=True only for debug/info purposes.

        Args:
            database: Redis database name
            pattern: Key pattern to match
            skip_stability: If True, return immediately without stability
                check. Only use for debug/informational purposes.
            stability_window: Number of consecutive identical scans required
            timeout: Maximum seconds to wait for stability
            interval: Seconds between scan attempts
            count: Hint for number of keys per SCAN batch

        Returns:
            Number of matching keys

        Raises:
            RedisScanTimeout: If keyspace does not stabilize within timeout
                (only when skip_stability=False)
        """
        return len(self.scan_keys(
            database, pattern,
            skip_stability=skip_stability,
            stability_window=stability_window,
            timeout=timeout,
            interval=interval,
            count=count
        ))

    def wait_for_key_count(self, database: str, pattern: str, *,
                           expected_count: int,
                           stability_window: int = DEFAULT_STABILITY_WINDOW,
                           timeout: float = DEFAULT_STABILITY_TIMEOUT,
                           interval: float = DEFAULT_STABILITY_INTERVAL,
                           count: int = DEFAULT_SCAN_COUNT) -> Set[str]:
        """
        Wait for the keyspace to reach an expected count AND stabilize.

        Useful for verifying that a specific number of entries exist after
        an operation (e.g., waiting for routes to be added/removed).

        Args:
            database: Redis database name
            pattern: Key pattern to match
            expected_count: Expected number of matching keys
            stability_window: Number of consecutive identical scans required
            timeout: Maximum seconds to wait
            interval: Seconds between scan attempts
            count: Hint for number of keys per SCAN batch

        Returns:
            Set of matching keys after reaching expected count and stabilizing

        Raises:
            RedisScanTimeout: If expected count is not reached within timeout
        """
        start_time = time.time()
        prev_hash = None
        stable_count = 0
        last_keys = set()

        while time.time() - start_time < timeout:
            current_keys = self._scan_once(database, pattern, count=count)
            current_hash = self._compute_keyset_hash(current_keys)
            current_count = len(current_keys)

            # Check if we've reached expected count AND stabilized
            if current_count == expected_count:
                if current_hash == prev_hash:
                    stable_count += 1
                    if stable_count >= stability_window:
                        elapsed = time.time() - start_time
                        logger.info(
                            f"SCAN {database} '{pattern}': reached "
                            f"expected count {expected_count} and "
                            f"stabilized after {elapsed:.2f}s"
                        )
                        return current_keys
                else:
                    stable_count = 0
                    prev_hash = current_hash
            else:
                # Count doesn't match - reset stability tracking
                stable_count = 0
                prev_hash = None
                logger.debug(
                    f"SCAN {database} '{pattern}': count "
                    f"{current_count} != expected {expected_count}"
                )

            last_keys = current_keys
            time.sleep(interval)

        elapsed = time.time() - start_time
        raise RedisScanTimeout(
            f"Keyspace '{pattern}' in {database} did not reach "
            f"expected count {expected_count} within {timeout}s "
            f"(last count: {len(last_keys)})"
        )


def create_scanner_for_asic(asichost) -> RedisScan:
    """
    Factory function to create a RedisScan instance for a SonicAsic.

    Handles namespace detection for multi-ASIC platforms.

    Args:
        asichost: A SonicAsic instance

    Returns:
        Configured RedisScan instance
    """
    namespace = None
    if hasattr(asichost, 'namespace') and asichost.namespace:
        namespace = asichost.namespace

    # Use the sonichost for shell commands if available
    host = getattr(asichost, 'sonichost', asichost)
    return RedisScan(host, namespace)
