/*
 * ngs_app.c - Command dispatch and telemetry streaming. See ngs_app.h.
 *
 * Pure C: everything hardware-specific goes through ngs_board.h.
 */

#include "ngs_app.h"

#include <string.h>

#include "ngs_board.h"
#include "ngs_protocol.h"

/* Cap on bytes pulled from the receive buffer per poll. Without it, a host
 * that streams commands faster than we serve them would starve telemetry and
 * inflate loop_max_us -- better to leave the surplus buffered and come back. */
#define NGS_RX_BUDGET_PER_POLL 512u

/* ------------------------------------------------------------------------ */
/* Transmit helpers                                                          */
/* ------------------------------------------------------------------------ */

static void app_send(NgsApp *app, uint8_t type, uint8_t seq, const void *payload, uint16_t len)
{
    size_t n = ngs_frame_encode(type, seq, payload, len, app->tx, sizeof(app->tx));
    if (n == 0) {
        return; /* only reachable if len > NGS_MAX_PAYLOAD; caller's bug */
    }
    ngs_board_write(app->tx, n);
    app->tx_frames++;
}

static void app_send_error(NgsApp *app, uint8_t code, uint8_t seq, uint8_t type)
{
    NgsErrorPayload e = {0};
    e.code = code;
    e.seq = seq;
    e.type = type;
    app_send(app, NGS_MSG_ERROR, seq, &e, sizeof(e));
}

/* Replies to `req` with an empty success response. */
static void app_send_ack(NgsApp *app, const NgsFrame *req)
{
    app_send(app, (uint8_t)(req->type | NGS_MSG_RESP), req->seq, NULL, 0);
}

void ngs_app_log(NgsApp *app, const char *msg)
{
    size_t len = strlen(msg);
    if (len > NGS_MAX_PAYLOAD) {
        len = NGS_MAX_PAYLOAD;
    }
    app_send(app, NGS_MSG_LOG, 0, msg, (uint16_t)len);
}

/* ------------------------------------------------------------------------ */
/* Command handlers                                                          */
/*                                                                           */
/* Each returns 0 after replying, or an NGS_ERR_* code for the caller to turn */
/* into an NGS_MSG_ERROR frame.                                              */
/* ------------------------------------------------------------------------ */

static int handle_ping(NgsApp *app, const NgsFrame *req)
{
    NgsPongPayload pong = {0};
    pong.uptime_us = ngs_board_micros();
    app_send(app, (uint8_t)(req->type | NGS_MSG_RESP), req->seq, &pong, sizeof(pong));
    return 0;
}

static int handle_get_info(NgsApp *app, const NgsFrame *req)
{
    NgsInfoPayload info = {0};
    info.proto_version = NGS_PROTO_VERSION;
    info.fw_major = NGS_FW_VERSION_MAJOR;
    info.fw_minor = NGS_FW_VERSION_MINOR;
    info.fw_patch = NGS_FW_VERSION_PATCH;
    info.cpu_hz = ngs_board_cpu_hz();
    info.max_payload = NGS_MAX_PAYLOAD;
    ngs_board_serial_number(info.mcu_serial);
    app_send(app, (uint8_t)(req->type | NGS_MSG_RESP), req->seq, &info, sizeof(info));
    return 0;
}

static int handle_get_status(NgsApp *app, const NgsFrame *req)
{
    NgsStatusPayload st = {0};
    st.uptime_us = ngs_board_micros();
    st.rx_frames = app->decoder.frames;
    st.tx_frames = app->tx_frames;
    st.rx_crc_errors = app->decoder.crc_errors;
    st.rx_overflows = app->decoder.overflows;
    st.loop_max_us = app->loop_max_us;
    st.temp_mc = ngs_board_temp_mc();
    app_send(app, (uint8_t)(req->type | NGS_MSG_RESP), req->seq, &st, sizeof(st));

    /* Read-and-clear: each GET_STATUS reports the worst loop since the last
     * one, which is what you want when watching for a regression. */
    app->loop_max_us = 0;
    return 0;
}

