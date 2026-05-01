"""
Tests for the `counterpoll queue/watermark/pg-drop ...` commands in SONiC
"""

import allure
import logging
# import random  # TODO(MIGSOFTWAR-39421): Commented out while debugging with hardcoded values
import time
import pytest

from tests.common.config_reload import config_reload
from tests.common.constants import CounterpollConstants
from tests.common.helpers.assertions import pytest_assert
from tests.common.helpers.counterpoll_helper import ConterpollHelper
from tests.common.helpers.sonic_db import SonicDbCli, SonicDbKeyNotFound
from tests.common.utilities import get_inventory_files, get_host_visible_vars
from tests.common.utilities import skip_release, wait_until
from tests.common.reboot import reboot

pytestmark = [
    pytest.mark.sanity_check(skip_sanity=True),
    pytest.mark.disable_loganalyzer,  # disable automatic loganalyzer
    pytest.mark.topology('any')
]

ENABLE = CounterpollConstants.COUNTERPOLL_ENABLE.split(' ')[-1]
DISABLE = CounterpollConstants.COUNTERPOLL_DISABLE.split(' ')[-1]

MAPS_LONG_PREFIX = 'COUNTERS_{}_*_MAP'

MAPS = 'maps'
DELAY = 'delay'
QUEUE_MAPS = {'prefix': 'QUEUE', MAPS: ['COUNTERS_QUEUE_NAME_MAP', 'COUNTERS_QUEUE_INDEX_MAP',
                                        'COUNTERS_QUEUE_TYPE_MAP', 'COUNTERS_QUEUE_PORT_MAP']}

PG_MAPS = {'prefix': 'PG', MAPS: ['COUNTERS_PG_INDEX_MAP', 'COUNTERS_PG_PORT_MAP', 'COUNTERS_PG_NAME_MAP']}

MAPS_PREFIX_FOR_ALL_COUNTERPOLLS = [QUEUE_MAPS['prefix'], PG_MAPS['prefix']]

FLEX_COUNTER_PREFIX = 'FLEX_COUNTER_TABLE:'
RELEVANT_COUNTERPOLLS = [CounterpollConstants.QUEUE, CounterpollConstants.WATERMARK, CounterpollConstants.PG_DROP]
RELEVANT_MAPS = {CounterpollConstants.QUEUE: {MAPS: [QUEUE_MAPS], CounterpollConstants.TYPE:
                                                    [CounterpollConstants.QUEUE_STAT_TYPE], DELAY: 15},
                 CounterpollConstants.WATERMARK: {MAPS: [QUEUE_MAPS, PG_MAPS],
                                                  CounterpollConstants.TYPE:
                                                      [CounterpollConstants.QUEUE_WATERMARK_STAT_TYPE,
                                                       CounterpollConstants.PG_WATERMARK_STAT_TYPE], DELAY: 65},
                 CounterpollConstants.PG_DROP: {MAPS: [PG_MAPS],
                                                CounterpollConstants.TYPE: [CounterpollConstants.PG_DROP_STAT_TYPE],
                                                DELAY: 15}
                 }

WATERMARK_COUNTERS_DB_STATS_TYPE = ['USER_WATERMARKS', 'PERSISTENT_WATERMARKS', 'PERIODIC_WATERMARKS']


@pytest.fixture(scope='module')
def dut_vars(duthosts, enum_rand_one_per_hwsku_frontend_hostname, request):
    inv_files = get_inventory_files(request)
    dut_vars = get_host_visible_vars(inv_files, enum_rand_one_per_hwsku_frontend_hostname)
    yield dut_vars


def get_keys_on_asics(duthost, db_id, key, raise_error_when_not_found=True):
    """
    Get keys matching a pattern from all ASICs.
    Args:
        duthost: DUT host object
        db_id: Database ID (e.g., "COUNTERS_DB")
        key: Key pattern to search for
        raise_error_when_not_found: If True, raises SonicDbKeyNotFound when no keys found
    Returns:
        Dict mapping asic_index to list of keys found
    """
    result = {}
    for asic in duthost.asics:
        try:
            result[asic.asic_index] = SonicDbCli(asic, db_id).get_keys(key, raise_error_when_not_found)
        except SonicDbKeyNotFound:
            if raise_error_when_not_found:
                raise
            result[asic.asic_index] = []
    return result


