import serial
import time

count = 0
avgError = 0.0
targetForce = 30.0

mark = serial.Serial("/dev/ttyUSB0", 115200, timeout=0.2)
teensy = serial.Serial("/dev/ttyACM1", 115200, timeout=0.1)
time.sleep(0.1)
while True:
    mark.write(b"?\r")
    response = mark.readline()
    response = response.decode(errors="ignore").strip()
    try:
        force = float(response)
    except:
        continue
    teensy.write((response + "\n").encode())

    error = force - targetForce
    count += 1
    avgError += (error - avgError) / count
    print(f"{force:.2f} N | avgError: {avgError:.4f} | count: {count}")

    time.sleep(1 / 30)
mark.close()

##testing git
