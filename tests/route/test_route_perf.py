import pytest
import logging
import time
import re
import random
import json
import ptf.testutils as testutils
import ptf.mask as mask
import ptf.packet as packet
from tests.common.dualtor.dual_tor_utils import get_t1_ptf_ports  # noqa F811
from datetime import datetime
from tests.common import config_reload
from tests.common.helpers.assertions import pytest_assert
from tests.common.helpers.generators import generate_ips
from tests.common.utilities import wait_until
from tests.route.utils import generate_intf_neigh, generate_route_file, prepare_dut, cleanup_dut


CRM_POLL_INTERVAL = 1
CRM_DEFAULT_POLL_INTERVAL = 300

pytestmark = [
    pytest.mark.topology("any", "t1-multi-asic"),
    pytest.mark.device_type("vs")
]

logger = logging.getLogger(__name__)

ROUTE_TABLE_NAME = "ASIC_STATE:SAI_OBJECT_TYPE_ROUTE_ENTRY"
DEFAULT_NUM_ROUTES = 10000
ROUTE_STABILITY_TIMEOUT = 120  # Max seconds to wait for route count to stabilize
ROUTE_STABILITY_INTERVAL = 5  # Check interval in seconds
BGP_CONVERGENCE_TIMEOUT = 300  # Max seconds to wait for BGP sessions to establish


def wait_for_bgp_sessions(duthost, timeout=BGP_CONVERGENCE_TIMEOUT):
    """
    Wait for all BGP sessions to reach 'established' state.
    
    This is a prerequisite for route stability on ALL SONiC platforms. The issue:
    
    1. After reboot/config reload, BGP sessions take time to establish with neighbors.
       During this time, the route count is artificially low (only connected/static
       routes are present, typically ~800-900 routes).
    
    2. If we only check for "route count stability", we may incorrectly conclude
       the system is ready when BGP hasn't even started exchanging routes yet.
       The route count appears stable because nothing is happening.
    
    3. Once BGP sessions establish, thousands of routes flood in from neighbors.
       If the test starts during this flood, route programming competes with
       test routes for orchagent's single-threaded processing capacity.
    
    This function ensures BGP sessions are up BEFORE we check route stability,
    guaranteeing we measure from a truly converged baseline.
    
    Args:
        duthost: DUT host object
        timeout: Maximum seconds to wait for BGP sessions (default 300s)
    
    Returns:
        bool: True if all BGP sessions are established, False if timeout
    """
    logger.info("Waiting for BGP sessions to establish (timeout: {}s)...".format(timeout))

    # Use get_bgp_neighbors_per_asic which returns {namespace: {neighbor_ip: info}}
    # This matches the format expected by check_bgp_session_state_all_asics
    bgp_neighbors = duthost.get_bgp_neighbors_per_asic(state="all")
    if not bgp_neighbors:
        logger.info("No BGP neighbors configured, skipping BGP wait")
        return True

    # Count total neighbors across all namespaces
    total_neighbors = sum(len(neighbors) for neighbors in bgp_neighbors.values())
    logger.info("Waiting for {} BGP neighbor(s) to establish".format(total_neighbors))
    
    if wait_until(timeout, 10, 0, duthost.check_bgp_session_state_all_asics, bgp_neighbors):
        logger.info("All BGP sessions established")
        return True
    else:
        # Log which sessions are not established
        for namespace, neighbors in bgp_neighbors.items():
            for neighbor_ip, info in neighbors.items():
                state = info.get('state', 'unknown')
                if state.lower() != 'established':
                    logger.warning("BGP neighbor {} in namespace '{}' is in state: {}".format(
                        neighbor_ip, namespace, state))
        logger.warning("BGP sessions not fully established after {}s, proceeding anyway".format(timeout))
        return False