def log_port_initialization_state(duthost):
    """
    Log the current state of port initialization for debugging.
    This helps identify if ports are fully initialized when counterpoll is enabled.
    """
    print("\n" + "=" * 60)
    print("DEBUG: PORT INITIALIZATION STATE")
    print("=" * 60)

    # Check interface status
    try:
        intf_status = duthost.show_and_parse('show interfaces status')
        oper_up_count = sum(
            1 for intf in intf_status if intf.get('oper', '').lower() == 'up'
        )
        print("DEBUG: Interfaces status: {} total, {} oper up".format(
            len(intf_status), oper_up_count))
        # Log first few interfaces as sample
        for intf in intf_status[:5]:
            print("DEBUG:   Interface sample: {}".format(intf))
    except Exception as e:
        print("DEBUG: WARNING - Failed to get interface status: {}".format(e))

    # Check swss container status
    try:
        result = duthost.shell(
            'docker ps --filter name=swss --format "{{.Status}}"',
            module_ignore_errors=True)
        print("DEBUG: swss container status: {}".format(
            result.get('stdout', 'unknown')))
    except Exception as e:
        print("DEBUG: WARNING - Failed to get swss status: {}".format(e))

    # Check orchagent process
    try:
        result = duthost.shell('pgrep -a orchagent', module_ignore_errors=True)
        print("DEBUG: orchagent process: {}".format(
            result.get('stdout', 'not found')))
    except Exception as e:
        print("DEBUG: WARNING - Failed to get orchagent status: {}".format(e))

    print("=" * 60 + "\n")


def log_counters_db_maps_state(duthost, context_msg=""):
    """
    Log the current state of COUNTERS_DB MAP tables for debugging.
    """
    print("\n" + "=" * 60)
    print("DEBUG: COUNTERS_DB MAPS STATE {}".format(context_msg))
    print("=" * 60)

    map_patterns = [
        'COUNTERS_PG_NAME_MAP',
        'COUNTERS_PG_INDEX_MAP',
        'COUNTERS_PG_PORT_MAP',
        'COUNTERS_QUEUE_NAME_MAP',
        'COUNTERS_PORT_NAME_MAP',
    ]

    for pattern in map_patterns:
        try:
            keys = get_keys_on_asics(
                duthost, 'COUNTERS_DB', pattern, raise_error_when_not_found=False)
            for asic_idx, key_list in keys.items():
                if key_list:
                    print("DEBUG:   [asic{}] {}: EXISTS ({} keys)".format(
                        asic_idx, pattern, len(key_list)))
                    # For NAME_MAP, also get the content to see if it has entries
                    if 'NAME_MAP' in pattern:
                        try:
                            for asic in duthost.asics:
                                if asic.asic_index == asic_idx:
                                    content = asic.run_sonic_db_cli_cmd(
                                        'COUNTERS_DB HGETALL {}'.format(pattern))
                                    lines = content.get('stdout_lines', [])
                                    entry_count = len(lines) // 2 if lines else 0
                                    print("DEBUG:     Content has {} entries".format(
                                        entry_count))
                                    # Log first few entries as sample
                                    for line in lines[:6]:
                                        print("DEBUG:       {}".format(line))
                                    break
                        except Exception as e:
                            print("DEBUG:     WARNING - Failed to get content: {}".format(e))
                else:
                    print("DEBUG:   [asic{}] {}: EMPTY/NOT FOUND".format(
                        asic_idx, pattern))
        except Exception as e:
            print("DEBUG:   {}: ERROR - {}".format(pattern, e))

    # Also check the wildcard pattern used by the test
    try:
        wildcard_keys = get_keys_on_asics(
            duthost, 'COUNTERS_DB', 'COUNTERS_*_*_MAP',
            raise_error_when_not_found=False)
        for asic_idx, key_list in wildcard_keys.items():
            print("DEBUG:   [asic{}] COUNTERS_*_*_MAP wildcard: {} keys found".format(
                asic_idx, len(key_list)))
            for key in key_list:
                print("DEBUG:     - {}".format(key))
    except Exception as e:
        print("DEBUG:   Wildcard pattern check failed: {}".format(e))

    print("=" * 60 + "\n")


