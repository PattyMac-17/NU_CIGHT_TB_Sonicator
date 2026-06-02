#!/usr/bin/env python3
"""
Sonicator force-control supervisor for Raspberry Pi.

Reads force from a Mark-10 gauge over USB serial, supervises a Teensy that runs
proportional setpoint control on a step/dir lead-screw ram, and runs an explicit
state machine through contact-seeking, ramp-to-target, hold-for-runtime, release,
and manual-ready. The Teensy keeps doing the proportional control loop the user
has already validated; Python is the supervisor.

Example launch (over SSH on the Pi):
    python3 Code/Python/continuous_polling_test.py --runtime 60 --target-force 30
    python3 Code/Python/continuous_polling_test.py --runtime 300 --target-force 25

Required arguments: --runtime (hold time in seconds after target is reached) and
--target-force (Newtons). All other parameters have sensible defaults; see --help.

Serial protocol to the Teensy (see
Code/Teensy/032026_motor_proportional_control.ino):
    "<float>\\n"   -> measured force; Teensy proportional control runs toward
                      targetForce. Sets manualMode = false.
    "T<float>\\n"  -> set Teensy targetForce. Does not change mode.
    "u" / "d"      -> manual mode, continuous up/down until next command.
    "s"            -> manual mode, stop.

Live keyboard commands (single key, no Enter required):
    u  jog up   (auto-stops after --jog-duration-ms; press again to continue)
    d  jog down (same)
    s  stop motor; during an automated state this PAUSES the run
    g  resume a paused run
    h  show this help
    Ctrl+C  quit safely (stops motor, flushes CSV, restores terminal)
"""

import argparse
import contextlib
import csv
import datetime as dt
import enum
import pathlib
import select
import signal
import sys
import termios
import time
import tty

import serial


# -----------------------------------------------------------------------------
# Defaults (advanced; the normal launch only needs --runtime and --target-force)
# -----------------------------------------------------------------------------
DEFAULT_TEENSY_PORT = "/dev/ttyACM0"
DEFAULT_TEENSY_BAUD = 115200
DEFAULT_MARK10_PORT = "/dev/ttyUSB0"
DEFAULT_MARK10_BAUD = 115200
DEFAULT_LOOP_HZ = 30.0
DEFAULT_CONTACT_THRESHOLD = 1.0  # N: contact detected when force exceeds this
DEFAULT_TARGET_REACHED = 1.0  # N: tolerance for advancing past ramping_to_target
DEFAULT_RELEASE_DURATION = 0.5  # s: hard time cap on manual upward release
DEFAULT_JOG_DURATION_MS = 50  # ms: manual u/d auto-stop window
DEFAULT_MAX_FORCE = 50.0  # N: hard safety cap
DEFAULT_MAX_OVERSHOOT = 5.0  # N: fault if force exceeds target + this while holding/timed_run
DEFAULT_MAX_PARSE_FAILS = 20  # consecutive force-gauge parse failures -> fault
CSV_FLUSH_EVERY = 30  # flush CSV every N rows (~1 s at 30 Hz loop)


# -----------------------------------------------------------------------------
# State machine
# -----------------------------------------------------------------------------
class State(enum.Enum):
    STARTUP_STOP = "startup_stop"
    SEEKING_CONTACT = "seeking_contact"
    RAMPING_TO_TARGET = "ramping_to_target"
    HOLDING_FORCE = "holding_force"
    TIMED_RUN = "timed_run"
    RELEASE = "release"
    MANUAL_READY = "manual_ready"
    FAULT = "fault"


AUTOMATIC_STATES = frozenset(
    {
        State.SEEKING_CONTACT,
        State.RAMPING_TO_TARGET,
        State.HOLDING_FORCE,
        State.TIMED_RUN,
        # RELEASE intentionally not here: it uses manual 'u' continuous mode,
        # so Python must not stream force values that would put the Teensy
        # back into automatic mode mid-release.
    }
)


