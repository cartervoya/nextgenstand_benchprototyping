/*
 * main.cpp - Teensy 4.1 entry point and the ngs_board.h implementation.
 *
 * Deliberately the only C++ in the firmware. Everything above the hardware
 * (framing, dispatch, streaming) is pure C in ngs_link.c / ngs_app.c so it can
 * be unit tested off-target. Keep new logic out of this file -- if you find
 * yourself writing an algorithm here, it belongs in ngs_app.c behind a new
 * ngs_board_* call.
 */

#include <Arduino.h>
#include <EEPROM.h>

extern "C" {
#include "ngs_app.h"
#include "ngs_board.h"
#include "ngs_protocol.h"
}

/* Teensy 4.1 ADC is 12-bit; 10 is the Arduino default and throws away two
 * bits for no reason. PWM matches so duty and ADC counts share a scale. */
static constexpr uint8_t kAdcBits = 12;
static constexpr uint8_t kPwmBits = 12;

static NgsApp g_app;
static uint8_t g_pwm_bits = kPwmBits;

/* Last frequency programmed per pin, so a repeat write is a no-op. 0 means
 * "never set", which is also what the host sends to mean "leave it alone". */
static uint32_t g_pwm_freq[NGS_MAX_DIGITAL_PIN + 1];

/* --------------------------------------------------------------------------
 * ngs_board.h implementation
 * ------------------------------------------------------------------------ */