def check_counters_populated(duthost, key):
    try:
        keys = get_keys_on_asics(
            duthost, "COUNTERS_DB", key, raise_error_when_not_found=True)
        populated = bool(keys.values())
        # Add debug logging for each check
        for asic_idx, key_list in keys.items():
            print("DEBUG: check_counters_populated: asic{} key='{}' found {} keys".format(
                asic_idx, key, len(key_list) if key_list else 0))
        return populated
    except SonicDbKeyNotFound:
        print("DEBUG: check_counters_populated: key='{}' not found (SonicDbKeyNotFound)".format(key))
        return False


def test_counterpoll_queue_watermark_pg_drop(duthosts, localhost, enum_rand_one_per_hwsku_frontend_hostname, dut_vars):
    """
    @summary: Verify FLEXCOUNTERS_DB and COUNTERS_DB content after `counterpoll queue/watermark/queue enable`

    this test runs the following steps once with config reload between counterpolls enabled,
     and once with switch reboot:
    1. Disable the counter polling for all counters
    2. save and reboot or reload configuration (randomized)
    3. run one of the (type[watermark,queue,pg-drop]) (randomized)
        a. queue: enable the queue counter polling, check whether queue map is generated in COUNTERS_DB,
           no WATERMARK or PG-DROP stats in FLEX_COUNTER_DB
        b. watermark: enable the watermark counter polling, check whether PG and queue maps are generated in COUNTER_DB
           and both PG and QUEUE WATERMARK stats exists in FLEX_COUNTER_DB, without any QUEUE or PG-DROP stats
           for watermark also check WATERMARK stats in COUNTERS_DB (USER/PERSISTENT/PERIODIC WATERMARKS)
        c. pg-drop: enable the pg-drop counter polling, check whether PG map is generated in COUNTERS_DB
           no WATERMARK or QUEUE stats in FLEX_COUNTER_DB
    4. enables all three counterpolls (queue,watermark,pg-drop) and count stats per type
    """
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    skip_release(duthost, ["202205", "202111", "202106", "202012", "201911", "201811", "201803"])

    counted_dict = {}
    # choosing only one between reload / reboot due to test duration limitations
    with allure.step("choosing random config apply method"):
        # TODO(MIGSOFTWAR-39421): Hardcoded for debugging
        # config_apply_method = random.choice(["config reload", "switch reboot"])
        config_apply_method = "config reload"
        print("DEBUG: Using hardcoded config_apply_method='{}' for MIGSOFTWAR-39421".format(
            config_apply_method))

    # Log initial state before any changes
    log_counters_db_maps_state(duthost, "[BEFORE disabling counterpolls]")
    log_port_initialization_state(duthost)

    with allure.step("disabling all counterpolls"):
        for asic in duthost.asics:
            ConterpollHelper.disable_counterpoll(asic, list(CounterpollConstants.COUNTERPOLL_MAPPING.values()))

    # verify relevant counterpolls (queue/watermark/pg-drop) are disabled
    with allure.step("Verifying initial output of {} on {} ..."
                     .format(CounterpollConstants.COUNTERPOLL_SHOW, duthost.hostname)):
        verify_all_counterpoll_status(duthost, DISABLE)

    with allure.step("saving config on dut {} after counterpoll disable...".format(duthost.hostname)):
        duthost.command('config save -y')

    # Log state after disabling counterpolls and before reload
    log_counters_db_maps_state(duthost, "[AFTER config save, BEFORE reload]")

    # choosing only one counterpoll to test due to test duration limitaions
    # TODO(MIGSOFTWAR-39421): Hardcoded for debugging
    # tested_counterpoll = random.choice(RELEVANT_COUNTERPOLLS)
    tested_counterpoll = CounterpollConstants.PG_DROP
    print("DEBUG: Using hardcoded tested_counterpoll='{}' for MIGSOFTWAR-39421".format(
        tested_counterpoll))

    # need reload or reboot after disabling counterpolls to clean DB stats, this is by design
    with allure.step(config_apply_method + " dut {} ...".format(duthost.hostname)):
        if 'reload' in config_apply_method:
            config_reload(duthost)
        elif 'reboot' in config_apply_method:
            reboot(duthost, localhost)
    # Sleep for 60 seconds to wait for config DB to be ready
    print("DEBUG: Sleeping 60 seconds for config DB to be ready...")
    time.sleep(60)
    print("DEBUG: 60 second sleep completed")

    # Log state after reload/reboot and sleep
    log_counters_db_maps_state(duthost, "[AFTER {} + 60s sleep]".format(config_apply_method))
    log_port_initialization_state(duthost)

    # verify all counterpolls are disabled after reload or reboot
    with allure.step("Verifying output of {} on {} after {} ..."
                     .format(CounterpollConstants.COUNTERPOLL_SHOW, duthost.hostname, config_apply_method)):
        verify_all_counterpoll_status(duthost, DISABLE)
    # enable the selected counterpoll queue/watermark/pg-drop
    with allure.step("enabling and verify randomly selected counterpoll {} on {} ..."
                     .format(duthost.hostname, [tested_counterpoll])):
        for asic in duthost.asics:
            ConterpollHelper.enable_counterpoll(asic, [tested_counterpoll])
            verify_counterpoll_status(asic, [tested_counterpoll], ENABLE)

    # Delay to allow the counterpoll to generate the maps in COUNTERS_DB
    # NOTE: Do NOT add logging between enable_counterpoll and wait_until - critical timing window
    with allure.step("waiting {} seconds for counterpoll to generate maps in COUNTERS_DB"):
        delay = RELEVANT_MAPS[tested_counterpoll][DELAY]
        print("DEBUG: Starting wait_until(timeout=120, interval=5, delay={}) for key='{}'".format(
            delay, MAPS_LONG_PREFIX.format('*')))
        print("DEBUG: Waiting {} seconds (delay) before first DB check...".format(delay))

        result = wait_until(
            120, 5, delay, check_counters_populated, duthost, MAPS_LONG_PREFIX.format('*'))

        # Log state after the wait (whether success or failure)
        log_counters_db_maps_state(duthost, "[AFTER wait_until, result={}]".format(result))
        log_port_initialization_state(duthost)

        if not result:
            # Additional diagnostics on failure
            print("DEBUG: ERROR - COUNTERS_DB failed to populate!")
            print("DEBUG: Collecting additional diagnostics...")

            # Check FLEX_COUNTER_DB state
            try:
                flex_keys = get_keys_on_asics(
                    duthost, 'FLEX_COUNTER_DB', '*PG*', raise_error_when_not_found=False)
                for asic_idx, key_list in flex_keys.items():
                    print("DEBUG: FLEX_COUNTER_DB *PG* keys on asic{}: {} found".format(
                        asic_idx, len(key_list)))
                    for key in key_list[:10]:
                        print("DEBUG:   - {}".format(key))
            except Exception as e:
                print("DEBUG: Failed to get FLEX_COUNTER_DB keys: {}".format(e))

            # Check CONFIG_DB FLEX_COUNTER_TABLE
            try:
                for asic in duthost.asics:
                    cfg_result = asic.run_sonic_db_cli_cmd(
                        'CONFIG_DB keys *FLEX_COUNTER*')
                    print("DEBUG: CONFIG_DB FLEX_COUNTER keys on asic{}: {}".format(
                        asic.asic_index, cfg_result.get('stdout', 'none')))
            except Exception as e:
                print("DEBUG: Failed to get CONFIG_DB FLEX_COUNTER keys: {}".format(e))

        pytest_assert(result, "COUNTERS_DB failed to populate")
    # verify QUEUE or PG maps are generated into COUNTERS_DB after enabling relevant counterpoll
    with allure.step("Verifying MAPS in COUNTERS_DB on {}...".format(duthost.hostname)):
        maps_dict = RELEVANT_MAPS[tested_counterpoll]
        maps_to_verify = maps_dict[MAPS]
        for map_to_verify in maps_to_verify:
            map_prefix = map_to_verify['prefix']
            maps = map_to_verify[MAPS]
            map_outputs = get_keys_on_asics(duthost, 'COUNTERS_DB', MAPS_LONG_PREFIX.format(map_prefix))
            for asic_idx, map_output in map_outputs.items():
                map = []
                failed = ""
                for map_entry in maps:
                    map.append(map_entry)
                msg = "no {} maps found in COUNTERS_DB on asic{}".format(map_prefix, asic_idx)
                pytest_assert(map_output, msg)
                for line in map_output:
                    try:
                        map.remove(line)
                    except ValueError:
                        failed = "MAP {} was not found in {} MAPS list".format(line, map_prefix)
                pytest_assert("" == failed, failed)
                pytest_assert(len(map) == 0,
                              "{} maps mismatch, one or more queue was not found in redis COUNTERS_DB on asic{}"
                              .format(map_prefix, asic_idx))

    failed_list = []
    with allure.step("Verifying {} STATS in FLEX_COUNTER_DB on {}...".format(tested_counterpoll, duthost.hostname)):
        stats_outputs = get_keys_on_asics(duthost, 'FLEX_COUNTER_DB', '*{}*'.format(map_prefix))
        for asic_idx, stats_output in stats_outputs.items():
            counted = 0
            # build expected counterpoll stats vs unexpected
            expected_types = []
            unexpected_types = []
            for counterpoll, v in list(RELEVANT_MAPS.items()):
                types_to_check = v[CounterpollConstants.TYPE]
                if counterpoll in tested_counterpoll:
                    for type in types_to_check:
                        expected_types.append(FLEX_COUNTER_PREFIX + type)
                else:
                    for type in types_to_check:
                        unexpected_types.append(FLEX_COUNTER_PREFIX + type)
            logging.info("expected types for for counterpoll {}:\n{}".format(tested_counterpoll, expected_types))
            logging.info("unexpected types for for counterpoll {}:\n{}".format(tested_counterpoll, unexpected_types))
            for line in stats_output:
                for expected in expected_types:
                    if expected in line:
                        counted += 1
                for unexpected in unexpected_types:
                    if unexpected in line:
                        failed_list.append("found for {} unexpected stat counter in FLEX_COUNTER_DB on asic{}: {}"
                                           .format(tested_counterpoll, asic_idx, line))
            logging.info("counted {} {} STATs type in FLEX_COUNTER_DB on {} asic{}..."
                         .format(counted, tested_counterpoll, duthost.hostname, asic_idx))
            pytest_assert(len(failed_list) == 0, failed_list)
            pytest_assert(counted > 0, "counted {} for {}".format(counted, tested_counterpoll))

    # for watermark only, also count stats with actual values in COUNTERS_DB
    if CounterpollConstants.WATERMARK in tested_counterpoll:
        with allure.step("counting {} STATS in FLEX_COUNTER_DB on {}...".format(tested_counterpoll, duthost.hostname)):
            count_watermark_stats_in_counters_db(duthost)

    pytest_assert([] == failed_list, failed_list)

    # no need for reload or reboot when enabling all queue/watermark/pg-drop counterpolls
    with allure.step("enable and verify all {} counterpolls on {} ..."
                     .format(RELEVANT_COUNTERPOLLS, duthost.hostname)):
        for asic in duthost.asics:
            ConterpollHelper.enable_counterpoll(asic, RELEVANT_COUNTERPOLLS)
            verify_counterpoll_status(asic, RELEVANT_COUNTERPOLLS, ENABLE)
    # count FLEXCOUNTER_DB countrpolls and put in results dict key per countrpoll
    with allure.step("check all counterpolls {} results on {} ...".format(RELEVANT_COUNTERPOLLS, duthost.hostname)):
        for counterpoll in RELEVANT_COUNTERPOLLS:
            counted_dict[counterpoll] = {}
            for asic in duthost.asics:
                counted_dict[counterpoll][asic.asic_index] = 0
        for map_prefix in MAPS_PREFIX_FOR_ALL_COUNTERPOLLS:
            stats_outputs = get_keys_on_asics(duthost, 'FLEX_COUNTER_DB', '*{}*'.format(map_prefix))
            for asic_idx, stats_output in stats_outputs.items():
                for line in stats_output:
                    for counterpoll, v in list(RELEVANT_MAPS.items()):
                        types_to_check = v[CounterpollConstants.TYPE]
                        for type in types_to_check:
                            if type in line:
                                counted_dict[counterpoll][asic_idx] += 1
        logging.info("counted_dict {}".format(counted_dict))
    # verify each queue/watermark/pg-drop counterpoll has stats in FLEX_COUNTER_DB
    for counterpoll in RELEVANT_COUNTERPOLLS:
        for asic in duthost.asics:
            pytest_assert(counted_dict[counterpoll][asic.asic_index] > 0,
                          "No stats in FLEX_COUNTER_DB for {} asic{}".format(duthost.hostname, asic.asic_index))
    # for watermark only, also count stats with actual values in COUNTERS_DB
    if CounterpollConstants.WATERMARK in tested_counterpoll:
        with allure.step("counting watermark STATS in FLEX_COUNTER_DB on {}...".format(duthost.hostname)):
            count_watermark_stats_in_counters_db(duthost)


