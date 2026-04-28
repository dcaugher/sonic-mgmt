import logging
import os
import time
import pytest

from tests.common.fixtures.conn_graph_facts import enum_fanout_graph_facts      # noqa: F401
from tests.common.helpers.pfc_storm import PFCMultiStorm
from tests.common.plugins.loganalyzer.loganalyzer import LogAnalyzer
from tests.common.helpers.pfcwd_helper import start_wd_on_ports, update_pfc_poll_interval, \
    start_background_traffic  # noqa: F401
from tests.common.helpers.pfcwd_helper import EXPECT_PFC_WD_DETECT_RE, EXPECT_PFC_WD_RESTORE_RE, \
    fetch_vendor_specific_diagnosis_re, verify_all_ports_pfc_storm_in_expected_state, \
    get_pfc_storm_baseline_counters
from tests.common.dualtor.mux_simulator_control import toggle_all_simulator_ports_to_enum_rand_one_per_hwsku_frontend_host_m # noqa F401, E501
from tests.common.helpers.pfcwd_helper import send_background_traffic
from tests.common import config_reload
from tests.common.utilities import wait_until
from tests.common.helpers.assertions import pytest_assert

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "templates")
FILE_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "files")

# Enable detailed PFC debugging instrumentation
PFC_DEBUG_ENABLED = True

# Cache for platform type to avoid repeated lookups
_platform_cache = {}


def _is_cisco_8000(duthost):
    """Check if DUT is cisco-8000 platform. Results are cached."""
    hostname = duthost.hostname
    if hostname not in _platform_cache:
        _platform_cache[hostname] = duthost.facts.get('asic_type', '').lower() == 'cisco-8000'
    return _platform_cache[hostname]


pytestmark = [
    pytest.mark.disable_loganalyzer,
    pytest.mark.topology('any')
]

logger = logging.getLogger(__name__)


# ============================================================================
# PFC STORM DEBUG INSTRUMENTATION
# These functions collect diagnostic data to help identify root cause of
# PFC storm detection failures. Set PFC_DEBUG_ENABLED = False to disable.
# ============================================================================

def _debug_collect_port_health(duthost, test_ports):
    """
    Collect port status, PFC config, and queue configuration.
    Helps identify Root Cause #2: Port/link connectivity issues.
    """
    if not PFC_DEBUG_ENABLED:
        return
    logger.info("=== DEBUG: Collecting port health information ===")
    
    # Overall port status
    try:
        result = duthost.shell("show interface status", module_ignore_errors=True)
        logger.info(f"Interface status:\n{result.get('stdout', 'N/A')}")
    except Exception as e:
        logger.warning(f"Failed to get interface status: {e}")
    
    # PFC configuration per port
    try:
        result = duthost.shell("show pfc config", module_ignore_errors=True)
        logger.info(f"PFC config:\n{result.get('stdout', 'N/A')}")
    except Exception as e:
        logger.warning(f"Failed to get PFC config: {e}")
    
    # PFCWD config
    try:
        result = duthost.shell("show pfcwd config", module_ignore_errors=True)
        logger.info(f"PFCWD config:\n{result.get('stdout', 'N/A')}")
    except Exception as e:
        logger.warning(f"Failed to get PFCWD config: {e}")


def _debug_collect_pfc_counters(duthost, test_ports, label):
    """
    Collect PFC RX counters on DUT.
    Helps identify Root Cause #1: PFC storm not being generated/received.
    """
    if not PFC_DEBUG_ENABLED:
        return
    logger.info(f"=== DEBUG [{label}]: Collecting PFC counters ===")
    
    try:
        result = duthost.shell("show pfc counters", module_ignore_errors=True)
        logger.info(f"PFC counters [{label}]:\n{result.get('stdout', 'N/A')}")
    except Exception as e:
        logger.warning(f"Failed to get PFC counters: {e}")


def _debug_collect_pfcwd_stats(duthost, label):
    """
    Collect detailed PFCWD stats.
    Helps identify Root Cause #3: Timing/race conditions.
    """
    if not PFC_DEBUG_ENABLED:
        return
    logger.info(f"=== DEBUG [{label}]: Collecting PFCWD stats ===")
    
    try:
        result = duthost.shell("show pfcwd stats", module_ignore_errors=True)
        logger.info(f"PFCWD stats [{label}]:\n{result.get('stdout', 'N/A')}")
    except Exception as e:
        logger.warning(f"Failed to get PFCWD stats: {e}")


