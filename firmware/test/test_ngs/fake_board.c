/*
 * fake_board.c - An ngs_board.h implementation backed by in-memory buffers.
 *
 * This is the payoff for routing every hardware access through ngs_board.h:
 * the tests drive ngs_app_poll() over a scripted receive buffer and read back
 * exactly the bytes the firmware would have put on the wire, exercising the
 * real framing and dispatch code with no hardware involved.
 */

#include "fake_board.h"

#include <string.h>

#include "ngs_protocol.h"

FakeBoard g_fake;

void fake_board_reset(void)
{
    memset(&g_fake, 0, sizeof(g_fake));
    g_fake.adc_value = 2048; /* mid-scale at 12 bits */
    g_fake.adc_resolution = 12;
    for (unsigned i = 0; i < FAKE_PIN_COUNT; i++) {
        g_fake.pin_mode[i] = 0xFF; /* "never configured" */
    }
}

void fake_board_feed(const uint8_t *data, size_t len)
{
    for (size_t i = 0; i < len && g_fake.rx_len < FAKE_BUF_SIZE; i++) {
        g_fake.rx[g_fake.rx_len++] = data[i];
    }
}

/* ---- ngs_board.h implementation --------------------------------------- */

void ngs_board_init(void) {}

size_t ngs_board_write(const uint8_t *data, size_t len)
{
    if (g_fake.tx_blocked) {
        return 0;
    }
    size_t n = 0;
    while (n < len && g_fake.tx_len < FAKE_BUF_SIZE) {
        g_fake.tx[g_fake.tx_len++] = data[n++];
    }
    return n;
}

int ngs_board_read(void)
{
    if (g_fake.rx_pos >= g_fake.rx_len) {
        return -1;
    }
    return g_fake.rx[g_fake.rx_pos++];
}

uint32_t ngs_board_micros(void)
{
    return g_fake.micros;
}

uint32_t ngs_board_cpu_hz(void)
{
    return 600000000u;
}

void ngs_board_serial_number(uint8_t out[8])
{
    for (uint8_t i = 0; i < 8; i++) {
        out[i] = (uint8_t)(0xA0 + i);
    }
}

int32_t ngs_board_temp_mc(void)
{
    return 42500;
}

int ngs_board_pin_mode(uint8_t pin, uint8_t mode)
{
    if (pin > NGS_MAX_DIGITAL_PIN) {
        return NGS_ERR_BAD_ARGUMENT;
    }
    if (mode > NGS_PIN_MODE_INPUT_PULLDOWN) {
        return NGS_ERR_BAD_ARGUMENT;
    }
    g_fake.pin_mode[pin] = mode;
    return 0;
}

int ngs_board_gpio_write(uint8_t pin, uint8_t value)
{
    if (pin > NGS_MAX_DIGITAL_PIN) {
        return NGS_ERR_BAD_ARGUMENT;
    }
    g_fake.pin_value[pin] = value ? 1 : 0;
    g_fake.gpio_writes++;
    return 0;
}

int ngs_board_gpio_read(uint8_t pin, uint8_t *value_out)
{
    if (pin > NGS_MAX_DIGITAL_PIN) {
        return NGS_ERR_BAD_ARGUMENT;
    }
    *value_out = g_fake.pin_value[pin];
    return 0;
}

int ngs_board_adc_read(uint8_t channel, uint8_t samples, uint16_t *raw_out, uint8_t *res_out)
{
    if (channel > NGS_MAX_ADC_CHANNEL) {
        return NGS_ERR_BAD_ARGUMENT;
    }
    g_fake.adc_reads++;
    g_fake.adc_last_samples = samples;
    g_fake.adc_last_channel = channel;
    *raw_out = g_fake.adc_value;
    *res_out = g_fake.adc_resolution;
    return 0;
}

int ngs_board_pwm_write(uint8_t pin, uint16_t duty, uint32_t freq_hz, uint8_t resolution)
{
    if (pin > NGS_MAX_DIGITAL_PIN || resolution > 16) {
        return NGS_ERR_BAD_ARGUMENT;
    }
    g_fake.pwm_pin = pin;
    g_fake.pwm_duty = duty;
    g_fake.pwm_freq = freq_hz;
    g_fake.pwm_writes++;
    return 0;
}

void ngs_board_led(int on)
{
    g_fake.led = on ? 1 : 0;
}

void ngs_board_reset(void)
{
    g_fake.reset_calls++;
    /* Unlike the real one, this returns so the test can keep going. */
}
