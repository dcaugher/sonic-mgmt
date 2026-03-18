"""
DualToR Stabilization Event Checks

These functions implement event-driven checks for DualToR stabilization after
config reload. Checks are organized into phases to optimize wait time:

Phase 1 - Infrastructure (parallel, ~60s max):
  1. mux container running       - Basic service startup
  2. linkmgrd process running    - Critical process in mux container

Phase 2 - Database Layer (parallel poll, ~120s max):
  3. APP_DB initialized          - MUX_CABLE_TABLE, HW_MUX_CABLE_TABLE != unknown
  4. STATE_DB populated          - MUX_LINKMGR_TABLE state = "healthy"
  7. ASIC_DB tunnel (A-A only)   - SAI_OBJECT_TYPE_TUNNEL exists
  8. gRPC channel up (A-A only)  - HW_MUX_CABLE_TABLE state != "unknown"

Phase 3 - Final Validation (sequential, ~60s max):
  5-6. CLI healthy + consistent  - show muxcable status validates all

Events in Phase 2 are checked together in each poll iteration since they
depend on different subsystems (linkmgrd vs ycabled/orchagent) and can
become ready in any order.
"""
import json
import logging
from functools import partial

from tests.common.helpers.parallel import parallel_run_threaded
from tests.common.utilities import wait_until

logger = logging.getLogger(__name__)


def _check_mux_container_running(sonic_host):
    """
    Event 1: Check if the mux docker container is running.
    This is a prerequisite before any mux-related checks.

    Returns:
        tuple: (success: bool, error_message: str or None)
    """
    try:
        # Escape Jinja2 delimiters for Ansible shell module
        output = sonic_host.shell(
            "docker inspect -f '{{ '{{' }} .State.Status {{ '}}' }}' mux",
            module_ignore_errors=True
        )
        # Check for Ansible-level failure (e.g., template errors)
        if output.get('failed') and 'msg' in output:
            return False, f"Ansible error: {output['msg']}"
        if output.get('rc', 1) != 0:
            stderr = output.get('stderr', '').strip()
            return False, f"docker inspect failed (rc={output.get('rc')}): {stderr or 'no error message'}"
        if 'running' not in output.get('stdout', ''):
            return False, f"mux container not running, status: {output.get('stdout', '').strip()}"
        return True, None
    except Exception as e:
        return False, f"Exception: {e}"


def _check_linkmgrd_running(sonic_host):
    """
    Event 2: Check if linkmgrd process is running in mux container.
    linkmgrd is the critical process that manages mux cable link states.

    Returns:
        tuple: (success: bool, error_message: str or None)
    """
    try:
        output = sonic_host.shell(
            "docker exec mux supervisorctl status linkmgrd",
            module_ignore_errors=True
        )
        # Check for Ansible-level failure
        if output.get('failed') and 'msg' in output:
            return False, f"Ansible error: {output['msg']}"
        if output.get('rc', 1) != 0:
            stderr = output.get('stderr', '').strip()
            return False, f"supervisorctl failed (rc={output.get('rc')}): {stderr or 'no error message'}"
        if 'RUNNING' not in output.get('stdout', ''):
            return False, f"linkmgrd not running: {output.get('stdout', '').strip()}"
        return True, None
    except Exception as e:
        return False, f"Exception: {e}"


def _check_infrastructure_ready(sonic_host):
    """
    Check that mux container infrastructure is ready (Events 1-2).

    Runs checks in parallel:
    - Event 1: mux container running
    - Event 2: linkmgrd process running

    Returns:
        True if all infrastructure checks pass, False otherwise.
    """
    checks = [
        ('mux container', partial(_check_mux_container_running, sonic_host)),
        ('linkmgrd process', partial(_check_linkmgrd_running, sonic_host)),
    ]

    try:
        results = parallel_run_threaded(
            [check_func for _, check_func in checks],
            timeout=30,
            thread_count=len(checks)
        )

        all_passed = True
        errors = []
        for (check_name, _), result in zip(checks, results):
            # Result is (success, error_msg) tuple
            success, error_msg = result if isinstance(result, tuple) else (result, None)
            if not success:
                error_detail = error_msg or "unknown error"
                errors.append(f"{check_name}: {error_detail}")
                all_passed = False

        if errors:
            # Log at INFO level so errors are visible in normal log output
            logger.info("Infrastructure check failures: %s", "; ".join(errors))

        return all_passed

    except TimeoutError:
        logger.warning("Infrastructure checks timed out after 30s")
        return False
    except Exception as e:
        logger.warning("Error running parallel infrastructure checks: %s", e)
        return False


