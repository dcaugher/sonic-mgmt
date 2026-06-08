"""
Unit tests for Redis health check methods in SonicHost class.

Tests cover:
- check_redis_health() - Redis PING/PONG verification
- critical_process_health() - supervisorctl process checking
- check_supervisor_alerts() - multi-container process monitoring
- _collect_redis_failure_diagnostics() - diagnostic collection

Run with:
    python3 -m pytest --noconftest tests/common/unit_tests/devices/test_redis_health.py -v
"""
import pytest
import importlib


def _get_sonic_host_class():
    """Lazy import of SonicHost to avoid heavy ansible deps at module load."""
    module = importlib.import_module("tests.common.devices.sonic")
    return module.SonicHost


class SonicHostTestHelper:
    """
    Helper class that wraps real SonicHost methods with mocked dependencies.

    This ensures we test the actual SonicHost method implementations.
    """

    def __init__(self):
        self.shell_results = {}
        self.shell_calls = []
        self._SonicHost = _get_sonic_host_class()
        # Mock data for get_critical_group_and_process_lists
        self._critical_groups = {}
        self._critical_procs = {}
        # Mock data for all_critical_process_status
        self._all_critical_status = None
        # Default to single ASIC
        self.facts = {"num_asic": 1}

    def shell(self, cmd, module_ignore_errors=False, timeout=None):
        """Mock shell that returns pre-configured results."""
        self.shell_calls.append(cmd)
        if cmd in self.shell_results:
            return self.shell_results[cmd]
        return {"rc": 0, "stdout": "", "stdout_lines": [], "stderr": ""}

    def set_shell_result(self, cmd, rc=0, stdout="", stderr="", stdout_lines=None):
        """Configure what shell() returns for a given command."""
        self.shell_results[cmd] = {
            "rc": rc,
            "stdout": stdout,
            "stdout_lines": (stdout_lines if stdout_lines is not None
                            else stdout.split("\n")),
            "stderr": stderr
        }

    def set_critical_processes(self, container, groups=None, processes=None):
        """Configure critical groups/processes for a container."""
        self._critical_groups[container] = groups or []
        self._critical_procs[container] = processes or []

    def get_critical_group_and_process_lists(self, container_name):
        """Mock get_critical_group_and_process_lists."""
        groups = self._critical_groups.get(container_name, [])
        procs = self._critical_procs.get(container_name, [])
        succeeded = container_name in self._critical_procs
        return groups, procs, succeeded

    def set_all_critical_process_status(self, status_dict):
        """Configure mock return for all_critical_process_status."""
        self._all_critical_status = status_dict

    def all_critical_process_status(self):
        """Mock all_critical_process_status."""
        if self._all_critical_status is not None:
            return self._all_critical_status
        return {}

    def get_database_docker_names(self):
        """Call the real SonicHost.get_database_docker_names."""
        return self._SonicHost.get_database_docker_names.__get__(self, type(self))()

    def check_redis_health(self):
        """Call the real SonicHost.check_redis_health with mocked shell."""
        return self._SonicHost.check_redis_health.__get__(self, type(self))()

    def critical_process_health(self, container_name):
        """Call the real SonicHost.critical_process_health with mocks."""
        return self._SonicHost.critical_process_health.__get__(
            self, type(self))(container_name)

    def check_supervisor_alerts(self):
        """Call the real SonicHost.check_supervisor_alerts with mocks."""
        return self._SonicHost.check_supervisor_alerts.__get__(
            self, type(self))()

    def _collect_redis_failure_diagnostics(self, container="database"):
        """Call the real SonicHost._collect_redis_failure_diagnostics."""
        return self._SonicHost._collect_redis_failure_diagnostics.__get__(
            self, type(self))(container)


# ==================== Tests for check_redis_health ====================

