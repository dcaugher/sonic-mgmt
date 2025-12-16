"""Generate JSON patches for SONiC Generic Config Updater (GCU).

This module generates JSON patches (RFC 6902) to transform a "no-leaf" configuration
(without a specific T1 neighbor) into a "full" configuration (with the T1 neighbor).

Key Design Considerations:
--------------------------
1. YANG Validation: SONiC uses YANG models to validate configuration changes.
   Some tables have required fields (e.g., PORT requires 'lanes', ACL_TABLE requires 'type').
   Property-level patches like {"op": "add", "path": "/asic0/PORT/Ethernet168/fec"}
   fail YANG validation because the entry doesn't have all required fields.
   Solution: Coalesce property-level patches into complete entry-level patches.

2. Dependency Ordering: Some tables depend on others being configured first.
   - PORT must exist before INTERFACE entries referencing that port
   - INTERFACE base entries (Ethernet168) must exist before IP entries (Ethernet168|10.0.0.1/31)
   - BGP_NEIGHBOR entries require their associated INTERFACE to exist
   - ACL_TABLE binding updates must come last (they reference existing tables)

3. ACL_TABLE Bindings: When adding new ports, existing ACL_TABLE entries need their
   'ports' field updated to include the new ports. These patches must come last.
"""

import json
import jsonpatch
import logging
import os

logger = logging.getLogger(__name__)


def escape_json_pointer(s):
    """Escape special characters for JSON Pointer (RFC 6901).

    In JSON Pointer, '~' must be escaped as '~0' and '/' must be escaped as '~1'.
    """
    return s.replace('~', '~0').replace('/', '~1')

# Required fields for YANG validation by table type.
# When these tables are modified, YANG validation requires these fields to be present.
# If jsonpatch generates property-level patches for these tables, we must coalesce them
# into entry-level patches that include these required fields.
REQUIRED_FIELDS = {
    'PORT': ['lanes'],      # PORT entries must have 'lanes' field
    'ACL_TABLE': ['type'],  # ACL_TABLE entries must have 'type' field (e.g., 'MIRROR', 'L3')
}

# Known table names for detecting single-ASIC config structure
KNOWN_TABLES = {
    'ACL_TABLE', 'ACL_RULE', 'BGP_NEIGHBOR', 'BUFFER_PG', 'CABLE_LENGTH',
    'DEVICE_NEIGHBOR', 'DEVICE_NEIGHBOR_METADATA', 'INTERFACE', 'PFC_WD',
    'PORT', 'PORTCHANNEL', 'PORTCHANNEL_INTERFACE', 'PORTCHANNEL_MEMBER',
    'PORT_QOS_MAP', 'LOOPBACK_INTERFACE', 'VLAN', 'VLAN_MEMBER',
}


def is_multi_asic_config(config):
    """Detect if config is multi-ASIC (has asicN namespaces) or single-ASIC.

    Multi-ASIC config structure:
        {"localhost": {...}, "asic0": {"PORT": {...}}, "asic1": {...}}

    Single-ASIC config structure:
        {"localhost": {...}, "PORT": {...}, "ACL_TABLE": {...}}

    Returns:
        bool: True if multi-ASIC, False if single-ASIC
    """
    for key in config.keys():
        if key.startswith('asic'):
            return True
    # Also check if top-level keys are table names (single-ASIC)
    for key in config.keys():
        if key in KNOWN_TABLES:
            return False
    # Default to multi-ASIC if unclear
    return True