def _debug_verify_fanout_pfc_gen(storm_hndle):
    """
    Verify pfc_gen.py is running on fanout switches and collect TX counters.
    Helps identify Root Cause #1: PFC storm generation issue.
    """
    if not PFC_DEBUG_ENABLED:
        return
    logger.info("=== DEBUG: Verifying PFC generation on fanouts ===")
    
    for peer, handle in storm_hndle.storm_handle.items():
        fanout = handle.peer_device
        fanout_os = fanout.get_fanout_os() if hasattr(fanout, 'get_fanout_os') else 'unknown'
        logger.info(f"Checking fanout {peer} (OS: {fanout_os})")
        
        # Check if pfc_gen.py is running
        try:
            result = fanout.shell("pgrep -af pfc_gen.py || echo 'NOT RUNNING'", 
                                  module_ignore_errors=True)
            logger.info(f"pfc_gen.py on {peer}: {result.get('stdout', 'N/A')}")
        except Exception as e:
            logger.warning(f"Failed to check pfc_gen.py on {peer}: {e}")
        
        # Check fanout interface PFC counters (SONiC fanouts)
        if fanout_os == 'sonic':
            try:
                intfs = handle.peer_info.get('pfc_fanout_interface', '').split(',')
                intf_list = ','.join(intfs[:5])  # Sample first 5
                result = fanout.shell(f"show pfc counters | head -20", 
                                      module_ignore_errors=True)
                logger.info(f"Fanout {peer} PFC TX counters:\n{result.get('stdout', 'N/A')}")
            except Exception as e:
                logger.warning(f"Failed to get fanout PFC counters: {e}")


def _debug_collect_platform_specific_diag(duthost, test_ports=None):
    """
    Collect platform-specific PFC diagnostics.
    Helps identify Root Cause #4: Platform-specific behavior.
    
    Args:
        duthost: DUT host instance
        test_ports: Optional list of test ports for detailed per-port analysis
    """
    if not PFC_DEBUG_ENABLED:
        return
    asic_type = duthost.facts.get('asic_type', 'unknown')
    logger.info(f"=== DEBUG: Collecting platform-specific diagnostics (ASIC: {asic_type}) ===")
    
    # PFC WD global config from DB
    try:
        result = duthost.shell(
            "sonic-db-cli CONFIG_DB HGETALL 'PFC_WD|GLOBAL'",
            module_ignore_errors=True)
        logger.info(f"PFC_WD GLOBAL config: {result.get('stdout', 'N/A')}")
    except Exception as e:
        logger.warning(f"Failed to get PFC_WD GLOBAL: {e}")
    
    # Flex counter config for PFCWD
    try:
        result = duthost.shell(
            "sonic-db-cli CONFIG_DB HGETALL 'FLEX_COUNTER_TABLE|PFCWD'",
            module_ignore_errors=True)
        logger.info(f"PFCWD flex counter config: {result.get('stdout', 'N/A')}")
    except Exception as e:
        logger.warning(f"Failed to get flex counter config: {e}")
    
    # Sample per-port PFC_WD config
    try:
        result = duthost.shell(
            "sonic-db-cli CONFIG_DB KEYS 'PFC_WD|Ethernet*' | head -5",
            module_ignore_errors=True)
        keys = result.get('stdout', '').strip().split('\n')[:3]
        for key in keys:
            if key:
                result = duthost.shell(
                    f"sonic-db-cli CONFIG_DB HGETALL '{key}'",
                    module_ignore_errors=True)
                logger.info(f"{key}: {result.get('stdout', 'N/A')}")
    except Exception as e:
        logger.warning(f"Failed to get per-port PFC_WD config: {e}")
    
    # Cisco-8000 specific diagnostics
    if _is_cisco_8000(duthost):
        _debug_collect_cisco_8000_diag(duthost, test_ports)


