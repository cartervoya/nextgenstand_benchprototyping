/*
 * test_main.cpp - Unity tests for the pure-C firmware layer.
 *
 * Runs ON the Teensy 4.1:  pio test -e teensy41
 *
 * On-target rather than native because (a) there is no host C compiler in this
 * project's toolchain, and (b) it verifies the CRC, COBS and packed-struct
 * assumptions on the actual Cortex-M7 ABI instead of on x86.
 *
 * C++ only because the Teensyduino Unity runner needs setup()/loop(); the code
 * under test is all C.
 */

#include <Arduino.h>
#include <unity.h>

extern "C" {
#include "fake_board.h"
#include "ngs_app.h"
#include "ngs_link.h"
#include "ngs_protocol.h"
}

static NgsApp app;

void setUp(void)
{
    fake_board_reset();
    ngs_app_init(&app);
    g_fake.tx_len = 0; /* drop the "ready" log so tests see only their own frames */
}

void tearDown(void) {}

/* ---- helpers ----------------------------------------------------------- */

/* Encodes a request and hands it to the fake board's receive buffer. */
static void send_request(uint8_t type, uint8_t seq, const void *payload, uint16_t len)
{
    static uint8_t buf[NGS_FRAME_WIRE_MAX];
    size_t n = ngs_frame_encode(type, seq, payload, len, buf, sizeof(buf));
    TEST_ASSERT_NOT_EQUAL_MESSAGE(0, n, "ngs_frame_encode failed");
    fake_board_feed(buf, n);
}

/* Decodes the single frame the firmware is expected to have written. */
static bool take_response(NgsFrame *out)
{
    static NgsDecoder dec;
    ngs_decoder_init(&dec);
    for (size_t i = 0; i < g_fake.tx_len; i++) {
        if (ngs_decoder_push(&dec, g_fake.tx[i], out) == NGS_DECODE_FRAME) {
            return true;
        }
    }
    return false;
}

/* Full round trip: encode, poll, decode. */
static bool round_trip(uint8_t type, uint8_t seq, const void *payload, uint16_t len, NgsFrame *out)
{
    g_fake.tx_len = 0;
    send_request(type, seq, payload, len);
    ngs_app_poll(&app);
    return take_response(out);
}

/* ---- CRC --------------------------------------------------------------- */

static void test_crc16_known_vector(void)
{
    /* CRC-16/CCITT-FALSE("123456789") == 0x29B1, the standard check value.
     * If this fails, the host and firmware will never agree on a frame. */
    const uint8_t data[] = {'1', '2', '3', '4', '5', '6', '7', '8', '9'};
    TEST_ASSERT_EQUAL_HEX16(0x29B1, ngs_crc16(data, sizeof(data)));
}

static void test_crc16_empty_is_init(void)
{
    TEST_ASSERT_EQUAL_HEX16(0xFFFF, ngs_crc16(NULL, 0));
}

/* ---- COBS -------------------------------------------------------------- */

static void test_cobs_round_trip_with_zeros(void)
{
    const uint8_t src[] = {0x11, 0x00, 0x00, 0x22, 0x00};
    uint8_t enc[NGS_COBS_MAX_ENCODED(sizeof(src))];
    uint8_t dec[sizeof(src)];

    size_t n = ngs_cobs_encode(src, sizeof(src), enc, sizeof(enc));
    TEST_ASSERT_NOT_EQUAL(NGS_COBS_ERROR, n);

    /* The whole point of COBS: no 0x00 survives, so a 0x00 delimits frames. */
    for (size_t i = 0; i < n; i++) {
        TEST_ASSERT_NOT_EQUAL_MESSAGE(0, enc[i], "encoded body contains 0x00");
    }

    size_t m = ngs_cobs_decode(enc, n, dec, sizeof(dec));
    TEST_ASSERT_EQUAL(sizeof(src), m);
    TEST_ASSERT_EQUAL_HEX8_ARRAY(src, dec, sizeof(src));
}