def _check_mux_state_initialized(sonic_host, mux_ports):
    """
    Event 3: Check that mux state is initialized (not "unknown") for all ports.
    Verifies APP_DB MUX_CABLE_TABLE and HW_MUX_CABLE_TABLE have valid states.

    Uses batched redis queries to check all ports in 2 commands instead of 2*N.

    Returns:
        tuple: (success: bool, error_message: str or None)
    """
    try:
        for table in ['MUX_CABLE_TABLE', 'HW_MUX_CABLE_TABLE']:
            # Build batched redis command using pipeline
            # Each HGET returns one line, results come in order
            redis_cmds = "\n".join(f'HGET "{table}:{port}" state' for port in mux_ports)
            result = sonic_host.shell(
                f'redis-cli -n 0 <<EOF\n{redis_cmds}\nEOF',
                module_ignore_errors=True
            )
            if result.get('failed') and 'msg' in result:
                return False, f"Ansible error querying {table}: {result['msg']}"
            if result.get('rc', 1) != 0:
                return False, f"APP_DB {table} batch query failed (rc={result.get('rc')})"

            # Parse results - one state per line, in same order as mux_ports
            states = result.get('stdout', '').strip().split('\n')
            if len(states) != len(mux_ports):
                return False, f"APP_DB {table} returned {len(states)} results, expected {len(mux_ports)}"

            for port, state in zip(mux_ports, states):
                state = state.strip()
                if not state or state == 'unknown' or state == '(nil)':
                    return False, f"APP_DB {table}:{port} state is '{state}', not initialized"
        return True, None
    except Exception as e:
        return False, f"Exception: {e}"


def _check_state_db_populated(sonic_host, mux_ports):
    """
    Event 4: Check STATE_DB tables are populated with valid states.
    Verifies MUX_CABLE_TABLE, HW_MUX_CABLE_TABLE have active/standby,
    and MUX_LINKMGR_TABLE has state = "healthy".

    Uses batched redis queries to check all ports in 3 commands instead of 3*N.

    Returns:
        tuple: (success: bool, error_message: str or None)
    """
    # Define table checks: (table_name, valid_states, description)
    table_checks = [
        ('MUX_CABLE_TABLE', {'active', 'standby'}, 'active/standby'),
        ('HW_MUX_CABLE_TABLE', {'active', 'standby'}, 'active/standby'),
        ('MUX_LINKMGR_TABLE', {'healthy'}, 'healthy'),
    ]

    try:
        for table, valid_states, expected_desc in table_checks:
            # Build batched redis command using pipeline
            # STATE_DB uses | separator, each HGET returns one line
            redis_cmds = "\n".join(f'HGET "{table}|{port}" state' for port in mux_ports)
            result = sonic_host.shell(
                f'redis-cli -n 6 <<EOF\n{redis_cmds}\nEOF',
                module_ignore_errors=True
            )
            if result.get('failed') and 'msg' in result:
                return False, f"Ansible error querying {table}: {result['msg']}"
            if result.get('rc', 1) != 0:
                return False, f"STATE_DB {table} batch query failed (rc={result.get('rc')})"

            # Parse results - one state per line, in same order as mux_ports
            states = result.get('stdout', '').strip().split('\n')
            if len(states) != len(mux_ports):
                return False, f"STATE_DB {table} returned {len(states)} results, expected {len(mux_ports)}"

            for port, state in zip(mux_ports, states):
                state = state.strip()
                if state == '(nil)' or not state:
                    return False, f"STATE_DB {table}|{port} not found"
                if state not in valid_states:
                    return False, f"STATE_DB {table}|{port} state is '{state}', expected {expected_desc}"
        return True, None
    except Exception as e:
        return False, f"Exception: {e}"