def _debug_collect_cisco_8000_diag(duthost, test_ports=None):
    """
    Cisco-8000 specific diagnostics for PFC/PFCWD issues.
    
    Investigates:
    - QoS map differences between port types (AZURE vs AZURE_UPLINK)
    - Per-port tc_to_queue_map assignment
    - Mux cable state (for dualtor topology)
    - PFCWD flex counter status per queue
    - SAI-level PFC events
    
    Args:
        duthost: DUT host instance
        test_ports: Optional list of test ports to analyze
    """
    logger.info("=== DEBUG [Cisco-8000]: Collecting platform-specific diagnostics ===")
    
    # 1. Check QoS MAP configurations (AZURE vs AZURE_UPLINK)
    logger.info("--- QoS Map Configurations ---")
    try:
        # Get all TC_TO_QUEUE_MAP entries
        result = duthost.shell(
            "sonic-db-cli CONFIG_DB KEYS 'TC_TO_QUEUE_MAP|*'",
            module_ignore_errors=True)
        qos_maps = result.get('stdout', '').strip().split('\n')
        for qos_map in qos_maps:
            if qos_map:
                result = duthost.shell(
                    f"sonic-db-cli CONFIG_DB HGETALL '{qos_map}'",
                    module_ignore_errors=True)
                logger.info(f"{qos_map}: {result.get('stdout', 'N/A')}")
    except Exception as e:
        logger.warning(f"Failed to get TC_TO_QUEUE_MAP: {e}")
    
    # 2. Check per-port QoS map assignment (this reveals AZURE vs AZURE_UPLINK per port)
    logger.info("--- Per-Port QoS Map Assignment ---")
    sample_ports = test_ports[:6] if test_ports and len(test_ports) > 6 else (test_ports or [])
    # Also include a couple of known uplink ports for comparison
    uplink_sample = ['Ethernet224', 'Ethernet232']
    all_sample_ports = list(set(sample_ports + uplink_sample))
    
    port_qos_info = {}
    for port in sorted(all_sample_ports):
        try:
            result = duthost.shell(
                f"sonic-db-cli CONFIG_DB HGET 'PORT_QOS_MAP|{port}' 'tc_to_queue_map'",
                module_ignore_errors=True)
            tc_map = result.get('stdout', '').strip()
            
            result = duthost.shell(
                f"sonic-db-cli CONFIG_DB HGET 'PORT_QOS_MAP|{port}' 'pfc_to_queue_map'",
                module_ignore_errors=True)
            pfc_map = result.get('stdout', '').strip()
            
            port_qos_info[port] = {'tc_to_queue_map': tc_map, 'pfc_to_queue_map': pfc_map}
            logger.info(f"  {port}: tc_to_queue={tc_map}, pfc_to_queue={pfc_map}")
        except Exception as e:
            logger.warning(f"Failed to get QoS map for {port}: {e}")
    
    # 3. Check mux cable state (critical for dualtor topology)
    logger.info("--- Mux Cable State ---")
    try:
        result = duthost.shell("show mux status", module_ignore_errors=True)
        if result.get('rc') == 0:
            logger.info(f"Mux status:\n{result.get('stdout', 'N/A')}")
        else:
            logger.info("Mux cable not configured (non-dualtor topology)")
    except Exception as e:
        logger.warning(f"Failed to get mux status: {e}")
    
    # 4. Check MUX_CABLE table for port classification
    logger.info("--- MUX_CABLE Configuration (server-facing port identification) ---")
    try:
        result = duthost.shell(
            "sonic-db-cli CONFIG_DB KEYS 'MUX_CABLE|*' | head -10",
            module_ignore_errors=True)
        mux_ports = result.get('stdout', '').strip().split('\n')
        mux_port_list = [p.replace('MUX_CABLE|', '') for p in mux_ports if p]
        logger.info(f"MUX_CABLE ports (server-facing): {mux_port_list[:10]}...")
        
        # Classify test ports
        if test_ports:
            mux_test_ports = [p for p in test_ports if p in mux_port_list]
            non_mux_test_ports = [p for p in test_ports if p not in mux_port_list]
            logger.info(f"Test ports classification:")
            logger.info(f"  Server-facing (mux): {len(mux_test_ports)} ports - {sorted(mux_test_ports)[:5]}...")
            logger.info(f"  Uplink (non-mux): {len(non_mux_test_ports)} ports - {sorted(non_mux_test_ports)}")
    except Exception as e:
        logger.warning(f"Failed to get MUX_CABLE config: {e}")
    
    # 5. Check PFCWD flex counter status - are counters being polled for all queues?
    logger.info("--- PFCWD Flex Counter Queue Status ---")
    try:
        # Check if PFCWD counters are enabled
        result = duthost.shell(
            "sonic-db-cli FLEX_COUNTER_DB KEYS 'FLEX_COUNTER_TABLE:PFC_WD:*' | wc -l",
            module_ignore_errors=True)
        counter_count = result.get('stdout', '').strip()
        logger.info(f"Total PFCWD flex counter entries: {counter_count}")
        
        # Sample a few entries to see which queue OIDs are being monitored
        result = duthost.shell(
            "sonic-db-cli FLEX_COUNTER_DB KEYS 'FLEX_COUNTER_TABLE:PFC_WD:*' | head -5",
            module_ignore_errors=True)
        sample_keys = result.get('stdout', '').strip().split('\n')
        for key in sample_keys[:3]:
            if key:
                result = duthost.shell(
                    f"sonic-db-cli FLEX_COUNTER_DB HGETALL '{key}'",
                    module_ignore_errors=True)
                logger.info(f"  {key}: {result.get('stdout', 'N/A')[:100]}...")
    except Exception as e:
        logger.warning(f"Failed to get PFCWD flex counter status: {e}")
    
    # 6. Check COUNTERS_DB for actual PFC WD queue counters
    logger.info("--- PFCWD Queue Counter OIDs (sample) ---")
    try:
        # Get a sample port's queue OID to verify counters exist
        sample_port = test_ports[0] if test_ports else 'Ethernet8'
        result = duthost.shell(
            f"sonic-db-cli COUNTERS_DB HGET COUNTERS_PORT_NAME_MAP {sample_port}",
            module_ignore_errors=True)
        port_oid = result.get('stdout', '').strip()
        logger.info(f"Port {sample_port} OID: {port_oid}")
        
        if port_oid:
            # Check queue counters for this port
            result = duthost.shell(
                f"sonic-db-cli COUNTERS_DB KEYS 'COUNTERS:oid:*' | grep -c '' || echo 0",
                module_ignore_errors=True)
            logger.info(f"Total COUNTERS entries: {result.get('stdout', '').strip()}")
    except Exception as e:
        logger.warning(f"Failed to get queue counter OIDs: {e}")
    
    # 7. Check SAI Redis logs for PFC-related events
    logger.info("--- SAI Redis PFC Events ---")
    try:
        result = duthost.shell(
            r"docker exec syncd grep -iE 'pfc|storm|watchdog|queue.*pause' "
            r"/var/log/swss/sairedis.rec 2>/dev/null | tail -30",
            module_ignore_errors=True)
        if result.get('stdout'):
            logger.info(f"SAI Redis PFC events:\n{result.get('stdout')}")
        else:
            logger.info("No PFC-related SAI events found in recent logs")
    except Exception as e:
        logger.warning(f"Failed to get SAI Redis logs: {e}")
    
    # 8. Check PFC priority enable status per port
    logger.info("--- PFC Priority Enable Status ---")
    try:
        for port in all_sample_ports[:4]:
            result = duthost.shell(
                f"sonic-db-cli CONFIG_DB HGET 'PORT|{port}' 'pfc_asym'",
                module_ignore_errors=True)
            pfc_asym = result.get('stdout', '').strip() or 'not set'
            
            result = duthost.shell(
                f"sonic-db-cli APPL_DB HGET 'PORT_TABLE:{port}' 'pfc_enable'",
                module_ignore_errors=True)
            pfc_enable = result.get('stdout', '').strip() or 'not set'
            logger.info(f"  {port}: pfc_asym={pfc_asym}, pfc_enable={pfc_enable}")
    except Exception as e:
        logger.warning(f"Failed to get PFC enable status: {e}")


