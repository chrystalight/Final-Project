// Streams the laminar-flow pressure sensor over serial. Pairs with pressure_gui.py.
//
// Wiring: ADS1115 VDD=5V, GND, SDA=pin 20, SCL=pin 21, ADDR=GND (0x48)
//         Pressure sensor Vs -> 5V, GND -> GND, Vout -> ADS1115 A0
//
// Output (115200 baud): "READY" on boot, then one "<t_us>,<volts>" line per
// sample, forever (~150 samples/s; the serial printing limits the rate).
//
// Requires the "Adafruit ADS1X15" library.

#include <Wire.h>
#include <Adafruit_ADS1X15.h>

const uint8_t CHANNEL = 0;  // ADS1115 input the sensor is on

// ADS1115 input range. GAIN_TWOTHIRDS reads the full 0-5 V a 5 V sensor can
// output, so it can't clip. Once you know the range the signal actually uses,
// pick the smallest range above its maximum for more resolution:
//   GAIN_TWOTHIRDS +/-6.144 V, GAIN_ONE +/-4.096 V, GAIN_TWO +/-2.048 V,
//   GAIN_FOUR +/-1.024 V, GAIN_EIGHT +/-0.512 V, GAIN_SIXTEEN +/-0.256 V
const adsGain_t GAIN = GAIN_TWOTHIRDS;

Adafruit_ADS1115 ads;

void setup() {
  Serial.begin(115200);
  if (!ads.begin(0x48)) {
    Serial.println("ERR ADS1115 not found - check wiring and ADDR pin");
    while (true) {}
  }
  ads.setGain(GAIN);
  ads.setDataRate(RATE_ADS1115_250SPS);
  ads.readADC_SingleEnded(CHANNEL);  // discard the first conversion
  Serial.println("READY");
}

void loop() {
  int16_t raw = ads.readADC_SingleEnded(CHANNEL);
  unsigned long t = micros();
  Serial.print(t);
  Serial.print(',');
  Serial.println(ads.computeVolts(raw), 5);
}