def wait_for_route_stability(asichost, timeout=ROUTE_STABILITY_TIMEOUT, interval=ROUTE_STABILITY_INTERVAL):
    """
    Wait for the route count in ASIC_DB to stabilize before running the test.
    
    This is essential for accurate performance measurements on ALL SONiC platforms
    (not just specific ASICs). The reasons are:
    
    1. Orchagent is single-threaded: It processes route updates from APP_DB
       sequentially. If BGP is still learning routes while the test pushes
       routes, they compete for orchagent's processing time.
    
    2. SWSS pipeline is shared: Routes from FRR/BGP and test routes both flow
       through the same SWSS -> orchagent -> syncd -> SAI path.
    
    3. After reboot or config reload, BGP takes time to establish sessions and
       exchange routes with neighbors. Running the test during this period
       causes unpredictable timeouts as the system is already busy.
    
    By waiting for route count stability, we ensure the test starts from a
    consistent baseline where the system is idle and ready to process test
    routes at full speed.
    
    Args:
        asichost: ASIC host object
        timeout: Maximum seconds to wait for stability (default 120s)
        interval: Check interval in seconds (default 5s)
    
    Returns:
        int: Stable route count
    """
    logger.info("Waiting for route count to stabilize (BGP convergence)...")
    stable_count = None
    stable_checks = 0
    required_stable_checks = 3  # Require 3 consecutive stable readings
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        current_count = asichost.count_routes(ROUTE_TABLE_NAME, skip_stability=True)
        
        if stable_count is None:
            stable_count = current_count
            logger.info("Initial route count: {}".format(current_count))
        elif current_count == stable_count:
            stable_checks += 1
            logger.info("Route count stable at {} ({}/{} checks)".format(
                current_count, stable_checks, required_stable_checks))
            if stable_checks >= required_stable_checks:
                logger.info("Route count stabilized at {} after {:.1f}s".format(
                    current_count, time.time() - start_time))
                return current_count
        else:
            logger.info("Route count changed: {} -> {} (delta: {:+d})".format(
                stable_count, current_count, current_count - stable_count))
            stable_count = current_count
            stable_checks = 0
        
        time.sleep(interval)
    
    # Timeout reached - log warning but proceed
    final_count = asichost.count_routes(ROUTE_TABLE_NAME, skip_stability=True)
    logger.warning("Route stability timeout after {}s. Proceeding with count: {}".format(
        timeout, final_count))
    return final_count


route_scale_per_role = {
    "m0": {
        "ipv4": 500,
        "ipv6": 500
    },
    "mx": {
        "ipv4": 500,
        "ipv6": 500
    },
    "t0": {
        "ipv4": 40000,
        "ipv6": 8000
    },
    "t1": {
        "ipv4": 40000,
        "ipv6": 8000
    }
}


def get_route_scale_per_role(tbinfo, ip_version):
    topo_name = tbinfo["topo"]["name"].split('-', 1)[0]
    logger.info("Test topology: {}".format(topo_name))
    if topo_name in route_scale_per_role:
        set_num_routes = route_scale_per_role[topo_name][ip_version]
    else:
        set_num_routes = DEFAULT_NUM_ROUTES
    return set_num_routes


def get_l3_alpm_template_from_config_bcm(duthost):
    """
    Get l3_alpm_template from config.bcm file
    :param duthost: DUT host object
    :return: l3_alpm_template value
    """
    ls_command = "docker exec syncd cat /etc/sai.d/sai.profile | grep SAI_INIT_CONFIG_FILE"
    ls_output = duthost.shell(ls_command, module_ignore_errors=True)['stdout']
    # Check if the file exists
    if ls_output:
        file_name = ls_output.split("=")[-1].strip()
        logging.info("Config bcm file found:{}".format(file_name))
        # Read the config.bcm file and find the l3_alpm_template variable
        cat_command = "docker exec syncd cat {} | grep l3_alpm_template".format(file_name)
        cat_output = duthost.shell(cat_command, module_ignore_errors=True)['stdout']
        if cat_output:
            # Extract the value of l3_alpm_template
            l3_alpm_template = cat_output.split(":")[-1].strip()
            logging.info("l3_alpm_template found:{}".format(l3_alpm_template))
            return int(l3_alpm_template)
        else:
            logging.info("Unable to find l3_alpm_template in config.bcm file")
            raise RuntimeError(
                "Unable to find l3_alpm_template in config.bcm file"
            )
    # If the file does not exist, raise an error
    else:
        logging.info("Unable to find config.bcm file in /etc/sai.d/sai.profile")
        raise RuntimeError(
            "Unable to find config.bcm file in /etc/sai.d/sai.profile"
        )
    return None