class TestCheckRedisHealth:
    """Tests for check_redis_health() method."""

    def test_redis_healthy_returns_true(self):
        """When redis-cli ping returns PONG, should return True."""
        host = SonicHostTestHelper()
        host.set_shell_result(
            "timeout 5s docker exec database redis-cli ping",
            rc=0, stdout="PONG"
        )

        result = host.check_redis_health()

        assert result is True
        assert "timeout 5s docker exec database redis-cli ping" in host.shell_calls

    def test_redis_no_response_returns_false(self):
        """When redis-cli ping returns no PONG, should return False."""
        host = SonicHostTestHelper()
        host.set_shell_result(
            "timeout 5s docker exec database redis-cli ping",
            rc=0, stdout=""
        )

        result = host.check_redis_health()

        assert result is False

    def test_redis_connection_refused_returns_false(self):
        """When redis-cli fails to connect, should return False."""
        host = SonicHostTestHelper()
        host.set_shell_result(
            "timeout 5s docker exec database redis-cli ping",
            rc=1, stdout="", stderr="Could not connect to Redis at 127.0.0.1:6379: Connection refused"
        )

        result = host.check_redis_health()

        assert result is False

    def test_redis_cannot_assign_address_returns_false(self):
        """Simulate the MIGSOFTWAR-42197 error - Cannot assign requested address."""
        host = SonicHostTestHelper()
        host.set_shell_result(
            "timeout 5s docker exec database redis-cli ping",
            rc=1,
            stdout="",
            stderr="Could not connect to Redis at /var/run/redis/redis.sock: Cannot assign requested address"
        )

        result = host.check_redis_health()

        assert result is False

    def test_redis_exception_returns_false(self):
        """When shell() raises an exception, should return False."""
        host = SonicHostTestHelper()

        # Make shell raise an exception
        def raise_exception(*args, **kwargs):
            raise RuntimeError("SSH connection failed")
        host.shell = raise_exception

        result = host.check_redis_health()

        assert result is False

    def test_multi_asic_all_databases_healthy(self):
        """Multi-ASIC: All database containers respond with PONG."""
        host = SonicHostTestHelper()
        host.facts = {"num_asic": 2}

        # All database containers respond
        host.set_shell_result("timeout 5s docker exec database redis-cli ping", rc=0, stdout="PONG")
        host.set_shell_result("timeout 5s docker exec database0 redis-cli ping", rc=0, stdout="PONG")
        host.set_shell_result("timeout 5s docker exec database1 redis-cli ping", rc=0, stdout="PONG")

        result = host.check_redis_health()

        assert result is True
        # Verify all containers were checked
        assert "timeout 5s docker exec database redis-cli ping" in host.shell_calls
        assert "timeout 5s docker exec database0 redis-cli ping" in host.shell_calls
        assert "timeout 5s docker exec database1 redis-cli ping" in host.shell_calls

    def test_multi_asic_one_database_unhealthy(self):
        """Multi-ASIC: One per-ASIC database fails, should return False."""
        host = SonicHostTestHelper()
        host.facts = {"num_asic": 2}

        # Global and database0 healthy, database1 unhealthy
        host.set_shell_result("timeout 5s docker exec database redis-cli ping", rc=0, stdout="PONG")
        host.set_shell_result("timeout 5s docker exec database0 redis-cli ping", rc=0, stdout="PONG")
        host.set_shell_result("timeout 5s docker exec database1 redis-cli ping", rc=1, stdout="", stderr="Connection refused")

        result = host.check_redis_health()

        assert result is False

    def test_multi_asic_global_database_unhealthy(self):
        """Multi-ASIC: Global database fails, per-ASIC healthy, should return False."""
        host = SonicHostTestHelper()
        host.facts = {"num_asic": 2}

        # Global unhealthy, per-ASIC healthy
        host.set_shell_result("timeout 5s docker exec database redis-cli ping", rc=1, stdout="", stderr="Connection refused")
        host.set_shell_result("timeout 5s docker exec database0 redis-cli ping", rc=0, stdout="PONG")
        host.set_shell_result("timeout 5s docker exec database1 redis-cli ping", rc=0, stdout="PONG")

        result = host.check_redis_health()

        assert result is False

    def test_get_database_docker_names_single_asic(self):
        """Single ASIC should return only ['database']."""
        host = SonicHostTestHelper()
        host.facts = {"num_asic": 1}

        names = host.get_database_docker_names()

        assert names == ["database"]

    def test_get_database_docker_names_multi_asic(self):
        """Multi-ASIC should return database plus databaseN for each ASIC."""
        host = SonicHostTestHelper()
        host.facts = {"num_asic": 4}

        names = host.get_database_docker_names()

        assert names == ["database", "database0", "database1", "database2", "database3"]

    def test_get_database_docker_names_with_asics_present(self):
        """When asics_present is set, use those IDs instead of range(num_asic).

        This handles inventory configurations with non-contiguous or subset ASICs.
        """
        host = SonicHostTestHelper()
        # 4 ASICs total but only ASICs 1 and 3 are present (non-contiguous)
        host.facts = {"num_asic": 4, "asics_present": [1, 3]}

        names = host.get_database_docker_names()

        # Should use asics_present, not range(4)
        assert names == ["database", "database1", "database3"]