def verify_all_counterpoll_status(duthost, expected):
    for asic in duthost.asics:
        verify_counterpoll_status(asic, RELEVANT_COUNTERPOLLS, expected)


def verify_counterpoll_status(duthost, counterpoll_list, expected):
    with allure.step("verifying {} for {} in output of {} on {}..."
                     .format(expected, counterpoll_list, CounterpollConstants.COUNTERPOLL_SHOW, duthost.hostname)):
        counterpoll_output = ConterpollHelper.get_counterpoll_show_output(duthost)
        pytest_assert(len(counterpoll_output) > 0, "cmd {} returns no output"
                      .format(CounterpollConstants.COUNTERPOLL_SHOW))

        verified_output_dict = {}
        for counterpoll_parsed_dict in counterpoll_output:
            for k, v in list(CounterpollConstants.COUNTERPOLL_MAPPING.items()):
                if k == counterpoll_parsed_dict[CounterpollConstants.TYPE]:
                    status = counterpoll_parsed_dict[CounterpollConstants.STATUS]
                    logging.info(f"Setting verified_output_dict[{v}] = {status}")
                    verified_output_dict[v] = status

        # Validate all of the relevant keys are disabled - QUEUE/WATERMARK/PG-DROP
        for counterpoll in counterpoll_list:
            pytest_assert(expected in verified_output_dict[counterpoll], "{} is {}. expected to be {}"
                          .format(counterpoll, verified_output_dict[counterpoll], expected))


def count_watermark_stats_in_counters_db(duthost):
    watermark_stats_output = get_keys_on_asics(duthost, 'COUNTERS_DB', '*{}*'
                                               .format(CounterpollConstants.WATERMARK.upper()))
    for asic_idx, output in watermark_stats_output.items():
        watermark_stats = {}
        for watermark_type in WATERMARK_COUNTERS_DB_STATS_TYPE:
            watermark_stats[watermark_type] = 0
            for line in output:
                if watermark_type in line:
                    watermark_stats[watermark_type] += 1
        logging.info("watermark_stats on {} {}".format(asic_idx, watermark_stats))
        for k, v in list(watermark_stats.items()):
            pytest_assert(v > 0, "watermark_stats {} in COUNTERS_DB on asic{}: {}, expected > 0".format(k, asic_idx, v))