def _check_muxcable_cli_healthy(sonic_host, mux_ports):
    """
    Events 5-6: Final CLI validation via 'show muxcable status --json'.
    This is the end-to-end operational readiness check.

    Validates:
    - STATUS: active/standby (not unknown) - Event 5
    - HEALTH: healthy (not unhealthy) - Event 5
    - HWSTATUS: consistent (not inconsistent) - Event 6

    Returns:
        tuple: (success: bool, error_message: str or None)
    """
    try:
        output = sonic_host.shell("show muxcable status --json", module_ignore_errors=True)
        if output.get('failed') and 'msg' in output:
            return False, f"Ansible error: {output['msg']}"
        if output.get('rc', 1) != 0:
            stderr = output.get('stderr', '').strip()
            return False, f"show muxcable status failed (rc={output.get('rc')}): {stderr or 'no error'}"

        mux_status = json.loads(output['stdout'])
        mux_cable_data = mux_status.get('MUX_CABLE', {})

        for port in mux_ports:
            if port not in mux_cable_data:
                return False, f"Port {port} not found in muxcable status"
            port_data = mux_cable_data[port]

            # Event 5a: STATUS must be active or standby
            status = port_data.get('STATUS', '').lower()
            if status not in ['active', 'standby']:
                return False, f"Port {port} STATUS is '{status}', expected active/standby"

            # Event 5b: HEALTH must be healthy
            health = port_data.get('HEALTH', '').lower()
            if health != 'healthy':
                return False, f"Port {port} HEALTH is '{health}', expected 'healthy'"

            # Event 6: HWSTATUS must be consistent (if present)
            hwstatus = port_data.get('HWSTATUS', '').lower()
            if hwstatus and hwstatus == 'inconsistent':
                return False, f"Port {port} HWSTATUS is 'inconsistent'"

        return True, None
    except json.JSONDecodeError as e:
        return False, f"JSON parse error: {e}"
    except Exception as e:
        return False, f"Exception: {e}"


def _check_asic_tunnel_created(sonic_host):
    """
    Event 7 (Active-Active only): Check that MuxTunnel exists in ASIC_DB.
    This verifies orchagent has programmed the tunnel to the ASIC.

    Returns:
        tuple: (success: bool, error_message: str or None)
    """
    try:
        result = sonic_host.shell(
            'redis-cli -n 1 keys "ASIC_STATE:SAI_OBJECT_TYPE_TUNNEL:*" | wc -l',
            module_ignore_errors=True
        )
        if result.get('failed') and 'msg' in result:
            return False, f"Ansible error: {result['msg']}"
        if result.get('rc', 1) != 0:
            return False, f"Failed to query ASIC_DB for tunnels (rc={result.get('rc')})"
        count = int(result.get('stdout', '0').strip())
        if count < 1:
            return False, f"No tunnels found in ASIC_DB (count={count})"
        return True, None
    except Exception as e:
        return False, f"Exception: {e}"


def _check_grpc_channel_up(sonic_host, active_active_ports):
    """
    Event 8 (Active-Active only): Check gRPC channel is up for active-active ports.
    Verifies HW_MUX_CABLE_TABLE state != "unknown" in APP_DB for active-active ports.

    Uses batched redis query to check all ports in 1 command.

    Returns:
        tuple: (success: bool, error_message: str or None)
    """
    if not active_active_ports:
        return True, None

    try:
        # Build batched redis command
        redis_cmds = "\n".join(f'HGET "HW_MUX_CABLE_TABLE:{port}" state' for port in active_active_ports)
        result = sonic_host.shell(
            f'redis-cli -n 0 <<EOF\n{redis_cmds}\nEOF',
            module_ignore_errors=True
        )
        if result.get('failed') and 'msg' in result:
            return False, f"Ansible error querying HW_MUX_CABLE_TABLE: {result['msg']}"
        if result.get('rc', 1) != 0:
            return False, f"APP_DB HW_MUX_CABLE_TABLE batch query failed (rc={result.get('rc')})"

        # Parse results
        states = result.get('stdout', '').strip().split('\n')
        if len(states) != len(active_active_ports):
            return False, f"APP_DB HW_MUX_CABLE_TABLE returned {len(states)} results, expected {len(active_active_ports)}"

        for port, state in zip(active_active_ports, states):
            state = state.strip()
            if not state or state == 'unknown' or state == '(nil)':
                return False, f"APP_DB HW_MUX_CABLE_TABLE:{port} state is '{state}', gRPC channel not up"
        return True, None
    except Exception as e:
        return False, f"Exception: {e}"