# ==================== Tests for critical_process_health ====================

class TestCriticalProcessHealth:
    """Tests for critical_process_health() method."""

    def test_all_critical_processes_running(self):
        """When all critical processes are RUNNING, should return (True, [])."""
        host = SonicHostTestHelper()
        # Set redis as the only critical process
        host.set_critical_processes("database", processes=["redis"])
        host.set_shell_result(
            "docker exec database supervisorctl status",
            rc=0,
            stdout_lines=[
                "redis                            RUNNING   pid 123, uptime 1:00:00",
                "supervisord-dependent-startup    EXITED    Jun 01 10:00 AM"
            ]
        )

        healthy, unhealthy = host.critical_process_health("database")

        assert healthy is True
        assert unhealthy == []

    def test_non_critical_exited_is_ignored(self):
        """Non-critical processes in EXITED state should be ignored."""
        host = SonicHostTestHelper()
        # Only redis is critical, dependent-startup is not
        host.set_critical_processes("database", processes=["redis"])
        host.set_shell_result(
            "docker exec database supervisorctl status",
            rc=0,
            stdout_lines=[
                "redis                            RUNNING   pid 123, uptime 1:00:00",
                "supervisord-dependent-startup    EXITED    Jun 01 10:00 AM"
            ]
        )

        healthy, unhealthy = host.critical_process_health("database")

        # Should be healthy because only redis is critical and it's running
        assert healthy is True
        assert unhealthy == []

    def test_critical_process_stopped(self):
        """When a critical process is STOPPED, should return (False, [name])."""
        host = SonicHostTestHelper()
        host.set_critical_processes("database", processes=["redis"])
        host.set_shell_result(
            "docker exec database supervisorctl status",
            rc=0,
            stdout_lines=[
                "redis                            STOPPED   Jun 01 10:00 AM",
                "supervisord-dependent-startup    EXITED    Jun 01 10:00 AM"
            ]
        )

        healthy, unhealthy = host.critical_process_health("database")

        assert healthy is False
        assert "redis" in unhealthy

    def test_critical_process_fatal(self):
        """When a critical process is FATAL, should return (False, [name])."""
        host = SonicHostTestHelper()
        host.set_critical_processes("database", processes=["redis"])
        host.set_shell_result(
            "docker exec database supervisorctl status",
            rc=0,
            stdout_lines=[
                "redis                            FATAL     Exited too quickly",
                "supervisord-dependent-startup    EXITED    Jun 01 10:00 AM"
            ]
        )

        healthy, unhealthy = host.critical_process_health("database")

        assert healthy is False
        assert "redis" in unhealthy

    def test_critical_process_exited(self):
        """When a critical process is EXITED, should return (False, [name])."""
        host = SonicHostTestHelper()
        host.set_critical_processes("database", processes=["redis"])
        host.set_shell_result(
            "docker exec database supervisorctl status",
            rc=0,
            stdout_lines=[
                "redis                            EXITED    Jun 01 10:00 AM",
                "supervisord-dependent-startup    EXITED    Jun 01 10:00 AM"
            ]
        )

        healthy, unhealthy = host.critical_process_health("database")

        assert healthy is False
        assert "redis" in unhealthy

    def test_multiple_critical_processes_down(self):
        """When multiple critical processes are down, should return all."""
        host = SonicHostTestHelper()
        host.set_critical_processes(
            "swss", processes=["orchagent", "portsyncd", "neighsyncd"]
        )
        host.set_shell_result(
            "docker exec swss supervisorctl status",
            rc=0,
            stdout_lines=[
                "orchagent                        STOPPED   Jun 01 10:00 AM",
                "portsyncd                        EXITED    Jun 01 10:00 AM",
                "neighsyncd                       RUNNING   pid 100, uptime 1:00:05"
            ]
        )

        healthy, unhealthy = host.critical_process_health("swss")

        assert healthy is False
        assert "orchagent" in unhealthy
        assert "portsyncd" in unhealthy
        assert "neighsyncd" not in unhealthy

    def test_supervisorctl_command_fails(self):
        """When supervisorctl command fails, should return failure."""
        host = SonicHostTestHelper()
        host.set_critical_processes("database", processes=["redis"])
        host.set_shell_result(
            "docker exec database supervisorctl status",
            rc=1,
            stderr="error: <class 'socket.error'>"
        )

        healthy, unhealthy = host.critical_process_health("database")

        assert healthy is False
        assert "supervisorctl command failed" in unhealthy

    def test_fallback_when_no_critical_list(self):
        """Without critical process list, fallback to checking all processes."""
        host = SonicHostTestHelper()
        # Don't set critical processes - simulates missing file
        host.set_shell_result(
            "docker exec database supervisorctl status",
            rc=0,
            stdout_lines=[
                "redis                            RUNNING   pid 123",
                "other_process                    STOPPED   Jun 01 10:00 AM"
            ]
        )

        healthy, unhealthy = host.critical_process_health("database")

        # Without critical list, all non-running processes are flagged
        assert healthy is False
        assert "other_process" in unhealthy