extern "C" {

void ngs_board_init(void)
{
    /* Baud is ignored for Teensy USB CDC -- it always runs at full USB speed.
     * The argument is kept only so host-side tools that insist on setting one
     * do not have to special-case this board. */
    Serial.begin(115200);

    pinMode(LED_BUILTIN, OUTPUT);
    analogReadResolution(kAdcBits);
    analogReadAveraging(4);
    analogWriteResolution(kPwmBits);
}

size_t ngs_board_write(const uint8_t *data, size_t len)
{
    /* Serial.write() blocks once the USB tx buffer fills and no host is
     * draining it, which would stall the whole control loop. availableForWrite
     * lets us drop instead -- a lost telemetry record is always preferable to
     * a frozen bench. Command responses are far smaller than the buffer, so in
     * practice only streaming ever hits this path. */
    if (!Serial || (size_t)Serial.availableForWrite() < len) {
        return 0;
    }

    size_t n = Serial.write(data, len);

    /* Teensyduino holds a partly filled USB packet for up to 5 ms hoping more
     * data arrives. That is the right trade for log spew and the wrong one for
     * request/response: every command would inherit up to 5 ms of latency for
     * no reason. Our frames are already complete when we get here, so push
     * them out now. */
    Serial.send_now();
    return n;
}

int ngs_board_read(void)
{
    return Serial.available() ? Serial.read() : -1;
}

uint32_t ngs_board_micros(void)
{
    return micros();
}

uint32_t ngs_board_cpu_hz(void)
{
    /* F_CPU_ACTUAL tracks runtime scaling; F_CPU is only the compile-time ask. */
    return F_CPU_ACTUAL;
}

void ngs_board_serial_number(uint8_t out[8])
{
    /* i.MXRT1062 unique ID, fused into OCOTP CFG0/CFG1. */
    const uint32_t lo = HW_OCOTP_CFG0;
    const uint32_t hi = HW_OCOTP_CFG1;
    memcpy(&out[0], &lo, sizeof(lo));
    memcpy(&out[4], &hi, sizeof(hi));
}

int32_t ngs_board_temp_mc(void)
{
    return (int32_t)(tempmonGetTemp() * 1000.0f);
}

int ngs_board_pin_mode(uint8_t pin, uint8_t mode)
{
    if (pin > NGS_MAX_DIGITAL_PIN) {
        return NGS_ERR_BAD_ARGUMENT;
    }
    switch (mode) {
    case NGS_PIN_MODE_OUTPUT:         pinMode(pin, OUTPUT); break;
    case NGS_PIN_MODE_INPUT:          pinMode(pin, INPUT); break;
    case NGS_PIN_MODE_INPUT_PULLUP:   pinMode(pin, INPUT_PULLUP); break;
    case NGS_PIN_MODE_INPUT_PULLDOWN: pinMode(pin, INPUT_PULLDOWN); break;
    default:                          return NGS_ERR_BAD_ARGUMENT;
    }
    return 0;
}

int ngs_board_gpio_write(uint8_t pin, uint8_t value)
{
    if (pin > NGS_MAX_DIGITAL_PIN) {
        return NGS_ERR_BAD_ARGUMENT;
    }
    digitalWriteFast(pin, value ? HIGH : LOW);
    return 0;
}

int ngs_board_gpio_read(uint8_t pin, uint8_t *value_out)
{
    if (pin > NGS_MAX_DIGITAL_PIN) {
        return NGS_ERR_BAD_ARGUMENT;
    }
    *value_out = digitalReadFast(pin) ? 1 : 0;
    return 0;
}

/* Analog channel -> digital pin.
 *
 * NOT `A0 + channel`: that only holds while the analog pins are contiguous,
 * which on a Teensy 4.1 they are up to A13 (pins 14..27) and then are not --
 * A14 is pin 38, not 28. Reading A14..A17 through the arithmetic version would
 * quietly return some other pin's voltage, which is the kind of bug you chase
 * with a multimeter for an afternoon. */
static const uint8_t kAdcPin[NGS_MAX_ADC_CHANNEL + 1] = {
    A0, A1, A2,  A3,  A4,  A5,  A6,  A7,  A8,
    A9, A10, A11, A12, A13, A14, A15, A16, A17,
};

int ngs_board_adc_read(uint8_t channel, uint8_t samples, uint16_t *raw_out, uint8_t *res_out)
{
    if (channel > NGS_MAX_ADC_CHANNEL) {
        return NGS_ERR_BAD_ARGUMENT;
    }
    if (samples == 0) {
        samples = 1; /* the protocol documents 0 as "one sample" */
    }

    uint32_t acc = 0;
    for (uint8_t i = 0; i < samples; i++) {
        acc += (uint32_t)analogRead(kAdcPin[channel]);
    }

    *raw_out = (uint16_t)(acc / samples);
    *res_out = kAdcBits;
    return 0;
}

int ngs_board_pwm_write(uint8_t pin, uint16_t duty, uint32_t freq_hz, uint8_t resolution)
{
    if (pin > NGS_MAX_DIGITAL_PIN) {
        return NGS_ERR_BAD_ARGUMENT;
    }
    if (resolution > 16) {
        return NGS_ERR_BAD_ARGUMENT;
    }

    if (resolution != 0) {
        analogWriteResolution(resolution);
        g_pwm_bits = resolution;
    }
    /* Reject a duty the current resolution cannot express, rather than letting
     * the core silently clamp it and report success. */
    if (g_pwm_bits < 16 && duty >= (1u << g_pwm_bits)) {
        return NGS_ERR_BAD_ARGUMENT;
    }
    /* The host sends the frequency with every write so a board that rebooted
     * comes back configured. Reprogramming the FlexPWM timer restarts its
     * counter, so only do it when the frequency actually changed -- otherwise
     * every setpoint change would glitch the output. */
    if (freq_hz != 0 && freq_hz != g_pwm_freq[pin]) {
        analogWriteFrequency(pin, (float)freq_hz);
        g_pwm_freq[pin] = freq_hz;
    }
    analogWrite(pin, duty);
    return 0;
}

/* Teensyduino emulates EEPROM in program flash: 4284 bytes on a 4.1, wear
 * levelled across 63 sectors. EEPROM.update() only touches a byte that has
 * actually changed, which is what keeps re-saving an unchanged configuration
 * from costing flash endurance. */
bool ngs_board_nvm_read(uint32_t offset, uint8_t *data, uint32_t len)
{
    if (offset + len > (uint32_t)E2END + 1u) {
        return false;
    }
    for (uint32_t i = 0; i < len; i++) {
        data[i] = EEPROM.read((int)(offset + i));
    }
    return true;
}

bool ngs_board_nvm_write(uint32_t offset, const uint8_t *data, uint32_t len)
{
    if (offset + len > (uint32_t)E2END + 1u) {
        return false;
    }
    for (uint32_t i = 0; i < len; i++) {
        EEPROM.update((int)(offset + i), data[i]);
    }
    return true;
}

void ngs_board_led(int on)
{
    digitalWriteFast(LED_BUILTIN, on ? HIGH : LOW);
}

void ngs_board_reset(void)
{
    Serial.flush();
    delay(10); /* let the ACK reach the wire before the endpoint drops */
    SCB_AIRCR = 0x05FA0004;  /* ARM system reset request */
    for (;;) {
    }
}

} /* extern "C" */

/* --------------------------------------------------------------------------
 * Arduino entry points
 * ------------------------------------------------------------------------ */

void setup()
{
    ngs_board_init();
    ngs_app_init(&g_app);
    ngs_app_log(&g_app, "ngs firmware ready");
}

void loop()
{
    ngs_app_poll(&g_app);

    /* Heartbeat: 1 Hz means "powered but idle", a fast flutter means the host
     * is actively streaming. Cheapest possible liveness check at the bench
     * without attaching a debugger. */
    static uint32_t last_toggle = 0;
    const uint32_t interval = g_app.stream_enabled ? 100000u : 500000u;
    const uint32_t now = micros();
    if (now - last_toggle >= interval) {
        last_toggle = now;
        static bool on = false;
        on = !on;
        ngs_board_led(on);
    }
}
