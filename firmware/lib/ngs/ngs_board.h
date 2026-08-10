/*
 * ngs_board.h - The only seam between the pure-C application logic and the
 *               Teensyduino / Arduino C++ world.
 *
 * ngs_app.c calls nothing but these functions and the C standard library, so
 * the whole command layer compiles and runs on a host for unit tests. The
 * Teensy implementation lives in main.cpp as extern "C" definitions; a test
 * build supplies its own.
 *
 * Every function that can fail returns 0 on success or an NGS_ERR_* code, so
 * handlers can pass the result straight back to the host.
 */

#ifndef NGS_BOARD_H
#define NGS_BOARD_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Teensy 4.1 limits, used to validate host-supplied pins before touching
 * hardware. Digital 0..41 are on the main headers; 42..54 cover the SD/QSPI
 * pads and the bottom pads. Analog A0..A17 map to channels 0..17. */
#define NGS_MAX_DIGITAL_PIN 54u
#define NGS_MAX_ADC_CHANNEL 17u

void ngs_board_init(void);

/* Transport. write() returns bytes accepted; read() returns the next byte or
 * -1 when the receive buffer is empty. Neither blocks. */
size_t ngs_board_write(const uint8_t *data, size_t len);
int ngs_board_read(void);

/* Identity and timing. */
uint32_t ngs_board_micros(void);
uint32_t ngs_board_cpu_hz(void);
void ngs_board_serial_number(uint8_t out[8]);
int32_t ngs_board_temp_mc(void); /* die temperature in milli-degrees C */

/* GPIO. `mode` is one of NGS_PIN_MODE_*. */
int ngs_board_pin_mode(uint8_t pin, uint8_t mode);
int ngs_board_gpio_write(uint8_t pin, uint8_t value);
int ngs_board_gpio_read(uint8_t pin, uint8_t *value_out);

/* Analog in. Averages `samples` readings (>= 1) and reports the ADC
 * resolution actually in use. */
int ngs_board_adc_read(uint8_t channel, uint8_t samples, uint16_t *raw_out, uint8_t *res_out);

/* Analog out. freq_hz == 0 leaves the frequency alone; resolution == 0 leaves
 * the resolution alone. */
int ngs_board_pwm_write(uint8_t pin, uint16_t duty, uint32_t freq_hz, uint8_t resolution);

void ngs_board_led(int on);
void ngs_board_reset(void); /* does not return */

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* NGS_BOARD_H */