@pytest.fixture
def check_config(duthosts, enum_rand_one_per_hwsku_frontend_hostname, enum_rand_one_frontend_asic_index, tbinfo):
    if tbinfo["topo"]["type"] in ["m0", "mx"]:
        return

    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    if (duthost.facts.get('platform_asic') == 'broadcom-dnx'):
        # CS00012377343 - l3_alpm_enable isn't supported on dnx
        return

    asic = duthost.facts["asic_type"]
    platform = duthost.facts["platform"]
    asic_id = enum_rand_one_frontend_asic_index

    if (asic == "broadcom"):
        if "x86_64-arista_7060x6" in platform or "x86_64-nokia_ixr7220_h6" in platform:
            # For TH5 (Arista 7060x6) and TH6 (Nokia IXR7220 H6) family devices,
            # l3_alpm_template is set in config.bcm instead of l3_alpm_enable
            # * 1 - Combined (By default)
            # * 2 - Parallel
            pytest_assert(
                get_l3_alpm_template_from_config_bcm(duthost) == 1,
                "l3_alpm_template is not set for route scaling"
            )
        else:
            broadcom_cmd = "bcmcmd -n " + str(asic_id) if duthost.is_multi_asic else "bcmcmd"
            alpm_cmd = "{} {}".format(broadcom_cmd, '"conf show l3_alpm_enable"')
            alpm_enable = duthost.command(alpm_cmd)["stdout_lines"][2].strip()
            logger.info("Checking config: {}".format(alpm_enable))
            pytest_assert(alpm_enable == "l3_alpm_enable=2", "l3_alpm_enable is not set for route scaling")


@pytest.fixture(autouse=True)
def ignore_expected_loganalyzer_exceptions(
    enum_rand_one_per_hwsku_frontend_hostname, loganalyzer
):
    """
    Ignore expected failures logs during test execution.
    The route_checker script will compare routes in APP_DB and ASIC_DB, and an ERROR will be
    recorded if mismatch. The testcase will add 10,000 routes to APP_DB, and route_checker may
    detect mismatch during this period. So a new pattern is added to ignore possible error logs.
    Args:
        duthost: DUT fixture
        loganalyzer: Loganalyzer utility fixture
    """
    ignoreRegex = [
        ".*ERR route_check.py:.*",
        ".*ERR.* 'routeCheck' status failed.*",
        ".*Process \'orchagent\' is stuck in namespace \'host\'.*"
        ]
    if loganalyzer:
        # Skip if loganalyzer is disabled
        loganalyzer[enum_rand_one_per_hwsku_frontend_hostname].ignore_regex.extend(
            ignoreRegex
        )


@pytest.fixture(scope="function", autouse=True)
def reload_dut(duthosts, enum_rand_one_per_hwsku_frontend_hostname, request, loganalyzer):
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    yield
    if hasattr(request.node, "rep_call") and request.node.rep_call.failed:
        # Issue a config_reload to clear statically added route table and ip addr
        logging.info("Reloading config..")
        config_reload(duthost, ignore_loganalyzer=loganalyzer)


@pytest.fixture(scope="module", autouse=True)
def set_polling_interval(duthosts, enum_rand_one_per_hwsku_frontend_hostname):
    """Set CRM polling interval to 1 second"""
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    wait_time = 2
    duthost.command("crm config polling interval {}".format(CRM_POLL_INTERVAL))
    logger.info("Waiting {} sec for CRM counters to become updated".format(wait_time))
    time.sleep(wait_time)
    yield
    duthost.command("crm config polling interval {}".format(CRM_DEFAULT_POLL_INTERVAL))
    logger.info("Waiting {} sec for CRM counters to become updated".format(wait_time))
    time.sleep(wait_time)