def _debug_poll_storm_timing(duthost, storm_hndle, test_ports, queue_idx, timeout_sec=60):
    """
    Poll PFCWD stats and record when each port enters storm state.
    Helps identify Root Cause #3: Timing/race conditions.
    
    Returns:
        dict: {port: seconds_to_storm} for ports that entered storm
    """
    if not PFC_DEBUG_ENABLED:
        return {}
    
    logger.info("=== DEBUG: Polling storm timing per-port ===")
    port_storm_times = {}
    start_time = time.time()
    
    while time.time() - start_time < timeout_sec:
        elapsed = time.time() - start_time
        try:
            stats = duthost.show_and_parse('show pfcwd stat')
        except Exception as e:
            logger.warning(f"Failed to parse pfcwd stats at t={elapsed:.1f}s: {e}")
            time.sleep(2)
            continue
        
        for entry in stats:
            try:
                queue_str = entry.get('queue', '')
                if ':' not in queue_str:
                    continue
                port, queue = queue_str.split(':')
                if int(queue) != queue_idx:
                    continue
                if port not in test_ports:
                    continue
                
                detect_restore = entry.get('storm detected/restored', '0/0')
                detect, restore = detect_restore.split('/')
                if int(detect) > int(restore) and port not in port_storm_times:
                    port_storm_times[port] = elapsed
                    logger.info(f"DEBUG: Port {port} entered storm at t={elapsed:.1f}s")
            except (ValueError, KeyError) as e:
                continue
        
        # Check if we've reached threshold
        if len(port_storm_times) >= len(test_ports) * 0.75:
            logger.info(f"DEBUG: Reached 75% threshold at t={elapsed:.1f}s")
            break
        
        time.sleep(2)
    
    # Report summary
    stormed_count = len(port_storm_times)
    never_stormed = set(test_ports) - set(port_storm_times.keys())
    logger.info(f"DEBUG TIMING SUMMARY: {stormed_count}/{len(test_ports)} ports stormed")
    if port_storm_times:
        times = list(port_storm_times.values())
        logger.info(f"DEBUG: Storm times - min={min(times):.1f}s, max={max(times):.1f}s, avg={sum(times)/len(times):.1f}s")
    if never_stormed:
        logger.warning(f"DEBUG: Ports that NEVER entered storm: {sorted(never_stormed)}")
    
    return port_storm_times


