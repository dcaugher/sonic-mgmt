import logging
import pytest
import time
import re

from tests.common.fixtures.conn_graph_facts import enum_fanout_graph_facts      # noqa: F401
from tests.common.helpers.assertions import pytest_assert
from tests.common.helpers.pfc_storm import PFCStorm
from tests.common.helpers.pfcwd_helper import start_wd_on_ports, start_background_traffic     # noqa: F401
from tests.common.helpers.pfcwd_helper import calculate_pfcwd_default_timers

from tests.common.plugins.loganalyzer import DisableLogrotateCronContext
from tests.common.helpers.pfcwd_helper import send_background_traffic
from tests.common import config_reload

pytestmark = [
    pytest.mark.topology('any')
]

ITERATION_NUM = 20

logger = logging.getLogger(__name__)


def check_pfcwd_is_monitoring(dut, port, queue_idx):
    """
    Check if PFCWD is actively monitoring the specified port and queue.

    Args:
        dut: DUT host instance
        port: Port name to check
        queue_idx: Queue index being monitored

    Returns:
        dict: Status information including whether PFCWD is monitoring
    """
    status = {
        'is_monitoring': False,
        'pfcwd_config': None,
        'pfcwd_stats': None,
        'orchagent_running': False,
        'pfcwd_process_info': None
    }

    try:
        # Check if orchagent is running (PFCWD runs within orchagent)
        orchagent_check = dut.shell("pgrep -f orchagent", module_ignore_errors=True)
        status['orchagent_running'] = orchagent_check['rc'] == 0
        if status['orchagent_running']:
            status['pfcwd_process_info'] = orchagent_check['stdout'].strip()

        # Check PFCWD configuration for the port
        pfcwd_config = dut.shell("show pfcwd config", module_ignore_errors=True)
        status['pfcwd_config'] = pfcwd_config.get('stdout', '')

        # Check PFCWD stats for the specific queue
        pfcwd_stats = dut.shell("show pfcwd stats", module_ignore_errors=True)
        status['pfcwd_stats'] = pfcwd_stats.get('stdout', '')

        # Check if our port/queue appears in the PFCWD config
        if port in status['pfcwd_config']:
            status['is_monitoring'] = True

        logger.debug("PFCWD monitoring check for {}:{} - monitoring={}, orchagent={}".format(
            port, queue_idx, status['is_monitoring'], status['orchagent_running']))

    except Exception as e:
        logger.warning("Failed to check PFCWD monitoring status: {}".format(str(e)))

    return status


def check_syslog_rate_limiting(dut):
    """
    Check rsyslog rate limiting configuration and statistics.

    Args:
        dut: DUT host instance

    Returns:
        dict: Rate limiting information
    """
    info = {
        'rate_limit_enabled': None,
        'rate_limit_interval': None,
        'rate_limit_burst': None,
        'messages_dropped': 0,
        'rsyslog_stats': None,
        'imuxsock_config': None
    }

    try:
        # Check rsyslog configuration for rate limiting
        rsyslog_conf = dut.shell(
            "grep -E 'ratelimit|imuxsock|SysSock' /etc/rsyslog.conf /etc/rsyslog.d/*.conf 2>/dev/null || true",
            module_ignore_errors=True
        )
        info['imuxsock_config'] = rsyslog_conf.get('stdout', '')

        # Check for rate limit settings
        if 'SysSock.RateLimit.Interval' in info['imuxsock_config']:
            info['rate_limit_enabled'] = True
            # Extract interval and burst values if present
            interval_match = re.search(r'SysSock\.RateLimit\.Interval="?(\d+)"?', info['imuxsock_config'])
            burst_match = re.search(r'SysSock\.RateLimit\.Burst="?(\d+)"?', info['imuxsock_config'])
            if interval_match:
                info['rate_limit_interval'] = int(interval_match.group(1))
            if burst_match:
                info['rate_limit_burst'] = int(burst_match.group(1))
        else:
            info['rate_limit_enabled'] = False

        # Check rsyslog internal stats for dropped messages
        # This requires impstats module to be enabled
        rsyslog_stats = dut.shell(
            r"grep -i 'imuxsock.*lost\|rate-limited\|discarded' /var/log/syslog 2>/dev/null | tail -5 || true",
            module_ignore_errors=True
        )
        info['rsyslog_stats'] = rsyslog_stats.get('stdout', '')

        # Count recent rate-limit related messages
        drop_count = dut.shell(
            r"grep -c 'rate-limit\|messages lost\|discarded' /var/log/syslog 2>/dev/null || echo 0",
            module_ignore_errors=True
        )
        try:
            info['messages_dropped'] = int(drop_count.get('stdout', '0').strip())
        except ValueError:
            info['messages_dropped'] = 0

        logger.debug("Syslog rate limiting check - enabled={}, interval={}, burst={}, dropped={}".format(
            info['rate_limit_enabled'], info['rate_limit_interval'],
            info['rate_limit_burst'], info['messages_dropped']))

    except Exception as e:
        logger.warning("Failed to check syslog rate limiting: {}".format(str(e)))

    return info