def exec_routes(
    duthost, enum_rand_one_frontend_asic_index, prefixes, str_intf_nexthop, op
):
    # Create a tempfile for routes
    route_file_dir = duthost.shell("mktemp")["stdout"]

    # Generate json file for routes
    generate_route_file(duthost, prefixes, str_intf_nexthop, route_file_dir, op)
    logger.info("Route file generated and copied")

    # Check the number of routes in ASIC_DB
    # Note: skip_stability=True because this test implements its own convergence
    # polling loop below. Adding stability checks here would introduce redundant
    # delays (up to 30s per call) on top of the test's own wait logic.
    asichost = duthost.asic_instance(enum_rand_one_frontend_asic_index)
    start_num_route = asichost.count_routes(ROUTE_TABLE_NAME, skip_stability=True)

    # Calculate timeout as a function of the number of routes
    # Allow at least 1 second even when there is a limited number of routes
    asic_type = duthost.facts["asic_type"]
    if asic_type == "vs":
        # In vs, route entries need more time to be installed
        route_timeout = max(len(prefixes) / 160, 1)
    elif asic_type == "cisco-8000":
        # Cisco-8000 with large ECMP groups (24+ nexthops) programs routes
        # at ~100-150 routes/sec due to ASIC batching behavior. Use a
        # conservative rate to avoid premature timeouts.
        route_timeout = max(len(prefixes) / 100, 1)
    else:
        route_timeout = max(len(prefixes) / 250, 1)

    # Calculate expected number of route and record start time
    if op == "SET":
        expected_num_routes = start_num_route + len(prefixes)
    elif op == "DEL":
        expected_num_routes = start_num_route - len(prefixes)
    else:
        pytest.fail("Operation {} not supported".format(op))
    start_time = datetime.now()

    logger.info("Before pushing route to swssconfig")
    # Apply routes with swssconfig
    json_name = "/dev/stdin < {}".format(route_file_dir)
    result = duthost.docker_exec_swssconfig(
        json_name, "swss", enum_rand_one_frontend_asic_index
    )

    if result["rc"] != 0:
        pytest.fail(
            "Failed to apply route configuration file: {}".format(result["stderr"])
        )
    logger.info("All route entries have been pushed")

    total_delay = 0
    # skip_stability=True: test manages its own polling; stability check would
    # add redundant 30s timeout per iteration
    actual_num_routes = asichost.count_routes(ROUTE_TABLE_NAME, skip_stability=True)
    while actual_num_routes != expected_num_routes:
        diff = abs(expected_num_routes - actual_num_routes)
        delay = max(diff / 5000, 1)
        now = datetime.now()
        total_delay = (now - start_time).total_seconds()
        logger.info(
            "Current {} expected {} delayed {} will delay {}".format(
                actual_num_routes, expected_num_routes, total_delay, delay
            )
        )
        time.sleep(delay)
        actual_num_routes = asichost.count_routes(
            ROUTE_TABLE_NAME, skip_stability=True)
        if total_delay >= route_timeout:
            break

    logger.info("After pushing route to swssconfig, current {} expected {}".format(
        actual_num_routes, expected_num_routes))

    # Record time when all routes show up in ASIC_DB
    end_time = datetime.now()
    logger.info(
        "All route entries have been installed in ASIC_DB in {} seconds".format(
            (end_time - start_time).total_seconds()
        )
    )

    # Check route entries are correct
    # skip_stability=True: routes have already converged per the loop above
    asic_route_keys = asichost.get_route_key(ROUTE_TABLE_NAME, skip_stability=True)
    table_name_length = len(ROUTE_TABLE_NAME)
    asic_route_keys_set = set(
        [
            re.search('"dest":"([0-9a-f:/.]*)"', x[table_name_length:]).group(1)
            for x in asic_route_keys
        ]
    )
    prefixes_set = set(prefixes)
    diff = prefixes_set - asic_route_keys_set
    if op == "SET":
        if diff:
            pytest.fail(
                "The following entries have not been installed into ASIC {}".format(
                    diff
                )
            )
    elif op == "DEL":
        if diff != prefixes_set:
            pytest.fail(
                "The following entries have not been withdrawn from ASIC {}".format(
                    prefixes_set - diff
                )
            )

    # Return time used for set/del routes
    return (end_time - start_time).total_seconds()


