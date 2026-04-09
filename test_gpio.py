import Jetson.GPIO as GPIO
import time
# Set your mode (BOARD is usually easiest for Jetson Nano)
GPIO.setmode(GPIO.BOARD)

# 1. Set pin 11 to HIGH (3.3V) immediately on setup
GPIO.setup(15, GPIO.OUT, initial=GPIO.HIGH)
time.sleep(5)
# 2. Set pin 12 to LOW (0V) immediately on setup
GPIO.setup(15, GPIO.OUT, initial=GPIO.LOW)
