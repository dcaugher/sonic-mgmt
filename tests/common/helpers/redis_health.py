"""
Utilities for detecting Redis BUSY errors and related service health issues.

These utilities help ensure that SAI/SDK or SONiC platform bugs that cause
Redis blocking (BUSY errors) are detected and reported, rather than masked
by test framework retry logic.

Usage in tests:

    # Option 1: Use the context manager directly
    from tests.common.helpers.redis_health import RedisHealthMonitor

    def test_something(duthosts, rand_one_dut_hostname):
        duthost = duthosts[rand_one_dut_hostname]
        with RedisHealthMonitor(duthost) as monitor:
            # Operations that might cause Redis BUSY errors
            duthost.shell("config interface ip remove ...")

    # Option 2: Use the pytest fixture
    from tests.common.helpers.redis_health import redis_health_monitor

    @pytest.fixture
    def my_fixture(duthosts, rand_one_dut_hostname):
        duthost = duthosts[rand_one_dut_hostname]
        # Get the monitor for this duthost
        return RedisHealthMonitor(duthost)

    # Option 3: Manual checks
    from tests.common.helpers.redis_health import (
        get_service_pids, check_service_restarts, check_syslog_for_busy_errors
    )

    def test_something(duthosts, rand_one_dut_hostname):
        duthost = duthosts[rand_one_dut_hostname]
        before_pids = get_service_pids(duthost)
        # ... do operations ...
        restarted = check_service_restarts(duthost, before_pids)
        busy_errors = check_syslog_for_busy_errors(duthost)
"""

import logging
import pytest
from tests.common.helpers.assertions import pytest_assert

logger = logging.getLogger(__name__)

# Critical services that can be affected by Redis BUSY errors
REDIS_DEPENDENT_SERVICES = [
    "syncd",
    "orchagent",
    "pmon",
    "lldp",
    "bgp",
    "snmp",
    "swss",
]

# Patterns that indicate Redis BUSY errors in syslog
BUSY_ERROR_PATTERNS = [
    r"BUSY Redis is busy running a script",
    r"Slow script detected.*still in execution after \d+ milliseconds",
]


def get_service_pids(duthost, services=None):
    """
    Get the PIDs of critical services to detect restarts.

    Args:
        duthost: DUT host object
        services: List of service names to check (default: REDIS_DEPENDENT_SERVICES)

    Returns:
        dict: Mapping of service name to PID (or None if not running)
    """
    if services is None:
        services = REDIS_DEPENDENT_SERVICES

    pids = {}
    for service in services:
        try:
            # Get the main process PID for the docker container
            result = duthost.shell(
                "docker inspect --format '{{{{.State.Pid}}}}' %s" % service,
                module_ignore_errors=True
            )
            if result['rc'] == 0 and result['stdout'].strip():
                pids[service] = result['stdout'].strip()
            else:
                pids[service] = None
        except Exception as e:
            logger.debug("Failed to get PID for service %s: %s", service, e)
            pids[service] = None

    return pids


def check_service_restarts(duthost, before_pids, services=None):
    """
    Check if any services restarted by comparing PIDs.

    Args:
        duthost: DUT host object
        before_pids: PID dict from get_service_pids() before the operation
        services: List of service names to check (default: REDIS_DEPENDENT_SERVICES)

    Returns:
        list: Names of services that restarted (empty if none)
    """
    after_pids = get_service_pids(duthost, services)
    restarted = []

    for service in before_pids:
        before = before_pids.get(service)
        after = after_pids.get(service)

        # Service restarted if PID changed (and both were running)
        if before is not None and after is not None and before != after:
            logger.warning(
                "Service %s restarted during operation (PID %s -> %s)",
                service, before, after
            )
            restarted.append(service)
        elif before is not None and after is None:
            logger.warning(
                "Service %s was running before (PID %s) but is not running after",
                service, before
            )
            restarted.append(service)

    return restarted