# ==================== Tests for check_supervisor_alerts ====================

class TestCheckSupervisorAlerts:
    """Tests for check_supervisor_alerts() method.

    Note: check_supervisor_alerts now uses all_critical_process_status() which
    properly handles multi-ASIC container names and critical process filtering.
    """

    def test_all_containers_healthy(self):
        """When all containers have healthy processes, return (True, {})."""
        host = SonicHostTestHelper()
        host.set_all_critical_process_status({
            "database": {
                "status": True,
                "exited_critical_process": [],
                "running_critical_process": ["redis"]
            },
            "swss": {
                "status": True,
                "exited_critical_process": [],
                "running_critical_process": ["orchagent"]
            }
        })

        all_ok, down_processes = host.check_supervisor_alerts()

        assert all_ok is True
        assert down_processes == {}

    def test_container_with_exited_critical_process(self):
        """Container with exited critical process should be flagged."""
        host = SonicHostTestHelper()
        host.set_all_critical_process_status({
            "database": {
                "status": False,
                "exited_critical_process": ["redis"],
                "running_critical_process": []
            },
            "swss": {
                "status": True,
                "exited_critical_process": [],
                "running_critical_process": ["orchagent"]
            }
        })

        all_ok, down_processes = host.check_supervisor_alerts()

        assert all_ok is False
        assert "database" in down_processes
        assert "redis" in down_processes["database"]

    def test_multiple_containers_with_issues(self):
        """Multiple containers can have issues simultaneously."""
        host = SonicHostTestHelper()
        host.set_all_critical_process_status({
            "database": {
                "status": False,
                "exited_critical_process": ["redis"],
                "running_critical_process": []
            },
            "swss": {
                "status": False,
                "exited_critical_process": ["orchagent", "portsyncd"],
                "running_critical_process": ["neighsyncd"]
            }
        })

        all_ok, down_processes = host.check_supervisor_alerts()

        assert all_ok is False
        assert "database" in down_processes
        assert "swss" in down_processes
        assert "redis" in down_processes["database"]
        assert "orchagent" in down_processes["swss"]
        assert "portsyncd" in down_processes["swss"]

    def test_status_false_with_empty_exited_list(self):
        """Status False with empty exited list means container unavailable.

        This can happen when a container is missing, not started, or unreachable.
        Even without specific process names, we should flag this as unhealthy.
        """
        host = SonicHostTestHelper()
        host.set_all_critical_process_status({
            "database": {
                "status": False,
                "exited_critical_process": [],
                "running_critical_process": []
            }
        })

        all_ok, down_processes = host.check_supervisor_alerts()

        # status=False means unhealthy, even with empty exited list
        assert all_ok is False
        assert "database" in down_processes
        assert "container_unavailable" in down_processes["database"]

    def test_multi_asic_container_names(self):
        """Verify multi-ASIC container names (swss0, bgp0) work correctly."""
        host = SonicHostTestHelper()
        # Simulates multi-ASIC where containers are named swss0, bgp0, etc.
        host.set_all_critical_process_status({
            "database": {
                "status": True,
                "exited_critical_process": [],
                "running_critical_process": ["redis"]
            },
            "swss0": {
                "status": False,
                "exited_critical_process": ["orchagent"],
                "running_critical_process": []
            },
            "bgp0": {
                "status": True,
                "exited_critical_process": [],
                "running_critical_process": ["bgpd"]
            }
        })

        all_ok, down_processes = host.check_supervisor_alerts()

        assert all_ok is False
        assert "swss0" in down_processes
        assert "orchagent" in down_processes["swss0"]


