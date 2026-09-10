"""Constants for the BedJet integration."""

DOMAIN = "bedjet"

# BedJet 3 GATT service UUID, also declared in manifest.json's bluetooth matchers.
BEDJET_SERVICE_UUID = "00001000-bed0-0080-aa55-4265644a6574"
LOCAL_NAME_PREFIX = "BEDJET"

# How long the BLE link has to stay down before the `device_unreachable`
# repair is raised. The household's own healing machinery (a heal script and
# an hourly re-home) normally reclaims a dropped link within a few minutes,
# so anything shorter would nag about outages that fix themselves; 15
# minutes means "the automatic recovery has had its chance and failed".
# Named `_S` like pybedjet's own timeouts: seconds, compared against
# `time.monotonic()`.
UNREACHABLE_GRACE_S = 15 * 60

# Repair-issue id suffix; the prefix is the device's BLE address, matching the
# entity unique_id scheme (`<address>_<key>`), so one issue exists per config
# entry and the address can be recovered from the id in `repairs.py`.
UNREACHABLE_ISSUE_SUFFIX = "_unreachable"

# Config-entry options keys. Neither is user-facing configuration: they are
# the two facts the repair wizard cannot discover at the moment it runs.
#
# `last_holding_proxy`: the display name of the Bluetooth proxy that carried
# the link the last time there was one. While the device is unreachable no
# scanner holds it, so there is nothing live to ask - yet naming the proxy is
# exactly what the wizard needs to offer a restart of it.
OPTION_LAST_HOLDING_PROXY = "last_holding_proxy"
# `recovery_outlet`: the switch the operator picked to cut mains power, so the
# last-resort step does not ask again every time.
OPTION_RECOVERY_OUTLET = "recovery_outlet"
