/*
 * fake_board.h - Inspectable state for the ngs_board.h test double.
 */

#ifndef FAKE_BOARD_H
#define FAKE_BOARD_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "ngs_board.h"

#define FAKE_BUF_SIZE 2048u
#define FAKE_PIN_COUNT (NGS_MAX_DIGITAL_PIN + 1u)

typedef struct {
    /* Bytes the firmware will read, and how far it has got through them. */
    uint8_t rx[FAKE_BUF_SIZE];
    size_t rx_len;
    size_t rx_pos;

    /* Bytes the firmware has written. */
    uint8_t tx[FAKE_BUF_SIZE];
    size_t tx_len;
    bool tx_blocked; /* set to simulate a full USB tx buffer */

    /* Test-controlled clock -- advance it explicitly instead of sleeping. */
    uint32_t micros;

    uint8_t pin_mode[FAKE_PIN_COUNT];
    uint8_t pin_value[FAKE_PIN_COUNT];
    uint32_t gpio_writes;

    uint16_t adc_value;
    uint8_t adc_resolution;
    uint32_t adc_reads;
    uint8_t adc_last_samples;
    uint8_t adc_last_channel;

    uint8_t pwm_pin;
    uint16_t pwm_duty;
    uint32_t pwm_freq;
    uint32_t pwm_writes;

    uint8_t led;
    uint32_t reset_calls;
} FakeBoard;

extern FakeBoard g_fake;

void fake_board_reset(void);
void fake_board_feed(const uint8_t *data, size_t len);

#endif /* FAKE_BOARD_H */