# ==================== Integration-style test ====================

class TestRedisHealthCheckIntegration:
    """Test the methods work together as expected."""

    def test_healthy_system(self):
        """Simulate a fully healthy system."""
        host = SonicHostTestHelper()

        # Redis responds
        host.set_shell_result(
            "timeout 5s docker exec database redis-cli ping",
            rc=0, stdout="PONG"
        )

        # All critical processes healthy
        host.set_all_critical_process_status({
            "database": {
                "status": True,
                "exited_critical_process": [],
                "running_critical_process": ["redis"]
            },
            "swss": {
                "status": True,
                "exited_critical_process": [],
                "running_critical_process": ["orchagent"]
            }
        })

        # Run all checks
        redis_ok = host.check_redis_health()
        supervisor_ok, down_procs = host.check_supervisor_alerts()

        assert redis_ok is True
        assert supervisor_ok is True
        assert down_procs == {}

    def test_migsoftwar_42197_scenario(self):
        """Simulate the exact MIGSOFTWAR-42197 failure scenario.

        Database container is running, but Redis process inside has been
        dead for hours. supervisor-proc-exit-listener knows about it but
        hasn't restarted it.
        """
        host = SonicHostTestHelper()

        # Redis does NOT respond (the key symptom)
        host.set_shell_result(
            "timeout 5s docker exec database redis-cli ping",
            rc=1,
            stdout="",
            stderr="Could not connect to Redis: Cannot assign requested address"
        )

        # all_critical_process_status shows redis is down
        host.set_all_critical_process_status({
            "database": {
                "status": False,
                "exited_critical_process": ["redis"],
                "running_critical_process": []
            },
            "swss": {
                "status": True,
                "exited_critical_process": [],
                "running_critical_process": ["orchagent"]
            }
        })

        # Run checks
        redis_ok = host.check_redis_health()
        supervisor_ok, down_procs = host.check_supervisor_alerts()

        # Both checks should FAIL and catch this issue
        assert redis_ok is False, \
            "Redis health check should fail when redis-cli can't connect"
        assert supervisor_ok is False, \
            "Supervisor alerts should catch redis exited"
        assert "database" in down_procs
        assert "redis" in down_procs["database"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
