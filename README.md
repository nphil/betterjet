<p align="center"><img src="brand/logo.png" alt="BetterJet" width="520"></p>

BetterJet is a local-push Home Assistant integration for the BedJet V3 over Bluetooth LE - instant control, no polling, no extra hardware.

This integration supports one BedJet V3 climate device through any Bluetooth adapter or ESPHome Bluetooth proxy Home Assistant already sees.

## Why BetterJet

BetterJet is a fork of the community BedJet integration, rebuilt around a held connection, deferred standby setpoints and a connection diagnostic so that HomeKit and dashboards respond instantly. The name makes the fork identity clear; the Home Assistant domain remains `bedjet` so existing entities and history are unchanged.

## The single-connection rule

A BedJet only accepts **one** BLE connection at a time, and it **stops advertising while something is connected to it**. That has two consequences:

- If the BedJet mobile app is connected, Home Assistant cannot connect until the app disconnects.
- If Home Assistant is holding the connection, the app cannot connect until Home Assistant lets go.

There is no way to share the connection, and no way to detect "the app wants in" from the Home Assistant side - the only signal available is "the device started advertising again", which means *something* released it.

### Handing the phone app the connection

BetterJet holds one GATT connection permanently while the device is enabled; there is no hold switch. If the link drops, it reconnects through `bleak_retry_connector` with backoff 1/2/5/10/30/60 s. The diagnostic **Connection** sensor (`sensor.<device>_connection`) reports the scanner or proxy currently carrying the link, or `disconnected`, with attributes `hold`, `drops_1h`, `last_drop` and `reconnect_attempt`. To use the BedJet mobile app, disable the device (or its integration entry) in Home Assistant first, then re-enable it afterwards.

### Bluetooth proxy slot economics

If your BedJet is only reachable through an ESPHome Bluetooth proxy, remember that a proxy also multiplexes a limited number of GATT connection slots across every device it serves. A held BedJet connection consumes one of those slots for as long as the integration is loaded, same as it would with a local adapter. This integration only ever holds zero or one connection to the BedJet itself; it does not open extra connections for probing or diagnostics.

## How it works

- The integration works with any Bluetooth adapter or ESPHome Bluetooth proxy Home Assistant already sees the device through - no dedicated ESPHome device or extra hardware is required.
- No polling: the BedJet notify characteristic streams a status frame at roughly 4 Hz whenever a connection is held - even while the unit is in standby - so every entity updates the moment a real frame arrives instead of on a timer.
- Commands (mode, temperature, fan speed, runtime, LED, mute) are confirmed against the next status frame that reflects them, with a 5 second timeout. There is no optimistic/local echo: if the timeout expires, the service call fails with a clear error instead of silently pretending the change happened.
- The integration listens passively to Home Assistant's Bluetooth scanner (including proxies) for advertisements from this device's address. An advertisement means the device is currently free (nothing is connected to it), which is what lets the library reconnect automatically after the app - or anything else - lets go.
- Setting a temperature while the unit is off is **deferred, not sent**: a BedJet in standby silently ignores a setpoint write, so the command used to sit for the full 5 second confirmation window and then fail (which is exactly what the Apple Home app does every time it writes a target temperature). The requested value is shown right away and written the moment you turn the unit on from this entity; if something else starts the unit first, the device's own target wins and the deferred value is dropped.
- The `connection` sensor names the Bluetooth adapter or proxy currently carrying the held GATT link (or `disconnected`), pushed from Home Assistant's live connection-slot allocations. Its attributes add `hold`, `drops_1h`, `last_drop` and `reconnect_attempt`, so a "restart the proxy" automation can tell which proxy to restart - and can leave alone a proxy other devices are using.

## When the link does not come back

Reconnecting is automatic and usually invisible. When it is not - the link has been down for **15 minutes** with nothing streaming - the integration raises a repair under **Settings → System → Repairs**, and the repair's **Fix** button opens an escalating wizard:

1. **Check the link again.** Costs nothing, and a link that came back on its own only needs someone to look.
2. **Reload the integration.** Rebuilds the client and reconnects. Nothing outside this BedJet is affected.
3. **Restart the Bluetooth proxy that last held the link.** Only offered when the proxy is known *and* it exposes a `restart_proxy` **ESPHome API action** (`api:` → `actions:` → `- action: restart_proxy` in the proxy's YAML, which is how these proxies expose it - a Restart *button* entity is not the same thing and is not what this looks for). The action is found as `esphome.<node name>_restart_proxy`, derived from the proxy's own ESPHome node name rather than whatever it has been renamed to in Home Assistant. Because the BedJet's single connection slot looks occupied to everything else, a proxy still holding a dead link is a failure no reload can clear - only the proxy can. The proxy firmware refuses to restart within 20 minutes of booting, so the wizard reports this as *asked to restart*, never as *rebooted*.
4. **Cut and restore mains power.** Last resort: pick the switch that feeds the BedJet, and the wizard turns it off, waits ten seconds, and turns it back on. The chosen switch is remembered for next time.

Each step re-checks the link for up to 45 seconds (60 after a power cycle) before reporting back. The repair clears itself the moment the link is healthy again, whether the wizard, an automation, or the device itself fixed it - it is reconciled against the live link on every connect and disconnect and once more whenever the integration is set up, so it can never outlive the outage it describes.

"Healthy" here means connected *and* streaming: since the BedJet emits a status frame about four times a second for as long as a real link is held, a connection producing no frames is a wedged link, not a working one. Entities that still show their last known values are not evidence of a live link.

## Installation

### HACS (recommended)

1. Add `https://github.com/nphil/betterjet` to HACS as a custom repository (category: Integration), or install it directly if it's in the default HACS list.
2. Search for **BetterJet** and download it.
3. Restart Home Assistant.

### Manual

1. Copy the `custom_components/bedjet` folder from this repository into your Home Assistant `custom_components` directory. The folder and domain are still intentionally `bedjet`, so existing installs upgrade in place with no entity changes.
2. Restart Home Assistant.

## Setup

Home Assistant will normally discover a BedJet automatically via Bluetooth and prompt you to add it. Otherwise, go to **Settings → Devices & services → Add integration**, search for **BetterJet**, and pick your device from the list of nearby BedJets.

## Entities

All unique IDs are of the form `<mac-address>_<key>` (the climate entity's unique ID is the bare MAC address, unchanged since the original integration, to preserve history).

| Platform | Key | Category | Enabled by default | Notes |
|---|---|---|---|---|
| Climate | *bare address* | — | ✅ | Modes off/heat/cool/dry; presets none/Turbo/Extended Heat/M1/M2/M3 (uses the memory slot's custom name if the device reports one, otherwise "M1"/"M2"/"M3"); fixed 19-43 °C (66-104 °F) limits |
| Fan | `fan` | — | ✅ | 5-100 % in 5 % steps; turning on sets Cool mode, turning off sets Off |
| Number | `runtime_remaining` | — | ✅ | Minutes remaining in the current run |
| Sensor | `ambient_temperature` | — | ✅ | °C |
| Sensor | `outlet_temperature` | — | ✅ | °C, the air temperature at the unit's outlet |
| Sensor | `notification` | — | ✅ | Enum: filter/firmware/biorhythm notifications the unit is reporting |
| Button | `acknowledge_notification` | — | ✅ | Clears the current notification on the device |
| Switch | `enable_led` | Config | ❌ | Unit's status LED |
| Switch | `mute_beeps` | Config | ❌ | Unit's beeper |
| Button | `sync_clock` | Config | ❌ | Pushes Home Assistant's current time to the unit's clock |
| Button | `firmware_update` | Config | ❌ | **Reboots the unit** to check for and apply a firmware update |
| Binary sensor | `connection_test` | Diagnostic | ❌ | Unit reports its internal connection test passed |
| Binary sensor | `dual_zone` | Diagnostic | ❌ | |
| Binary sensor | `units_setup` | Diagnostic | ❌ | |
| Sensor | `bio_sequence_step` | Diagnostic | ❌ | Current step while running a biorhythm program |
| Sensor | `shutdown_reason` | Diagnostic | ❌ | Raw shutdown reason code from the last frame |
| Sensor | `turbo_time` | Diagnostic | ❌ | Seconds of Turbo boost remaining |
| Sensor | `update_phase` | Diagnostic | ❌ | Raw firmware update phase code |
| Sensor | `scanner` | Diagnostic | ❌ | Which Bluetooth adapter/proxy currently sees this device |
| Sensor | `connection` | Diagnostic | ✅ | Name of the adapter/proxy holding the GATT link, or `disconnected`; attributes `hold`, `drops_1h`, `last_drop`, `reconnect_attempt` |

Every entity except `connection` goes unavailable when the device is disconnected (`connection` stays available - reporting `disconnected` is its job). The BedJet accepts one BLE connection at a time and stops advertising while connected, so to use the BedJet mobile app, disable the device (or the integration entry) in Home Assistant first; re-enable it afterwards and the connection is re-established from the next advertisement.

## Diagnostics

Download diagnostics from the device page (⋮ → Download diagnostics) to get the current connection state (connected/available/holding, which scanner source last saw it, time since the last frame) plus a full decode of the most recent status frame - handy when reporting a problem.

## Debug logging

To see connection attempts, frame decodes, and command confirmations, add to `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.bedjet: debug
    bleak_retry_connector: debug
    habluetooth: debug
```

## Credits

This integration descends from [robert-friedland/homeassistant-bedjet](https://github.com/robert-friedland/homeassistant-bedjet), continued by [asheliahut](https://github.com/asheliahut), then rewritten and maintained by [natekspencer](https://github.com/natekspencer), and now maintained here by [nphil](https://github.com/nphil). Thank you to everyone in that chain for the reverse-engineering work that made this possible.

## Branding

Brand assets in this repository are:

- `brand/icon.png` (256 px)
- `brand/icon@2x.png` (512 px)
- `brand/icon-1024.png`
- `brand/logo.png`
- `brand/logo@2x.png`
- `brand/logo-light.png`
- `brand/social-preview.png`

The icon is an original design inspired by the BedJet mark (a bed silhouette with airflow), not the manufacturer's logo. BedJet is a trademark of BedJet LLC; BetterJet is unaffiliated with BedJet LLC.