def test_perf_add_remove_routes(
    tbinfo,
    duthosts,
    ptfadapter,
    enum_rand_one_per_hwsku_frontend_hostname,
    request,
    check_config,
    ip_versions,
    enum_rand_one_frontend_asic_index,
    is_backend_topology
):
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    asichost = duthost.asic_instance(enum_rand_one_frontend_asic_index)
    
    # Wait for system to be ready before starting the performance test.
    # This applies to ALL SONiC platforms: after reboot/config reload, BGP takes
    # time to establish sessions and exchange routes. If we push test routes while
    # orchagent is still processing BGP updates, the shared SWSS pipeline becomes
    # congested, causing unreliable timing measurements and potential timeouts.
    #
    # Two-phase wait:
    # 1. Wait for BGP sessions to establish (otherwise route count is artificially low)
    # 2. Wait for route count to stabilize (BGP route exchange complete)
    wait_for_bgp_sessions(duthost)
    wait_for_route_stability(asichost)
    
    mg_facts = duthost.get_extended_minigraph_facts(tbinfo)
    max_scale = request.config.getoption("--max_scale")
    # Number of routes for test
    set_num_routes = request.config.getoption("--num_routes")
    if max_scale and set_num_routes is not None:
        raise Exception("--max_scale and --num_routes are mutually exclusive")
    elif not max_scale and set_num_routes is None:
        set_num_routes = get_route_scale_per_role(tbinfo, "ipv{}".format(ip_versions))

    # Generate interfaces and neighbors
    NUM_NEIGHS = 50  # Update max num neighbors for multi-asic
    intf_neighs, str_intf_nexthop = generate_intf_neigh(
        asichost, NUM_NEIGHS, ip_versions, mg_facts, is_backend_topology
    )

    crm_facts = duthost.get_crm_facts()
    logger.info(json.dumps(crm_facts, indent=4))
    route_tag = "ipv{}_route".format(ip_versions)
    used_routes_count = asichost.count_crm_resources(
        "main_resources", route_tag, "used"
    )
    avail_routes_count = asichost.count_crm_resources(
        "main_resources", route_tag, "available"
    )
    pytest_assert(
        avail_routes_count,
        "CRM main_resources data is not ready within adjusted CRM polling time {}s".format(
            CRM_POLL_INTERVAL
        ),
    )

    if (max_scale):
        num_routes = avail_routes_count
    else:
        num_routes = min(avail_routes_count, set_num_routes)
    logger.info(
        "IP route utilization before test start: Used: {}, Available: {}, Test count: {}".format(
            used_routes_count, avail_routes_count, num_routes
        )
    )

    ipv4_prefix_set = [101, 41, 200, 9]
    # IPv6 prefix first octet choices. Each generates routes like {octet}:{octet}:x:y::/64
    # Note: 0x00FF was removed because it generates prefixes starting with 00ff:00ff::
    # which falls in the IETF reserved range (0000::/8 to 00ff::/8). Some platforms
    # silently reject these routes, causing unpredictable test results.
    ipv6_prefix_set = [0x3000, 0x1000, 0x2000, 0x0123]
    # Generate ip prefixes of routes
    if ip_versions == 4:
        random_oct = random.choice(ipv4_prefix_set)
        prefixes = [
            "%d.%d.%d.%d/%d"
            % (
                random_oct + int(idx_route / 256**2),
                int(idx_route / 256) % 256,
                idx_route % 256,
                0,
                24,
            )
            for idx_route in range(num_routes)
        ]
    else:
        random_oct = random.choice(ipv6_prefix_set)
        prefixes = [
            "%x:%x:%x:%x::/%d"
            % (
                random_oct,
                random_oct + int(idx_route / 65536),
                int(idx_route / 65536) % 65536,
                idx_route % 65536,
                64,
            )
            for idx_route in range(1, num_routes + 1)
        ]
    try:
        # Set up interface and interface for routes
        prepare_dut(asichost, intf_neighs)

        # Add routes
        time_set = exec_routes(
            duthost,
            enum_rand_one_frontend_asic_index,
            prefixes,
            str_intf_nexthop,
            "SET",
        )
        logger.info(
            "Time to set %d ipv%d routes is %.2f seconds."
            % (num_routes, ip_versions, time_set)
        )

        # Traffic verification with 10 random routes
        port_indices = mg_facts["minigraph_ptf_indices"]
        # split off the vlan id from the interface name separated by the . delimiter
        nexthop_intf = [nh_intf.split(".")[0] for nh_intf in str_intf_nexthop["ifname"].split(",")]
        src_port = random.choice(nexthop_intf)
        ptf_src_port = (
            port_indices[mg_facts["minigraph_portchannels"][src_port]["members"][0]]
            if src_port.startswith("PortChannel")
            else port_indices[src_port]
        )
        ptf_dst_ports = []
        for nh_ports in nexthop_intf:
            if nh_ports.startswith("PortChannel"):
                for member in mg_facts["minigraph_portchannels"][nh_ports]["members"]:
                    ptf_dst_ports.append(port_indices[member])
            else:
                ptf_dst_ports.append(port_indices[nh_ports])
        dst_nws = random.sample(prefixes, 10)
        for dst_nw in dst_nws:
            if ip_versions == 4:
                ip_dst = generate_ips(1, dst_nw, [])
                send_and_verify_traffic(
                    asichost, duthost, ptfadapter, tbinfo, ip_dst, ptf_dst_ports, ptf_src_port
                )
            else:
                ip_dst = dst_nw.split("/")[0] + "1"
                send_and_verify_traffic(
                    asichost,
                    duthost,
                    ptfadapter,
                    tbinfo,
                    ip_dst,
                    ptf_dst_ports,
                    ptf_src_port,
                    ipv6=True,
                )

        # Remove routes
        time_del = exec_routes(
            duthost,
            enum_rand_one_frontend_asic_index,
            prefixes,
            str_intf_nexthop,
            "DEL",
        )
        logger.info(
            "Time to del %d ipv%d routes is %.2f seconds."
            % (num_routes, ip_versions, time_del)
        )
    finally:
        cleanup_dut(asichost, intf_neighs)