static void test_cobs_round_trip_254_run(void)
{
    /* 254 non-zero bytes is exactly where COBS has to split a run and open a
     * fresh code byte -- the classic off-by-one in these implementations. */
    uint8_t src[300];
    for (size_t i = 0; i < sizeof(src); i++) {
        src[i] = (uint8_t)(i % 255u) + 1u; /* never 0 */
    }
    uint8_t enc[NGS_COBS_MAX_ENCODED(sizeof(src))];
    uint8_t dec[sizeof(src)];

    size_t n = ngs_cobs_encode(src, sizeof(src), enc, sizeof(enc));
    TEST_ASSERT_NOT_EQUAL(NGS_COBS_ERROR, n);
    size_t m = ngs_cobs_decode(enc, n, dec, sizeof(dec));
    TEST_ASSERT_EQUAL(sizeof(src), m);
    TEST_ASSERT_EQUAL_HEX8_ARRAY(src, dec, sizeof(src));
}

static void test_cobs_decode_rejects_embedded_zero(void)
{
    const uint8_t bad[] = {0x03, 0x11, 0x00};
    uint8_t dec[8];
    TEST_ASSERT_EQUAL(NGS_COBS_ERROR, ngs_cobs_decode(bad, sizeof(bad), dec, sizeof(dec)));
}

static void test_cobs_decode_rejects_run_past_end(void)
{
    const uint8_t bad[] = {0x05, 0x11}; /* claims 4 data bytes, only 1 present */
    uint8_t dec[8];
    TEST_ASSERT_EQUAL(NGS_COBS_ERROR, ngs_cobs_decode(bad, sizeof(bad), dec, sizeof(dec)));
}

/* ---- Framing ----------------------------------------------------------- */

static void test_frame_round_trip(void)
{
    const uint8_t payload[] = {0xDE, 0xAD, 0x00, 0xBE, 0xEF};
    uint8_t wire[NGS_FRAME_WIRE_MAX];

    size_t n = ngs_frame_encode(NGS_MSG_READ_ADC, 0x5A, payload, sizeof(payload), wire, sizeof(wire));
    TEST_ASSERT_NOT_EQUAL(0, n);
    TEST_ASSERT_EQUAL_HEX8_MESSAGE(0x00, wire[n - 1], "frame must end with a delimiter");

    NgsDecoder dec;
    NgsFrame frame;
    ngs_decoder_init(&dec);

    for (size_t i = 0; i < n - 1; i++) {
        TEST_ASSERT_EQUAL(NGS_DECODE_MORE, ngs_decoder_push(&dec, wire[i], &frame));
    }
    TEST_ASSERT_EQUAL(NGS_DECODE_FRAME, ngs_decoder_push(&dec, wire[n - 1], &frame));

    TEST_ASSERT_EQUAL_HEX8(NGS_MSG_READ_ADC, frame.type);
    TEST_ASSERT_EQUAL_HEX8(0x5A, frame.seq);
    TEST_ASSERT_EQUAL(sizeof(payload), frame.len);
    TEST_ASSERT_EQUAL_HEX8_ARRAY(payload, frame.payload, sizeof(payload));
}

static void test_frame_encode_rejects_oversize_payload(void)
{
    static uint8_t payload[NGS_MAX_PAYLOAD + 1];
    uint8_t wire[NGS_FRAME_WIRE_MAX];
    TEST_ASSERT_EQUAL(0, ngs_frame_encode(NGS_MSG_PING, 0, payload, sizeof(payload), wire,
                                          sizeof(wire)));
}

static void test_frame_max_payload_fits_wire_buffer(void)
{
    /* Guards NGS_FRAME_WIRE_MAX: a worst-case payload must still fit, or the
     * firmware would silently drop its largest frames at runtime. */
    static uint8_t payload[NGS_MAX_PAYLOAD];
    for (size_t i = 0; i < sizeof(payload); i++) {
        payload[i] = (uint8_t)(i | 1u); /* all non-zero: worst case for COBS */
    }
    static uint8_t wire[NGS_FRAME_WIRE_MAX];
    size_t n = ngs_frame_encode(NGS_MSG_TELEMETRY, 0, payload, sizeof(payload), wire, sizeof(wire));
    TEST_ASSERT_NOT_EQUAL_MESSAGE(0, n, "NGS_FRAME_WIRE_MAX is too small");
    TEST_ASSERT_LESS_OR_EQUAL(sizeof(wire), n);
}