def _debug_collect_interface_counters(duthost, test_ports):
    """
    Collect interface counters to verify traffic is reaching ports.
    Helps identify Root Cause: Traffic not being routed to server ports.
    """
    if not PFC_DEBUG_ENABLED:
        return
    logger.info("=== DEBUG: Interface Counters (TX_OK/RX_OK) ===")
    
    try:
        result = duthost.shell("show interfaces counters -a", module_ignore_errors=True)
        output = result.get('stdout', '')
        logger.info(f"Full interface counters:\n{output}")
        
        # Parse and summarize counters for test ports
        lines = output.strip().split('\n')
        header_found = False
        port_stats = {}
        for line in lines:
            if 'IFACE' in line and 'TX_OK' in line:
                header_found = True
                continue
            if header_found and line.strip():
                parts = line.split()
                if len(parts) >= 5:
                    iface = parts[0]
                    if iface in test_ports:
                        port_stats[iface] = {
                            'rx_ok': parts[2] if len(parts) > 2 else 'N/A',
                            'tx_ok': parts[6] if len(parts) > 6 else 'N/A'
                        }
        
        logger.info("=== Test Port Counter Summary ===")
        for port in sorted(test_ports)[:10]:
            stats = port_stats.get(port, {'rx_ok': 'N/A', 'tx_ok': 'N/A'})
            logger.info(f"  {port}: RX_OK={stats['rx_ok']}, TX_OK={stats['tx_ok']}")
        
        # Count ports with no TX traffic (potential problem)
        zero_tx_ports = [p for p, s in port_stats.items() if s.get('tx_ok') in ('0', 'N/A', '')]
        if zero_tx_ports:
            logger.warning(f"ALERT: {len(zero_tx_ports)} ports have ZERO TX: {sorted(zero_tx_ports)[:10]}...")
    except Exception as e:
        logger.warning(f"Failed to get interface counters: {e}")


def _debug_collect_routing_info(duthost, test_ports, traffic_params=None):
    """
    Collect routing information to verify traffic can reach server ports.
    Checks routes to server IPs to verify they point to expected interfaces.
    """
    if not PFC_DEBUG_ENABLED:
        return
    logger.info("=== DEBUG: Routing Information ===")
    
    # Check VLAN membership for server ports
    try:
        result = duthost.shell("show vlan brief", module_ignore_errors=True)
        logger.info(f"VLAN membership:\n{result.get('stdout', 'N/A')}")
    except Exception as e:
        logger.warning(f"Failed to get VLAN info: {e}")
    
    # Check routes to sample server IPs
    sample_server_ips = ['192.168.0.2', '192.168.0.3', '192.168.0.10', '192.168.0.20']
    logger.info("=== Routes to sample server IPs ===")
    for ip in sample_server_ips:
        try:
            result = duthost.shell(f"ip route get {ip} 2>/dev/null || echo 'Route not found'",
                                   module_ignore_errors=True)
            route = result.get('stdout', '').strip().split('\n')[0]
            logger.info(f"  {ip}: {route}")
        except Exception as e:
            logger.warning(f"Failed to get route for {ip}: {e}")
    
    # Check neighbor table for server MACs
    logger.info("=== Neighbor (ARP) table for VLAN ===")
    try:
        result = duthost.shell("ip neigh show dev Vlan1000 | head -10", module_ignore_errors=True)
        logger.info(f"VLAN1000 neighbors:\n{result.get('stdout', 'N/A')}")
    except Exception as e:
        logger.warning(f"Failed to get neighbor table: {e}")
    
    # Check if server IPs are locally connected via VLAN
    logger.info("=== VLAN interface config ===")
    try:
        result = duthost.shell("show ip interface | grep -E 'Vlan|Interface'", module_ignore_errors=True)
        logger.info(f"IP interfaces:\n{result.get('stdout', 'N/A')}")
    except Exception as e:
        logger.warning(f"Failed to get IP interface info: {e}")


def _debug_collect_all_diagnostics(duthost, storm_hndle, test_ports, label, is_failure=False):
    """
    Collect all diagnostics in one call.
    Call with is_failure=True for more verbose output on test failure.
    """
    if not PFC_DEBUG_ENABLED:
        return
    
    logger.info(f"\n{'='*60}")
    logger.info(f"PFC DEBUG DIAGNOSTICS - {label}")
    if is_failure:
        logger.info("*** FAILURE MODE - COLLECTING EXTENDED DIAGNOSTICS ***")
    logger.info(f"{'='*60}\n")
    
    _debug_collect_pfcwd_stats(duthost, label)
    _debug_collect_pfc_counters(duthost, test_ports, label)
    
    if is_failure:
        _debug_collect_port_health(duthost, test_ports)
        _debug_collect_interface_counters(duthost, test_ports)
        _debug_collect_routing_info(duthost, test_ports)
        _debug_verify_fanout_pfc_gen(storm_hndle)
        _debug_collect_platform_specific_diag(duthost, test_ports)


@pytest.fixture(scope="class")
def pfc_queue_idx():
    # Needed for start_background_traffic
    yield 3   # Hardcoded in the testcase as well.