def parse_patch_path(path, is_multi_asic):
    """Parse a JSON patch path into components.

    For multi-ASIC: /asic0/PORT/Ethernet0/fec -> (asic0, PORT, Ethernet0, fec)
    For single-ASIC: /PORT/Ethernet0/fec -> (None, PORT, Ethernet0, fec)

    Args:
        path: JSON pointer path string
        is_multi_asic: Whether the config is multi-ASIC

    Returns:
        tuple: (namespace, table_name, key, property) - any can be None if not present
    """
    parts = path.split('/')
    # parts[0] is always '' (empty string before leading /)

    if is_multi_asic:
        # Multi-ASIC: /namespace/TABLE/key/property
        namespace = parts[1] if len(parts) > 1 else None
        table_name = parts[2] if len(parts) > 2 else None
        key = parts[3] if len(parts) > 3 else None
        prop = parts[4] if len(parts) > 4 else None
    else:
        # Single-ASIC: /TABLE/key/property (no namespace)
        namespace = None
        table_name = parts[1] if len(parts) > 1 else None
        key = parts[2] if len(parts) > 2 else None
        prop = parts[3] if len(parts) > 3 else None

    return (namespace, table_name, key, prop)


def build_patch_path(namespace, table_name, key, is_multi_asic):
    """Build a JSON patch path from components.

    Args:
        namespace: ASIC namespace (e.g., 'asic0') or None for single-ASIC
        table_name: Table name (e.g., 'PORT')
        key: Entry key (e.g., 'Ethernet0')
        is_multi_asic: Whether to include namespace in path

    Returns:
        str: JSON pointer path
    """
    escaped_key = escape_json_pointer(key)
    if is_multi_asic and namespace:
        return "/{}/{}/{}".format(namespace, table_name, escaped_key)
    else:
        return "/{}/{}".format(table_name, escaped_key)


def get_table_from_config(config, namespace, table_name, is_multi_asic):
    """Get a table from config, handling single vs multi-ASIC structure.

    Args:
        config: The configuration dictionary
        namespace: ASIC namespace or None
        table_name: Table name to retrieve
        is_multi_asic: Whether config is multi-ASIC

    Returns:
        dict: The table contents or empty dict
    """
    if is_multi_asic and namespace:
        return config.get(namespace, {}).get(table_name, {})
    else:
        return config.get(table_name, {})


def get_entry_from_config(config, namespace, table_name, key, is_multi_asic):
    """Get a specific entry from config table.

    Args:
        config: The configuration dictionary
        namespace: ASIC namespace or None
        table_name: Table name
        key: Entry key
        is_multi_asic: Whether config is multi-ASIC

    Returns:
        dict: The entry value or empty dict
    """
    table = get_table_from_config(config, namespace, table_name, is_multi_asic)
    return table.get(key, {})