def _check_all_db_tables_ready(sonic_host, mux_ports, active_active_ports):
    """
    Combined DB readiness check - runs all DB checks in parallel.

    This combines events 3, 4, 7, 8 into a single parallel check since they
    query independent subsystems and can run concurrently.

    Checks (run in parallel):
    - APP_DB: MUX_CABLE_TABLE, HW_MUX_CABLE_TABLE state != unknown (Event 3)
    - STATE_DB: MUX_CABLE_TABLE, HW_MUX_CABLE_TABLE state = active/standby (Event 4)
    - STATE_DB: MUX_LINKMGR_TABLE state = healthy (Event 4)
    - ASIC_DB: SAI_OBJECT_TYPE_TUNNEL exists (Event 7, active-active only)
    - APP_DB: HW_MUX_CABLE_TABLE for active-active ports != unknown (Event 8)

    Returns:
        True if all DB conditions are met, False otherwise.
    """
    # Build list of checks to run in parallel
    checks = [
        ('APP_DB mux state', partial(_check_mux_state_initialized, sonic_host, mux_ports)),
        ('STATE_DB populated', partial(_check_state_db_populated, sonic_host, mux_ports)),
    ]

    # Add active-active specific checks
    if active_active_ports:
        checks.extend([
            ('ASIC_DB tunnel', partial(_check_asic_tunnel_created, sonic_host)),
            ('gRPC channel', partial(_check_grpc_channel_up, sonic_host, active_active_ports)),
        ])

    try:
        # Run all checks in parallel with 60s timeout per poll iteration
        # (increased from 30s to accommodate batched queries on slow networks)
        results = parallel_run_threaded(
            [check_func for _, check_func in checks],
            timeout=60,
            thread_count=len(checks)
        )

        # Check results and log any failures
        all_passed = True
        errors = []
        for (check_name, _), result in zip(checks, results):
            # Result is (success, error_msg) tuple
            success, error_msg = result if isinstance(result, tuple) else (result, None)
            if not success:
                error_detail = error_msg or "unknown error"
                errors.append(f"{check_name}: {error_detail}")
                all_passed = False

        if errors:
            # Log at INFO level so errors are visible in normal log output
            logger.info("DB check failures: %s", "; ".join(errors))

        return all_passed

    except TimeoutError:
        logger.warning("DB checks timed out after 30s")
        return False
    except Exception as e:
        logger.warning("Error running parallel DB checks: %s", e)
        return False


def wait_for_dualtor_stabilization(sonic_host, mux_config, timeout=300):
    """
    Event-driven DualToR stabilization with optimized parallel checks.

    Phases:
    1. Infrastructure (parallel): mux container + linkmgrd process
    2. Database layer (parallel poll): All DB tables checked together
    3. Final validation: CLI health check

    This approach minimizes wait time by polling all DB conditions together
    while still providing clear diagnostics on which phase/check failed.

    Args:
        sonic_host: SONiC host object
        mux_config: MUX_CABLE config dict from config_facts
        timeout: Maximum total timeout in seconds (default 300)

    Returns:
        True if all events completed successfully, False otherwise
    """
    mux_ports = list(mux_config.keys())
    if not mux_ports:
        logger.info("No mux ports configured, skipping DualToR stabilization")
        return True

    # Identify active-active ports (if any)
    active_active_ports = [
        port for port, cfg in mux_config.items()
        if cfg.get('cable_type', 'active-standby') == 'active-active'
    ]

    # Timeouts
    infra_timeout = 60      # Phase 1: container + linkmgrd
    db_timeout = 120        # Phase 2: all DB tables (can take longer)
    cli_timeout = 60        # Phase 3: CLI validation
    poll_interval = 5

    logger.info("DualToR stabilization: %d mux ports (%d active-active)",
                len(mux_ports), len(active_active_ports))

    # =========================================================================
    # Phase 1: Infrastructure (container + linkmgrd in parallel)
    # =========================================================================
    logger.debug("Phase 1: Waiting for mux infrastructure...")
    if not wait_until(infra_timeout, poll_interval, 0, _check_infrastructure_ready, sonic_host):
        logger.warning("DualToR stabilization failed: infrastructure not ready (see debug log)")
        return False
    logger.debug("Phase 1 complete: infrastructure ready")

    # =========================================================================
    # Phase 2: Database layer (all checks polled together)
    # =========================================================================
    # Events 3, 4, 7, 8 can all be checked in parallel since:
    # - Events 3-4 depend on linkmgrd (now running)
    # - Events 7-8 depend on ycabled/orchagent (independent subsystems)
    logger.debug("Phase 2: Waiting for all DB tables...")

    if not wait_until(db_timeout, poll_interval, 0,
                      _check_all_db_tables_ready, sonic_host, mux_ports, active_active_ports):
        logger.warning("DualToR stabilization failed: DB tables not ready (see debug log)")
        return False
    logger.debug("Phase 2 complete: all DB tables ready")

    # =========================================================================
    # Phase 3: Final CLI validation
    # =========================================================================
    # Events 5-6: CLI aggregates all state - final sanity check
    logger.debug("Phase 3: CLI validation...")

    # Wrapper to log errors from the tuple-returning check
    def cli_check_with_logging():
        success, error_msg = _check_muxcable_cli_healthy(sonic_host, mux_ports)
        if not success and error_msg:
            logger.info("CLI check failure: %s", error_msg)
        return success

    if not wait_until(cli_timeout, poll_interval, 0, cli_check_with_logging):
        logger.warning("DualToR stabilization failed: CLI status not healthy")
        return False
    logger.debug("Phase 3 complete: CLI validation passed")

    logger.info("DualToR subsystem fully stabilized")
    return True