@pytest.fixture(scope='module')
def degrade_pfcwd_detection(duthosts, enum_rand_one_per_hwsku_frontend_hostname, fanouthosts):
    """
    A fixture to degrade PFC Watchdog detection logic.
    It's requried because leaf fanout switch can't generate enough PFC pause to trigger
    PFC storm on all ports.
    """
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    dut_asic_type = duthost.facts["asic_type"].lower()
    skip_fixture = False
    if dut_asic_type != "mellanox":
        skip_fixture = True
    # The workaround is not applicable for Mellanox leaf-fanout running ONYX or SONiC
    # as we can leverage ASIC to generate PFC pause frames
    for fanouthost in list(fanouthosts.values()):
        fanout_os = fanouthost.get_fanout_os()
        if fanout_os == 'onyx' or fanout_os == 'sonic' and fanouthost.facts['asic_type'] == "mellanox":
            skip_fixture = True
            break
    if skip_fixture:
        yield
        return
    logger.info("--- Degrade PFCWD detection logic --")
    SRC_FILE = FILE_DIR + "/pfc_detect_mellanox.lua"
    DST_FILE = "/usr/share/swss/pfc_detect_mellanox.lua"
    # Backup original PFC Watchdog detection script
    cmd = "docker exec -i swss cp {} {}.bak".format(DST_FILE, DST_FILE)
    duthost.shell(cmd)
    # Copy the new script to DUT
    duthost.copy(src=SRC_FILE, dest='/tmp')
    # Copy the new script to swss container
    cmd = "docker cp /tmp/pfc_detect_mellanox.lua swss:{}".format(DST_FILE)
    duthost.shell(cmd)
    # Reload DUT to apply the new script
    config_reload(duthost, safe_reload=True, check_intf_up_ports=True, wait_for_bgp=True)
    yield
    # Restore the original PFC Watchdog detection script
    cmd = "docker exec -i swss cp {}.bak {}".format(DST_FILE, DST_FILE)
    duthost.shell(cmd)
    config_reload(duthost, safe_reload=True, check_intf_up_ports=True, wait_for_bgp=True)
    # Cleanup
    duthost.file(path='/tmp/pfc_detect_mellanox.lua', state='absent')


@pytest.fixture(scope='class', autouse=True)
def stop_pfcwd(duthosts, enum_rand_one_per_hwsku_frontend_hostname):
    """
    Fixture that stops PFC Watchdog before each test run

    Args:
        duthost (AnsibleHost): DUT instance
    """
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    logger.info("--- Stop Pfcwd --")
    duthost.command("pfcwd stop")

    yield

    logger.info("--- Start Pfcwd --")
    duthost.command("pfcwd start_default")


@pytest.fixture(scope='class', autouse=True)
def storm_test_setup_restore(setup_pfc_test, enum_fanout_graph_facts, duthosts,     # noqa: F811
                             enum_rand_one_per_hwsku_frontend_hostname, fanouthosts):
    """
    Fixture that inits the test vars, start PFCwd on ports and cleans up after the test run

    Args:
        setup_pfc_test (fixture): module scoped, autouse PFC fixture
        enum_fanout_graph_facts (fixture): fanout graph info
        duthost (AnsibleHost): DUT instance
        fanouthosts (AnsibleHost): fanout instance

    Yields:
        storm_hndle (PFCStorm): class PFCStorm instance
    """
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    asic_type = duthost.facts['asic_type']
    setup_info = setup_pfc_test
    neighbors = setup_info['neighbors']
    port_list = setup_info['port_list']
    ports = (" ").join(port_list)
    pfc_queue_index = 3
    pfc_frames_number = 10000000
    pfc_wd_detect_time = 200
    pfc_wd_restore_time = 200
    peer_params = populate_peer_info(asic_type, port_list, neighbors, pfc_queue_index, pfc_frames_number)
    storm_hndle = set_storm_params(duthost, enum_fanout_graph_facts, fanouthosts, peer_params)
    update_pfc_poll_interval(duthost, 200)
    start_wd_on_ports(duthost, ports, pfc_wd_restore_time, pfc_wd_detect_time)

    yield storm_hndle

    logger.info("--- Storm test cleanup ---")
    storm_hndle.stop_pfc_storm()


