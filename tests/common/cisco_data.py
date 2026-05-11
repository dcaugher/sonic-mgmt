import json
import re
from tests.common.reboot import reboot
from tests.common.utilities import wait_until


# =============================================================================
# Cisco 8000 ASIC Type
# =============================================================================
ASIC_TYPE = "cisco-8000"


# =============================================================================
# HWSKUs by ASIC Generation
# =============================================================================

# Gibraltar (GB) - 1st gen Memory Mode
GB_HWSKUS = [
    "Cisco-8101-C48T8",
    "Cisco-8101-C64",
    "Cisco-8101-O32",
    "Cisco-8101-O8C48",
    "Cisco-8101-O8V48",
    "Cisco-8101-T32",
    "Cisco-8101-V64",
    "Cisco-8101-32FH-O",
    "Cisco-8101C01-C28S4",
    "Cisco-8101C01-C32",
    "Cisco-8101C01-V64",
    "Cisco-8102-C64",
    "Cisco-8102-28FH-DPU-C28",
    "Cisco-8102-28FH-DPU-O",
    "Cisco-8102-28FH-DPU-O8C20",
    "Cisco-8102-28FH-DPU-O8C40",
    "Cisco-8102-28FH-DPU-O8V40",
    "Cisco-8102-28FH-DPU-O12C16",
    "Cisco-8111-C32",
    "Cisco-88-LC0-36FH-M-O36",
    "Cisco-88-LC0-36FH-O36",
]

# Graphene (GR) - G100
GR_HWSKU_PREFIX = "Cisco-8111-O"
GR_HWSKUS = [
    "Cisco-8111-O32",
    "Cisco-8111-O64",
]

# Graphene 2 (GR2) - G200
GR2_HWSKU_PREFIX = "Cisco-8122"
GR2X_HWSKU_PREFIX = "Cisco-8122X"
GR2_HWSKUS = [
    "Cisco-8122-O64",
    "Cisco-8122-O64S2",
    "Cisco-8122-O128",
    "Cisco-8122-O128S2",
    "Cisco-8122X-O128S2",
]

# Pacific (PAC) - Line Cards
PAC_HWSKU_PREFIX = "Cisco-8800-LC-"
PAC_HWSKUS = [
    "Cisco-8800-LC-48H-C48",
]

# P200
P200_HWSKU_PREFIX = "Cisco-8223-"
P200_HWSKUS = [
    "Cisco-8223-O128S2",
    "Cisco-8223-P32O64S2",
    "Cisco-8223-P64S2",
]

# All Cisco 8000 HWSKUs
ALL_HWSKUS = GB_HWSKUS + GR_HWSKUS + GR2_HWSKUS + PAC_HWSKUS + P200_HWSKUS

# Virtual switch HWSKU
VS_HWSKU = "cisco-8101-p4-32x100-vs"

# HWSKUs requiring separated uplink/downlink DSCP-to-TC mapping
SEPARATED_DSCP_TC_HWSKUS = [
    "Cisco-8101-O8C48",
    "Cisco-8101-O8V48",
    "Cisco-8102-28FH-DPU-O-T1",
    "Cisco-8102-28FH-DPU-O",
]


# =============================================================================
# Platforms by ASIC Generation
# =============================================================================

# Gibraltar (GB) platforms
GB_PLATFORMS = [
    "x86_64-8101_32fh_o-r0",
    "x86_64-8101_32fh_o_c01-r0",
    "x86_64-8102_28fh_dpu_o-r0",
    "x86_64-8102_64h_o-r0",
]

# Graphene (GR) - G100 platforms
GR_PLATFORM_PREFIX = "x86_64-8111_"
GR_PLATFORMS = [
    "x86_64-8111_32eh_o-r0",
]

# Graphene 2 (GR2) - G200 platforms
GR2_PLATFORM_PREFIX = "x86_64-8122"
GR2X_PLATFORM_PREFIX = "x86_64-8122x"
GR2_PLATFORMS = [
    "x86_64-8122_64eh_o-r0",
    "x86_64-8122_64ehf_o-r0",
    "x86_64-8122x_64ef_o-r0",
]

# P200 platforms
P200_PLATFORM_PREFIX = "x86_64-8223_"
P200_PLATFORMS = [
    "x86_64-8223_64e_mo-r0",
    "x86_64-8223_64ef_mo-r0",

]

