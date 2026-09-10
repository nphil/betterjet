# WHATCHANGED - HA layer (HALayer)

Scope: `custom_components/bedjet/` top-level platform files, `coordinator.py` (new),
`diagnostics.py` (new), `repairs.py` (new), `const.py`,
`strings.json`/`translations/en.json`/`icons.json`,
`manifest.json`, repo-root `hacs.json`, `README.md`, `.github/workflows/{validate,release,stale}.yaml`.
Does not touch `custom_components/bedjet/pybedjet/` (LibLayer) or `tests/` (TestsCI-2).

## Root cause of the "must disable the integration to use the phone app" bug

The BedJet only accepts one BLE connection and stops advertising while held. The
base integration had no way to voluntarily release the connection short of
disabling the whole config entry. Fixed by adding a `bluetooth_connection`
switch entity that maps directly to the library's `hold_connection` flag:
turning it off calls `stop()`-equivalent (via the setter) and turning it on
resumes the maintain-and-reconnect loop. This switch is the only entity that
stays available while disconnected, since it's precisely the tool needed to
get reconnected.

## Coordinator: polling -> push

Old: `DataUpdateCoordinator` polling every 15s (`UPDATE_SECONDS`), each tick
calling `bedjet.update()` (a full read cycle). Real device streams a decoded
status frame at ~4 Hz whenever connected (even in standby), so polling both
under-used the data (15s staleness) and required its own error-swallowing
logic for "stale but still connected" (`is_data_stale`).
New: `BedJetCoordinator(DataUpdateCoordinator[BedJetState | None])` has no
`update_interval`/`update_method`; the library's `register_callback` fires on
every valid decoded frame and pushes it straight in via
`async_set_updated_data`. `coordinator.available` proxies `device.available`
(connected AND a frame within the library's 300s staleness window), not
`last_update_success` (which the base coordinator sets to `True` on
construction and never revisits for a push-only coordinator).
No coordinator-level dedup was added for consecutive identical frames (an
earlier draft of this doc claimed `always_update=False` did this - that was
wrong and has been corrected: `always_update` is only consulted by
`DataUpdateCoordinator`'s polling refresh path, not by
`async_set_updated_data`, which is what a push coordinator actually calls;
it would have been dead configuration). No dedup is needed here regardless:
pybedjet already rate-limits its own callback firing for continuous-value-
only changes per the Contract (mode/fan/target/notification changes still
publish immediately), and Home Assistant's own state machine
(`core.py`'s `async_set_internal`) is already a fast no-op - it does not
fire `state_changed` - when an entity writes state and attributes identical
to what's already recorded. Layering a third dedup on top of both of those
would have been redundant complexity, not a fix for a real problem.

## Setup no longer blocks on a first frame

The first version of this rewrite still waited (bounded, with a timeout) for
one confirmed status frame during `async_setup_entry` before forwarding
platforms, mirroring the base integration's old `DEVICE_TIMEOUT` behavior.
On review that directly undermines the fix above: if the phone app holds the
slot at Home Assistant startup, the entry would fail into
`ConfigEntryNotReady` retries and *no entities would exist at all* -
including the `bluetooth_connection` switch a user would need to see in
order to do anything about it. `async_setup_entry` now registers the
advertisement callback, builds the coordinator, calls `await device.start()`
(confirmed with LibLayer to return promptly regardless of connection
outcome - it only spawns the library's own backoff-retry connect
supervisor), and forwards platforms immediately. Every entity except the
connection switch simply reports unavailable until the first frame lands,
which is what `BedJetEntity.available` already existed to express.
`ConfigEntryNotReady` is now raised only when
`bluetooth.async_last_service_info` returns `None` - this address has never
been seen by Home Assistant's Bluetooth stack at all, so there's no
`BLEDevice` to even construct `BedJet` with; HA's built-in retry-with-backoff
covers the case where a scanner or proxy picks it up later. `const.py`'s
`CONNECT_TIMEOUT` is gone along with the `asyncio.Event`/timeout dance it
existed for.

`async_unload_entry` was also reordered: it now unloads the platforms first
and only calls `device.stop()` if that succeeds, instead of stopping the BLE
connection unconditionally before attempting unload (which would drop the
connection even if a platform vetoed the unload and the entry stayed loaded).

## Availability

`BedJetEntity.available` is overridden (not just relying on
`CoordinatorEntity.available`, which only tracks `last_update_success`) to
require both `coordinator.available` and `device.hold_connection`, so every
entity but the connection switch goes unavailable the instant the slot is
handed away - there is genuinely nothing to report at that point, and no
optimistic value is shown.

## No optimistic state

Every command handler (`climate.async_set_hvac_mode`/`async_set_temperature`/
`async_set_fan_mode`/`async_set_preset_mode`, `fan.async_turn_on`/`_off`,
`number.async_set_native_value`, `switch.async_turn_on`/`_off` for
`enable_led`/`mute_beeps`, `button.async_press`) awaits the pybedjet call
directly; the library itself waits for the confirming frame (or times out and
raises `BedJetCommandError`/`BedJetConnectionError`). `BedJetEntity._async_send_command`
is a single shared wrapper that turns any `BedJetError` into
`HomeAssistantError` so the UI/service-call caller sees a real error instead
of a silently-reverted optimistic value. No entity sets `_attr_*` from the
arguments passed to a service call - only from the next pushed `BedJetState`.

## Two review-caught bugs fixed before landing

- `climate.async_set_preset_mode("none")` originally mapped unconditionally
  to `set_mode(BedJetMode.HEAT)`. Since `_attr_preset_mode` reports `"none"`
  for every mode except Turbo/Extended Heat (i.e. also for Cool, Dry, and
  plain Heat), any caller that set preset `"none"` while the unit was in Cool
  or Dry would have silently switched it to Heat. Fixed to only send
  `set_mode(HEAT)` when the device is currently in Turbo or Extended Heat
  (matching the base integration's original Turbo-only revert behavior);
  otherwise it's a no-op, matching what `"none"` already displays.
- `sensor.py`'s `notification` value function used `if (notification :=
  device.state.notification)` to distinguish "no notification field decoded
  yet" (`None`) from "explicitly no active notification"
  (`BedJetNotification.NONE`, value `0`). A plain `Enum` member is always
  truthy regardless of its underlying value, but if `BedJetNotification` were
  ever an `IntEnum` the `NONE` member (`0`) would be falsy and get treated
  the same as `None` (`unknown`) instead of rendering `"none"`. Changed to an
  explicit `is not None` check, which is correct either way.

`diagnostics.py` originally subtracted `device.last_frame_at` from
`time.time()`. Confirmed with LibLayer that `last_frame_at` is a
monotonic-clock stamp, same family as `BluetoothServiceInfoBleak.time` -
mixing either with wall-clock `time.time()` would have produced a
meaningless multi-decade "seconds since" value. Fixed to use
`bluetooth_data_tools.monotonic_time_coarse()` throughout (the exact
function HA's own bluetooth component uses whenever it compares against
`.time`, verified in `components/bluetooth/util.py` and
`active_update_coordinator.py` - on Linux it's `CLOCK_MONOTONIC_COARSE`,
numerically compatible with `CLOCK_MONOTONIC`/`time.monotonic()` and
verified so at import time by the library itself; elsewhere it's just
`time.monotonic`), restored the raw `last_frame_at` field alongside the
derived `seconds_since_last_frame` (both were asked for, an earlier pass
dropped the raw one), and added the `advertisement_age_seconds` field the
assignment asked for (via `bluetooth.async_last_service_info`), which was
missing entirely from the first pass.

## Two more fixes from a second design review

- **`scanner_source` was silently always `None`.** LibLayer caught it first:
  `BluetoothServiceInfoBleak.advertisement` is a plain bleak
  `AdvertisementData` with no `.source` field - `.source` only exists on the
  sibling `BluetoothServiceInfoBleak` object itself, and nothing was ever
  passing it through. `BedJet.__init__` and
  `set_ble_device_and_advertisement_data` both gained an additive
  keyword-only `source: str | None`; `__init__.py` (both the initial
  construction and the ongoing passive-advertisement callback) and
  `config_flow.py`'s probe now pass `source=service_info.source` explicitly.
- **Availability changes from toggling the connection switch weren't
  reaching other entities.** `BedJetEntity.available` is a property, so it's
  always correct *if read*, but Home Assistant's state machine only shows
  what was last passed to `async_write_ha_state()`. Every entity re-checks
  and republishes on a coordinator listener fan-out - which normally only
  happens when a new frame is pushed via `async_set_updated_data`. Flipping
  `hold_connection` via the connection switch doesn't produce a new
  `BedJetState` (it isn't part of that dataclass), so without an explicit
  nudge, the other ~14 entities would keep showing their last known
  values - looking "available" - indefinitely after a user releases the
  slot, exactly contradicting the "no stale-looking state" goal this switch
  exists to serve. Fixed: `BedJetConnectionSwitch.async_turn_on`/`_off` now
  call `self.coordinator.async_update_listeners()` after updating
  `hold_connection`, forcing every entity to re-check `available` and
  republish immediately. Confirmed with LibLayer that this is belt-and-
  suspenders rather than covering a real gap: `register_callback` fires
  unconditionally (bypassing the continuous-value throttle) on every
  decoded frame, on successful connect, on every disconnect (expected or
  not), and on the watchdog's 300s UNAVAILABLE transition - so
  `coordinator.async_set_updated_data` runs and every entity re-reads
  `available` for the device-initiated case too, without any HA-side
  polling. No fallback timer was added; one would have been dead,
  duplicate logic sitting next to a mechanism that already exists.)

## Entity unique_id map (old -> new)

Unique IDs are stable per Contract; nothing below renames an ID a user
already has in their entity registry.

| Platform | Key | Status |
|---|---|---|
| climate | *(bare `{address}`)* | unchanged |
| fan | `fan` | unchanged |
| sensor | `ambient_temperature` | unchanged |
| sensor | `notification` | unchanged id; **dropped `entity_category: diagnostic`** - the assignment's category spec lists notification only under "enabled by default", not in the diagnostic list, so it now has no category (was previously diagnostic+enabled, an inconsistent combination) |
| sensor | `bio_sequence_step` | unchanged (now diagnostic+disabled, was already) |
| sensor | `shutdown_reason` | unchanged (diagnostic+disabled, was already) |
| sensor | `turbo_time` | unchanged; **value is now a plain `int` from the frame** (was `timedelta.total_seconds()` over a computed value) - same unit (seconds), simpler source |
| sensor | `update_phase` | unchanged; dropped two bogus `state.24`/`state.26` translation entries that were dead code (numeric sensor, no `device_class: enum`, so those strings could never render) |
| sensor | `run_end_time` | **REMOVED** - not in the fixed Contract's `BedJetState` (no run-end wall-clock timestamp is decoded); no replacement, this was derived speculatively in the old code from `runtime_remaining + now()` and could drift from the device's own clock |
| sensor | `outlet_temperature` | **NEW** - `state.actual_temp_c`, enabled by default (previously only exposed as the climate entity's `current_temperature` attribute, never as its own sensor) |
| sensor | `scanner` | **NEW** - `device.scanner_source`, diagnostic, disabled by default |
| binary_sensor | `connection_test` | unchanged |
| binary_sensor | `dual_zone` | unchanged |
| binary_sensor | `units_setup` | unchanged |
| switch | `enable_led` | unchanged id, stays CONFIG category; added `entity_registry_enabled_default=False` (was enabled by default) per the assignment's disabled-by-default config list |
| switch | `mute_beeps` | same change as `enable_led` above |
| switch | `bluetooth_connection` | **NEW** - see "Root cause" above; no entity_category, always available |
| button | `sync_clock` | unchanged id; now calls `device.sync_clock()` with no args (the library takes a `clock` callable at construction and reads it internally, so HA no longer computes hour/minute itself) |
| button | `acknowledge_notification` | **NEW** - `device.acknowledge_notification()`, enabled by default, no category |
| button | `firmware_update` | **NEW** - `device.request_firmware_update()`, CONFIG, disabled by default, name/description warn it reboots the unit |
| number | `runtime_remaining` | unchanged |

## config_flow

Kept bluetooth-discovery + manual-pick + `already_configured` abort shape.
Replaced the old `bedjet.update()` probe (a method that no longer exists in
the Contract) with a short-timeout connect-and-wait-for-first-frame probe
using the same `BedJet`/`register_callback`/`start`/`stop` surface the
integration itself uses, so the probe path and the real setup path share one
connection model instead of two. Title now comes from
`human_readable_name(None, discovery_info.name, discovery_info.address)` -
the old code's first argument was a device-name string read over BLE via a
GET_BIO/name characteristic; that name-reading capability is not part of the
Contract for this rewrite (deliberately dropped, no device model/firmware/name
properties are read at all - `entity.py` hardcodes `model="BedJet 3"`), so the
fallback chain (advertised local name, then address) is what actually runs.
Added `config.error.cannot_connect`/`unknown` translation strings alongside
the existing `config.abort` ones, since the manual-pick step's per-field
`errors["base"]` was pointing at abort-only translation keys before (a
pre-existing gap - those errors would have rendered as raw untranslated keys).

## manifest.json / hacs.json / workflows

- `manifest.json`: `version` -> `1.0.0`, `iot_class` `local_polling` ->
  `local_push`, `documentation`/`issue_tracker` -> `nphil/ha-bedjet`,
  `codeowners` -> `["@nphil"]`. Bluetooth matchers and loggers unchanged.
- `hacs.json`: dropped `zip_release`/`filename` (releases are `gh release
  create` from the parent repo owner, not a HACS-driven zip asset),
  `homeassistant` -> `2026.1.0`.
- `.github/workflows/release.yaml` and `stale.yaml` deleted (releases are
  manual `gh release create`; TestsCI-2 owns the new `tests.yml`).
  `validate.yaml` kept, hassfest job unchanged, HACS job now passes
  `ignore: brands` (this fork isn't in the upstream `home-assistant/brands`
  repo under this name yet, which would otherwise fail validation on every
  push).

## README

Full rewrite: leads with the one-connection rule and the `bluetooth_connection`
switch as the actual mechanism (not "disable the integration"), explains BLE
proxy slot sharing, documents every entity with its category/enabled-default
in a table, gives a debug-logger snippet, and preserves the credit lineage
(robert-friedland -> asheliahut -> natekspencer -> nphil). Dropped the
auto-generated release/download/star-history badges tied to natekspencer's
GitHub stats, since this fork's releases/stars are tracked separately.

## `device_unreachable` repair + escalating recovery wizard

Added for the case the household's own healing machinery (a heal script plus an
hourly re-home) cannot fix: a link that stays down. `repairs.py` is new,
`coordinator.py` grew the issue lifecycle, `__init__.py` gained one call, and
`const.py` holds the four new constants (`UNREACHABLE_GRACE_S`,
`UNREACHABLE_ISSUE_SUFFIX`, `OPTION_LAST_HOLDING_PROXY`,
`OPTION_RECOVERY_OUTLET`). `manifest.json` needs nothing: `repairs` is not a
declared dependency, Home Assistant discovers the platform by module name.

### The bug this is shaped around

A sibling BLE integration in this house raised a repair at 12:22, the condition
cleared at 13:00, and the issue was still open 13 hours later. Its
`async_delete_issue` was gated on an in-memory "previously synced" flag that a
config-entry reload reset to `None`, and the reload landed at 12:38 - between
the two. So the rule here is that issue state is only ever reconciled against
*reality*: `async_reconcile_unreachable_issue()` reads the live link, never a
remembered "did we open one?". Both registry calls are idempotent, so it is
called from every health edge *and* unconditionally from `async_setup_entry` -
and that setup-time call is the one that survives a reload, because a fresh
coordinator's memory is empty by construction.

### What "unreachable" means here

`device.available` (connected AND a frame inside the library's staleness
window), not `device.connected`. The BedJet streams status at ~4 Hz for as long
as a real link is held, so a connection that stopped producing frames is a
wedged link that still occupies the device's only connection slot - which is
precisely the failure the repair exists to surface. `device.connected` alone,
or "the entities still show values", would suppress it.

Conjoining habluetooth's slot accounting (`connection_source is not None`) was
considered and rejected, and the reasoning is in the reconcile's docstring so
it does not get re-litigated: a frame can only arrive over a live GATT link, so
freshness already implies the link, and `resolve_connection_source` falls back
to the last scanner source whenever we are connected - the conjunction
discriminates in no reachable state, while putting a `get_manager()` call
(which raises before the Bluetooth manager exists) in the path that *clears*
the repair. Allocation-without-frames is the ghost link, which `available`
already reports as down.

The issue is raised only after the link has been down continuously for
`UNREACHABLE_GRACE_S` (15 minutes), timed with `async_call_later` against
`time.monotonic()` (a wall-clock jump must not fabricate an outage) and
cancelled from `async_shutdown`, which Home Assistant registers as a
config-entry unload callback. Nothing pushes a frame while the link is down, so
a timer is the only thing that can turn "down now" into "down for 15 minutes".

Two edge flags now exist and mean different things: `_was_connected` drives the
operator-facing connect/disconnect INFO log (GATT edge), `_link_was_up` drives
the repair (health edge). The health edge is gated purely to keep the issue
registry from being rewritten four times a second - `async_get_or_create` fires
a registry-updated event and schedules a save even when the issue already
exists - and cannot strand an issue, because setup reconciles unconditionally.

### Remembering the proxy

While the repair is open, nothing holds the device, so there is no live scanner
to name - yet naming one is exactly what a "restart the proxy" step needs. The
connect edge writes the proxy's node name into
`entry.options["last_holding_proxy"]`, and only when it changed. The connect
edge is sufficient: a GATT link belongs to the radio that opened it, so it
cannot migrate between proxies without a disconnect first.

**Which name, exactly** - this was nearly a silent production failure. The
first cut persisted `connection_scanner_name`, i.e. what the Connection sensor
shows. habluetooth builds a remote scanner's `name` as "<adapter> (<source>)"
(`base_scanner.py`: `self.name = adapter_human_name(adapter, source) if adapter
!= source else source`), and the live instance confirmed it on 2026-09-09:
`sensor.master_bedroom_bedjet_connection` read `downstairs-bluetooth-proxy
(D4:D4:DA:9D:40:8A)` while the registered action was
`esphome.downstairs_bluetooth_proxy_restart_proxy`. Slugifying the display name
would have looked up
`esphome.downstairs_bluetooth_proxy_d4_d4_da_9d_40_8a_restart_proxy`, found
nothing, and dropped the proxy rung from the menu forever - with no error
anywhere. The new `holding_proxy_name` property reads `scanner.adapter`, the
bare ESPHome node name, which is also immune to the device being renamed in
Home Assistant (the downstairs proxy is named for the Tool Room area in HA
while its node is still `downstairs`). `connection_scanner_name` and the
Connection sensor are deliberately unchanged; automations read that sensor.

Two option-write hazards flagged during this batch do not apply to this
integration, and were checked rather than assumed: there is no
`add_update_listener` registration anywhere (so an options write cannot trigger
a reload, let alone a reload loop while a link flaps), and there is no options
flow at all (so no `async_create_entry(data=user_input)` can wipe
`last_holding_proxy`/`recovery_outlet`). Both of this integration's own writes
merge with `{**entry.options, ...}`; if an options flow is ever added, it must
do the same.

### The wizard

`BleRecoveryFixFlow` (`async_step_init` -> `async_step_menu`) offers, in order:
`recheck`, `reload`, `restart_proxy`, `power_cycle` - cheapest first, the
mains-cutting rung last. This ordering is deliberately identical across the
four BLE integrations that got this pattern, so the operator sees one ladder
everywhere.

`restart_proxy` is only in the menu when a proxy is known (the live hold, else
the remembered node name) *and* `slugify(<node>) + "_restart_proxy"` is a
registered `esphome` action. ESPHome exposes API actions (`api:` -> `actions:`,
not a Restart *button* entity - the proxies' config buttons are registry-
disabled here) as `esphome.<node slug>_<action>`, so
`downstairs-bluetooth-proxy` answers to
`esphome.downstairs_bluetooth_proxy_restart_proxy`. Offering a rung that cannot
be climbed is worse than not offering it. The lookup uses
`hass.services.has_service(...)`, not `async_services()`: core's own docstring
says the latter copies the whole service registry and is expensive, and the
menu re-derives this on every draw.

Every rung then polls the health predicate in `POLL_INTERVAL_S` slices against
a `monotonic()` deadline (so the check's own cost comes out of the budget
rather than silently extending it) for up to 45 s (60 s after a power cycle,
which also has to wait out a boot), with `asyncio.sleep` rather than a blocking
wait: the link returns through pybedjet's
own supervisor, so there is nothing here to await, and a repair flow must never
sit on the event loop. Healthy -> `async_create_entry(data={})` *and* a call
into the coordinator's own reconcile, so both views of the issue agree
immediately instead of drifting until the next health edge. Still unhealthy ->
back to the menu with a `last_result` placeholder saying what was tried and how
it went (empty string on first entry, never the literal "None").

`restart_proxy` reports "asked to restart", never "rebooted": the proxy firmware
refuses to restart within 20 minutes of booting (it logs `refused: up only N s`
and carries on), so that rung can legitimately do nothing at all.

`power_cycle` shows an `EntitySelector(domain="switch")` prefilled from
`entry.options["recovery_outlet"]`, stores the choice back there on submit, then
`switch.turn_off` -> 10 s -> `switch.turn_on`. There is no smart plug wired to
this BedJet today, so the step has to work with whatever outlet the operator
picks, and it is described in `strings.json` as cutting mains power and as the
last resort. Flow aborts with a translated reason when the config entry is gone
(`entry_not_found`) or not loaded (`not_loaded`); the entry is re-resolved on
every use, since the first rung of the ladder replaces `runtime_data` wholesale.

### strings.json / translations/en.json

New `issues.device_unreachable` block: title, `fix_flow.step.menu`
(description + `menu_options` labels), `fix_flow.step.power_cycle`
(description + `data`/`data_description`), and both `fix_flow.abort` reasons.
The prose says what each rung does, that reload is the cheap one, that a proxy
restart may be refused if the proxy booted recently, and that the power cycle
cuts mains power. Both files stay byte-identical, as before.