static void test_decoder_detects_corrupted_byte(void)
{
    const uint8_t payload[] = {1, 2, 3, 4};
    uint8_t wire[NGS_FRAME_WIRE_MAX];
    size_t n = ngs_frame_encode(NGS_MSG_PING, 1, payload, sizeof(payload), wire, sizeof(wire));

    wire[3] ^= 0xFF; /* flip a payload byte; CRC must catch it */

    NgsDecoder dec;
    NgsFrame frame;
    ngs_decoder_init(&dec);
    int rc = NGS_DECODE_MORE;
    for (size_t i = 0; i < n; i++) {
        rc = ngs_decoder_push(&dec, wire[i], &frame);
    }
    TEST_ASSERT_EQUAL(NGS_DECODE_ERROR, rc);
    TEST_ASSERT_EQUAL_HEX8(NGS_ERR_BAD_CRC, dec.last_error);
    TEST_ASSERT_EQUAL(0, dec.frames);
}

static void test_decoder_resyncs_after_garbage(void)
{
    /* A device reset mid-frame leaves a partial frame in the host's stream and
     * vice versa. The next good frame must still be accepted. */
    NgsDecoder dec;
    NgsFrame frame;
    ngs_decoder_init(&dec);

    const uint8_t garbage[] = {0xFF, 0x12, 0x34, 0x00, 0x00, 0x00};
    for (size_t i = 0; i < sizeof(garbage); i++) {
        ngs_decoder_push(&dec, garbage[i], &frame);
    }

    uint8_t wire[NGS_FRAME_WIRE_MAX];
    size_t n = ngs_frame_encode(NGS_MSG_PING, 7, NULL, 0, wire, sizeof(wire));
    int rc = NGS_DECODE_MORE;
    for (size_t i = 0; i < n; i++) {
        rc = ngs_decoder_push(&dec, wire[i], &frame);
    }
    TEST_ASSERT_EQUAL(NGS_DECODE_FRAME, rc);
    TEST_ASSERT_EQUAL_HEX8(7, frame.seq);
}

static void test_decoder_ignores_repeated_delimiters(void)
{
    NgsDecoder dec;
    NgsFrame frame;
    ngs_decoder_init(&dec);
    for (int i = 0; i < 5; i++) {
        TEST_ASSERT_EQUAL(NGS_DECODE_MORE, ngs_decoder_push(&dec, 0x00, &frame));
    }
    TEST_ASSERT_EQUAL(0, dec.crc_errors);
    TEST_ASSERT_EQUAL(0, dec.overflows);
}

static void test_decoder_reports_overflow_once(void)
{
    NgsDecoder dec;
    NgsFrame frame;
    ngs_decoder_init(&dec);

    /* Far more non-zero bytes than any legal frame, then a delimiter. */
    for (size_t i = 0; i < sizeof(dec.enc) + 64u; i++) {
        TEST_ASSERT_EQUAL(NGS_DECODE_MORE, ngs_decoder_push(&dec, 0x41, &frame));
    }
    TEST_ASSERT_EQUAL(NGS_DECODE_ERROR, ngs_decoder_push(&dec, 0x00, &frame));
    TEST_ASSERT_EQUAL_HEX8(NGS_ERR_OVERFLOW, dec.last_error);
    TEST_ASSERT_EQUAL_MESSAGE(1, dec.overflows, "overflow should be reported once per frame");
}

/* ---- Struct layout ----------------------------------------------------- */

static void test_payload_sizes_match_host(void)
{
    /* These are the sizes host/ngs_host/protocol.py encodes. A silent change
     * here -- a reordered field, a dropped pad -- desyncs the two sides, and
     * this is the cheapest place to catch it. */
    TEST_ASSERT_EQUAL_MESSAGE(4, sizeof(NgsPongPayload), "NgsPongPayload");
    TEST_ASSERT_EQUAL_MESSAGE(20, sizeof(NgsInfoPayload), "NgsInfoPayload");
    TEST_ASSERT_EQUAL_MESSAGE(28, sizeof(NgsStatusPayload), "NgsStatusPayload");
    TEST_ASSERT_EQUAL_MESSAGE(4, sizeof(NgsGpioSetPayload), "NgsGpioSetPayload");
    TEST_ASSERT_EQUAL_MESSAGE(4, sizeof(NgsGpioGetPayload), "NgsGpioGetPayload");
    TEST_ASSERT_EQUAL_MESSAGE(8, sizeof(NgsAdcReadPayload), "NgsAdcReadPayload");
    TEST_ASSERT_EQUAL_MESSAGE(12, sizeof(NgsPwmWritePayload), "NgsPwmWritePayload");
    TEST_ASSERT_EQUAL_MESSAGE(12, sizeof(NgsStreamCfgPayload), "NgsStreamCfgPayload");
    TEST_ASSERT_EQUAL_MESSAGE(16, sizeof(NgsTelemetryHeader), "NgsTelemetryHeader");
    TEST_ASSERT_EQUAL_MESSAGE(4, sizeof(NgsErrorPayload), "NgsErrorPayload");
}