# All Cisco 8000 platforms
ALL_PLATFORMS = GB_PLATFORMS + GR_PLATFORMS + GR2_PLATFORMS + P200_PLATFORMS

# Virtual switch platform
VS_PLATFORM = "x86_64-kvm_x86_64-r0"


# =============================================================================
# SmartSwitch HWSKUs (switches with DPUs)
# =============================================================================
SMARTSWITCH_HWSKU_PREFIX = "Cisco-8102-28FH-DPU-"
SMARTSWITCH_HWSKUS = [
    "Cisco-8102-28FH-DPU-C28",
    "Cisco-8102-28FH-DPU-O",
    "Cisco-8102-28FH-DPU-O8C20",
    "Cisco-8102-28FH-DPU-O8C40",
    "Cisco-8102-28FH-DPU-O8V40",
    "Cisco-8102-28FH-DPU-O12C16",
]


# =============================================================================
# Line Card HWSKUs (for modular chassis)
# =============================================================================

# Q200 (Gibraltar) Line Cards
Q200_LC_HWSKUS = [
    "Cisco-88-LC0-36FH-M-O36",
    "Cisco-88-LC0-36FH-O36",
]
Q200_LC_PLATFORMS = [
    "x86_64-88_lc0_36fh-r0",
    "x86_64-88_lc0_36fh_m-r0",
    "x86_64-88_lc0_36fh_mo-r0",
]

# Q100 (Pacific) Line Cards
Q100_LC_HWSKUS = [
    "Cisco-8800-LC-48H-C48",
]
Q100_LC_PLATFORMS = [
    "x86_64-8800_lc_48h-r0",
    "x86_64-8800_lc_48h_o-r0",
]

# All Line Card HWSKUs/Platforms (for backward compatibility)
LINE_CARD_HWSKUS = Q200_LC_HWSKUS + Q100_LC_HWSKUS
LINE_CARD_PLATFORMS = Q200_LC_PLATFORMS + Q100_LC_PLATFORMS

# Line card platform that supports split-voq
LC_SPLIT_VOQ_PLATFORM = "x86_64-88_lc0_36fh-r0"

# Line card platforms that support long links (>2000m)
LONGLINK_LC_PLATFORMS = [
    "x86_64-88_lc0_36fh_mo-r0",
    "x86_64-88_lc0_36fh_m-r0",
]


# Platforms that use model.json format for config
MODEL_JSON_PLATFORMS = ["x86_64-8102_64h_o-r0"]


# =============================================================================
# Helper Functions
# =============================================================================

def is_cisco_device(dut):
    return dut.facts["asic_type"] == ASIC_TYPE


def is_model_json_format(duthost):
    return duthost.facts['platform'] in MODEL_JSON_PLATFORMS


def get_markings_config_file(duthost):
    """
        Get the config file where the ECN markings are enabled or disabled.
    """
    if duthost.facts["asic_type"] != ASIC_TYPE:
        raise RuntimeError("This is applicable only to cisco platforms.")
    platform = duthost.facts['platform']
    hwsku = duthost.facts['hwsku']
    if is_model_json_format(duthost):
        match = re.search(r"\-([^-_]+)_", platform)
        if match:
            model = match.group(1)
        else:
            raise RuntimeError("Couldn't get the model from platform:{}".format(platform))
    else:
        model = "serdes"
    config_file = "/usr/share/sonic/device/{}/{}/{}.json".format(platform, hwsku, model)
    return config_file


def get_markings_dut(duthost, key_list=['ecn_dequeue_marking', 'ecn_latency_marking', 'voq_allocation_mode']):
    """
        Get the ecn marking values from the duthost.
    """
    config_file = get_markings_config_file(duthost)
    dest_file = "/tmp/"
    contents = duthost.fetch(src=config_file, dest=dest_file)
    local_file = contents['dest']
    with open(local_file) as fd:
        json_contents = json.load(fd)
    markings_dict = {}
    # Getting markings from first device.
    device = json_contents['devices'][0]
    for key in key_list:
        markings_dict[key] = device['device_property'][key]
    return markings_dict