# -----------------------------------------------------------------------------
# Force parsing
# -----------------------------------------------------------------------------
def parse_force(line: str):
    """Parse the leading numeric token from a Mark-10 response.

    Handles bare-number ("12.5") and unit-tagged ("12.5 N", "-3.2 lbF") formats.
    Returns None if no leading number is parseable. Assumes the Mark-10 is
    configured to output in Newtons; no unit conversion is performed.
    """
    line = line.strip()
    if not line:
        return None
    tok = line.split()[0]
    try:
        return float(tok)
    except ValueError:
        return None


# -----------------------------------------------------------------------------
# Serial open helpers
# -----------------------------------------------------------------------------
def open_teensy(port: str, baud: int) -> serial.Serial:
    """Open the Teensy port and put the motor in a known stopped state.

    Opening a USB-CDC port resets the Teensy on most boards. Sleep long enough
    for the firmware to boot, drain any boot bytes, then send 's'.
    """
    ser = serial.Serial(port, baud, timeout=0.1)
    time.sleep(2.0)
    ser.reset_input_buffer()
    ser.write(b"s")
    return ser


def open_mark10(port: str, baud: int) -> serial.Serial:
    return serial.Serial(port, baud, timeout=0.2)


# -----------------------------------------------------------------------------
# Non-blocking keyboard
# -----------------------------------------------------------------------------
@contextlib.contextmanager
def keyboard_cbreak():
    """Switch stdin to cbreak mode so single keystrokes are available immediately.

    Restores the original tty settings on exit, even if the program crashes.
    """
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def try_read_key():
    """Return one character if available on stdin, else None. Non-blocking."""
    if select.select([sys.stdin], [], [], 0)[0]:
        return sys.stdin.read(1)
    return None


# -----------------------------------------------------------------------------
# Clock that survives pause (used for timed_run and release)
# -----------------------------------------------------------------------------
class PausableClock:
    def __init__(self):
        self._started_at = None
        self._accumulated = 0.0

    def start(self):
        self._started_at = time.monotonic()
        self._accumulated = 0.0

    def pause(self):
        if self._started_at is not None:
            self._accumulated += time.monotonic() - self._started_at
            self._started_at = None

    def resume(self):
        if self._started_at is None:
            self._started_at = time.monotonic()

    def elapsed(self) -> float:
        if self._started_at is None:
            return self._accumulated
        return self._accumulated + (time.monotonic() - self._started_at)


