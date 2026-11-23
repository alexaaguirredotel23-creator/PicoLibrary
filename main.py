"""
# main.py
# Raspberry Pi Pico 2 W display, three-button input and conference CRM startup.
# Author: Codex
"""
import time
from Log import *
from crm import CRMController, CRMModel, CRMService

# Set this to the same course server where you saved the customer.
COURSE = "ism6106"
MODE = "B"   # A preloads everything. B loads one customer's details on selection.
PREFERRED_NAME = "Alexa Aguirre"
# The published Lab 3 code used GP4/GP5. Also check GP0/GP1 from the physical setup.
LCD_PINS = ((4, 5), (0, 1))
NEXT_PIN, BACK_PIN, OPEN_PIN = 10, 11, 12
STRIP_PIN, BUZZER_PIN = 6, 7
BUTTON_ACTIVE_LOW = True
PIXELS = 8


class PicoView:
    """Display two lines, read the buttons, and signal activity history.

    Parameters:
    ----------
    lcd : I2cLcd
        Existing 16 by 2 LCD driver.
    strip : NeoPixel
        Eight addressable LEDs.
    buzzer : PassiveBuzzer
        Existing PicoLibrary buzzer driver.
    buttons : tuple
        Pairs of action name and GPIO input.

    Examples:
    ---------
    view.show("Client 1/5", "Alexa Aguirre") displays a directory entry.
    """

    def __init__(self, lcd, strip, buzzer, buttons):
        self.lcd = lcd
        self.strip = strip
        self.buzzer = buzzer
        self.buttons = buttons
        self.raw = self.stable = None
        self.changed = time.ticks_ms()
        self.lines = ("", "")
        self.page = 0
        self.last_page = self.changed
        self.neutral()

    def _draw(self):
        for line_index, line in enumerate(self.lines):
            # Keep the shorter line steady while a longer line advances in 16-char pages.
            pages = max(1, (len(line) + 15) // 16)
            start = (self.page % pages) * 16
            chunk = line[start:start + 16]
            self.lcd.move_to(0, line_index)
            self.lcd.putstr(chunk + " " * (16 - len(chunk)))

    def show(self, heading, text):
        """Print the complete message and display its first page immediately."""
        self.lines = (heading, text)
        self.page = 0
        self.last_page = time.ticks_ms()
        Log.i("PicoView.show | %s | %s" % (heading, text))
        self._draw()

    def refresh(self):
        """Advance long names, roles or descriptions every 2.5 seconds."""
        if max(len(line) for line in self.lines) <= 16:
            return
        now = time.ticks_ms()
        if time.ticks_diff(now, self.last_page) >= 2500:
            self.page += 1
            self.last_page = now
            self._draw()

    def read_button(self):
        """Return one event per debounced press, including while a key is held."""
        value = None
        for action, pin in self.buttons:
            if pin.value() == (0 if BUTTON_ACTIVE_LOW else 1):
                value = action
                break
        now = time.ticks_ms()
        if value != self.raw:
            self.raw, self.changed = value, now
        if self.stable != self.raw and time.ticks_diff(now, self.changed) >= 40:
            self.stable = self.raw
            return self.stable
        return None

    def neutral(self):
        """Turn off the previous customer's activity indicator."""
        self.strip.fill((0, 0, 0))
        self.strip.write()

    def signal(self, count):
        """Show one green pixel per activity (up to eight), or red and one chirp for zero."""
        self.neutral()
        if count:
            for i in range(min(count, PIXELS)):
                self.strip[i] = (0, 16, 0)
        else:
            self.strip.fill((18, 0, 0))
        self.strip.write()
        Log.i("PicoView.signal: %d activities" % count)
        if count == 0:
            self.buzzer.beep(540, 100)


def make_view():
    from machine import I2C, Pin
    from neopixel import NeoPixel
    from Buzzer import PassiveBuzzer
    from pico_i2c_lcd import I2cLcd

    lcd = None
    for sda, scl in LCD_PINS:
        bus = I2C(0, sda=Pin(sda), scl=Pin(scl), freq=100000)
        try:
            found = bus.scan()
        except OSError:
            continue
        addresses = [a for a in found if 0x20 <= a <= 0x27 or 0x38 <= a <= 0x3F]
        if addresses:
            lcd = I2cLcd(bus, addresses[0], 2, 16)
            Log.i("LCD found on GP%d/GP%d" % (sda, scl))
            break
    if lcd is None:
        raise OSError("LCD not found on GP4/GP5 or GP0/GP1. Check the LCD pins.")
    pull = Pin.PULL_UP if BUTTON_ACTIVE_LOW else Pin.PULL_DOWN
    buttons = tuple((action, Pin(n, Pin.IN, pull)) for action, n in
                    (("next", NEXT_PIN), ("back", BACK_PIN), ("open", OPEN_PIN)))
    return PicoView(lcd, NeoPixel(Pin(STRIP_PIN, Pin.OUT), PIXELS),
                    PassiveBuzzer(BUZZER_PIN), buttons)


def connect_wifi():
    """Read two lines from wifi.txt on the Pico only: hotspot name, then password."""
    import network
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        return wlan
    try:
        with open("wifi.txt") as f:
            ssid = f.readline().rstrip("\r\n")
            password = f.readline().rstrip("\r\n")
    except OSError:
        raise OSError("Create wifi.txt on the Pico: hotspot name and password on separate lines")
    wlan.connect(ssid, password)
    start = time.ticks_ms()
    while not wlan.isconnected():
        if wlan.status() < 0 or time.ticks_diff(time.ticks_ms(), start) >= 20000:
            wlan.disconnect()
            raise OSError("Hotspot connection failed. Check wifi.txt and hotspot availability.")
        time.sleep_ms(200)
    return wlan


def wait_after_error(view, exc):
    Log.e("CRM error: %s" % exc)
    view.neutral()
    view.show("See Thonny error", "Yellow = retry")
    while view.read_button() != "open":
        time.sleep_ms(20)


def run():
    view = make_view()
    model = CRMModel(CRMService("https://iacademy.oracle.com/ords/" + COURSE + "/customer"), MODE)
    controller = CRMController(model, view, PREFERRED_NAME)
    while True:
        try:
            view.show("Conference CRM", "Connecting Wi-Fi")
            wlan = connect_wifi()
            view.show("Wi-Fi connected", "Loading clients")
            controller.start()
            break
        except (OSError, ValueError, MemoryError, ImportError) as exc:
            wait_after_error(view, exc)
    while True:
        event = view.read_button()
        if event:
            while True:
                try:
                    needs_details = MODE == "B" and controller.screen == "directory" and event == "open"
                    if needs_details and not wlan.isconnected():
                        wlan = connect_wifi()
                    controller.handle(event)
                    break
                except (OSError, ValueError, MemoryError, ImportError) as exc:
                    wait_after_error(view, exc)
        view.refresh()
        time.sleep_ms(15)


if __name__ == "__main__":
    run()