def coalesce_property_patches(patches, full_config, no_leaf_config, tables_to_coalesce):
    """Coalesce property-level patches into entry-level patches.

    When jsonpatch generates patches like:
        {"op": "add", "path": "/asic1/PORT/Ethernet168/fec", "value": "rs"}
        {"op": "replace", "path": "/asic1/PORT/Ethernet168/lanes", "value": "..."}

    This function coalesces them into a single entry-level patch:
        {"op": "replace", "path": "/asic1/PORT/Ethernet168", "value": {...full entry...}}

    This is ALWAYS required for tables with YANG-required fields (PORT, ACL_TABLE)
    because the patch sorter on the DUT tries many different orderings and intermediate
    states may be missing required fields. Even if the entry exists with lanes,
    a patch ordering like:
        1. Add fec
        2. Replace lanes
        3. Replace description
    could fail YANG validation at step 1 if the sorter reorders operations.

    Solution: Always coalesce ALL property-level patches for these tables into
    a single entry-level "replace" operation with the complete final state.

    Works for both single-ASIC and multi-ASIC configurations:
    - Multi-ASIC paths: /asic0/PORT/Ethernet0/fec
    - Single-ASIC paths: /PORT/Ethernet0/fec

    Args:
        patches: List of patch operations from jsonpatch
        full_config: The full configuration dictionary (target state)
        no_leaf_config: The configuration without leaf (current state)
        tables_to_coalesce: List of table names to coalesce (e.g., ['PORT', 'INTERFACE'])

    Returns:
        tuple: (coalesced_patches, remaining_patches, is_multi_asic)
    """
    # Detect if this is multi-ASIC or single-ASIC config
    multi_asic = is_multi_asic_config(full_config)

    # Track entries that need coalescing: {(namespace, table, key): True}
    entries_to_coalesce = {}
    # Track property-level patches to remove
    patches_to_remove = set()

    # Determine minimum path components for property-level patch
    # Multi-ASIC: /asic0/TABLE/key/property = 5 components
    # Single-ASIC: /TABLE/key/property = 4 components
    min_property_components = 5 if multi_asic else 4

    for i, patch in enumerate(patches):
        path_components = patch['path'].split('/')

        if len(path_components) < min_property_components:
            continue

        namespace, table_name, key, prop = parse_patch_path(patch['path'], multi_asic)

        # Skip if no property (this is entry-level, not property-level)
        if prop is None:
            continue

        if table_name not in tables_to_coalesce:
            continue

        # ALWAYS coalesce property-level patches for tables with required YANG fields.
        # The patch sorter on the DUT tries many orderings, and intermediate states
        # may fail YANG validation even if the final state is valid.
        # For tables like PORT (requires "lanes") and ACL_TABLE (requires "type"),
        # we must ensure the patch is a single entry-level operation.
        if table_name in REQUIRED_FIELDS:
            entries_to_coalesce[(namespace, table_name, key)] = True
            patches_to_remove.add(i)
        else:
            # For other tables (INTERFACE, BGP_NEIGHBOR), only coalesce if entry is new
            no_leaf_table = get_table_from_config(no_leaf_config, namespace, table_name, multi_asic)
            if key not in no_leaf_table:
                entries_to_coalesce[(namespace, table_name, key)] = True
                patches_to_remove.add(i)

    # Build coalesced patches
    coalesced_patches = []
    # Track ports that have lane changes (these can't be handled by GCU)
    ports_with_lane_changes = set()

    for (namespace, table_name, key) in entries_to_coalesce:
        full_value = get_entry_from_config(full_config, namespace, table_name, key, multi_asic)
        if full_value:
            # Use "replace" if entry exists in no_leaf_config, "add" if it's new
            no_leaf_table = get_table_from_config(no_leaf_config, namespace, table_name, multi_asic)
            op = "replace" if key in no_leaf_table else "add"

            # CRITICAL: Skip PORT patches that change lanes.
            # Changing lanes on a port requires port breakout, which is a disruptive operation
            # that can't be done atomically via GCU. The patch sorter will fail with
            # "maximum recursion depth exceeded" trying to find a valid ordering because
            # other tables (BUFFER_PG, PFC_WD) depend on the port having consistent lanes.
            if table_name == 'PORT' and key in no_leaf_table:
                no_leaf_entry = no_leaf_table.get(key, {})
                full_lanes = full_value.get('lanes', '')
                no_leaf_lanes = no_leaf_entry.get('lanes', '')
                if full_lanes != no_leaf_lanes:
                    logger.warning("Skipping PORT patch for %s - lanes change from '%s' to '%s'. "
                                   "Port breakout changes require 'config reload' and can't be done via GCU.",
                                   key, no_leaf_lanes, full_lanes)
                    ports_with_lane_changes.add((namespace, key))
                    continue

            coalesced_patches.append({
                "op": op,
                "path": build_patch_path(namespace, table_name, key, multi_asic),
                "value": full_value
            })

    # Remove the property-level patches that were coalesced
    remaining_patches = [p for i, p in enumerate(patches) if i not in patches_to_remove]

    return coalesced_patches, remaining_patches, multi_asic, ports_with_lane_changes


