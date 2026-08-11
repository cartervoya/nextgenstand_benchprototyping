/*
 * ngs_app.h - Command dispatch and telemetry streaming. Pure C.
 *
 * Owns the decoder, the response buffer, and the streaming state. main.cpp
 * calls ngs_app_init() once then ngs_app_poll() every loop() iteration.
 */

#ifndef NGS_APP_H
#define NGS_APP_H

#include <stdbool.h>
#include <stdint.h>

#include "ngs_control.h"
#include "ngs_link.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Bumped independently of NGS_PROTO_VERSION; reported by NGS_MSG_GET_INFO.
 * Overridable from platformio.ini so CI can stamp a build. */
#ifndef NGS_FW_VERSION_MAJOR
#define NGS_FW_VERSION_MAJOR 0
#endif
#ifndef NGS_FW_VERSION_MINOR
#define NGS_FW_VERSION_MINOR 1
#endif
#ifndef NGS_FW_VERSION_PATCH
#define NGS_FW_VERSION_PATCH 0
#endif

typedef struct {
    NgsDecoder decoder;
    uint8_t tx[NGS_FRAME_WIRE_MAX];

    /* Telemetry streaming state, configured by NGS_MSG_SET_STREAM. */
    bool stream_enabled;
    uint32_t stream_period_us;
    uint32_t stream_channel_mask;
    uint32_t stream_next_us; /* next due time, on the micros() timebase */
    uint32_t stream_seq;

    uint32_t tx_frames;
    uint32_t loop_max_us;
    uint32_t last_poll_us;

    /* Emergency stop. Latched: once engaged it stays engaged until the host
     * explicitly clears it. The safe-state table is registered by the host in
     * advance so the device can reach a safe state on its own -- which is what
     * makes the watchdog below possible at all. */
    bool estop;
    uint8_t estop_source; /* NGS_ESTOP_SRC_* */
    uint8_t safe_count;
    uint8_t safe_kind[NGS_SAFE_MAX_ENTRIES];
    uint8_t safe_pin[NGS_SAFE_MAX_ENTRIES];
    uint16_t safe_value[NGS_SAFE_MAX_ENTRIES];
    uint16_t safe_resolution[NGS_SAFE_MAX_ENTRIES];
    uint32_t watchdog_ms; /* 0 disables */
    uint32_t last_rx_us;  /* when the host last got a frame through */

    /* Closed-loop pump control. Runs on its own period inside ngs_app_poll,
     * independent of how often the host talks to us. */
    NgsControl control;
    /* Both learned from the last NGS_MSG_WRITE_PWM: the loop drives the pin
     * the operator was already driving, at the resolution already configured,
     * so it never has to reprogram the timer. */
    uint8_t control_pin;
    uint8_t control_bits;
} NgsApp;

void ngs_app_init(NgsApp *app);

/* Drains the receive buffer, answers whatever arrived, and emits a telemetry
 * record if one is due. Non-blocking; call it as often as possible. */
void ngs_app_poll(NgsApp *app);

/* Sends an NGS_MSG_LOG frame. Truncated to NGS_MAX_PAYLOAD. Safe to call
 * before the host has connected -- the bytes are simply dropped. */
void ngs_app_log(NgsApp *app, const char *msg);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* NGS_APP_H */
