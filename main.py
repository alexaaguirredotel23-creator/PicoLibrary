"""Mini-Lab 3: Feedback Kiosk (Raspberry Pi Pico / MicroPython).

Visitor flow and manager alarm run as two independent state machines.
In Wokwi, click the ultrasonic sensor and set its distance below 10 cm.
"""

from machine import I2C, Pin, PWM, time_pulse_us
from neopixel import NeoPixel
import time


# GPIO allocations (match diagram.json)
TRIG_PIN = 2
ECHO_PIN = 3
SDA_PIN = 4
SCL_PIN = 5
STRIP_PIN = 6
BUZZER_PIN = 7
GREEN_PIN = 10
RED_PIN = 11
MANAGER_PIN = 12

DETECTION_CM = 10
VOTE_TIMEOUT_MS = 8000
RESULT_DURATION_MS = 3000
LONG_PRESS_MS = 1200
ALERT_THRESHOLD = 30


class LCDDisplay:
    """LCD 1602 over a PCF8574 I2C backpack, address 0x27."""

    BACKLIGHT = 0x08
    ENABLE = 0x04

    def __init__(self):
        self.bus = I2C(0, sda=Pin(SDA_PIN), scl=Pin(SCL_PIN), freq=100000)
        self.address = 0x27
        self.last_lines = None
        time.sleep_ms(50)

        # Initialize the HD44780 controller in 4-bit mode.
        for delay in (5, 5, 1):
            self._pulse(0x30 | self.BACKLIGHT)
            time.sleep_ms(delay)
        self._pulse(0x20 | self.BACKLIGHT)
        self._command(0x28)  # 4-bit, two lines
        self._command(0x0C)  # display on, cursor off
        self._command(0x06)  # auto-increment cursor
        self._command(0x01)  # clear display
        time.sleep_ms(2)

    def _pulse(self, bits):
        self.bus.writeto(self.address, bytes([bits | self.ENABLE]))
        time.sleep_us(1)
        self.bus.writeto(self.address, bytes([bits & ~self.ENABLE]))
        time.sleep_us(50)

    def _send(self, value, register_select):
        high = (value & 0xF0) | self.BACKLIGHT | register_select
        low = ((value << 4) & 0xF0) | self.BACKLIGHT | register_select
        self._pulse(high)
        self._pulse(low)

    def _command(self, command):
        self._send(command, 0)

    def _write(self, character):
        self._send(ord(character), 1)

    def showMessage(self, line1, line2=""):
        # MicroPython does not implement str.ljust().
        lines = ((line1[:16] + " " * 16)[:16], (line2[:16] + " " * 16)[:16])
        if lines == self.last_lines:
            return
        self.last_lines = lines
        for line_index, line in enumerate(lines):
            self._command(0x80 if line_index == 0 else 0xC0)
            for character in line:
                self._write(character)


class UltrasonicSensor:
    def __init__(self):
        self.trig = Pin(TRIG_PIN, Pin.OUT)
        self.echo = Pin(ECHO_PIN, Pin.IN)
        self.trig.off()

    def distanceCm(self):
        self.trig.off()
        time.sleep_us(2)
        self.trig.on()
        time.sleep_us(10)
        self.trig.off()
        try:
            duration = time_pulse_us(self.echo, 1, 30000)
            return duration / 58.0 if duration >= 0 else 999.0
        except OSError:
            return 999.0

    def tripped(self):
        return self.distanceCm() <= DETECTION_CM


class LightStrip:
    def __init__(self):
        self.leds = NeoPixel(Pin(STRIP_PIN, Pin.OUT), 8)
        self.current = None

    def setColor(self, color):
        if self.current == color:
            return
        self.current = color
        for index in range(8):
            self.leds[index] = color
        self.leds.write()

    def clear(self):
        self.setColor((0, 0, 0))