# -----------------------------------------------------------------------------
# CSV logger
# -----------------------------------------------------------------------------
class CsvLogger:
    HEADER = [
        "iso_timestamp",
        "elapsed_s",
        "force_N",
        "target_force_N",
        "last_msg",
        "state",
        "paused",
    ]

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w", newline="")
        self._w = csv.writer(self._fh)
        self._w.writerow(self.HEADER)
        self._rows_since_flush = 0

    def write_row(
        self,
        t0: float,
        now: float,
        force,
        target: float,
        last_msg: str,
        state: State,
        paused: bool,
    ):
        ts = dt.datetime.now().isoformat(timespec="milliseconds")
        self._w.writerow(
            [
                ts,
                f"{now - t0:.3f}",
                "" if force is None else f"{force:.3f}",
                f"{target:.3f}",
                last_msg or "",
                state.value,
                "1" if paused else "0",
            ]
        )
        self._rows_since_flush += 1
        if self._rows_since_flush >= CSV_FLUSH_EVERY:
            self._fh.flush()
            self._rows_since_flush = 0

    def close(self):
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Sonicator force-control supervisor (Raspberry Pi side).",
    )
    p.add_argument(
        "--runtime",
        type=float,
        required=True,
        help="Hold time in seconds AFTER target force is reached.",
    )
    p.add_argument(
        "--target-force",
        type=float,
        required=True,
        help="Target force in Newtons (rejected if > --max-force).",
    )
    # Ports + baud
    p.add_argument("--teensy-port", default=DEFAULT_TEENSY_PORT)
    p.add_argument("--teensy-baud", type=int, default=DEFAULT_TEENSY_BAUD)
    p.add_argument("--mark10-port", default=DEFAULT_MARK10_PORT)
    p.add_argument("--mark10-baud", type=int, default=DEFAULT_MARK10_BAUD)
    # Control / state tuning
    p.add_argument(
        "--loop-hz",
        type=float,
        default=DEFAULT_LOOP_HZ,
        help="Control-loop rate in Hz.",
    )
    p.add_argument(
        "--contact-threshold",
        type=float,
        default=DEFAULT_CONTACT_THRESHOLD,
        help="Force (N) at which contact is detected during seeking_contact.",
    )
    p.add_argument(
        "--target-reached-tol",
        type=float,
        default=DEFAULT_TARGET_REACHED,
        help="Tolerance (N) for advancing past ramping_to_target.",
    )
    p.add_argument(
        "--release-duration",
        type=float,
        default=DEFAULT_RELEASE_DURATION,
        help="Seconds of upward motion (manual mode) during the end-of-run release.",
    )
    p.add_argument(
        "--jog-duration-ms",
        type=int,
        default=DEFAULT_JOG_DURATION_MS,
        help="Auto-stop window for manual u/d jogs (ms).",
    )
    # Safety
    p.add_argument(
        "--max-force",
        type=float,
        default=DEFAULT_MAX_FORCE,
        help="Hard safety cap (N); faults immediately if exceeded in the loop.",
    )
    p.add_argument(
        "--force-override",
        action="store_true",
        help="Allow --target-force > --max-force. DANGEROUS.",
    )
    p.add_argument(
        "--max-overshoot",
        type=float,
        default=DEFAULT_MAX_OVERSHOOT,
        help="Fault if force exceeds (target + this) while holding/timed_run. "
             "Catches a mis-flashed Teensy that doesn't honor T<value> commands.",
    )
    p.add_argument(
        "--max-parse-fails",
        type=int,
        default=DEFAULT_MAX_PARSE_FAILS,
        help="Consecutive force-gauge parse failures before faulting.",
    )
    # Logging
    p.add_argument(
        "--log-path",
        type=pathlib.Path,
        default=None,
        help="CSV log file. Defaults to Code/Python/logs/sonicator_<ts>.csv.",
    )
    return p.parse_args(argv)


# -----------------------------------------------------------------------------
# Controller
# -----------------------------------------------------------------------------
HELP_TEXT = (
    "Live keyboard commands:\n"
    "  u  jog up   (~{jog} ms then auto-stop; press again to continue)\n"
    "  d  jog down (~{jog} ms then auto-stop)\n"
    "  s  stop motor (during an automated state this PAUSES the run)\n"
    "  g  resume a paused run\n"
    "  h  show this help\n"
    "  Ctrl+C  quit safely\n"
)