def send_and_verify_traffic(
    asichost, duthost, ptfadapter, tbinfo, ip_dst, expected_ports, ptf_src_port, ipv6=False
):
    if ipv6:
        pkt = testutils.simple_tcpv6_packet(
            eth_dst=asichost.get_router_mac().lower(),
            eth_src=ptfadapter.dataplane.get_mac(0, ptf_src_port),
            ipv6_src="2001:db8:85a3::8a2e:370:7334",
            ipv6_dst=ip_dst,
            ipv6_hlim=64,
            tcp_sport=1234,
            tcp_dport=4321,
        )
    else:
        pkt = testutils.simple_tcp_packet(
            eth_dst=asichost.get_router_mac().lower(),
            eth_src=ptfadapter.dataplane.get_mac(0, ptf_src_port),
            ip_src="1.1.1.1",
            ip_dst=ip_dst,
            ip_ttl=64,
            tcp_sport=1234,
            tcp_dport=4321,
        )

    exp_pkt = pkt.copy()
    exp_pkt = mask.Mask(exp_pkt)
    exp_pkt.set_do_not_care_scapy(packet.Ether, "dst")
    exp_pkt.set_do_not_care_scapy(packet.Ether, "src")
    if ipv6:
        exp_pkt.set_do_not_care_scapy(packet.IPv6, "hlim")
    else:
        exp_pkt.set_do_not_care_scapy(packet.IP, "ttl")
        exp_pkt.set_do_not_care_scapy(packet.IP, "chksum")

    logger.info(
        "Sending packet from src port - {} , expecting to receive on any port".format(
            ptf_src_port
        )
    )
    ptfadapter.dataplane.flush()
    testutils.send(ptfadapter, ptf_src_port, pkt)
    testutils.verify_packet_any_port(ptfadapter, exp_pkt, ports=expected_ports)