def find_interface_entries_for_bgp(full_config, namespace, local_addr, is_multi_asic):
    """Find INTERFACE entries that match a BGP neighbor's local_addr.

    Works for both single-ASIC and multi-ASIC configurations.

    Args:
        full_config: The full configuration dictionary
        namespace: The ASIC namespace (e.g., 'asic0') or None for single-ASIC
        local_addr: The local IP address from BGP_NEIGHBOR entry
        is_multi_asic: Whether config uses multi-ASIC structure

    Returns:
        list: List of (interface_key, value) tuples for matching INTERFACE entries
    """
    interface_entries = []
    interface_table = get_table_from_config(full_config, namespace, "INTERFACE", is_multi_asic)

    base_interface = None
    for interface_key, value in interface_table.items():
        # Check if this interface key contains the local_addr
        # Keys are like "Ethernet96" or "Ethernet96|10.0.0.160/31"
        if '|' in interface_key and local_addr in interface_key:
            interface_entries.append((interface_key, value))
            # Extract base interface name
            base_interface = interface_key.split('|')[0]

    # Also add the base interface entry if found
    if base_interface and base_interface in interface_table:
        # Insert at beginning so base interface comes before IP entries
        interface_entries.insert(0, (base_interface, interface_table[base_interface]))

    return interface_entries


def find_acl_table_bindings_for_ports(full_config, no_leaf_config, namespace, ports, is_multi_asic):
    """Find ACL_TABLE entries that need port bindings updated.

    When ports are added, we need to update ACL_TABLE entries to include them
    in the 'ports' field if they were bound in the original config.

    Works for both single-ASIC and multi-ASIC configurations.

    Args:
        full_config: The full configuration dictionary (with all ports)
        no_leaf_config: The config without the leaf (missing ports)
        namespace: The ASIC namespace (e.g., 'asic0') or None for single-ASIC
        ports: Set of port names being added
        is_multi_asic: Whether config uses multi-ASIC structure

    Returns:
        list: List of patch operations to update ACL_TABLE bindings
    """
    patches = []
    full_acl_table = get_table_from_config(full_config, namespace, "ACL_TABLE", is_multi_asic)
    no_leaf_acl_table = get_table_from_config(no_leaf_config, namespace, "ACL_TABLE", is_multi_asic)

    for acl_name, full_acl_entry in full_acl_table.items():
        full_ports = full_acl_entry.get("ports", [])
        if not full_ports:
            continue

        # Get current ports in no_leaf config (may be empty or missing some ports)
        no_leaf_acl_entry = no_leaf_acl_table.get(acl_name, {})
        no_leaf_ports = no_leaf_acl_entry.get("ports", [])

        # Find ports that need to be added (in full but not in no_leaf)
        ports_to_add = []
        for port in full_ports:
            if port in ports and port not in no_leaf_ports:
                ports_to_add.append(port)

        # Generate patches to add each missing port to the ACL_TABLE binding
        for port in ports_to_add:
            # Use JSON Patch to add to the ports array
            # We need to replace the entire ports list with the updated one
            escaped_acl_name = escape_json_pointer(acl_name)
            new_ports_list = list(no_leaf_ports) + [port]

            # Build path based on single-ASIC vs multi-ASIC
            if is_multi_asic:
                ports_path = "/{}/ACL_TABLE/{}/ports".format(namespace, escaped_acl_name)
            else:
                ports_path = "/ACL_TABLE/{}/ports".format(escaped_acl_name)

            # Check if we already have a patch for this ACL_TABLE's ports
            # If so, update it instead of creating a new one
            existing_patch = None
            for p in patches:
                if p['path'] == ports_path:
                    existing_patch = p
                    break

            if existing_patch:
                # Add to existing patch's value
                if port not in existing_patch['value']:
                    existing_patch['value'].append(port)
            else:
                patches.append({
                    "op": "replace",
                    "path": ports_path,
                    "value": new_ports_list
                })

    return patches


