// Two OPT101 cells -> ADS1115 -> Arduino Mega: stream photodiode voltages over serial.
//
// Wiring: ADS1115 VDD=5V, GND, SDA=pin 20, SCL=pin 21, ADDR=GND (0x48)
//         OPT101 #1 (OA)  OUT -> ADS1115 A0;  LED #1 (with resistor) on pin 8 -> GND
//         OPT101 #2 (ICA) OUT -> ADS1115 A1;  LED #2 (with resistor) on pin 9 -> GND
//         Both OPT101 5V/GND to Mega 5V/GND.
//
// Output: "OA:<V>,ICA:<V>" per line at 115200 baud (Serial Monitor or Plotter).
// Send 'l' over serial to toggle both LEDs (to check the dark/ambient reading).
//
// Requires the "Adafruit ADS1X15" library.

#include <Wire.h>
#include <Adafruit_ADS1X15.h>

const uint8_t LED_PINS[] = {8, 9};

// ADS1115 input range per channel. Pick the smallest range that is still
// ABOVE that channel's brightest reading (LED on, clear fluid) with margin;
// anything above the range clips flat at the limit.
//   GAIN_ONE     +/-4.096 V  (125 uV/bit)
//   GAIN_TWO     +/-2.048 V  (62.5 uV/bit)
//   GAIN_FOUR    +/-1.024 V  (31.25 uV/bit)
//   GAIN_EIGHT   +/-0.512 V  (15.6 uV/bit)
//   GAIN_SIXTEEN +/-0.256 V  (7.8 uV/bit)
const adsGain_t OA_GAIN  = GAIN_ONE;
const adsGain_t ICA_GAIN = GAIN_ONE;

Adafruit_ADS1115 ads;
bool ledOn = true;

// Set the gain, read the channel, and convert using that same gain.
float readVolts(uint8_t channel, adsGain_t gain) {
  ads.setGain(gain);
  int16_t raw = ads.readADC_SingleEnded(channel);
  return ads.computeVolts(raw);
}

void setLeds(bool on) {
  for (uint8_t pin : LED_PINS) digitalWrite(pin, on ? HIGH : LOW);
}

void setup() {
  Serial.begin(115200);
  for (uint8_t pin : LED_PINS) pinMode(pin, OUTPUT);
  setLeds(true);

  if (!ads.begin(0x48)) { 
    Serial.println("ADS1115 not found - check wiring and ADDR pin");
    while (true) {}
  }
  ads.setDataRate(RATE_ADS1115_250SPS);
}

void loop() {
  if (Serial.available()) {
    if (Serial.read() == 'l') {
      ledOn = !ledOn;
      setLeds(ledOn);
    }
  }

  float oa  = readVolts(0, OA_GAIN);
  float ica = readVolts(1, ICA_GAIN);

  Serial.print("OA:");
  Serial.print(oa, 5);
  Serial.print(",ICA:");
  Serial.println(ica, 5);
}
