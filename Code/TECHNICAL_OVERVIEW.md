# Technical Overview — Sonicator Force-Control System

Engineering handoff document for the sonicator force-control rig. It describes
the system architecture, the two pieces of software, the serial protocol that
joins them, the supervisor state machine, the safety model, and known gaps for
the next team.

---

## 1. System architecture

The rig is a two-controller system. A **Raspberry Pi** acts as the supervisor and
a **Teensy** microcontroller runs the low-level motor control loop. Force is
measured by a **Mark-10** digital force gauge.

```mermaid
flowchart LR
    Gauge["<b>Mark-10 gauge</b><br/>/dev/ttyUSB0"]
    Pi["<b>Raspberry Pi</b><br/>launch.py<br/>(supervisor)"]
    Teensy["<b>Teensy</b><br/>032026_motor_<br/>proportional_control.ino"]
    Driver["<b>DM332T driver</b>"]
    Ram["<b>Lead-screw ram</b><br/>(stepper)"]

    Gauge -- "force readings<br/>(USB serial, ?\r query)" --> Pi
    Pi -- "force / target / manual cmds<br/>(USB serial, 115200 baud)" --> Teensy
    Teensy -- "STEP / DIR pulses" --> Driver
    Driver --> Ram
```

> If your viewer doesn't render Mermaid, the flow is simply:
> **Mark-10 gauge → Raspberry Pi (`launch.py`) → Teensy → DM332T driver →
> lead-screw ram.** The Pi reads force from the gauge and sends commands to the
> Teensy; the Teensy drives the stepper.

**Division of responsibility (important design decision):**

- The **Teensy** owns the closed-loop control. It runs a proportional controller
  that drives the stepper toward a `targetForce` based on the force value it is
  fed. This loop was tuned and validated on the bench, and the design
  deliberately keeps it untouched.
- The **Pi** is *only the supervisor*. It reads the force gauge, decides which
  phase of a trial we are in, streams force readings to the Teensy, sets the
  target, handles the operator's keyboard input, enforces software safety
  limits, and logs everything. It does **not** do closed-loop control itself.

This separation is why the supervisor streams *raw measured force* to the Teensy
rather than a control output: the proportional math lives on the Teensy.

---

## 2. Source files

| File | Role |
|---|---|
| [Python/launch.py](Python/launch.py) | Raspberry Pi supervisor. The program operators run. Contains the CLI, state machine, serial I/O, safety checks, keyboard handling, and CSV logging. |
| [Teensy/032026_motor_proportional_control.ino](Teensy/032026_motor_proportional_control.ino) | Teensy firmware. Proportional setpoint control of the step/dir stepper, plus a manual jog mode. |

---

## 3. Serial protocol (Pi → Teensy)

A single bidirectional protocol on the Teensy's USB-CDC port at **115200 baud**.
Defined in the firmware header and mirrored in the `launch.py` docstring.

| Message | Direction | Effect on Teensy |
|---|---|---|
| `<float>\n` | Pi → Teensy | A measured force value. Runs proportional control toward `targetForce`. Sets `manualMode = false`. Refreshes the watchdog timer. |
| `T<float>\n` | Pi → Teensy | Sets `targetForce`. Does **not** change mode and does **not** touch the watchdog. |
| `u` | Pi → Teensy | Manual mode: continuous **up** steps until the next command. |
| `d` | Pi → Teensy | Manual mode: continuous **down** steps until the next command. |
| `s` | Pi → Teensy | Manual mode: **stop**. |

The Mark-10 side uses a simple query: the supervisor writes `?\r` and reads back
a line, then parses the leading numeric token (see `parse_force`). Units are
assumed to be Newtons — **no unit conversion is performed**; the gauge must be
configured to output Newtons.

### Firmware control loop summary

- **Manual mode** (`u`/`d`/`s`): bit-bangs STEP/DIR pins directly. `u`/`d` pulse
  continuously (~800 µs per step ≈ 1240 steps/s); `s` idles.
- **Automatic mode** (after a `<float>\n`): computes `forceError = targetForce -
  currentForce`, derives a step interval inversely proportional to the error
  (`Kp_step / |error|`, clamped to `[minStepInterval, maxStepInterval]` =
  8–75 ms), and steps toward the target while the error exceeds a `deadband` of
  0.1 N. Direction is set by the sign of the error.
- **Watchdog:** automatic motion only runs if a force value arrived within the
  last **100 ms** (`millis() - lastForceTime < 100`). If the Pi stops streaming,
  the motor halts automatically. This is the primary hardware-side safety.
- **Key tuning constants:** `Kp_step = 20.0`, `minStepInterval = 8`,
  `maxStepInterval = 75`, `deadband = 0.1`. Pins: `DIR_Pin = 36`,
  `STEP_Pin = 34`. The DM332T microstep/current DIP switches are noted at the top
  of the `.ino` (`v1 -> 101111; v2 -> 101011`).

