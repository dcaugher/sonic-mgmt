### Description of PR

Summary:
Fix `test_pfcwd_interval_config_updates` failure on T2 topologies due to incorrect DUT selection.

Fixes: MIGSOFTWAR-37212

**Root Cause**: On T2 topologies, the testbed has both supervisor nodes and linecard nodes. The test was using `duthost` directly, which could target a supervisor node. Supervisors have no frontend ASICs with Ethernet interfaces or PFC_WD configuration, causing:

1. `enum_rand_one_frontend_asic_index` returns `None` (no frontend ASICs)
2. Namespace prefix fixtures resolve to empty strings (wrong namespace)
3. `show pfcwd config` returns no Ethernet interfaces
4. `get_detection_restoration_times()` loop finds no interfaces, falls through
5. Function returns `None` instead of `(detection_time, restoration_time)`
6. Caller fails with `TypeError: cannot unpack non-iterable NoneType object`

**Fix**: 
1. Use `rand_one_dut_front_end_hostname` instead of `duthost` to ensure the test targets a linecard (data-plane node) on T2, or the regular DUT on non-T2 topologies
2. Fix `pytest_assert(True, ...)` -> `pytest_assert(False, ...)` to properly fail when no interfaces are found (was a no-op before)

### Type of change

- [x] Bug fix
- [ ] Testbed and Framework(new/improvement)
- [ ] New Test case
    - [ ] Skipped for non-supported platforms
- [ ] Test case improvement


### Back port request
- [ ] 202205
- [ ] 202305
- [ ] 202311
- [ ] 202405
- [ ] 202411
- [ ] 202505
- [x] 202511

### Approach
#### What is the motivation for this PR?

`test_pfcwd_interval_config_updates` fails on T2 topologies (e.g., T2-TITAN) with `TypeError: cannot unpack non-iterable NoneType object` because the test targets a supervisor node that has no PFC_WD configuration.

#### How did you do it?

- Changed fixtures and test function to use `duthosts[rand_one_dut_front_end_hostname]` instead of `duthost`
- This fixture automatically selects a linecard on T2 topologies or the regular DUT on other topologies
- Fixed the fallback assertion in `get_detection_restoration_times()` to properly fail instead of silently returning None
- Applied flake8 formatting fixes throughout the file
- Added module-level documentation explaining the T2 topology handling

#### How did you verify/test it?

- Verified fix logic against error trace in MIGSOFTWAR-37212
- Confirmed `rand_one_dut_front_end_hostname` behavior in conftest.py
- Verified flake8 compliance

#### Any platform specific information?

Affects T2 topologies where supervisor and linecard nodes are separate. The fix is backward-compatible with all other topologies.

#### Supported testbed topology if it's a new test case?

N/A - bug fix for existing test. Test remains marked `@pytest.mark.topology('any')`.

### Documentation

Added module-level docstring explaining T2 topology handling and inline comment for the assertion fix.
