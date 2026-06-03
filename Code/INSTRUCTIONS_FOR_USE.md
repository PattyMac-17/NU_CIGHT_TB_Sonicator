# Instructions for Use — Running a Sonicator Trial

This guide walks you through running a force-controlled trial on the sonicator
rig using the `launch.py` program on the Raspberry Pi. No programming knowledge
is required — you will type a few commands and watch the screen.

> **Before you start:** This guide assumes the hardware is already powered on and
> connected (the motor driver, the Mark-10 force gauge, and the Teensy controller
> are all on and plugged in). If anything is unplugged or unpowered, stop and ask
> the engineering team — running with hardware in an unknown state is not safe.

---

## What a trial does

When you start a trial, the system automatically:

1. **Seeks contact** — slowly drives the ram down until it touches the sample.
2. **Ramps to your target force** — increases pressure until it reaches the
   force you asked for (in Newtons).
3. **Holds** that force for the run time you asked for (in seconds).
4. **Releases** — backs the ram off the sample.
5. **Finishes** in a "ready" state where you can move the ram by hand if needed.

Everything that happens is saved automatically to a spreadsheet file (a `.csv`)
so you have a record of every trial.

---

## Step 1 — Connect to the Raspberry Pi

You will connect to the Pi from your laptop using **SSH** (a way to control the
Pi over the network from a terminal window).

> **The exact connection details (address, username, password) are in the
> printed sheet that ships with the device.** They are kept off this document for
> security. Follow that sheet to open the connection.

Once connected, your terminal prompt will look like this:

```
capstone@sonicatorPi:~ $
```

That `capstone@sonicatorPi` tells you that you are now controlling the Pi.

---

## Step 2 — Go to the Desktop folder

The program lives on the Pi's Desktop. Type this and press **Enter**:

```
cd ~/Desktop
```

Your prompt will change to show you are in the Desktop folder:

```
capstone@sonicatorPi:~/Desktop $
```

---

## Step 3 — Start a trial

The command has two parts you choose every time:

| What you set | Word to type | Meaning |
|---|---|---|
| Run time | `--runtime` | How many **seconds** to hold the force after the target is reached |
| Target force | `--target-force` | How many **Newtons** of force to push with |

Type the command on one line. For example, to hold **5 Newtons for 5 seconds**:

```
python3 launch.py --runtime 5 --target-force 5
```

Another example — hold **25 Newtons for 5 minutes (300 seconds)**:

```
python3 launch.py --runtime 300 --target-force 25
```

Press **Enter** to begin. The ram will start moving on its own toward the sample.

> **Safety limit:** The program will refuse any target force above **50 N**. This
> is intentional. Do not try to override it without engineering sign-off.

---

## Step 4 — Watch the trial run

Once running, a live status line updates at the bottom of the screen, showing the
clock, the current stage, and the measured force, for example:

```
14:32:07 t= 12.40s  [timed_run        ] force= 25.05 N  hold= 3.20/300.0s
```

- The word in brackets (e.g. `seeking_contact`, `ramping_to_target`,
  `timed_run`, `release`) tells you which stage you're in.
- `force= 25.05 N` is what the gauge is reading right now.
- `hold= 3.20/300.0s` (only during the hold stage) shows how far into the hold
  time you are.

When the run is complete you'll see a message like:

```
[manual_ready] run complete; u/d/s to jog, Ctrl+C to exit
```

That means the trial finished normally and the ram has backed off the sample.

---

## Step 5 — Stop or control the run by keyboard

While a trial is running you can press single keys (no need to press Enter):

| Key | What it does |
|---|---|
| **s** | **Stop / pause.** Stops the motor immediately. During an automatic run this *pauses* it. |
| **g** | **Go / resume.** Continues a run you paused with `s`. |
| **u** | Nudge the ram **up** a little (away from the sample). |
| **d** | Nudge the ram **down** a little (toward the sample). |
| **h** | Show the on-screen help reminder. |
| **Ctrl + C** | **Quit safely.** Stops the motor, saves the log, and exits. |

> **If anything looks wrong, press `s` first.** It stops the motor right away.
> Then press `Ctrl + C` to exit safely if you want to end the session.

---

## Step 6 — Finish and find your results

When you are done, press **Ctrl + C**. The program stops the motor, saves the
log file, and prints the location of the saved file, for example:

```
[log] saved /home/capstone/Desktop/logs/sonicator_20260603-143207.csv
```

**Every trial is saved automatically** in the `logs` folder on the Desktop. Each
file is named with the date and time it ran (`sonicator_YYYYMMDD-HHMMSS.csv`), so
trials never overwrite each other.

The `.csv` file opens in Excel or Google Sheets and contains, for every fraction
of a second of the trial: the timestamp, elapsed time, measured force, target
force, and which stage the system was in.

---

## If something goes wrong (FAULT)

The system protects itself. If it sees a problem it will **stop the motor** and
print a message starting with `!! FAULT:`. Common causes:

- **Over-force** — the measured force went above the 50 N safety cap.
- **Force gauge not responding** — the Mark-10 stopped sending readings.
- **Firmware mismatch** — the force overshot the target by too much, which can
  mean the Teensy controller has the wrong program loaded.

If you see a FAULT:

1. The motor is already stopped — you are safe.
2. Press **Ctrl + C** to exit.
3. Note the exact FAULT message and pass it to the engineering team.

---

## Quick reference card

```
# 1. Connect to the Pi (use the printed sheet for the SSH details)
# 2. Go to the program folder:
cd ~/Desktop

# 3. Start a trial (example: 5 N for 5 seconds):
python3 launch.py --runtime 5 --target-force 5

# While running:  s = stop/pause   g = resume   u/d = nudge up/down   Ctrl+C = quit
# Results are saved automatically in:  ~/Desktop/logs/
```