static void test_wire_is_little_endian(void)
{
    /* protocol.py uses '<' format strings throughout. Cortex-M7 runs LE, but
     * assert it rather than assume it. */
    const uint16_t v = 0x1234;
    uint8_t bytes[2];
    memcpy(bytes, &v, sizeof(v));
    TEST_ASSERT_EQUAL_HEX8(0x34, bytes[0]);
    TEST_ASSERT_EQUAL_HEX8(0x12, bytes[1]);
}

/* ---- Dispatch (real ngs_app.c over the fake board) --------------------- */

static void test_ping_returns_uptime(void)
{
    NgsFrame resp;
    g_fake.micros = 123456;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_PING, 0x11, NULL, 0, &resp));

    TEST_ASSERT_EQUAL_HEX8(NGS_MSG_PING | NGS_MSG_RESP, resp.type);
    TEST_ASSERT_EQUAL_HEX8(0x11, resp.seq);
    TEST_ASSERT_EQUAL(sizeof(NgsPongPayload), resp.len);

    NgsPongPayload pong;
    memcpy(&pong, resp.payload, sizeof(pong));
    TEST_ASSERT_EQUAL_UINT32(123456, pong.uptime_us);
}

static void test_get_info_reports_protocol_version(void)
{
    NgsFrame resp;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_GET_INFO, 2, NULL, 0, &resp));
    TEST_ASSERT_EQUAL(sizeof(NgsInfoPayload), resp.len);

    NgsInfoPayload info;
    memcpy(&info, resp.payload, sizeof(info));
    TEST_ASSERT_EQUAL_UINT8(NGS_PROTO_VERSION, info.proto_version);
    TEST_ASSERT_EQUAL_UINT32(NGS_MAX_PAYLOAD, info.max_payload);
    TEST_ASSERT_EQUAL_UINT32(600000000u, info.cpu_hz);
}

static void test_unknown_type_returns_error(void)
{
    NgsFrame resp;
    TEST_ASSERT_TRUE(round_trip(0x7E, 0x33, NULL, 0, &resp));
    TEST_ASSERT_EQUAL_HEX8(NGS_MSG_ERROR, resp.type);

    NgsErrorPayload err;
    memcpy(&err, resp.payload, sizeof(err));
    TEST_ASSERT_EQUAL_HEX8(NGS_ERR_UNKNOWN_TYPE, err.code);
    TEST_ASSERT_EQUAL_HEX8(0x33, err.seq);
    TEST_ASSERT_EQUAL_HEX8(0x7E, err.type);
}

static void test_wrong_payload_size_returns_error(void)
{
    NgsFrame resp;
    const uint8_t stub[2] = {1, 2}; /* SET_GPIO wants 4 bytes */
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_GPIO, 4, stub, sizeof(stub), &resp));
    TEST_ASSERT_EQUAL_HEX8(NGS_MSG_ERROR, resp.type);

    NgsErrorPayload err;
    memcpy(&err, resp.payload, sizeof(err));
    TEST_ASSERT_EQUAL_HEX8(NGS_ERR_BAD_PAYLOAD, err.code);
}

static void test_set_gpio_drives_pin(void)
{
    NgsFrame resp;
    NgsGpioSetPayload req = {};
    req.pin = 13;
    req.value = 1;
    req.mode = NGS_PIN_MODE_OUTPUT;

    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_GPIO, 5, &req, sizeof(req), &resp));
    TEST_ASSERT_EQUAL_HEX8(NGS_MSG_SET_GPIO | NGS_MSG_RESP, resp.type);
    TEST_ASSERT_EQUAL_MESSAGE(0, resp.len, "ack carries no payload");
    TEST_ASSERT_EQUAL_UINT8(1, g_fake.pin_value[13]);
    TEST_ASSERT_EQUAL_UINT8(NGS_PIN_MODE_OUTPUT, g_fake.pin_mode[13]);
}