> **Subtle firmware guard:** a bare `\n` with an empty buffer is ignored. This was
> added because a stray newline used to silently re-enter automatic mode at
> 0.0 N target. Preserve this guard if you refactor the parser.

---

## 4. Supervisor structure (`launch.py`)

The supervisor is a single-threaded, fixed-rate loop. Major components:

| Component | Responsibility |
|---|---|
| `parse_args` | CLI definition. Only `--runtime` and `--target-force` are required; everything else has a documented default (ports, baud, loop rate, thresholds, safety caps, log path). |
| `parse_force` | Tolerant parsing of the Mark-10 line (bare number or unit-tagged). Returns `None` on failure. |
| `open_teensy` / `open_mark10` | Serial open helpers. `open_teensy` sleeps 2 s for the USB-CDC reset-on-connect, drains boot bytes, and sends `s`. |
| `keyboard_cbreak` / `try_read_key` | Put stdin in cbreak mode for single-key, non-blocking input; restore tty on exit. |
| `PausableClock` | Monotonic clock that can pause/resume, used for the timed hold and the release window so a pause doesn't eat into the run time. |
| `CsvLogger` | Buffered CSV writer; flushes every `CSV_FLUSH_EVERY` (30) rows ≈ once per second at 30 Hz. |
| `Controller` | Owns the state machine, serial sends, force polling, safety checks, keyboard handling, and the main loop. |

### Main loop (`Controller.run`, ~30 Hz)

Each tick, in strict order:

1. **Drain keyboard input** (operator commands have priority over automation).
2. **Poll force** from the Mark-10, then run **safety checks** (parse-fail
   streak, over-force cap, overshoot guard).
3. **Advance the state machine** (`advance_state`) — may emit one-shot serial
   messages via `_enter_state`.
4. **Emit this tick's streaming message** — jog logic, or stream the measured
   force if in an automatic state (this is what keeps the Teensy's watchdog
   alive and drives its control loop).
5. **Log a CSV row** and update the in-place status line.
6. **Sleep** until the next deadline (drift-resistant; resyncs if it falls
   behind).

---

## 5. State machine

```
STARTUP_STOP ──(first valid force)──► SEEKING_CONTACT
SEEKING_CONTACT ──(force ≥ contact_threshold)──► RAMPING_TO_TARGET
RAMPING_TO_TARGET ──(force ≥ target − target_reached_tol)──► HOLDING_FORCE
HOLDING_FORCE ──(one tick, latch)──► TIMED_RUN   [starts run_clock]
TIMED_RUN ──(run_clock ≥ runtime)──► RELEASE     [manual 'u', starts release_clock]
RELEASE ──(release_clock ≥ release_duration)──► MANUAL_READY
(any state) ──(safety violation / serial error)──► FAULT
```

| State | Behavior |
|---|---|
| `STARTUP_STOP` | Waiting for the first valid force reading. Advances even though `paused` defaults false. |
| `SEEKING_CONTACT` | Sends the **full** `--target-force` as the Teensy target so the proportional path runs at its fastest. `--contact-threshold` is used **by the Pi only** to decide when to advance — it is not a Teensy setting. |
| `RAMPING_TO_TARGET` | Target unchanged; re-sent for robustness. Advances when force is within `--target-reached-tol` of target. |
| `HOLDING_FORCE` | One-tick latch state; immediately advances to `TIMED_RUN`. |
| `TIMED_RUN` | The hold. `run_clock` counts up to `--runtime`. The Teensy keeps doing proportional control to maintain force. |
| `RELEASE` | Uses **manual `u` continuous** (~1240 steps/s) for a hard-timed `--release-duration`, *not* the proportional path. See the design note below. |
| `MANUAL_READY` | Run complete. Motor stopped; operator may jog with `u`/`d`/`s`. |
| `FAULT` | Motor stopped; loop continues to log/print but takes no automatic action. Terminal. |

**`AUTOMATIC_STATES`** = {`SEEKING_CONTACT`, `RAMPING_TO_TARGET`, `HOLDING_FORCE`,
`TIMED_RUN`}. Only in these states does the supervisor stream measured force to
the Teensy.

> **Why RELEASE is not in `AUTOMATIC_STATES`:** release uses manual `u` mode. If
> the supervisor also streamed force values during release, each `<float>\n`
> would flip the Teensy back into automatic mode mid-release. Release is kept on
> manual continuous because (a) it's much faster than the proportional path, so a
> short window actually relieves a compressed sample, and (b) it works even on
> stock firmware that ignores `T` commands.

---

## 6. Pause / resume