def setup_markings_dut(duthost, localhost, **kwargs):
    """
        Setup dequeue or latency depending on arguments.
        Applicable to cisco-8000 Platforms only.
    """
    config_file = get_markings_config_file(duthost)
    dest_file = "/tmp/"
    contents = duthost.fetch(src=config_file, dest=dest_file)
    local_file = contents['dest']
    with open(local_file) as fd:
        json_contents = json.load(fd)
    reboot_required = False
    for device in json_contents['devices']:
        for k, v in list(kwargs.items()):
            if device['device_property'][k] != v:
                reboot_required = True
                device['device_property'][k] = v
    if reboot_required:
        duthost.copy(content=json.dumps(json_contents, sort_keys=True, indent=4), dest=config_file)
        reboot(duthost, localhost)


def copy_dshell_script_cisco_8000(dut, asic, dshell_script, script_name):
    if dut.facts['asic_type'] != ASIC_TYPE:
        raise RuntimeError("This function should have been called only for cisco-8000.")

    script_path = "/tmp/{}".format(script_name)
    dut.copy(content=dshell_script, dest=script_path)
    if dut.sonichost.is_multi_asic:
        dest = f"syncd{asic}"
    else:
        dest = "syncd"
    dut.docker_copy_to_all_asics(
        container_name=dest,
        src=script_path,
        dst="/")


def copy_set_voq_watchdog_script_cisco_8000(dut, asic="", enable=True):
    dshell_script = '''
from common import d0
def set_voq_watchdog(enable):
    d0.set_bool_property(sdk.la_device_property_e_VOQ_WATCHDOG_ENABLED, enable)
set_voq_watchdog({})
'''.format(enable)

    copy_dshell_script_cisco_8000(dut, asic, dshell_script, script_name="set_voq_watchdog.py")


def check_dshell_ready(duthost):
    show_command = "sudo show platform npu rx cgm_global"
    err_msg = "debug shell server for asic 0 is not running"
    output = duthost.command(show_command)['stdout']
    if err_msg in output:
        return False
    return True


def run_dshell_command(duthost, command):
    if not wait_until(300, 20, 0, check_dshell_ready, duthost):
        raise RuntimeError("Debug shell is not ready on {}".format(duthost.hostname))
    return duthost.shell(command)


def is_gb_platform(dut):
    """Check if DUT is a Gibraltar (GB) platform."""
    return dut.facts.get('platform') in GB_PLATFORMS


def is_gr_platform(dut):
    """Check if DUT is a G100 (GR/Graphene) platform."""
    return dut.facts.get('platform', '').startswith(GR_PLATFORM_PREFIX)


def is_gr2_platform(dut):
    """Check if DUT is a G200 (GR2/Graphene2) platform."""
    return dut.facts.get('platform', '').startswith(GR2_PLATFORM_PREFIX)


def is_gr2x_platform(dut):
    """Check if DUT is a G200X (GR2X/Graphene2X) platform."""
    return dut.facts.get('platform', '').startswith(GR2X_PLATFORM_PREFIX)


def is_p200_platform(dut):
    """Check if DUT is a P200 platform."""
    return dut.facts.get('platform', '').startswith(P200_PLATFORM_PREFIX)


def is_smartswitch(dut):
    """Check if DUT is a SmartSwitch (has DPUs)."""
    return dut.facts.get('hwsku', '').startswith(SMARTSWITCH_HWSKU_PREFIX)


def is_compute_ai_platform(dut):
    """Check if DUT is a Compute AI platform (G100/G200)."""
    return is_gr_platform(dut) or is_gr2_platform(dut)


def is_line_card(dut):
    """Check if DUT is a line card HWSKU."""
    return dut.facts.get('hwsku') in LINE_CARD_HWSKUS


def get_asic_gen(dut):
    """
    Get the ASIC generation name for a Cisco device.

    Returns:
        str: 'gb', 'gr', 'gr2', 'pac', 'p200', or 'unknown'
    """
    if not is_cisco_device(dut):
        return 'unknown'

    hwsku = dut.facts.get('hwsku', '')

    if hwsku.startswith(GR_HWSKU_PREFIX):
        return 'gr'
    elif hwsku.startswith(GR2_HWSKU_PREFIX):
        return 'gr2'
    elif hwsku.startswith(PAC_HWSKU_PREFIX):
        return 'pac'
    elif hwsku.startswith(P200_HWSKU_PREFIX):
        return 'p200'
    elif hwsku in GB_HWSKUS:
        return 'gb'
    else:
        return 'unknown'