static void test_set_gpio_rejects_out_of_range_pin(void)
{
    NgsFrame resp;
    NgsGpioSetPayload req = {};
    req.pin = NGS_MAX_DIGITAL_PIN + 1u;
    req.mode = NGS_PIN_MODE_OUTPUT;

    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_GPIO, 6, &req, sizeof(req), &resp));
    TEST_ASSERT_EQUAL_HEX8(NGS_MSG_ERROR, resp.type);

    NgsErrorPayload err;
    memcpy(&err, resp.payload, sizeof(err));
    TEST_ASSERT_EQUAL_HEX8(NGS_ERR_BAD_ARGUMENT, err.code);
    TEST_ASSERT_EQUAL_MESSAGE(0, g_fake.gpio_writes, "must not touch hardware on a bad pin");
}

static void test_read_adc_echoes_request_and_averages(void)
{
    NgsFrame resp;
    NgsAdcReadPayload req = {};
    req.channel = 3;
    req.samples = 8;
    g_fake.adc_value = 1234;

    TEST_ASSERT_TRUE(round_trip(NGS_MSG_READ_ADC, 7, &req, sizeof(req), &resp));
    TEST_ASSERT_EQUAL(sizeof(NgsAdcReadPayload), resp.len);

    NgsAdcReadPayload got;
    memcpy(&got, resp.payload, sizeof(got));
    TEST_ASSERT_EQUAL_UINT8(3, got.channel);
    TEST_ASSERT_EQUAL_UINT8(8, got.samples);
    TEST_ASSERT_EQUAL_UINT16(1234, got.raw);
    TEST_ASSERT_EQUAL_UINT8(12, got.resolution);
    /* Averaging happens inside ngs_board_adc_read, so the app layer should make
     * exactly one call and pass the sample count straight through. */
    TEST_ASSERT_EQUAL_UINT32(1, g_fake.adc_reads);
    TEST_ASSERT_EQUAL_UINT8(8, g_fake.adc_last_samples);
}

static void test_read_adc_rejects_bad_channel(void)
{
    NgsFrame resp;
    NgsAdcReadPayload req = {};
    req.channel = NGS_MAX_ADC_CHANNEL + 1u;
    req.samples = 1;

    TEST_ASSERT_TRUE(round_trip(NGS_MSG_READ_ADC, 8, &req, sizeof(req), &resp));
    TEST_ASSERT_EQUAL_HEX8(NGS_MSG_ERROR, resp.type);
}

static void test_write_pwm_applies_settings(void)
{
    NgsFrame resp;
    NgsPwmWritePayload req = {};
    req.pin = 4;
    req.duty = 2048;
    req.freq_hz = 20000;
    req.resolution = 12;

    TEST_ASSERT_TRUE(round_trip(NGS_MSG_WRITE_PWM, 9, &req, sizeof(req), &resp));
    TEST_ASSERT_EQUAL_HEX8(NGS_MSG_WRITE_PWM | NGS_MSG_RESP, resp.type);
    TEST_ASSERT_EQUAL_UINT8(4, g_fake.pwm_pin);
    TEST_ASSERT_EQUAL_UINT16(2048, g_fake.pwm_duty);
    TEST_ASSERT_EQUAL_UINT32(20000, g_fake.pwm_freq);
}

static void test_status_counts_frames_and_clears_loop_max(void)
{
    NgsFrame resp;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_PING, 1, NULL, 0, &resp));
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_GET_STATUS, 2, NULL, 0, &resp));

    NgsStatusPayload st;
    memcpy(&st, resp.payload, sizeof(st));
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(2, st.rx_frames, "ping + status");
    TEST_ASSERT_EQUAL_UINT32(0, st.rx_crc_errors);
    TEST_ASSERT_EQUAL_INT32(42500, st.temp_mc);

    /* loop_max_us is read-and-clear, so the next report starts from zero. */
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_GET_STATUS, 3, NULL, 0, &resp));
    memcpy(&st, resp.payload, sizeof(st));
    TEST_ASSERT_EQUAL_UINT32(0, st.loop_max_us);
}

static void test_bad_crc_frame_produces_error_response(void)
{
    uint8_t wire[NGS_FRAME_WIRE_MAX];
    size_t n = ngs_frame_encode(NGS_MSG_PING, 1, NULL, 0, wire, sizeof(wire));
    wire[1] ^= 0xFF;

    g_fake.tx_len = 0;
    fake_board_feed(wire, n);
    ngs_app_poll(&app);

    NgsFrame resp;
    TEST_ASSERT_TRUE(take_response(&resp));
    TEST_ASSERT_EQUAL_HEX8(NGS_MSG_ERROR, resp.type);
    TEST_ASSERT_EQUAL_UINT32(1, app.decoder.crc_errors);
}