static int handle_set_gpio(NgsApp *app, const NgsFrame *req)
{
    if (req->len != sizeof(NgsGpioSetPayload)) {
        return NGS_ERR_BAD_PAYLOAD;
    }
    NgsGpioSetPayload p;
    memcpy(&p, req->payload, sizeof(p));

    int err = ngs_board_pin_mode(p.pin, p.mode);
    if (err != 0) {
        return err;
    }
    err = ngs_board_gpio_write(p.pin, p.value);
    if (err != 0) {
        return err;
    }
    app_send_ack(app, req);
    return 0;
}

static int handle_get_gpio(NgsApp *app, const NgsFrame *req)
{
    if (req->len != sizeof(NgsGpioGetPayload)) {
        return NGS_ERR_BAD_PAYLOAD;
    }
    NgsGpioGetPayload p;
    memcpy(&p, req->payload, sizeof(p));

    int err = ngs_board_pin_mode(p.pin, p.mode);
    if (err != 0) {
        return err;
    }
    err = ngs_board_gpio_read(p.pin, &p.value);
    if (err != 0) {
        return err;
    }
    app_send(app, (uint8_t)(req->type | NGS_MSG_RESP), req->seq, &p, sizeof(p));
    return 0;
}

static int handle_read_adc(NgsApp *app, const NgsFrame *req)
{
    if (req->len != sizeof(NgsAdcReadPayload)) {
        return NGS_ERR_BAD_PAYLOAD;
    }
    NgsAdcReadPayload p;
    memcpy(&p, req->payload, sizeof(p));

    /* Read into locals rather than passing &p.raw: the struct is packed, so a
     * uint16_t* into it is not guaranteed aligned. Cortex-M7 would tolerate it
     * but the compiler is right to object, and this costs nothing. */
    uint16_t raw = 0;
    uint8_t resolution = 0;
    int err = ngs_board_adc_read(p.channel, p.samples, &raw, &resolution);
    if (err != 0) {
        return err;
    }
    p.raw = raw;
    p.resolution = resolution;
    app_send(app, (uint8_t)(req->type | NGS_MSG_RESP), req->seq, &p, sizeof(p));
    return 0;
}

static int handle_write_pwm(NgsApp *app, const NgsFrame *req)
{
    if (req->len != sizeof(NgsPwmWritePayload)) {
        return NGS_ERR_BAD_PAYLOAD;
    }
    NgsPwmWritePayload p;
    memcpy(&p, req->payload, sizeof(p));

    int err = ngs_board_pwm_write(p.pin, p.duty, p.freq_hz, p.resolution);
    if (err != 0) {
        return err;
    }
    app_send_ack(app, req);
    return 0;
}

static int handle_set_stream(NgsApp *app, const NgsFrame *req)
{
    if (req->len != sizeof(NgsStreamCfgPayload)) {
        return NGS_ERR_BAD_PAYLOAD;
    }
    NgsStreamCfgPayload p;
    memcpy(&p, req->payload, sizeof(p));

    if (p.enable) {
        if (p.period_us == 0 || p.channel_mask == 0) {
            return NGS_ERR_BAD_ARGUMENT;
        }
        /* Reject channels this board does not have, rather than silently
         * dropping them and leaving the host to wonder why count is short. */
        if (p.channel_mask >> (NGS_MAX_ADC_CHANNEL + 1u)) {
            return NGS_ERR_BAD_ARGUMENT;
        }
        app->stream_channel_mask = p.channel_mask;
        app->stream_period_us = p.period_us;
        app->stream_next_us = ngs_board_micros() + p.period_us;
        app->stream_seq = 0;
        app->stream_enabled = true;
    } else {
        app->stream_enabled = false;
    }

    app_send_ack(app, req);
    return 0;
}

static int handle_reset(NgsApp *app, const NgsFrame *req)
{
    /* Acknowledge first: once we reboot the USB endpoint drops and anything
     * still queued is lost, so the host would otherwise always time out. */
    app_send_ack(app, req);
    ngs_board_reset();
    return 0; /* unreachable */
}

/* ------------------------------------------------------------------------ */
/* Dispatch                                                                  */
/* ------------------------------------------------------------------------ */