`s` during an automatic state sets `paused = True`, stops the motor, and pauses
both clocks. While paused the supervisor sends nothing to the Teensy (the
watchdog halts the motor within 100 ms regardless). `g` clears the pause,
**re-asserts the correct target** for the resumed state (`_state_target`), and
resumes the clocks. The pausable clocks ensure paused time is not counted against
`--runtime` or `--release-duration`.

`u`/`d` during a run trigger a time-limited jog: the supervisor sends the manual
command and sets `jog_until = now + jog_duration_ms`. When the jog window
expires the supervisor sends one `s`. **Jog time-limiting is enforced entirely on
the Pi** — there is no homing or limit switch on the ram, so this is the only
thing bounding manual travel.

---

## 7. Safety model

Defense in depth across both controllers:

1. **Teensy watchdog (hardware side):** automatic motion stops within 100 ms if
   force values stop arriving. Covers a crashed/killed supervisor or unplugged
   USB.
2. **Over-force cap (`--max-force`, default 50 N):** the supervisor faults
   immediately if the measured force exceeds the cap. Overridable only with
   `--force-override` (logged as a warning; intended for engineering use).
3. **Overshoot guard (`--max-overshoot`, default 5 N):** while holding, faults if
   force exceeds `target + max_overshoot`. This specifically catches a Teensy
   flashed with **old firmware that ignores `T` commands** and drives toward a
   compiled-in target.
4. **Parse-failure streak (`--max-parse-fails`, default 20):** faults after N
   consecutive unparseable gauge reads (gauge unplugged / wrong port / bad
   config).
5. **Pre-flight validation:** rejects non-positive runtime, negative target,
   target over the cap (without override), non-positive loop rate, and a
   non-TTY stdin (cbreak needs a real terminal).
6. **Guaranteed motor-off:** the `finally` block always writes `s` to the Teensy
   and closes ports, no matter how the program exits. `FAULT` and `MANUAL_READY`
   both stop the motor on entry. SIGINT (Ctrl+C) sets `running = False` for a
   clean shutdown rather than a hard kill.

---

## 8. Configuration reference (CLI defaults)

| Flag | Default | Notes |
|---|---|---|
| `--runtime` | *(required)* | Hold seconds after target reached. |
| `--target-force` | *(required)* | Newtons; rejected if > `--max-force`. |
| `--teensy-port` | `/dev/ttyACM0` | |
| `--teensy-baud` | `115200` | |
| `--mark10-port` | `/dev/ttyUSB0` | |
| `--mark10-baud` | `115200` | |
| `--loop-hz` | `30.0` | Supervisor loop rate. |
| `--contact-threshold` | `1.0 N` | Pi-side contact detection. |
| `--target-reached-tol` | `1.0 N` | Advance-past-ramp tolerance. |
| `--release-duration` | `0.5 s` | Manual-up release window. |
| `--jog-duration-ms` | `50 ms` | Manual jog auto-stop. |
| `--max-force` | `50.0 N` | Hard safety cap. |
| `--force-override` | off | Allow target > cap. **Dangerous.** |
| `--max-overshoot` | `5.0 N` | Overshoot fault threshold. |
| `--max-parse-fails` | `20` | Consecutive parse fails before fault. |
| `--log-path` | auto | Defaults to `<script dir>/logs/sonicator_<ts>.csv`. |

---

## 9. Data logging

CSV columns: `iso_timestamp, elapsed_s, force_N, target_force_N, last_msg, state,
paused`. One row per loop tick (~30/s). Flushed every ~1 s, on fault, and on
exit. Default location is a `logs/` folder beside `launch.py` — on the deployed
Pi that is `~/Desktop/logs/` since the script lives on the Desktop. `target_N`
and `last_msg` are logged even though they're omitted from the terminal status
line.

---

## 10. Known gaps / handoff notes

- **No homing or limit switches** on the lead-screw ram. All travel bounding is
  software/operator-side. Adding limit switches + a homing routine is the single
  biggest robustness improvement available.
- **Hard-coded units.** The Mark-10 must be configured to output Newtons; there
  is no unit detection or conversion. A mis-configured gauge (lbF) would be
  silently misinterpreted. Consider validating units from the gauge response.
- **Two firmware versions in the field.** The overshoot guard exists because an
  older Teensy build ignores `T` commands. Standardize on the current firmware
  and remove the ambiguity. The DM332T DIP-switch settings (`v1`/`v2`) at the top
  of the `.ino` must match the deployed hardware.
- **Proportional-only control.** No integral term, so a steady-state offset
  within the deadband (0.1 N) is expected and by design. If tighter steady-state
  accuracy is needed, this is where to add it — but re-validate on the bench.
- **Single-threaded, blocking serial.** The loop relies on short serial timeouts
  (Mark-10 0.2 s, Teensy 0.1 s). A slow/hung gauge read can stretch a tick; the
  loop is drift-resistant but not hard real-time.