class PassiveBuzzer:
    def __init__(self):
        self.pwm = PWM(Pin(BUZZER_PIN))
        self.pwm.freq(880)
        self.pwm.duty_u16(0)

    def playAlarm(self, now_ms):
        # Intermittent beeping (non-blocking).
        self.pwm.duty_u16(18000 if (now_ms // 350) % 2 == 0 else 0)

    def stop(self):
        self.pwm.duty_u16(0)


class Button:
    """Debounced button with short-press and long-press events."""

    def __init__(self, gpio):
        self.pin = Pin(gpio, Pin.IN, Pin.PULL_UP)
        self.was_down = False
        self.down_since = 0
        self.raw_down = False
        self.changed_at = time.ticks_ms()

    def poll(self, now_ms):
        down = self.pin.value() == 0
        if down != self.raw_down:
            self.raw_down = down
            self.changed_at = now_ms
        if time.ticks_diff(now_ms, self.changed_at) < 35:
            return None
        if down == self.was_down:
            return None

        self.was_down = down
        if down:
            self.down_since = now_ms
            return "PRESS"
        if time.ticks_diff(now_ms, self.down_since) >= LONG_PRESS_MS:
            return "LONG"
        return "SHORT"


class FeedbackStats:
    def __init__(self):
        self.positiveVotes = 0
        self.negativeVotes = 0

    def recordVote(self, positive):
        if positive:
            self.positiveVotes += 1
        else:
            self.negativeVotes += 1

    def total(self):
        return self.positiveVotes + self.negativeVotes

    def satisfaction(self):
        return round(100 * self.positiveVotes / self.total()) if self.total() else 100

    def reset(self):
        self.positiveVotes = 0
        self.negativeVotes = 0


class StateModel:
    def __init__(self):
        self.visitor = "IDLE"
        self.manager = "NORMAL"
        self.started_at = time.ticks_ms()

    def addTransition(self, new_state, now_ms):
        self.visitor = new_state
        self.started_at = now_ms

    def processEvent(self, event_name):
        # Events are dispatched by FeedbackController using the two state regions.
        return event_name


class FeedbackController:
    def __init__(self):
        self.display = LCDDisplay()
        self.sensor = UltrasonicSensor()
        self.strip = LightStrip()
        self.buzzer = PassiveBuzzer()
        self.green = Button(GREEN_PIN)
        self.red = Button(RED_PIN)
        self.manager = Button(MANAGER_PIN)
        self.stats = FeedbackStats()
        self.model = StateModel()
        self.last_vote = None
        self.showIdle()

    def showIdle(self):
        if self.model.manager != "NORMAL":
            self.display.showMessage("MANAGER ALERT", "Needs attention")
        else:
            self.display.showMessage("Welcome!", "Move within 10cm")

    def showPrompt(self):
        self.display.showMessage("Rate your visit", "Green +  Red -")

    def showResult(self):
        response = "Thanks! Positive" if self.last_vote else "Thanks! Negative"
        score = "Score: {}% ({})".format(self.stats.satisfaction(), self.stats.total())
        self.display.showMessage(response, score)

    def showStepAway(self):
        self.display.showMessage("Thank you!", "Please step away")

    def startAlert(self):
        if self.model.manager == "NORMAL":
            self.model.manager = "ALARM_SOUNDING"
            print("ALERT: satisfaction below 30 percent")

    def silenceAlarm(self):
        if self.model.manager == "ALARM_SOUNDING":
            self.model.manager = "ALARM_SILENCED"
            self.buzzer.stop()
            print("Manager silenced alarm")

    def clearAlert(self):
        self.model.manager = "NORMAL"
        self.buzzer.stop()

    def resetStats(self):
        self.stats.reset()
        self.clearAlert()
        print("Manager reset feedback statistics")
        if self.model.visitor == "IDLE":
            self.showIdle()

    def recordVote(self, positive):
        self.stats.recordVote(positive)
        self.last_vote = positive
        print("Votes: +{} / -{} | Satisfaction: {}%".format(
            self.stats.positiveVotes, self.stats.negativeVotes, self.stats.satisfaction()))
        if self.stats.satisfaction() < ALERT_THRESHOLD:
            self.startAlert()
        elif self.model.manager != "NORMAL":
            self.clearAlert()

    def updateLights(self):
        if self.model.manager != "NORMAL":
            color = (45, 0, 0)
        elif self.model.visitor == "AWAITING_VOTE":
            color = (30, 24, 0)
        elif self.model.visitor == "SHOWING_RESULT":
            color = (0, 35, 5) if self.last_vote else (35, 8, 0)
        elif self.model.visitor == "WAITING_FOR_LEAVE":
            color = (0, 20, 25)
        else:
            color = (0, 0, 12)
        self.strip.setColor(color)

    def tick(self):
        now = time.ticks_ms()
        green_event = self.green.poll(now)
        red_event = self.red.poll(now)
        manager_event = self.manager.poll(now)

        # Independent manager region: short press = silence, long = reset.
        if manager_event == "LONG":
            self.resetStats()
        elif manager_event == "SHORT":
            self.silenceAlarm()

        # Independent visitor region.
        state = self.model.visitor
        elapsed = time.ticks_diff(now, self.model.started_at)

        if state == "IDLE":
            if self.sensor.tripped():
                self.model.addTransition("AWAITING_VOTE", now)
                self.showPrompt()
        elif state == "AWAITING_VOTE":
            if green_event == "PRESS" or red_event == "PRESS":
                self.recordVote(green_event == "PRESS")
                self.model.addTransition("SHOWING_RESULT", now)
                self.showResult()
            elif elapsed >= VOTE_TIMEOUT_MS:
                self.model.addTransition("WAITING_FOR_LEAVE", now)
                self.showStepAway()
        elif state == "SHOWING_RESULT":
            if elapsed >= RESULT_DURATION_MS:
                self.model.addTransition("WAITING_FOR_LEAVE", now)
                self.showStepAway()
        elif state == "WAITING_FOR_LEAVE":
            if self.sensor.distanceCm() > DETECTION_CM:
                self.model.addTransition("IDLE", now)
                self.showIdle()

        self.updateLights()
        if self.model.manager == "ALARM_SOUNDING":
            self.buzzer.playAlarm(now)
        else:
            self.buzzer.stop()


controller = FeedbackController()
print("Feedback kiosk ready. Set sensor distance below 10 cm to begin.")
while True:
    controller.tick()
    time.sleep_ms(40)