static void app_dispatch(NgsApp *app, const NgsFrame *req)
{
    int err;

    switch (req->type) {
    case NGS_MSG_PING:       err = handle_ping(app, req); break;
    case NGS_MSG_GET_INFO:   err = handle_get_info(app, req); break;
    case NGS_MSG_GET_STATUS: err = handle_get_status(app, req); break;
    case NGS_MSG_RESET:      err = handle_reset(app, req); break;
    case NGS_MSG_SET_GPIO:   err = handle_set_gpio(app, req); break;
    case NGS_MSG_GET_GPIO:   err = handle_get_gpio(app, req); break;
    case NGS_MSG_READ_ADC:   err = handle_read_adc(app, req); break;
    case NGS_MSG_WRITE_PWM:  err = handle_write_pwm(app, req); break;
    case NGS_MSG_SET_STREAM: err = handle_set_stream(app, req); break;
    default:                 err = NGS_ERR_UNKNOWN_TYPE; break;
    }

    if (err != 0) {
        app_send_error(app, (uint8_t)err, req->seq, req->type);
    }
}

/* ------------------------------------------------------------------------ */
/* Telemetry                                                                 */
/* ------------------------------------------------------------------------ */

static void app_emit_telemetry(NgsApp *app)
{
    uint8_t buf[sizeof(NgsTelemetryHeader) + (NGS_MAX_ADC_CHANNEL + 1u) * sizeof(uint16_t)];
    NgsTelemetryHeader hdr = {0};
    uint8_t count = 0;
    uint8_t resolution = 0;
    size_t off = sizeof(hdr);

    for (uint8_t ch = 0; ch <= NGS_MAX_ADC_CHANNEL; ch++) {
        if ((app->stream_channel_mask & (1u << ch)) == 0) {
            continue;
        }
        uint16_t raw = 0;
        if (ngs_board_adc_read(ch, 1, &raw, &resolution) != 0) {
            continue;
        }
        memcpy(&buf[off], &raw, sizeof(raw)); /* LE on Cortex-M, as on the host */
        off += sizeof(raw);
        count++;
    }

    hdr.timestamp_us = ngs_board_micros();
    hdr.seq = app->stream_seq++;
    hdr.channel_mask = app->stream_channel_mask;
    hdr.count = count;
    hdr.resolution = resolution;
    memcpy(buf, &hdr, sizeof(hdr));

    app_send(app, NGS_MSG_TELEMETRY, 0, buf, (uint16_t)off);
}

/* ------------------------------------------------------------------------ */
/* Public entry points                                                       */
/* ------------------------------------------------------------------------ */

void ngs_app_init(NgsApp *app)
{
    memset(app, 0, sizeof(*app));
    ngs_decoder_init(&app->decoder);
    app->last_poll_us = ngs_board_micros();
}

void ngs_app_poll(NgsApp *app)
{
    uint32_t now = ngs_board_micros();

    /* Unsigned wrap makes this correct across the ~71 minute micros() rollover. */
    uint32_t elapsed = now - app->last_poll_us;
    if (elapsed > app->loop_max_us) {
        app->loop_max_us = elapsed;
    }
    app->last_poll_us = now;

    for (unsigned budget = 0; budget < NGS_RX_BUDGET_PER_POLL; budget++) {
        int b = ngs_board_read();
        if (b < 0) {
            break;
        }

        NgsFrame frame;
        int rc = ngs_decoder_push(&app->decoder, (uint8_t)b, &frame);
        if (rc == NGS_DECODE_FRAME) {
            app_dispatch(app, &frame);
        } else if (rc == NGS_DECODE_ERROR) {
            /* seq/type are unknown -- the frame never decoded. */
            app_send_error(app, app->decoder.last_error, 0, 0);
        }
    }

    if (app->stream_enabled) {
        /* Signed difference so the comparison survives the micros() wrap. */
        if ((int32_t)(now - app->stream_next_us) >= 0) {
            app_emit_telemetry(app);
            app->stream_next_us += app->stream_period_us;
            /* If we fell far behind (host stalled, period too short), resync
             * instead of spinning to catch up on a backlog nobody wants. */
            if ((int32_t)(now - app->stream_next_us) >= 0) {
                app->stream_next_us = now + app->stream_period_us;
            }
        }
    }
}