def check_syslog_file_status(dut):
    """
    Check syslog file status to detect rotation or truncation.

    Args:
        dut: DUT host instance

    Returns:
        dict: Syslog file status information
    """
    info = {
        'syslog_inode': None,
        'syslog_size': None,
        'syslog_mtime': None,
        'rotated_files': []
    }

    try:
        # Get syslog file stats
        stat_result = dut.shell("stat -c '%i %s %Y' /var/log/syslog", module_ignore_errors=True)
        if stat_result['rc'] == 0:
            parts = stat_result['stdout'].strip().split()
            if len(parts) >= 3:
                info['syslog_inode'] = parts[0]
                info['syslog_size'] = int(parts[1])
                info['syslog_mtime'] = parts[2]

        # Check for recently rotated syslog files
        rotated = dut.shell("ls -la /var/log/syslog.* 2>/dev/null | head -5 || true", module_ignore_errors=True)
        info['rotated_files'] = rotated.get('stdout', '').strip().split('\n') if rotated.get('stdout') else []

        logger.debug("Syslog file status - inode={}, size={}, rotated_count={}".format(
            info['syslog_inode'], info['syslog_size'], len(info['rotated_files'])))

    except Exception as e:
        logger.warning("Failed to check syslog file status: {}".format(str(e)))

    return info


def check_pfc_counters(dut, port, queue_idx):
    """
    Check PFC counters to verify if PFC frames are being received.

    Args:
        dut: DUT host instance
        port: Port name
        queue_idx: Queue index

    Returns:
        dict: PFC counter information
    """
    info = {
        'rx_pfc_frames': None,
        'tx_pfc_frames': None,
        'pfc_pause_duration': None,
        'counter_output': None
    }

    try:
        # Get PFC counters for the port
        pfc_counters = dut.shell("show pfc counters | grep -E 'Port|{}'".
                                 format(port), module_ignore_errors=True)
        info['counter_output'] = pfc_counters.get('stdout', '')

        # Also check queue-specific counters if available
        queue_counters = dut.shell("show queue counters {} | head -20".format(port), module_ignore_errors=True)
        if queue_counters['rc'] == 0:
            info['queue_counters'] = queue_counters.get('stdout', '')

        logger.debug("PFC counters for {}:{} - output: {}".format(
            port, queue_idx, info['counter_output'][:200] if info['counter_output'] else 'None'))

    except Exception as e:
        logger.warning("Failed to check PFC counters: {}".format(str(e)))

    return info