def generate_config_patch(full_config_path, no_leaf_config_path):
    """
    Generate a JSON patch file by comparing two configuration files.
    Args:
        full_config_path (str): Path to the full configuration JSON file
        no_leaf_config_path (str): Path to the configuration JSON file without leaf
    Returns:
        str: Path to the generated patch file
    """
    # Load full configuration
    with open(full_config_path, 'r') as file:
        full_config = json.load(file)

    # Load configuration without leaf
    with open(no_leaf_config_path, 'r') as file:
        no_leaf_config = json.load(file)

    # Generate patches
    patches = jsonpatch.make_patch(no_leaf_config, full_config)

    # Add Cluster supported Tables (INTERFACE added for BGP_NEIGHBOR dependencies)
    filtered_tables = [
        "ACL_TABLE", "BGP_NEIGHBOR", "BUFFER_PG", "CABLE_LENGTH", "DEVICE_NEIGHBOR",
        "DEVICE_NEIGHBOR_METADATA", "INTERFACE", "PFC_WD", "PORT", "PORTCHANNEL",
        "PORTCHANNEL_INTERFACE", "PORTCHANNEL_MEMBER", "PORT_QOS_MAP"
    ]
    admin_status_tables = ["BGP_NEIGHBOR", "PORT", "PORTCHANNEL"]

    # Coalesce property-level patches into entry-level patches for tables that need it.
    # Why each table is coalesced:
    # - PORT: Requires 'lanes' field for YANG validation ("Missing required element 'lanes'")
    # - INTERFACE: Needs proper ordering (base entry before IP entry)
    # - ACL_TABLE: Requires 'type' field for YANG validation ("Missing required element 'type'")
    # - BGP_NEIGHBOR: Needs complete entry to avoid "All Keys are not parsed in BGP_NEIGHBOR" error
    tables_to_coalesce = ["PORT", "INTERFACE", "ACL_TABLE", "BGP_NEIGHBOR"]
    coalesced_port_patches, remaining_patches, is_multi_asic, ports_with_lane_changes = coalesce_property_patches(
        patches.patch, full_config, no_leaf_config, tables_to_coalesce
    )
    logger.info("Coalesced %d property-level patches into %d entry-level patches (multi_asic=%s)",
                len(patches.patch) - len(remaining_patches), len(coalesced_port_patches), is_multi_asic)
    if ports_with_lane_changes:
        logger.warning("Skipped %d ports with lane changes (require port breakout): %s",
                       len(ports_with_lane_changes), ports_with_lane_changes)

    # First pass: collect BGP_NEIGHBOR local_addr values and PORT entries being added
    bgp_local_addrs = {}  # {namespace: set of local_addr values}
    ports_being_added = {}  # {namespace: set of port names}

    # Check coalesced patches for ports being added
    for patch in coalesced_port_patches:
        namespace, table_name, key, _ = parse_patch_path(patch['path'], is_multi_asic)
        if table_name == 'PORT' and key and key.startswith('Ethernet'):
            if namespace not in ports_being_added:
                ports_being_added[namespace] = set()
            ports_being_added[namespace].add(key)

    for patch in remaining_patches:
        namespace, table_name, key, _ = parse_patch_path(patch['path'], is_multi_asic)

        if table_name is None or key is None:
            continue
        if namespace == "localhost":
            continue

        if patch['op'] == 'add' and table_name == 'BGP_NEIGHBOR':
            value = patch.get('value', {})
            if isinstance(value, dict):
                local_addr = value.get('local_addr')
                if local_addr:
                    if namespace not in bgp_local_addrs:
                        bgp_local_addrs[namespace] = set()
                    bgp_local_addrs[namespace].add(local_addr)

        # Track ports being added for ACL_TABLE binding updates
        if patch['op'] == 'add' and table_name == 'PORT' and key.startswith('Ethernet'):
            if namespace not in ports_being_added:
                ports_being_added[namespace] = set()
            ports_being_added[namespace].add(key)

    # Build set of required INTERFACE entries based on BGP_NEIGHBOR local_addr values
    required_interface_patches = []  # List of patches to add for INTERFACE entries
    added_interface_keys = set()  # Track what we have added to avoid duplicates

    for namespace, local_addrs in bgp_local_addrs.items():
        for local_addr in local_addrs:
            interface_entries = find_interface_entries_for_bgp(full_config, namespace, local_addr, is_multi_asic)
            for interface_key, value in interface_entries:
                # Check if this interface is missing in no_leaf_config
                no_leaf_interface = get_table_from_config(no_leaf_config, namespace, "INTERFACE", is_multi_asic)
                patch_key = (namespace, interface_key)
                if interface_key not in no_leaf_interface and patch_key not in added_interface_keys:
                    escaped_key = escape_json_pointer(interface_key)
                    required_interface_patches.append({
                        "op": "add",
                        "path": build_patch_path(namespace, "INTERFACE", escaped_key, is_multi_asic),
                        "value": value
                    })
                    added_interface_keys.add(patch_key)

    # Track which entries were coalesced into complete entry-level patches.
    # These entries already have admin_status included in their full value from full_config,
    # so we must NOT generate separate admin_status patches for them (would be duplicates).
    # Example: /asic1/BGP_NEIGHBOR/10.0.0.171 -> already has {"admin_status": "up", ...}
    coalesced_entry_paths = set()
    for patch in coalesced_port_patches:
        # Extract the entry path (without property suffix)
        namespace, table_name, key, _ = parse_patch_path(patch['path'], is_multi_asic)
        if table_name and key:
            entry_path = build_patch_path(namespace, table_name, key, is_multi_asic)
            coalesced_entry_paths.add(entry_path)

    filtered_patch_list = []
    for patch in remaining_patches:
        # Parse path using the helper to handle both single-ASIC and multi-ASIC
        namespace, table_name, key, prop = parse_patch_path(patch['path'], is_multi_asic)

        if namespace == "localhost":  # internal to SONiC, do not update
            continue

        # Skip if table not supported
        if table_name not in filtered_tables:
            continue

        # Skip patches that reference ports with lane changes.
        # These ports can't be modified via GCU (require port breakout via config reload).
        # This includes BUFFER_PG, PFC_WD, PORT_QOS_MAP, DEVICE_NEIGHBOR entries for these ports.
        if ports_with_lane_changes and key:
            # Check if any port with lane changes is referenced in this key
            # Keys can be like "Ethernet232" or "Ethernet232|3-4" (for BUFFER_PG)
            port_name = key.split('|')[0] if '|' in key else key
            if (namespace, port_name) in ports_with_lane_changes:
                logger.debug("Skipping patch for %s - references port %s with lane changes",
                             patch['path'], port_name)
                continue

        # Skip ACL_TABLE/ports patches - we generate our own complete replacement patches
        # via find_acl_table_bindings_for_ports() which creates proper "replace" ops.
        # Without this filter, we'd have duplicate/conflicting patches like:
        #   {"op": "add", "path": "/asic2/ACL_TABLE/EVERFLOW/ports/0", "value": "Ethernet200"}
        #   {"op": "replace", "path": "/asic2/ACL_TABLE/EVERFLOW/ports", "value": [...]}
        if table_name == 'ACL_TABLE' and prop == 'ports':
            continue
        # Also skip array index patches for ports (e.g., /ACL_TABLE/EVERFLOW/ports/0)
        path_components = patch['path'].split('/')
        if table_name == 'ACL_TABLE':
            # Check if path has /ports/ in it (could be /ports/0, /ports/1, etc.)
            if 'ports' in path_components:
                continue

        # For entry-level "add" patches on BGP_NEIGHBOR, PORT, PORTCHANNEL tables,
        # inject admin_status directly into the value instead of creating separate patches.
        # This is CRITICAL because the YANG patch sorter may try many orderings, and
        # a separate admin_status patch could be applied before the entry exists.
        # By including admin_status in the entry value, we ensure it's created atomically.
        if patch['op'] == 'add' and table_name in admin_status_tables and key:
            # Only for entry-level patches (prop is None), not property-level patches
            if prop is None and isinstance(patch.get('value'), dict):
                if 'admin_status' not in patch['value']:
                    # Create a new patch with admin_status injected
                    new_value = dict(patch['value'])
                    new_value['admin_status'] = 'up'
                    patch = dict(patch)
                    patch['value'] = new_value

        filtered_patch_list.append(patch)

    # Build the final patch list with correct dependency order:
    # 1. PORT entries (coalesced) - must come first
    # 2. ACL_TABLE entries (coalesced) - need complete entries with "type" field
    # 3. INTERFACE base entries (e.g., Ethernet168) - before IP entries
    # 4. INTERFACE IP entries (e.g., Ethernet168|10.0.0.170/31)
    # 5. BGP_NEIGHBOR entries (coalesced) - need complete entries for YANG validation
    # 6. Other table entries
    # 7. ACL_TABLE binding updates - last

    # Separate coalesced patches by table type for proper dependency ordering.
    # Each table type has different dependencies and must be applied in the right order.
    coalesced_port_only = [p for p in coalesced_port_patches if '/PORT/' in p['path']]       # First: ports must exist
    coalesced_acl_table = [p for p in coalesced_port_patches if '/ACL_TABLE/' in p['path']]  # Second: ACL tables
    coalesced_interface = [p for p in coalesced_port_patches if '/INTERFACE/' in p['path']] # Third: interfaces
    coalesced_bgp_neighbor = [p for p in coalesced_port_patches if '/BGP_NEIGHBOR/' in p['path']]  # Fourth: BGP

    # Filter out ports with lane changes from ACL_TABLE 'ports' arrays.
    # These ports can't be added via GCU, so we must not reference them in ACL bindings.
    if ports_with_lane_changes:
        # Build set of port names with lane changes for quick lookup
        ports_to_exclude = {port_name for (_, port_name) in ports_with_lane_changes}

        filtered_acl_patches = []
        for patch in coalesced_acl_table:
            if 'value' in patch and isinstance(patch['value'], dict) and 'ports' in patch['value']:
                # Filter out ports with lane changes from the ports array
                original_ports = patch['value']['ports']
                filtered_ports = [p for p in original_ports if p not in ports_to_exclude]
                if filtered_ports != original_ports:
                    # Create new patch with filtered ports
                    new_patch = dict(patch)
                    new_patch['value'] = dict(patch['value'])
                    new_patch['value']['ports'] = filtered_ports
                    logger.debug("Filtered ports with lane changes from ACL_TABLE patch: %s -> %s",
                                 original_ports, filtered_ports)
                    filtered_acl_patches.append(new_patch)
                else:
                    filtered_acl_patches.append(patch)
            else:
                filtered_acl_patches.append(patch)
        coalesced_acl_table = filtered_acl_patches

    # Sort INTERFACE patches: base entries (Ethernet168) before IP entries (Ethernet168|10.0.0.1/31).
    # The '|' character separates interface name from IP, so count('|') == 0 means base entry.
    # Base entries must be created first because IP entries depend on them.
    required_interface_patches.sort(key=lambda p: (p['path'].count('|'), p['path']))
    coalesced_interface.sort(key=lambda p: (p['path'].count('|'), p['path']))

    # Combine all INTERFACE patches and dedupe (same interface may appear in both lists)
    # required_interface_patches: from BGP_NEIGHBOR local_addr analysis
    # coalesced_interface: from coalesce_property_patches()
    all_interface_patches = []
    seen_interface_paths = set()
    for p in required_interface_patches + coalesced_interface:
        if p['path'] not in seen_interface_paths:
            all_interface_patches.append(p)
            seen_interface_paths.add(p['path'])

    # Build final ordered list respecting all dependencies:
    # PORT -> ACL_TABLE -> INTERFACE -> BGP_NEIGHBOR -> other tables -> ACL bindings
    final_patch_list = (coalesced_port_only + coalesced_acl_table + all_interface_patches +
                        coalesced_bgp_neighbor + filtered_patch_list)

    # Track coalesced ACL_TABLE entries - these already have complete 'ports' arrays,
    # so we should NOT generate separate binding patches for them.
    coalesced_acl_tables = set()
    for patch in coalesced_acl_table:
        namespace, table_name, key, _ = parse_patch_path(patch['path'], is_multi_asic)
        if table_name == 'ACL_TABLE' and key:
            coalesced_acl_tables.add((namespace, key))

    # Generate ACL_TABLE binding patches for ports being added.
    # These update existing ACL_TABLE entries to add new ports to their 'ports' field.
    # Example: EVERFLOW ACL table needs Ethernet192 added to its ports list.
    # SKIP tables that were already coalesced - they already have correct ports.
    # These MUST come last because:
    # 1. The ACL_TABLE entry must already exist (created by coalesced_acl_table)
    # 2. We're using 'replace' op on the ports array, not 'add' to the table
    acl_binding_patches = []
    for namespace, ports in ports_being_added.items():
        acl_patches = find_acl_table_bindings_for_ports(
            full_config, no_leaf_config, namespace, ports, is_multi_asic
        )
        # Filter out patches for already-coalesced ACL tables
        for patch in acl_patches:
            patch_namespace, patch_table, patch_acl_name, _ = parse_patch_path(patch['path'], is_multi_asic)
            if (patch_namespace, patch_acl_name) not in coalesced_acl_tables:
                acl_binding_patches.append(patch)

    # Add ACL binding patches at the end (after all table entries exist)
    final_patch_list.extend(acl_binding_patches)

    filtered_patch = jsonpatch.JsonPatch(final_patch_list)

    # Log the complete patch for diagnostic purposes
    logger.info("Generated patch with %d operations:", len(final_patch_list))
    logger.info("  - Coalesced PORT entries: %d", len(coalesced_port_only))
    logger.info("  - Coalesced ACL_TABLE entries: %d", len(coalesced_acl_table))
    logger.info("  - INTERFACE entries: %d", len(all_interface_patches))
    logger.info("  - ACL_TABLE binding updates: %d", len(acl_binding_patches))
    logger.info("  - Other entries: %d", len(filtered_patch_list))

    if ports_with_lane_changes:
        logger.warning("IMPORTANT: %d ports require port breakout changes and were skipped: %s",
                       len(ports_with_lane_changes),
                       [port for (_, port) in ports_with_lane_changes])
        logger.warning("Port breakout changes cannot be done via GCU. Use 'config load_minigraph' instead.")

    logger.debug("Complete patch content:\n%s", json.dumps(filtered_patch.patch, indent=2))

    # Generate output path in same directory as full config
    output_dir = os.path.dirname(full_config_path)
    output_file = os.path.join(output_dir, 'generated_patch.json')

    # Write patch to file
    with open(output_file, 'w') as file:
        json.dump(filtered_patch.patch, file, indent=4)

    # Also write metadata file with information about skipped items
    metadata = {
        'total_patches': len(final_patch_list),
        'port_patches': len(coalesced_port_only),
        'acl_table_patches': len(coalesced_acl_table),
        'interface_patches': len(all_interface_patches),
        'bgp_neighbor_patches': len(coalesced_bgp_neighbor),
        'acl_binding_patches': len(acl_binding_patches),
        'ports_with_lane_changes': [port for (_, port) in ports_with_lane_changes],
        'is_multi_asic': is_multi_asic
    }
    metadata_file = os.path.join(output_dir, 'generated_patch_metadata.json')
    with open(metadata_file, 'w') as file:
        json.dump(metadata, file, indent=4)

    return output_file
