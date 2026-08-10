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
