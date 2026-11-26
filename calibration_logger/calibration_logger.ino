// Calibration firmware for the OPT101 optical cells. Pairs with calibration_gui.py.
//
// Wiring: ADS1115 VDD=5V, GND, SDA=pin 20, SCL=pin 21, ADDR=GND (0x48)
//         OPT101 #1 (OA)  OUT -> ADS1115 A0;  LED #1 (with resistor) on pin 8 -> GND
//         OPT101 #2 (ICA) OUT -> ADS1115 A1;  LED #2 (with resistor) on pin 9 -> GND
//
// Serial protocol (115200 baud, newline-terminated):
//   On boot             -> "READY"
//   "PING"              -> "READY"
//   "R <ch> <ms>"       -> "START", then "<t_us>,<volts>" per sample for <ms>
//                          milliseconds on channel <ch> (0 = OA, 1 = ICA),
//                          then "END <n_samples>"
//   anything else       -> "ERR <reason>"
//
// Both LEDs stay on the whole time so they are thermally settled between trials.
//
// Requires the "Adafruit ADS1X15" library.

#include <Wire.h>
#include <Adafruit_ADS1X15.h>

const uint8_t NUM_CHANNELS = 2;
const uint8_t LED_PINS[NUM_CHANNELS] = {8, 9};

// ADS1115 input range per channel (0 = OA, 1 = ICA). Use the smallest range
// that is still above that channel's clear-fluid reading, or it will clip.
//   GAIN_ONE +/-4.096 V, GAIN_TWO +/-2.048 V, GAIN_FOUR +/-1.024 V, ...
const adsGain_t GAINS[NUM_CHANNELS] = {GAIN_ONE, GAIN_ONE};

const unsigned long MAX_DURATION_MS = 60000;

Adafruit_ADS1115 ads;
char cmdBuf[32];
uint8_t cmdLen = 0;

void setup() {
  Serial.begin(115200);
  for (uint8_t i = 0; i < NUM_CHANNELS; i++) {
    pinMode(LED_PINS[i], OUTPUT);
    digitalWrite(LED_PINS[i], HIGH);
  }

  if (!ads.begin(0x48)) {
    Serial.println("ERR ADS1115 not found - check wiring and ADDR pin");
    while (true) {}
  }
  ads.setDataRate(RATE_ADS1115_250SPS);
  Serial.println("READY");
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (cmdLen > 0) {
        cmdBuf[cmdLen] = '\0';
        handleCommand(cmdBuf);
        cmdLen = 0;
      }
    } else if (cmdLen < sizeof(cmdBuf) - 1) {
      cmdBuf[cmdLen++] = c;
    }
  }
}

void handleCommand(const char *cmd) {
  if (strcmp(cmd, "PING") == 0) {
    Serial.println("READY");
    return;
  }

  int channel;
  long durationMs;
  if (sscanf(cmd, "R %d %ld", &channel, &durationMs) == 2) {
    if (channel < 0 || channel >= NUM_CHANNELS) {
      Serial.println("ERR channel out of range");
    } else if (durationMs <= 0 || (unsigned long)durationMs > MAX_DURATION_MS) {
      Serial.println("ERR duration out of range");
    } else {
      record(channel, durationMs);
    }
    return;
  }

  Serial.println("ERR unknown command");
}

void record(uint8_t channel, unsigned long durationMs) {
  ads.setGain(GAINS[channel]);
  ads.readADC_SingleEnded(channel);  // discard first conversion after switching

  Serial.println("START");
  unsigned long n = 0;
  unsigned long t0 = micros();
  unsigned long durationUs = durationMs * 1000UL;
  while (micros() - t0 < durationUs) {
    int16_t raw = ads.readADC_SingleEnded(channel);
    unsigned long t = micros() - t0;
    Serial.print(t);
    Serial.print(',');
    Serial.println(ads.computeVolts(raw), 5);
    n++;
  }
  Serial.print("END ");
  Serial.println(n);
}