/* ---- Streaming --------------------------------------------------------- */

static void test_stream_rejects_zero_period(void)
{
    NgsFrame resp;
    NgsStreamCfgPayload cfg = {};
    cfg.enable = 1;
    cfg.period_us = 0;
    cfg.channel_mask = 0x1;

    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_STREAM, 1, &cfg, sizeof(cfg), &resp));
    TEST_ASSERT_EQUAL_HEX8(NGS_MSG_ERROR, resp.type);
    TEST_ASSERT_FALSE(app.stream_enabled);
}

static void test_stream_rejects_nonexistent_channel(void)
{
    NgsFrame resp;
    NgsStreamCfgPayload cfg = {};
    cfg.enable = 1;
    cfg.period_us = 1000;
    cfg.channel_mask = 1u << (NGS_MAX_ADC_CHANNEL + 1u);

    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_STREAM, 2, &cfg, sizeof(cfg), &resp));
    TEST_ASSERT_EQUAL_HEX8(NGS_MSG_ERROR, resp.type);
}

static void test_stream_emits_one_record_per_period(void)
{
    NgsFrame resp;
    NgsStreamCfgPayload cfg = {};
    cfg.enable = 1;
    cfg.period_us = 1000;
    cfg.channel_mask = 0b1011; /* channels 0, 1, 3 */

    g_fake.micros = 10000;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_STREAM, 3, &cfg, sizeof(cfg), &resp));
    TEST_ASSERT_EQUAL_HEX8(NGS_MSG_SET_STREAM | NGS_MSG_RESP, resp.type);
    TEST_ASSERT_TRUE(app.stream_enabled);

    /* Not due yet. */
    g_fake.tx_len = 0;
    g_fake.micros = 10500;
    ngs_app_poll(&app);
    TEST_ASSERT_EQUAL_MESSAGE(0, g_fake.tx_len, "record emitted before its period elapsed");

    /* Now due. */
    g_fake.micros = 11000;
    ngs_app_poll(&app);
    TEST_ASSERT_TRUE(take_response(&resp));
    TEST_ASSERT_EQUAL_HEX8(NGS_MSG_TELEMETRY, resp.type);

    NgsTelemetryHeader hdr;
    memcpy(&hdr, resp.payload, sizeof(hdr));
    TEST_ASSERT_EQUAL_UINT8_MESSAGE(3, hdr.count, "one sample per set mask bit");
    TEST_ASSERT_EQUAL_UINT32(0b1011, hdr.channel_mask);
    TEST_ASSERT_EQUAL_UINT32(0, hdr.seq);
    TEST_ASSERT_EQUAL(sizeof(hdr) + 3u * sizeof(uint16_t), resp.len);
}

static void test_stream_resyncs_when_host_stalls(void)
{
    NgsFrame resp;
    NgsStreamCfgPayload cfg = {};
    cfg.enable = 1;
    cfg.period_us = 1000;
    cfg.channel_mask = 0x1;

    g_fake.micros = 1000;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_STREAM, 4, &cfg, sizeof(cfg), &resp));

    /* Jump far past several due times. The firmware must emit one record and
     * rebase, not queue up a backlog of stale ones. */
    g_fake.micros = 500000;
    g_fake.tx_len = 0;
    ngs_app_poll(&app);
    TEST_ASSERT_EQUAL_UINT32_MESSAGE(1, app.stream_seq, "should emit exactly one record");
    TEST_ASSERT_EQUAL_UINT32(501000, app.stream_next_us);
}

static void test_stream_disable_stops_records(void)
{
    NgsFrame resp;
    NgsStreamCfgPayload cfg = {};
    cfg.enable = 1;
    cfg.period_us = 1000;
    cfg.channel_mask = 0x1;
    g_fake.micros = 1000;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_STREAM, 5, &cfg, sizeof(cfg), &resp));

    cfg.enable = 0;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_STREAM, 6, &cfg, sizeof(cfg), &resp));
    TEST_ASSERT_FALSE(app.stream_enabled);

    g_fake.micros = 100000;
    g_fake.tx_len = 0;
    ngs_app_poll(&app);
    TEST_ASSERT_EQUAL_MESSAGE(0, g_fake.tx_len, "no records after disable");
}