def diagnose_missing_timestamp(dut, pattern, iteration, test_port, queue_idx, storm_handle):
    """
    Comprehensive diagnostic when a timestamp is missing.

    Args:
        dut: DUT host instance
        pattern: The syslog pattern that was not found
        iteration: Current test iteration number
        test_port: The port being tested
        queue_idx: Queue index
        storm_handle: PFCStorm instance

    Returns:
        dict: Diagnostic information
    """
    logger.warning("=" * 80)
    logger.warning("DIAGNOSTIC: Missing timestamp for pattern '{}' at iteration {}".format(pattern, iteration))
    logger.warning("=" * 80)

    diag = {
        'pattern': pattern,
        'iteration': iteration,
        'pfcwd_status': None,
        'syslog_rate_limit': None,
        'syslog_file_status': None,
        'pfc_counters': None,
        'recent_pfcwd_logs': None,
        'recent_syslog_errors': None
    }

    # 1. Check PFCWD monitoring status
    logger.info("[DIAG] Checking PFCWD monitoring status...")
    diag['pfcwd_status'] = check_pfcwd_is_monitoring(dut, test_port, queue_idx)
    logger.info("[DIAG] PFCWD status: orchagent_running={}, is_monitoring={}".format(
        diag['pfcwd_status']['orchagent_running'], diag['pfcwd_status']['is_monitoring']))
    logger.info("[DIAG] PFCWD config:\n{}".format(diag['pfcwd_status']['pfcwd_config']))
    logger.info("[DIAG] PFCWD stats:\n{}".format(diag['pfcwd_status']['pfcwd_stats']))

    # 2. Check syslog rate limiting
    logger.info("[DIAG] Checking syslog rate limiting...")
    diag['syslog_rate_limit'] = check_syslog_rate_limiting(dut)
    logger.info("[DIAG] Rate limiting: enabled={}, interval={}, burst={}, dropped={}".format(
        diag['syslog_rate_limit']['rate_limit_enabled'],
        diag['syslog_rate_limit']['rate_limit_interval'],
        diag['syslog_rate_limit']['rate_limit_burst'],
        diag['syslog_rate_limit']['messages_dropped']))
    if diag['syslog_rate_limit']['rsyslog_stats']:
        logger.info("[DIAG] Rsyslog stats:\n{}".format(diag['syslog_rate_limit']['rsyslog_stats']))

    # 3. Check syslog file status
    logger.info("[DIAG] Checking syslog file status...")
    diag['syslog_file_status'] = check_syslog_file_status(dut)
    logger.info("[DIAG] Syslog file: inode={}, size={}, rotated_files={}".format(
        diag['syslog_file_status']['syslog_inode'],
        diag['syslog_file_status']['syslog_size'],
        len(diag['syslog_file_status']['rotated_files'])))

    # 4. Check PFC counters
    logger.info("[DIAG] Checking PFC counters...")
    diag['pfc_counters'] = check_pfc_counters(dut, test_port, queue_idx)
    logger.info("[DIAG] PFC counters:\n{}".format(diag['pfc_counters']['counter_output']))

    # 5. Check recent PFCWD-related logs
    logger.info("[DIAG] Checking recent PFCWD logs...")
    try:
        recent_pfcwd = dut.shell(
            "grep -E 'PFC|storm|PFCWD|pfcwd' /var/log/syslog 2>/dev/null | tail -20 || true",
            module_ignore_errors=True
        )
        diag['recent_pfcwd_logs'] = recent_pfcwd.get('stdout', '')
        logger.info("[DIAG] Recent PFCWD logs:\n{}".format(diag['recent_pfcwd_logs']))
    except Exception as e:
        logger.warning("[DIAG] Failed to get recent PFCWD logs: {}".format(str(e)))

    # 6. Check for syslog errors
    logger.info("[DIAG] Checking for syslog errors...")
    try:
        syslog_errors = dut.shell(
            "grep -iE 'error|warning|failed' /var/log/syslog 2>/dev/null | tail -10 || true",
            module_ignore_errors=True
        )
        diag['recent_syslog_errors'] = syslog_errors.get('stdout', '')
        logger.info("[DIAG] Recent syslog errors:\n{}".format(diag['recent_syslog_errors']))
    except Exception as e:
        logger.warning("[DIAG] Failed to get syslog errors: {}".format(str(e)))

    # 7. Check fanout storm process (if not VS)
    if dut.facts['asic_type'] != 'vs' and storm_handle and storm_handle.peer_device:
        logger.info("[DIAG] Checking fanout storm process...")
        try:
            fanout_ps = storm_handle.peer_device.shell(
                "ps aux | grep -E 'pfc_gen|PFC' | grep -v grep || true",
                module_ignore_errors=True
            )
            diag['fanout_pfc_process'] = fanout_ps.get('stdout', '')
            logger.info("[DIAG] Fanout PFC processes:\n{}".format(diag['fanout_pfc_process']))
        except Exception as e:
            logger.warning("[DIAG] Failed to check fanout process: {}".format(str(e)))

    logger.warning("=" * 80)
    logger.warning("END DIAGNOSTIC for iteration {}".format(iteration))
    logger.warning("=" * 80)

    return diag


@pytest.fixture(scope="class")
def pfc_queue_idx(pfcwd_timer_setup_restore):
    # This is used by the common code, this needs to be defined
    # before using start_background_traffic() fixture.
    yield pfcwd_timer_setup_restore['storm_handle'].pfc_queue_idx


@pytest.fixture(autouse=True)
def ignore_loganalyzer_exceptions(enum_rand_one_per_hwsku_frontend_hostname, loganalyzer):
    """
    Fixture that ignores expected failures during test execution.

    Args:
        duthost (AnsibleHost): DUT instance
        loganalyzer (loganalyzer): Loganalyzer utility fixture
    """
    if loganalyzer:
        ignoreRegex = [
            (".*ERR syncd#syncd: :- process_on_fdb_event: "
             "invalid OIDs in fdb notifications, NOT translating and NOT storing in ASIC DB.*"),
            (".*ERR syncd#syncd: :- process_on_fdb_event: "
             "FDB notification was not sent since it contain invalid OIDs, bug.*")
        ]
        loganalyzer[enum_rand_one_per_hwsku_frontend_hostname].ignore_regex.extend(ignoreRegex)

    yield