def check_syslog_for_busy_errors(duthost, since_timestamp=None, fail_on_error=True):
    """
    Check the DUT syslog for Redis BUSY errors.

    This detects SAI/SDK or SONiC platform bugs that cause Redis to be blocked
    by long-running Lua scripts (typically from syncd bulk operations).

    Args:
        duthost: DUT host object
        since_timestamp: Only check logs after this timestamp (format: "YYYY-MM-DD HH:MM:SS")
                        If None, checks last 5 minutes of logs
        fail_on_error: If True, raises assertion error when BUSY errors found

    Returns:
        list: Lines from syslog containing BUSY errors (empty if none)
    """
    # Build the journalctl command
    if since_timestamp:
        time_filter = '--since="%s"' % since_timestamp
    else:
        time_filter = '--since="5 minutes ago"'

    # Search for BUSY errors in syslog
    busy_errors = []
    for pattern in BUSY_ERROR_PATTERNS:
        try:
            result = duthost.shell(
                'journalctl %s --no-pager | grep -E "%s" || true' % (time_filter, pattern),
                module_ignore_errors=True
            )
            if result['rc'] == 0 and result['stdout'].strip():
                for line in result['stdout'].strip().split('\n'):
                    if line.strip():
                        busy_errors.append(line.strip())
        except Exception as e:
            logger.debug("Failed to check syslog for pattern '%s': %s", pattern, e)

    # Also check /var/log/syslog directly as fallback
    if not busy_errors:
        try:
            result = duthost.shell(
                'grep -E "BUSY Redis|Slow script detected" /var/log/syslog 2>/dev/null | tail -50 || true',
                module_ignore_errors=True
            )
            if result['rc'] == 0 and result['stdout'].strip():
                for line in result['stdout'].strip().split('\n'):
                    if line.strip():
                        busy_errors.append(line.strip())
        except Exception as e:
            logger.debug("Failed to check /var/log/syslog: %s", e)

    if busy_errors:
        logger.error(
            "Detected %d Redis BUSY error(s) in syslog - this indicates a platform bug "
            "(SAI/SDK bulk operations blocking Redis):",
            len(busy_errors)
        )
        for error in busy_errors[:10]:  # Log first 10
            logger.error("  %s", error)
        if len(busy_errors) > 10:
            logger.error("  ... and %d more", len(busy_errors) - 10)

        if fail_on_error:
            pytest_assert(
                False,
                "Redis BUSY errors detected in syslog. This indicates a SAI/SDK or SONiC "
                "platform bug where bulk operations are blocking Redis for too long. "
                "Found %d BUSY error(s). First error: %s" % (len(busy_errors), busy_errors[0])
            )

    return busy_errors


def get_current_timestamp(duthost):
    """
    Get the current timestamp from the DUT for use with check_syslog_for_busy_errors.

    Args:
        duthost: DUT host object

    Returns:
        str: Timestamp in format suitable for journalctl --since
    """
    try:
        result = duthost.shell('date "+%Y-%m-%d %H:%M:%S"')
        return result['stdout'].strip()
    except Exception as e:
        logger.warning("Failed to get timestamp from DUT: %s", e)
        return None


class RedisHealthMonitor:
    """
    Context manager for monitoring Redis health during test operations.

    Usage:
        with RedisHealthMonitor(duthost) as monitor:
            # Perform operations that might trigger Redis BUSY errors
            duthost.shell("config interface ip remove ...")

        # After the context, monitor.check() is called automatically
        # which will fail the test if BUSY errors or service restarts are detected
    """

    def __init__(self, duthost, check_busy_errors=True, check_service_restarts=True,
                 fail_on_busy=True, fail_on_restart=False):
        """
        Initialize the Redis health monitor.

        Args:
            duthost: DUT host object
            check_busy_errors: Whether to check syslog for BUSY errors
            check_service_restarts: Whether to check for service restarts
            fail_on_busy: If True, fail test when BUSY errors detected
            fail_on_restart: If True, fail test when services restart
        """
        self.duthost = duthost
        self.check_busy_errors = check_busy_errors
        self.check_service_restarts = check_service_restarts
        self.fail_on_busy = fail_on_busy
        self.fail_on_restart = fail_on_restart
        self.start_timestamp = None
        self.before_pids = None
        self.busy_errors = []
        self.restarted_services = []

    def __enter__(self):
        """Start monitoring."""
        if self.check_busy_errors:
            self.start_timestamp = get_current_timestamp(self.duthost)

        if self.check_service_restarts:
            self.before_pids = get_service_pids(self.duthost)

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Stop monitoring and check for issues."""
        # Don't mask the original exception
        if exc_type is not None:
            return False

        self.check()
        return False

    def check(self):
        """
        Check for Redis health issues.

        This is called automatically on context exit, but can also be called
        manually if you need to check at specific points during a test.
        """
        if self.check_busy_errors:
            self.busy_errors = check_syslog_for_busy_errors(
                self.duthost,
                since_timestamp=self.start_timestamp,
                fail_on_error=self.fail_on_busy
            )

        if self.check_service_restarts and self.before_pids:
            self.restarted_services = check_service_restarts(
                self.duthost,
                self.before_pids
            )
            if self.restarted_services and self.fail_on_restart:
                pytest_assert(
                    False,
                    "Services restarted during operation (possible Redis BUSY crash): %s"
                    % ", ".join(self.restarted_services)
                )