static void test_blocked_tx_does_not_wedge_polling(void)
{
    /* If the host stops draining USB, writes fail. The firmware must keep
     * consuming input rather than spinning or blocking. */
    NgsStreamCfgPayload cfg = {};
    cfg.enable = 1;
    cfg.period_us = 100;
    cfg.channel_mask = 0x1;

    g_fake.micros = 1000;
    send_request(NGS_MSG_SET_STREAM, 7, &cfg, sizeof(cfg));
    ngs_app_poll(&app);

    g_fake.tx_blocked = true;
    for (uint32_t i = 0; i < 100; i++) {
        g_fake.micros += 100;
        ngs_app_poll(&app);
    }
    /* Records were attempted and dropped, and the loop is still healthy. */
    TEST_ASSERT_GREATER_THAN_UINT32(0, app.stream_seq);

    g_fake.tx_blocked = false;
    NgsFrame resp;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_PING, 8, NULL, 0, &resp));
    TEST_ASSERT_EQUAL_HEX8(NGS_MSG_PING | NGS_MSG_RESP, resp.type);
}

static void test_multiple_frames_in_one_poll(void)
{
    /* USB CDC delivers up to 512 bytes at a time, so several commands commonly
     * land in a single read. All of them must be served in one poll. */
    g_fake.tx_len = 0;
    for (uint8_t i = 1; i <= 4; i++) {
        send_request(NGS_MSG_PING, i, NULL, 0);
    }
    ngs_app_poll(&app);

    NgsDecoder dec;
    NgsFrame frame;
    ngs_decoder_init(&dec);
    uint8_t seen = 0;
    for (size_t i = 0; i < g_fake.tx_len; i++) {
        if (ngs_decoder_push(&dec, g_fake.tx[i], &frame) == NGS_DECODE_FRAME) {
            seen++;
            TEST_ASSERT_EQUAL_HEX8(NGS_MSG_PING | NGS_MSG_RESP, frame.type);
            TEST_ASSERT_EQUAL_HEX8(seen, frame.seq);
        }
    }
    TEST_ASSERT_EQUAL_UINT8(4, seen);
}

/* ---- Runner ------------------------------------------------------------ */

void setup()
{
    /* Give the host's serial monitor time to attach before Unity starts
     * printing, otherwise the first results scroll past unseen. */
    delay(2000);

    UNITY_BEGIN();

    RUN_TEST(test_crc16_known_vector);
    RUN_TEST(test_crc16_empty_is_init);

    RUN_TEST(test_cobs_round_trip_with_zeros);
    RUN_TEST(test_cobs_round_trip_254_run);
    RUN_TEST(test_cobs_decode_rejects_embedded_zero);
    RUN_TEST(test_cobs_decode_rejects_run_past_end);

    RUN_TEST(test_frame_round_trip);
    RUN_TEST(test_frame_encode_rejects_oversize_payload);
    RUN_TEST(test_frame_max_payload_fits_wire_buffer);
    RUN_TEST(test_decoder_detects_corrupted_byte);
    RUN_TEST(test_decoder_resyncs_after_garbage);
    RUN_TEST(test_decoder_ignores_repeated_delimiters);
    RUN_TEST(test_decoder_reports_overflow_once);

    RUN_TEST(test_payload_sizes_match_host);
    RUN_TEST(test_wire_is_little_endian);

    RUN_TEST(test_ping_returns_uptime);
    RUN_TEST(test_get_info_reports_protocol_version);
    RUN_TEST(test_unknown_type_returns_error);
    RUN_TEST(test_wrong_payload_size_returns_error);
    RUN_TEST(test_set_gpio_drives_pin);
    RUN_TEST(test_set_gpio_rejects_out_of_range_pin);
    RUN_TEST(test_read_adc_echoes_request_and_averages);
    RUN_TEST(test_read_adc_rejects_bad_channel);
    RUN_TEST(test_write_pwm_applies_settings);
    RUN_TEST(test_status_counts_frames_and_clears_loop_max);
    RUN_TEST(test_bad_crc_frame_produces_error_response);

    RUN_TEST(test_stream_rejects_zero_period);
    RUN_TEST(test_stream_rejects_nonexistent_channel);
    RUN_TEST(test_stream_emits_one_record_per_period);
    RUN_TEST(test_stream_resyncs_when_host_stalls);
    RUN_TEST(test_stream_disable_stops_records);
    RUN_TEST(test_blocked_tx_does_not_wedge_polling);
    RUN_TEST(test_multiple_frames_in_one_poll);

    UNITY_END();
}

void loop() {}