def populate_peer_info(asic_type, port_list, neighbors, q_idx, frames_cnt):
    """
    Build the peer_info map which will be used by the storm generation class

    Args:
        port_list (list): set of ports on which the PFC storm needs to be generated
        neighbors (dict): fanout info for each DUT port
        q_idx (int): queue on which PFC frames need to be generated
        frames_cnt (int): Number of PFC frames to generate

    Returns:
        peer_params (dict): all PFC params needed for each fanout for storm generation
    """
    peer_params = dict()
    if asic_type != 'vs':
        peer_port_map = dict()
        for port in port_list:
            peer_dev = neighbors[port]['peerdevice']
            peer_port = neighbors[port]['peerport']
            peer_port_map.setdefault(peer_dev, []).append(peer_port)

        for peer_dev in peer_port_map:
            peer_port_map[peer_dev] = (',').join(peer_port_map[peer_dev])
            peer_params[peer_dev] = {'pfc_frames_number': frames_cnt,
                                     'pfc_queue_index': q_idx,
                                     'intfs': peer_port_map[peer_dev]
                                     }
    return peer_params


def set_storm_params(duthost, fanout_graph, fanouthosts, peer_params):
    """
    Setup storm parameters

    Args:
        duthost (AnsibleHost): DUT instance
        fanout_graph (fixture): fanout info
        fanouthosts (AnsibleHost): fanout instance
        peer_params (dict): all PFC params needed for each fanout for storm generation

    Returns:
        storm_hndle (PFCMultiStorm): class PFCMultiStorm intance
    """
    storm_hndle = PFCMultiStorm(duthost, fanout_graph, fanouthosts, peer_params)
    storm_hndle.set_storm_params()
    return storm_hndle


def resolve_arp(duthost, ptfhost, test_ports_info, vlan, ip_version):
    """
    Populate ARP info for the DUT vlan port

    Args:
        ptfhost: ptf host instance
        test_ports_info: test ports information
    """
    for port, port_info in test_ports_info.items():
        if port_info['test_port_type'] == 'vlan':
            neighbor_ip = port_info['test_neighbor_addr']
            ptf_port = f"eth{port_info['test_port_id']}"
            if ip_version == "IPv4":
                ptfhost.command(f"ifconfig {ptf_port} {neighbor_ip}")
                duthost.command(f"docker exec -i swss arping {neighbor_ip} -c 5")
            else:
                ptfhost.command(f"ip -6 addr add {neighbor_ip}/{vlan['prefix']} dev {ptf_port}")
                duthost.command(f"docker exec -i swss ping -6 -c 5 {neighbor_ip}")
            break