@pytest.fixture(scope='module', autouse=True)
def pfcwd_timer_setup_restore(setup_pfc_test, enum_fanout_graph_facts, duthosts,        # noqa: F811
                              enum_rand_one_per_hwsku_frontend_hostname, fanouthosts):
    """
    Fixture that inits the test vars, start PFCwd on ports and cleans up after the test run

    Args:
        setup_pfc_test (fixture): module scoped, autouse PFC fixture
        enum_fanout_graph_facts (fixture): fanout graph info
        duthost (AnsibleHost): DUT instance
        fanouthosts (AnsibleHost): fanout instance

    Yields:
        timers (dict): pfcwd timer values
        storm_handle (PFCStorm): class PFCStorm instance
    """
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    asic_type = duthost.facts['asic_type']
    logger.info("--- Pfcwd timer test setup ---")
    setup_info = setup_pfc_test
    test_ports = setup_info['test_ports']
    timers = setup_info['pfc_timers']
    eth0_ip = setup_info['eth0_ip']
    dynamic_timers = calculate_pfcwd_default_timers(duthost)
    logger.info(f"Updating timers: original={timers}, dynamic={dynamic_timers}")
    timers.update(dynamic_timers)
    # In Python2, dict.keys() returns list object, but in Python3 returns an iterable but not indexable object.
    # So that convert to list explicitly.
    pfc_wd_test_port = list(test_ports.keys())[0]
    neighbors = setup_info['neighbors']
    fanout_info = enum_fanout_graph_facts
    dut = duthost
    fanout = fanouthosts
    peer_params = populate_peer_info(asic_type, neighbors, fanout_info, pfc_wd_test_port)
    storm_handle = set_storm_params(dut, fanout_info, fanout, peer_params)
    dut.command("pfcwd interval {}".format(timers['pfc_wd_poll_time']))
    start_wd_on_ports(dut, pfc_wd_test_port, timers['pfc_wd_restore_time'],
                      timers['pfc_wd_detect_time'])
    # enable routing from mgmt interface to localhost
    dut.sysctl(name="net.ipv4.conf.eth0.route_localnet", value=1, sysctl_set=True)
    # rule to forward syslog packets from mgmt interface to localhost
    syslog_ip = duthost.get_rsyslog_ipv4()
    dut.iptables(action="insert", chain="PREROUTING", table="nat", protocol="udp",
                 destination=eth0_ip, destination_port=514, jump="DNAT",
                 to_destination="{}:514".format(syslog_ip))

    logger.info("--- Pfcwd Timer Testrun ---")
    yield {'timers': timers,
           'storm_handle': storm_handle,
           'test_ports': test_ports,
           'selected_test_port': pfc_wd_test_port
           }

    logger.info("--- Pfcwd timer test cleanup ---")
    # clear pfcwd stats and reset to default for next run
    config_reload(duthost, safe_reload=True, check_intf_up_ports=True, wait_for_bgp=True)
    dut.iptables(table="nat", flush="yes")
    dut.sysctl(name="net.ipv4.conf.eth0.route_localnet", value=0, sysctl_set=True)
    storm_handle.stop_storm()


def populate_peer_info(asic_type, neighbors, fanout_info, port):
    """
    Build the peer_info map which will be used by the storm generation class

    Args:
        neighbors (dict): fanout info for each DUT port
        fanout_info (dict): fanout graph info
        port (string): test port

    Returns:
        peer_info (dict): all PFC params needed for fanout for storm generation
    """
    if asic_type == 'vs':
        return {}
    peer_dev = neighbors[port]['peerdevice']
    peer_port = neighbors[port]['peerport']
    peer_info = {'peerdevice': peer_dev,
                 'hwsku': fanout_info[peer_dev]['device_info']['HwSku'],
                 'pfc_fanout_interface': peer_port
                 }
    return peer_info


def set_storm_params(dut, fanout_info, fanout, peer_params):
    """
    Setup storm parameters

    Args:
        dut (AnsibleHost): DUT instance
        fanout_info (fixture): fanout graph info
        fanout (AnsibleHost): fanout instance
        peer_params (dict): all PFC params needed for fanout for storm generation

    Returns:
        storm_handle (PFCStorm): class PFCStorm intance
    """
    logger.info("Setting up storm params")
    pfc_queue_index = 4
    pfc_frames_count = 1000000
    peer_device = peer_params['peerdevice'] if dut.facts['asic_type'] != 'vs' else ""
    if dut.topo_type == 't2' and fanout[peer_device].os == 'sonic':
        pfc_gen_file = 'pfc_gen_t2.py'
        pfc_send_time = 8
    else:
        pfc_gen_file = 'pfc_gen.py'
        pfc_send_time = None
    storm_handle = PFCStorm(dut, fanout_info, fanout, pfc_queue_idx=pfc_queue_index,
                            pfc_frames_number=pfc_frames_count, pfc_gen_file=pfc_gen_file,
                            pfc_send_period=pfc_send_time, peer_info=peer_params)
    storm_handle.deploy_pfc_gen()
    return storm_handle