class Controller:
    def __init__(
        self,
        args: argparse.Namespace,
        mark: serial.Serial,
        teensy: serial.Serial,
        log: CsvLogger,
    ):
        self.args = args
        self.mark = mark
        self.teensy = teensy
        self.log = log

        self.state = State.STARTUP_STOP
        self.paused = False
        self.running = True

        self.fail_streak = 0
        self.last_msg = ""
        self.jog_until = 0.0  # monotonic deadline; 0 means no jog in flight

        self.run_clock = PausableClock()
        self.release_clock = PausableClock()

        # Latest target Python has asked the Teensy to hold; used for CSV and
        # for re-sending after pause/resume.
        self._teensy_target = 0.0

        self.t0 = time.monotonic()
        self._tick = 0

    # ----- serial sending -------------------------------------------------
    def _send(self, data: bytes, label: str):
        try:
            self.teensy.write(data)
        except Exception as e:
            self._enter_fault(f"teensy write failed: {e}")
            return
        self.last_msg = label

    def send_force(self, f: float):
        self._send(f"{f:.3f}\n".encode(), f"F:{f:.2f}")

    def send_target(self, t: float):
        self._teensy_target = t
        self._send(f"T{t:.3f}\n".encode(), f"T:{t:.2f}")

    def send_manual(self, c: str):
        self._send(c.encode(), c)

    # ----- force gauge ----------------------------------------------------
    def poll_force(self):
        try:
            self.mark.write(b"?\r")
            line = self.mark.readline().decode(errors="ignore")
        except Exception as e:
            self._enter_fault(f"force gauge read failed: {e}")
            return None
        return parse_force(line)

    # ----- helpers --------------------------------------------------------
    def _state_target(self) -> float:
        """What Python wants the Teensy's targetForce to be in the current state.

        During RELEASE the Teensy is in manual mode (continuous 'u'), so this
        value is just for the CSV/status line — it is not sent to the Teensy.
        """
        if self.state == State.SEEKING_CONTACT:
            return self.args.contact_threshold
        if self.state in (
            State.RAMPING_TO_TARGET,
            State.HOLDING_FORCE,
            State.TIMED_RUN,
        ):
            return self.args.target_force
        return self._teensy_target

    def _print(self, msg: str):
        # Break out of the in-place status line (\r) so transition/log messages
        # appear on their own lines, then leave the cursor ready for the next
        # in-place status update.
        sys.stdout.write("\r\x1b[K" + msg + "\n")
        sys.stdout.flush()

    # ----- state transitions ---------------------------------------------
    def _enter_state(self, new_state: State, force):
        prev = self.state
        self.state = new_state
        f_str = "----" if force is None else f"{force:.2f}"
        self._print(f"[state] {prev.value} -> {new_state.value}  (force={f_str} N)")

        if new_state == State.SEEKING_CONTACT:
            self.send_target(self.args.contact_threshold)
        elif new_state == State.RAMPING_TO_TARGET:
            self.send_target(self.args.target_force)
        elif new_state == State.HOLDING_FORCE:
            pass  # one-tick latch; we'll advance to TIMED_RUN next call
        elif new_state == State.TIMED_RUN:
            self.run_clock.start()
            self._print(
                f"[timed_run] hold clock started; runtime = {self.args.runtime:.1f} s"
            )
        elif new_state == State.RELEASE:
            # Manual 'u' continuous: ~1240 steps/sec, far faster than the
            # proportional path (~125 steps/sec) so 0.5 s actually relieves a
            # compressed sample. Also works on stock firmware that ignores T.
            self.send_manual("u")
            self.release_clock.start()
        elif new_state == State.MANUAL_READY:
            self.send_manual("s")
            self._print("[manual_ready] run complete; u/d/s to jog, Ctrl+C to exit")
        elif new_state == State.FAULT:
            self.send_manual("s")

    def _enter_fault(self, msg: str):
        if self.state == State.FAULT:
            return
        self._print(f"!! FAULT: {msg}")
        # send 's' directly; avoid recursing through _send which could re-fault
        try:
            self.teensy.write(b"s")
        except Exception:
            pass
        self.last_msg = f"FAULT:{msg[:32]}"
        self.state = State.FAULT
        self.paused = False
        # freeze any running clocks
        self.run_clock.pause()
        self.release_clock.pause()

    def advance_state(self, force):
        # STARTUP_STOP advances on the first valid reading, even with paused=False
        if self.state == State.STARTUP_STOP:
            if force is not None:
                self._enter_state(State.SEEKING_CONTACT, force)
            return

        if self.paused or self.state == State.FAULT:
            return

        if force is None:
            return  # no transition decisions possible

        if self.state == State.SEEKING_CONTACT:
            if force >= self.args.contact_threshold:
                self._enter_state(State.RAMPING_TO_TARGET, force)
            return

        if self.state == State.RAMPING_TO_TARGET:
            if force >= self.args.target_force - self.args.target_reached_tol:
                self._enter_state(State.HOLDING_FORCE, force)
            return

        if self.state == State.HOLDING_FORCE:
            # one-tick latch -> start the hold clock
            self._enter_state(State.TIMED_RUN, force)
            return

        if self.state == State.TIMED_RUN:
            if self.run_clock.elapsed() >= self.args.runtime:
                self._print(
                    f"[timed_run] runtime {self.args.runtime:.1f} s reached "
                    f"(elapsed {self.run_clock.elapsed():.2f} s)"
                )
                self._enter_state(State.RELEASE, force)
            return

        if self.state == State.RELEASE:
            if self.release_clock.elapsed() >= self.args.release_duration:
                # Belt-and-suspenders: explicit 's' before MANUAL_READY also sends one.
                self.send_manual("s")
                self._enter_state(State.MANUAL_READY, force)
            return

    # ----- keyboard -------------------------------------------------------
    def handle_key(self, ch: str, now: float):
        if ch in ("h", "H", "?"):
            self._print(HELP_TEXT.format(jog=self.args.jog_duration_ms))
            return
        if self.state == State.FAULT:
            return
        if ch == "u" or ch == "d":
            self.send_manual(ch)
            self.jog_until = now + (self.args.jog_duration_ms / 1000.0)
            return
        if ch == "s":
            self.send_manual("s")
            self.jog_until = 0.0
            if self.state in AUTOMATIC_STATES and not self.paused:
                self.paused = True
                self.run_clock.pause()
                self.release_clock.pause()
                self._print("[paused] press 'g' to resume, u/d to jog manually")
            return
        if ch == "g":
            if self.paused:
                self.paused = False
                # Re-assert the right Teensy target for the state we're resuming into
                self.send_target(self._state_target())
                self.run_clock.resume()
                self.release_clock.resume()
                self._print("[resumed]")
            return
        # any other key -> ignored silently

    # ----- main loop ------------------------------------------------------
    def _status_line(self, force):
        f_str = "----" if force is None else f"{force:6.2f}"
        tgt = self._state_target()
        elapsed_total = time.monotonic() - self.t0
        wall = dt.datetime.now().strftime("%H:%M:%S")
        extra = ""
        if self.state == State.TIMED_RUN and not self.paused:
            extra = f"  hold={self.run_clock.elapsed():5.2f}/{self.args.runtime:.1f}s"
        elif self.state == State.RELEASE and not self.paused:
            extra = (f"  rel={self.release_clock.elapsed():4.2f}"
                     f"/{self.args.release_duration:.2f}s")
        flags = " [PAUSED]" if self.paused else ""
        return (
            f"{wall} t={elapsed_total:6.2f}s  [{self.state.value:<17}] "
            f"force={f_str} N  target={tgt:6.2f} N  "
            f"last={self.last_msg:<10}{extra}{flags}"
        )

    def _print_status_inplace(self, force):
        # Pad to a fixed width so shorter status lines fully overwrite previous
        # longer ones; \r returns to column 0 of the same terminal line.
        sys.stdout.write("\r" + self._status_line(force).ljust(140))
        sys.stdout.flush()

    def run(self):
        period = 1.0 / self.args.loop_hz
        self._print(HELP_TEXT.format(jog=self.args.jog_duration_ms))
        self._print(f"[log] {self.log.path}")
        next_deadline = time.monotonic()

        while self.running:
            now = time.monotonic()

            # 1) Keyboard (priority over automation)
            while True:
                ch = try_read_key()
                if ch is None:
                    break
                self.handle_key(ch, now)
            if not self.running:
                break

            # 2) Force read + safety checks
            force = self.poll_force()
            if self.state != State.FAULT:
                if force is None:
                    self.fail_streak += 1
                    if self.fail_streak >= self.args.max_parse_fails:
                        self._enter_fault(
                            f"{self.fail_streak} consecutive force-gauge parse failures"
                        )
                else:
                    self.fail_streak = 0
                    if force > self.args.max_force and not self.args.force_override:
                        self._enter_fault(
                            f"over-force {force:.2f} N > {self.args.max_force} N cap"
                        )
                    # Overshoot guard: while holding force, the Teensy proportional
                    # control should keep us within ~0.1 N of target. A large
                    # excursion above target most often means the Teensy firmware
                    # is the old version that ignores T-commands and is still
                    # driving toward its compiled-in target.
                    elif self.state in (State.HOLDING_FORCE, State.TIMED_RUN):
                        ceiling = self.args.target_force + self.args.max_overshoot
                        if force > ceiling:
                            self._enter_fault(
                                f"force {force:.2f} N > target+overshoot "
                                f"{ceiling:.2f} N (is the Teensy flashed with the "
                                f"latest firmware?)"
                            )

            # 3) State machine (one-shot messages emitted via _enter_state)
            self.advance_state(force)

            # 4) This-tick streaming outbound message
            jog_active = self.jog_until > 0.0
            if jog_active and now >= self.jog_until:
                # jog window expired -> stop the motor once
                self.send_manual("s")
                self.jog_until = 0.0
            elif jog_active:
                # jog still in flight; let the Teensy keep running in manual mode
                pass
            elif (
                self.state in AUTOMATIC_STATES and not self.paused and force is not None
            ):
                self.send_force(force)
            # MANUAL_READY / FAULT / paused-not-jogging / no-force: nothing to send

            # 5) Log + throttled status print
            self.log.write_row(
                self.t0,
                now,
                force,
                self._state_target(),
                self.last_msg,
                self.state,
                self.paused,
            )
            self._tick += 1
            # Continuous in-place status update every tick; transitions/log
            # messages use _print() which breaks out to a fresh line.
            self._print_status_inplace(force)
            # last_msg is "this tick's message"; reset for next tick
            self.last_msg = ""

            # 6) Sleep until next tick (drift-resistant)
            next_deadline += period
            sleep_for = next_deadline - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                # fell behind; resync deadline
                next_deadline = time.monotonic()