@pytest.mark.usefixtures('degrade_pfcwd_detection', 'stop_pfcwd', 'storm_test_setup_restore', 'start_background_traffic')  # noqa: E501
class TestPfcwdAllPortStorm(object):
    """ PFC storm test class """
    # Threshold percentage for port storm verification (75% of ports must reach expected state)
    PFC_STORM_THRESHOLD_PERCENTAGE = 75
    # Threshold percentage for restore verification (100% of stormed ports must restore)
    PFC_RESTORE_THRESHOLD_PERCENTAGE = 100

    def run_test(self, duthost, storm_hndle, expect_regex, syslog_marker, action, selected_test_ports,
                 stormed_ports_list=None, tbinfo=None):
        """Storm generation/restoration on all ports and verification."""
        loganalyzer = LogAnalyzer(ansible_host=duthost, marker_prefix=syslog_marker)
        ignore_file = os.path.join(TEMPLATES_DIR, "ignore_pfc_wd_messages")
        reg_exp = loganalyzer.parse_regexp_file(src=ignore_file)
        loganalyzer.ignore_regex.extend(reg_exp)

        if duthost.facts['asic_type'] != 'vs':
            loganalyzer.expect_regex = []
            loganalyzer.expect_regex.extend(expect_regex)

        loganalyzer.match_regex = []

        # DEBUG: Collect pre-action diagnostics
        _debug_collect_all_diagnostics(duthost, storm_hndle, selected_test_ports, 
                                       f"PRE-{action.upper()}")

        with loganalyzer:
            if action == "storm":
                baseline_counters = get_pfc_storm_baseline_counters(duthost, storm_hndle)
                storm_hndle.start_pfc_storm()
                threshold = self.PFC_STORM_THRESHOLD_PERCENTAGE
                
                # DEBUG: Verify fanout is generating PFC frames
                _debug_verify_fanout_pfc_gen(storm_hndle)
            else:  # restore
                baseline_counters = None
                storm_hndle.stop_pfc_storm()
                threshold = self.PFC_RESTORE_THRESHOLD_PERCENTAGE

            port_type = "ports" if action == "storm" else "stormed ports"
            logger.info(f"Waiting for {threshold}% of {port_type} to reach {action} state")

            # Scale timeout with port count to allow sufficient time on high port-count platforms.
            num_ports = len(stormed_ports_list) if stormed_ports_list else len(selected_test_ports)
            timeout = max(60, num_ports * 2)
            # LT2/FT2 topologies need at least 120s because the traffic generator
            # takes longer to spin up on those setups.
            if tbinfo and tbinfo['topo']['type'] in ["lt2", "ft2"]:
                timeout = max(timeout, 120)
            
            # DEBUG: Track per-port storm timing (runs in parallel with wait_until)
            queue_idx = 3  # Default PFC queue used in this test
            if action == "storm" and PFC_DEBUG_ENABLED:
                # Collect timing data in background - this helps identify slow ports
                logger.info("DEBUG: Starting per-port storm timing collection...")
            
            result = wait_until(timeout, 2, 5, verify_all_ports_pfc_storm_in_expected_state, duthost,
                               storm_hndle, action, selected_test_ports, baseline_counters, threshold,
                               stormed_ports_list)
            
            # DEBUG: Collect post-action diagnostics (extended if failed)
            _debug_collect_all_diagnostics(duthost, storm_hndle, selected_test_ports,
                                           f"POST-{action.upper()}", is_failure=not result)
            
            pytest_assert(
                result,
                f"Not enough ports reached {action} state (threshold: {threshold}%)"
            )

    def test_all_port_storm_restore(
            self, duthosts, enum_rand_one_per_hwsku_frontend_hostname,
            storm_test_setup_restore, setup_pfc_test, ptfhost,
            setup_standby_ports_on_non_enum_rand_one_per_hwsku_frontend_host_m_unconditionally,     # noqa: F811
            toggle_all_simulator_ports_to_enum_rand_one_per_hwsku_frontend_host_m,                  # noqa: F811
            set_pfc_time_cisco_8000, tbinfo):
        """
        Tests PFC storm/restore on all ports

        Args:
            duthost (AnsibleHost): DUT instance
            storm_test_setup_restore (fixture): class scoped autouse setup fixture
        """
        duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
        storm_hndle = storm_test_setup_restore
        logger.info("--- Testing if PFC storm is detected on all ports ---")

        # Track which ports actually enter storm state
        stormed_ports_list = []

        # get all the tested ports from ALL fanouts
        # BUG FIX: Previously the loop would overwrite fanout_intfs/device_conn each iteration,
        # causing only the LAST fanout's ports to be tested. Now we accumulate across all peers.
        queues = []
        selected_test_ports = []

        for peer in storm_hndle.peer_params.keys():
            fanout_intfs = storm_hndle.peer_params[peer]['intfs'].split(',')
            device_conn = storm_hndle.fanout_graph[peer]['device_conn']
            queues.append(storm_hndle.storm_handle[peer].pfc_queue_idx)

            # Accumulate test ports from this fanout (moved INSIDE the loop)
            if duthost.facts['asic_type'] != 'vs':
                for intf in fanout_intfs:
                    test_port = device_conn[intf]['peerport']
                    if test_port in setup_pfc_test['test_ports'] and test_port not in selected_test_ports:
                        selected_test_ports.append(test_port)

        queues = list(set(queues))
        logger.info(f"Testing {len(selected_test_ports)} ports from {len(storm_hndle.peer_params)} fanouts: {sorted(selected_test_ports)}")
        resolve_arp(duthost, ptfhost, setup_pfc_test['test_ports'],
                    setup_pfc_test["vlan"], setup_pfc_test["ip_version"])
        
        # Use higher pkt_count for topologies where traffic is spread across many ports.
        # In dualtor topology, all server ports share ONE rx_port_id (the uplink), so forward
        # traffic from the uplink is split across all server destinations. With the original
        # pkt_count=500, each server port only receives ~500/num_servers packets, which may
        # not be sufficient to keep the queue occupied when PFC pause frames arrive.
        # Increase to 5000 to ensure ~200+ packets per server port per iteration.
        effective_pkt_count = 5000 if len(selected_test_ports) > 8 else 500
        logger.info(f"Using pkt_count={effective_pkt_count} for {len(selected_test_ports)} ports "
                    f"(~{effective_pkt_count // max(1, len(selected_test_ports) - 4)} pkts/server)")
        
        with send_background_traffic(duthost, ptfhost, queues, selected_test_ports, setup_pfc_test['test_ports'],
                                     pkt_count=effective_pkt_count):
            self.run_test(duthost,
                          storm_hndle,
                          expect_regex=[EXPECT_PFC_WD_DETECT_RE + fetch_vendor_specific_diagnosis_re(duthost)],
                          syslog_marker="all_port_storm",
                          action="storm",
                          stormed_ports_list=stormed_ports_list,
                          selected_test_ports=selected_test_ports,
                          tbinfo=tbinfo)

        logger.info(f"--- {len(stormed_ports_list)} ports entered storm state ---")
        logger.info("--- Testing if PFC storm is restored on stormed ports ---")
        self.run_test(duthost, storm_hndle, expect_regex=[EXPECT_PFC_WD_RESTORE_RE],
                      syslog_marker="all_port_storm_restore", action="restore",
                      stormed_ports_list=stormed_ports_list,
                      selected_test_ports=selected_test_ports,
                      tbinfo=tbinfo)
