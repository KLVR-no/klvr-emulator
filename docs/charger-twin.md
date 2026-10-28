# Charger twin

One browser window that is the front of a Charger Pro. Klvr Control connects to it as it would to a charger on the network. The window shows the firmware's display, the 48 bay lights, and the knob. It follows `docs/design-principles.md` and `docs/user-facing-contract.md` in the firmware repo. Those pages decide every look and word. This page does not change them.

Parent: KLVR-no/klvr-emulator#19. Firmware core: KLVR-no/klvr-chargerpro-firmware#366. Branches: `preview/charger-twin` in both repos. Nothing here lands on firmware `release/v1.9.1`, firmware `main`, Control `release/v1.4.0`, Control `main`, or emulator `main` until 1.9.1 and Control 1.4.0 have shipped.

## Who it is for

Someone at a desk who needs the charger in front of them: to see the glass, turn the knob, seat and pull cells, and let Control talk to the same unit. Integrators use the same HTTP charger. The engineer view is one step away.

## The rule

The emulator decides nothing. Every state, count, time, overlay, LED pattern and pixel comes from firmware code built from one pinned commit. The emulator supplies what the hardware supplies and shows what comes back.

One tick produces one snapshot. Control's status, the glass and the bay lights are three views of that snapshot. They cannot disagree inside a tick.

## The window

The page is the charger, centred, with generous space around it. Black, white and grey. One grotesk. Interface motion under 200 ms. Colour on a bay is only the firmware's light. Interactive blue is only for a control, never for a cell.

### Glass

The onboard display is the firmware framebuffer, 480×128, scaled so the pixels stay sharp. Nothing is drawn on top of it. A pixel matches firmware CI (`tests/display/states`) when no channel is off by more than one RGB565 step. Zero pixels may fail that test.

### Knob

A round encoder sits at the right of the glass. Drag around it, or scroll while the pointer is on it, and it turns with detents. A click is a press. Holding is a long press. After 1 s on the main screen the firmware opens network settings. The page does not decide that. Arrow keys turn. The space bar presses.

What the glass does with a turn is the firmware: targeting, the dotted ring, "N in 1h 05m", the 5 s timeout, the room card hiding while the knob has the time.

### Bays

Forty-eight wells, in the same face Control already draws: two rows of 24, each row split 12 and 12. Bay order is the firmware's index.

Each well shows a cell only when one is seated. The light on the well is the firmware's LED: colour and brightness, including the charging breath (gamma table, 18 ms steps, about 3.5 s) and Identify (8 ms steps, about 1.5 s). The page does not animate a lookalike.

### Seat and pull

One type switch for the whole rack, AA or AAA. Click an empty well and that cell is seated. Click a cell and it is pulled. The cell appears or disappears immediately. The light is whatever the firmware does on the next tick.

### Fill

Two actions beside the type switch. Empty clears the rack. Fill deals a different setup each time: how many cells, AA against AAA, and how many are already full. The same press does not repeat the same rack.

### One step away

A single step opens the engineer view: the pinned firmware commit, the klvr-contract version, the live verdict, the clock (real time, and a faster setting), and per-bay temperature and state of charge as readings you inject. Hot rooms, OTA and Identify are started from here. They are firmware inputs, not a second screen the page invents.

The dashboard that ships on emulator `main` today (a status chip, an OTA card, and AA / AAA / Eject on every slot) is not this window. It is replaced here.

## What the firmware provides

Built from the pinned commit, as a host library:

| Piece | Where it already lives |
|---|---|
| Thermal rules, totals, ETA, overlay, room temperature | `include/app/thermal_engine.h` |
| Bay state to API slot, percent, LED choice, reported type | `include/app/charger_state_logic.h` |
| Totals and ETA on the wire | `include/app/charger_api_totals.h` |
| Identify lights | `include/app/identify_led.h` |
| Long press | `include/app/ui_long_press.h` |
| Glass | `eez-gui/klvr-ui/ui_stubs.c` on LVGL, the same path as `tools/ui-simulator` |
| Charging breath | `drivers/klvr_battery_leds/klvr_battery_leds.c` |
| Bay transitions | `drivers/klvr_battery_charger/klvr_battery_charger.c` |

The library takes hardware facts and a time, and returns the snapshot: thermal verdict, bay states, LED colour and brightness, the 480×128 frame, and the API fields. The harness stays in the firmware repo so a display change still fails CI before it ships.

Two copies of the rack scan exist on `1a97b5b` and already disagree. The driver uses `charger_reported_type()` and the temperature tenth. `tools/ui-simulator/render.c` uses the raw bay type and drops the tenth. #366 makes that one function. The driver keeps the throttle log and the spinlock publish. The temperature gate stays at the read.

The state machine is still inside the Zephyr driver. It becomes one function the driver and the host both call. The breath step becomes one function of time. If a transition or that step can only be kept by rewriting it, that piece moves to Zephyr `native_sim`. A port is a failed cut.

## What the emulator provides

Cell presence, type, voltage and current readings, temperatures and their tenths, encoder edges, rear-board messages (network, name, OTA, Identify), and a clock that can run faster.

It does not compute totals, ETA, overlay, slot state, LED pattern, or pixels. `encode_verdict_totals()` and the linear SoC-to-seconds model in `klvr_emulator/main.py` (from #18, and the 7200 s / 95% slope) are deleted when the status handler is filled from the snapshot.

Bumping the pin is the whole firmware update. CI fails if this repo contains display, state or thermal logic of its own, and if an API field, a word, or a reference frame drifts.

## Order

1. One shared thermal tick. Firmware #366. The host test fails until the function exists. Display goldens stay exact.
2. Bay transitions called from that same core, fed by readings rather than a pre-baked state.
3. The breath step, called with the firmware clock.
4. One snapshot: frame, lights, API. The emulator serves HTTP and the WebSocket from it.
5. This window. Empty, Fill, seat, pull, knob.
6. The pin, and a CI check that this repo did not grow a second decider.

## Left in place

- `tools/ui-simulator` is the glass. It stays.
- `klvr_battery_simulator.c` is the charger's optional playback of recorded cell readings into the ADC. Production leaves it off (`CONFIG_KLVR_BATTERY_CHARGER_BATTERY_SIMULATOR=n`). It is not a second product.
- Control's `emulator` submodule is a pin of this repo, not another program. It stays on `release/v1.4.0` until that release has shipped. This topic does not move it.

## Removed

`app/boards/native_sim_native_64.conf`, `native_sim_native_64.overlay` and `native_posix.overlay` are an SDL display spike. Nothing builds them. The conf asks for 32-bit colour and Montserrat, which is not the charger's glass. They go on `preview/charger-twin`. The virtual charger in the roadmap is this host library.