@pytest.mark.usefixtures('pfcwd_timer_setup_restore', 'start_background_traffic')
class TestPfcwdAllTimer(object):
    """ PFCwd timer test class """

    def _check_baseline(self, iteration, test_port, queue_idx):
        """
        Check baseline system state at the beginning of each iteration.
        """
        logger.info("[Iteration {}] Checking baseline system state...".format(iteration))

        # Track syslog file state (for detecting rotation)
        self._syslog_state_before = check_syslog_file_status(self.dut)
        logger.debug("[Iteration {}] Syslog inode before: {}, size: {}".format(
            iteration, self._syslog_state_before['syslog_inode'],
            self._syslog_state_before['syslog_size']))

        # Periodic detailed check every 5 iterations or on first iteration
        if iteration == 1 or iteration % 5 == 0:
            # Check PFCWD is monitoring
            pfcwd_status = check_pfcwd_is_monitoring(self.dut, test_port, queue_idx)
            if not pfcwd_status['is_monitoring']:
                logger.warning("[Iteration {}] PFCWD may not be monitoring port {}!".format(iteration, test_port))
                logger.warning("[Iteration {}] PFCWD config: {}".format(iteration, pfcwd_status['pfcwd_config']))

            if not pfcwd_status['orchagent_running']:
                logger.error("[Iteration {}] CRITICAL: orchagent is not running!".format(iteration))

            # Check syslog rate limiting
            rate_limit_info = check_syslog_rate_limiting(self.dut)
            if rate_limit_info['rate_limit_enabled']:
                logger.info("[Iteration {}] Syslog rate limiting is ENABLED - interval={}, burst={}".format(
                    iteration, rate_limit_info['rate_limit_interval'], rate_limit_info['rate_limit_burst']))
            if rate_limit_info['messages_dropped'] > 0:
                logger.warning("[Iteration {}] Syslog has dropped {} messages!".format(
                    iteration, rate_limit_info['messages_dropped']))

            # Log PFC counter baseline
            pfc_counters = check_pfc_counters(self.dut, test_port, queue_idx)
            logger.debug("[Iteration {}] PFC counters baseline: {}".format(
                iteration, pfc_counters['counter_output'][:200] if pfc_counters['counter_output'] else 'None'))

    def _check_syslog_rotation(self, iteration):
        """
        Check if syslog was rotated during the test iteration.
        """
        syslog_state_after = check_syslog_file_status(self.dut)

        if hasattr(self, '_syslog_state_before') and self._syslog_state_before:
            if self._syslog_state_before['syslog_inode'] != syslog_state_after['syslog_inode']:
                logger.warning("[Iteration {}] SYSLOG WAS ROTATED! inode changed from {} to {}".format(
                    iteration, self._syslog_state_before['syslog_inode'], syslog_state_after['syslog_inode']))
                return True

            # Check if file shrank (possible truncation)
            if (self._syslog_state_before['syslog_size'] is not None and
                    syslog_state_after['syslog_size'] is not None and
                    syslog_state_after['syslog_size'] < self._syslog_state_before['syslog_size']):
                logger.warning("[Iteration {}] SYSLOG MAY HAVE BEEN TRUNCATED! size went from {} to {}".format(
                    iteration, self._syslog_state_before['syslog_size'], syslog_state_after['syslog_size']))
                return True

        return False

    def run_test(self, setup_info):
        """
        Test execution
        """
        # Get current iteration number from the instance
        iteration = getattr(self, '_current_iteration', 0)
        test_port = setup_info['selected_test_port']
        queue_idx = self.storm_handle.pfc_queue_idx

        # Check baseline at the start of each iteration
        self._check_baseline(iteration, test_port, queue_idx)

        with DisableLogrotateCronContext(self.dut):
            logger.info("Flush logs")
            self.dut.shell("logrotate -f /etc/logrotate.conf")

        selected_test_ports = [test_port]
        test_ports_info = setup_info['test_ports']
        queues = [queue_idx]

        with send_background_traffic(self.dut, self.ptf, queues, selected_test_ports, test_ports_info, pkt_count=500):
            self.storm_handle.start_storm()
            logger.info("Wait for queue to recover from PFC storm")
            time.sleep(32)
            self.storm_handle.stop_storm()
            time.sleep(16)

        if self.dut.facts['asic_type'] == 'vs':
            logger.info("Skip time detect for VS")
            return

        # Check if syslog was rotated during the test
        if self._check_syslog_rotation(iteration):
            logger.warning("[Iteration {}] Syslog rotation detected - timestamps may be lost".format(iteration))

        skip_this_loop = False
        missing_patterns = []

        if self.dut.topo_type == 't2' and self.storm_handle.peer_device.os == 'sonic':
            storm_detect_ms = self.retrieve_timestamp("[d]etected PFC storm")
            if storm_detect_ms == 0:
                missing_patterns.append("[d]etected PFC storm")
        else:
            storm_start_ms = self.retrieve_timestamp("[P]FC_STORM_START")
            storm_detect_ms = self.retrieve_timestamp("[d]etected PFC storm")
            if storm_start_ms == 0:
                missing_patterns.append("[P]FC_STORM_START")
            if storm_detect_ms == 0:
                missing_patterns.append("[d]etected PFC storm")

        logger.info("Wait for PFC storm end marker to appear in logs")
        time.sleep(16)

        if self.dut.topo_type == 't2' and self.storm_handle.peer_device.os == 'sonic':
            storm_restore_ms = self.retrieve_timestamp("[s]torm restored")
            if storm_restore_ms == 0:
                missing_patterns.append("[s]torm restored")
        else:
            storm_end_ms = self.retrieve_timestamp("[P]FC_STORM_END")
            storm_restore_ms = self.retrieve_timestamp("[s]torm restored")
            if storm_end_ms == 0:
                missing_patterns.append("[P]FC_STORM_END")
            if storm_restore_ms == 0:
                missing_patterns.append("[s]torm restored")

        if self.dut.topo_type == 't2' and self.storm_handle.peer_device.os == 'sonic':
            if storm_detect_ms == 0 or storm_restore_ms == 0:
                logging.warning("storm_detect_ms {} or storm_restore_ms {} is 0".format(
                    storm_detect_ms, storm_restore_ms))
                skip_this_loop = True
        else:
            if storm_start_ms == 0 or storm_detect_ms == 0 or storm_end_ms == 0 or storm_restore_ms == 0:
                logging.warning("storm_start_ms {} or storm_detect_ms {} or "
                                "storm_end_ms {} or storm_restore_ms {} is 0".format(
                                    storm_start_ms, storm_detect_ms, storm_end_ms, storm_restore_ms))
                skip_this_loop = True

        if skip_this_loop:
            logger.warning("Skip this loop due to missing timestamps")

            # Track consecutive failures
            self._consecutive_failures = getattr(self, '_consecutive_failures', 0) + 1
            logger.warning("[Iteration {}] Consecutive failures: {}".format(iteration, self._consecutive_failures))

            # Run full diagnostics if this is the first failure or if we've hit multiple consecutive failures
            if self._consecutive_failures == 1 or self._consecutive_failures >= 3:
                for pattern in missing_patterns:
                    diagnose_missing_timestamp(
                        self.dut, pattern, iteration, test_port, queue_idx, self.storm_handle)
            return
        else:
            # Reset consecutive failure counter on success
            self._consecutive_failures = 0

        if not (self.dut.topo_type == 't2' and self.storm_handle.peer_device.os == 'sonic'):
            real_detect_time = storm_detect_ms - storm_start_ms
            real_restore_time = storm_restore_ms - storm_end_ms
            self.all_detect_time.append(real_detect_time)
            self.all_restore_time.append(real_restore_time)

        dut_detect_restore_time = storm_restore_ms - storm_detect_ms
        logger.info(
            "Iteration all_dut_detect_time list {} and length {}".format(
                ",".join(str(i) for i in self.all_detect_time), len(self.all_detect_time)))
        self.all_dut_detect_restore_time.append(dut_detect_restore_time)
        logger.info(
            "Iteration all_dut_detect_restore_time list {} and length {}".format(
                ",".join(str(i) for i in self.all_dut_detect_restore_time), len(self.all_dut_detect_restore_time)))

    def verify_pfcwd_timers(self):
        """
        Compare the timestamps obtained and verify the timer accuracy
        """
        if self.dut.facts['asic_type'] == 'vs':
            logger.info("Skip timer verify for VS")
            return

        self.all_detect_time.sort()
        self.all_restore_time.sort()
        logger.info("Verify that real detection time is not greater than configured")
        logger.info("sorted all detect time {}".format(self.all_detect_time))
        logger.info("sorted all restore time {}".format(self.all_restore_time))

        check_point = ITERATION_NUM // 2 - 1
        config_detect_time = self.timers['pfc_wd_detect_time'] + self.timers['pfc_wd_poll_time']
        # Loose the check if two conditions are met
        # 1. Leaf-fanout is Non-Onyx or non-Mellanox SONiC devices
        # 2. Device is Mellanox plaform, Loose the check
        # 3. Device is broadcom plaform, add half of polling time as compensation for the detect config time
        # It's because the pfc_gen.py running on leaf-fanout can't guarantee the PFCWD is triggered consistently
        logger.debug("dut asic_type {}".format(self.dut.facts['asic_type']))
        for fanouthost in list(self.fanout.values()):
            if fanouthost.get_fanout_os() != "onyx" or \
                    fanouthost.get_fanout_os() == "sonic" and fanouthost.facts['asic_type'] != "mellanox":
                if self.dut.facts['asic_type'] == "mellanox":
                    logger.info("Loose the check for non-Onyx or non-Mellanox leaf-fanout testbed")
                    check_point = ITERATION_NUM // 3 - 1
                    break
                elif self.dut.facts['asic_type'] == "broadcom":
                    logger.info("Configuring detect time for broadcom DUT")
                    config_detect_time = (
                        self.timers['pfc_wd_detect_time'] +
                        self.timers['pfc_wd_poll_time'] +
                        (self.timers['pfc_wd_poll_time'] // 2)
                    )
                    break

        # Validate that we have enough samples before accessing list by index
        # If more than half of iterations failed to collect timestamps, fail with a clear message
        required_samples = check_point + 1
        detect_count = len(self.all_detect_time)
        restore_count = len(self.all_restore_time)
        if detect_count < required_samples or restore_count < required_samples:
            detect_failures = ITERATION_NUM - detect_count
            restore_failures = ITERATION_NUM - restore_count
            pytest.fail(
                "Too many iterations failed to collect PFCWD timestamps. "
                "Detect time samples: {}/{} (failures: {}), Restore time samples: {}/{} (failures: {}). "
                "Required at least {} samples. This may indicate environment or timing issues.".format(
                    detect_count, ITERATION_NUM, detect_failures,
                    restore_count, ITERATION_NUM, restore_failures,
                    required_samples))

        err_msg = ("Real detection time is greater than configured: Real detect time: {} "
                   "Expected: {} (wd_detect_time + wd_poll_time)".format(self.all_detect_time[check_point],
                                                                         config_detect_time))
        pytest_assert(self.all_detect_time[check_point] < config_detect_time, err_msg)

        if self.timers['pfc_wd_poll_time'] < self.timers['pfc_wd_detect_time']:
            logger.info("Verify that real detection time is not less than configured")
            err_msg = ("Real detection time is less than configured: Real detect time: {} "
                       "Expected: {} (wd_detect_time)".format(self.all_detect_time[check_point],
                                                              self.timers['pfc_wd_detect_time']))
            pytest_assert(self.all_detect_time[check_point] > self.timers['pfc_wd_detect_time'], err_msg)

        if self.timers['pfc_wd_poll_time'] < self.timers['pfc_wd_restore_time']:
            logger.info("Verify that real restoration time is not less than configured")
            err_msg = ("Real restoration time is less than configured: Real restore time: {} "
                       "Expected: {} (wd_restore_time)".format(self.all_restore_time[check_point],
                                                               self.timers['pfc_wd_restore_time']))
            pytest_assert(self.all_restore_time[check_point] > self.timers['pfc_wd_restore_time'], err_msg)

        logger.info("Verify that real restoration time is less than configured")
        config_restore_time = self.timers['pfc_wd_restore_time'] + self.timers['pfc_wd_poll_time']
        err_msg = ("Real restoration time is greater than configured: Real restore time: {} "
                   "Expected: {} (wd_restore_time + wd_poll_time)".format(self.all_restore_time[check_point],
                                                                          config_restore_time))
        pytest_assert(self.all_restore_time[check_point] < config_restore_time, err_msg)

    def verify_pfcwd_timers_t2(self):
        """
        Compare the timestamps obtained and verify the timer accuracy for t2 chassis
        """
        if self.dut.facts['asic_type'] == 'vs':
            logger.info("Skip timer verify for VS")
            return

        self.all_dut_detect_restore_time.sort()
        # Detect to restore elapsed time should always be less than 10 seconds since
        # storm is sent for 8 seconds
        dut_config_pfcwd_time = 10000

        logger.info(
            "all_dut_detect_restore_time sorted list {} and length {}".format(
                ",".join(str(i) for i in self.all_dut_detect_restore_time), len(self.all_dut_detect_restore_time)))

        logger.info("Verify that real dut detection-restoration time is less than expected value")
        err_msg = ("Real dut detection-restoration time is greater than configured: Real dut detection-restore time: {}"
                   " Expected: {}".format(self.all_dut_detect_restore_time[5], dut_config_pfcwd_time))
        pytest_assert(self.all_dut_detect_restore_time[5] < dut_config_pfcwd_time, err_msg)

    def retrieve_timestamp(self, pattern):
        """
        Retreives the syslog timestamp in ms associated with the pattern

        Args:
            pattern (string): pattern to be searched in the syslog

        Returns:
            timestamp_ms (int): syslog timestamp in ms for the line matching the pattern
        """
        try:
            cmd = "grep \"{}\" /var/log/syslog".format(pattern)
            syslog_msg = self.dut.shell(cmd)['stdout']

            # Regular expressions for the two timestamp formats
            regex = re.compile(r'\b[A-Za-z]{3}\s{1,2}\d{1,2} \d{2}:\d{2}:\d{2}\.\d{6}\b')
            search_string = regex.search(syslog_msg)
            if search_string:
                timestamp = search_string.group()
            else:
                logger.warning("Get timestamp: Unexpected syslog message format, syslog_msg {}".format(syslog_msg))
                return int(0)

            timestamp_ms = self.dut.shell("date -d '{}' +%s%3N".format(timestamp))['stdout']
            return int(timestamp_ms)
        except Exception as e:
            logger.warning("Get timestamp: An unexpected error occurred: pattern {} err {}".format(pattern, str(e)))
            return int(0)

    def test_pfcwd_timer_accuracy(self, duthosts, ptfhost, enum_rand_one_per_hwsku_frontend_hostname,
                                  pfcwd_timer_setup_restore, fanouthosts, set_pfc_time_cisco_8000):
        """
        Tests PFCwd timer accuracy

        Args:
            duthost (AnsibleHost): DUT instance
            pfcwd_timer_setup_restore (fixture): class scoped autouse setup fixture
        """
        duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
        setup_info = pfcwd_timer_setup_restore
        self.storm_handle = setup_info['storm_handle']
        self.timers = setup_info['timers']
        self.dut = duthost
        self.ptf = ptfhost
        self.fanout = fanouthosts
        self.all_detect_time = list()
        self.all_restore_time = list()
        self.all_dut_detect_restore_time = list()
        self._consecutive_failures = 0
        self._total_failures = 0
        self._failure_iterations = []

        test_port = setup_info['selected_test_port']
        queue_idx = self.storm_handle.pfc_queue_idx

        # ============================================================
        # INITIAL ENVIRONMENT CHECK
        # ============================================================
        logger.info("=" * 80)
        logger.info("PFCWD TIMER ACCURACY TEST - INITIAL ENVIRONMENT CHECK")
        logger.info("=" * 80)
        logger.info("Test port: {}, Queue: {}".format(test_port, queue_idx))
        logger.info("Timers: {}".format(self.timers))
        logger.info("DUT: {} (asic_type: {}, topo_type: {})".format(
            duthost.hostname, duthost.facts.get('asic_type'), duthost.topo_type))

        if self.dut.facts['asic_type'] != 'vs':
            # Check initial PFCWD status
            logger.info("Checking initial PFCWD status...")
            initial_pfcwd_status = check_pfcwd_is_monitoring(self.dut, test_port, queue_idx)
            logger.info("PFCWD monitoring: {}, orchagent running: {}".format(
                initial_pfcwd_status['is_monitoring'], initial_pfcwd_status['orchagent_running']))
            logger.info("PFCWD config:\n{}".format(initial_pfcwd_status['pfcwd_config']))

            # Check syslog rate limiting configuration
            logger.info("Checking syslog rate limiting configuration...")
            initial_rate_limit = check_syslog_rate_limiting(self.dut)
            logger.info("Syslog rate limiting: enabled={}, interval={}, burst={}".format(
                initial_rate_limit['rate_limit_enabled'],
                initial_rate_limit['rate_limit_interval'],
                initial_rate_limit['rate_limit_burst']))
            if initial_rate_limit['imuxsock_config']:
                logger.info("Rsyslog config:\n{}".format(initial_rate_limit['imuxsock_config']))

            # Check initial PFC counters
            logger.info("Checking initial PFC counters...")
            initial_pfc_counters = check_pfc_counters(self.dut, test_port, queue_idx)
            logger.info("Initial PFC counters:\n{}".format(initial_pfc_counters['counter_output']))

            # Check initial syslog file status
            logger.info("Checking initial syslog file status...")
            initial_syslog_status = check_syslog_file_status(self.dut)
            logger.info("Syslog inode: {}, size: {}".format(
                initial_syslog_status['syslog_inode'], initial_syslog_status['syslog_size']))

        logger.info("=" * 80)
        logger.info("Starting test iterations...")
        logger.info("=" * 80)

        try:
            if self.dut.topo_type == 't2' and self.storm_handle.peer_device.os == 'sonic':
                for i in range(1, 11):
                    logger.info("--- Pfcwd Timer Test iteration #{} ---".format(i))
                    self._current_iteration = i
                    self.run_test(setup_info)

                    # Track failures
                    if self._consecutive_failures > 0:
                        self._total_failures += 1
                        self._failure_iterations.append(i)

                self.verify_pfcwd_timers_t2()
            else:
                for i in range(1, ITERATION_NUM):
                    logger.info("--- Pfcwd Timer Test iteration #{} ---".format(i))
                    self._current_iteration = i

                    cmd = "show pfc counters"
                    pfcwd_cmd_response = self.dut.shell(cmd, module_ignore_errors=True)
                    logger.debug("loop {} cmd {} rsp {}".format(i, cmd, pfcwd_cmd_response.get('stdout', None)))

                    cmd = "show pfcwd stats"
                    pfcwd_cmd_response = self.dut.shell(cmd, module_ignore_errors=True)
                    logger.debug("loop {} cmd {} rsp {}".format(i, cmd, pfcwd_cmd_response.get('stdout', None)))

                    prev_consecutive = self._consecutive_failures
                    self.run_test(setup_info)

                    # Track if this iteration failed (consecutive counter increased)
                    if self._consecutive_failures > prev_consecutive:
                        self._total_failures += 1
                        self._failure_iterations.append(i)

                # Log summary before verification
                logger.info("=" * 80)
                logger.info("TEST ITERATION SUMMARY")
                logger.info("=" * 80)
                logger.info("Total iterations: {}".format(ITERATION_NUM - 1))
                logger.info("Successful iterations: {}".format(ITERATION_NUM - 1 - self._total_failures))
                logger.info("Failed iterations: {} - {}".format(
                    self._total_failures, self._failure_iterations if self._failure_iterations else "none"))
                logger.info("Detect time samples collected: {}".format(len(self.all_detect_time)))
                logger.info("Restore time samples collected: {}".format(len(self.all_restore_time)))
                logger.info("=" * 80)

                self.verify_pfcwd_timers()

        except Exception as e:
            logger.info("exception: ")
            cmd = "show pfc counters"
            pfcwd_cmd_response = self.dut.shell(cmd, module_ignore_errors=True)
            logger.info("pfcwd_cmd {} response: {}".format(cmd, pfcwd_cmd_response.get('stdout', None)))

            cmd = "show pfcwd stats"
            pfcwd_cmd_response = self.dut.shell(cmd, module_ignore_errors=True)
            logger.info("pfcwd_cmd {} response: {}".format(cmd, pfcwd_cmd_response.get('stdout', None)))

            # Run final diagnostics on exception
            if self.dut.facts['asic_type'] != 'vs':
                logger.info("Running final diagnostics due to exception...")
                diagnose_missing_timestamp(
                    self.dut, "exception_diagnostics", self._current_iteration,
                    test_port, queue_idx, self.storm_handle)

            pytest.fail(str(e))

        finally:
            if self.storm_handle:
                self.storm_handle.stop_storm()