# -----------------------------------------------------------------------------
# main
# -----------------------------------------------------------------------------
def _default_log_path() -> pathlib.Path:
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    here = pathlib.Path(__file__).resolve().parent
    return here / "logs" / f"sonicator_{ts}.csv"


def main(argv=None) -> int:
    args = parse_args(argv)

    # ---- argument validation ----
    if args.runtime <= 0:
        print("ERROR: --runtime must be > 0", file=sys.stderr)
        return 2
    if args.target_force < 0:
        print("ERROR: --target-force must be >= 0", file=sys.stderr)
        return 2
    if args.target_force > args.max_force:
        if not args.force_override:
            print(
                f"ERROR: --target-force {args.target_force} N exceeds safety cap "
                f"--max-force {args.max_force} N. Pass --force-override to override.",
                file=sys.stderr,
            )
            return 2
        print(
            f"!! WARNING: --force-override active; target {args.target_force} N "
            f"exceeds safety cap {args.max_force} N",
            file=sys.stderr,
        )
    if args.loop_hz <= 0:
        print("ERROR: --loop-hz must be > 0", file=sys.stderr)
        return 2

    # cbreak only works on a real TTY; check early before opening hardware
    if not sys.stdin.isatty():
        print(
            "ERROR: stdin is not a TTY. Run this directly in an interactive SSH "
            "terminal (not piped or redirected).",
            file=sys.stderr,
        )
        return 2

    log_path = args.log_path if args.log_path is not None else _default_log_path()
    print(f"[log] writing to {log_path}")

    teensy = None
    mark = None
    csv_log = None
    try:
        teensy = open_teensy(args.teensy_port, args.teensy_baud)
        print(f"[teensy] opened {args.teensy_port} @ {args.teensy_baud}")
        mark = open_mark10(args.mark10_port, args.mark10_baud)
        print(f"[mark10] opened {args.mark10_port} @ {args.mark10_baud}")
        csv_log = CsvLogger(log_path)

        with keyboard_cbreak():
            controller = Controller(args, mark, teensy, csv_log)

            signal.signal(
                signal.SIGINT, lambda *_: setattr(controller, "running", False)
            )

            try:
                controller.run()
            except Exception as e:
                print(
                    f"\nERROR in control loop: {type(e).__name__}: {e}", file=sys.stderr
                )
                try:
                    csv_log.write_row(
                        controller.t0,
                        time.monotonic(),
                        None,
                        0.0,
                        f"EXCEPTION:{type(e).__name__}",
                        State.FAULT,
                        False,
                    )
                except Exception:
                    pass
        return 0

    except serial.SerialException as e:
        print(f"ERROR: serial open failed: {e}", file=sys.stderr)
        return 3
    finally:
        # Always send 's' and clean up, no matter how we got here
        if teensy is not None:
            try:
                teensy.write(b"s")
                time.sleep(0.05)
                teensy.close()
            except Exception:
                pass
        if mark is not None:
            try:
                mark.close()
            except Exception:
                pass
        if csv_log is not None:
            csv_log.close()
            print(f"\n[log] saved {log_path}")


if __name__ == "__main__":
    sys.exit(main())
