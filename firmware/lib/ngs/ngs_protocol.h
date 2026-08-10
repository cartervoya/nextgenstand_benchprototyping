/*
 * ngs_protocol.h - Wire protocol shared between the Teensy 4.1 firmware and the
 *                  Python host (host/ngs_host/protocol.py).
 *
 * THIS FILE IS THE SINGLE SOURCE OF TRUTH.
 *
 * host/ngs_host/protocol.py mirrors every NGS_MSG_* / NGS_ERR_* constant and
 * every struct layout below. host/tests/test_protocol_sync.py parses this
 * header and fails if the two ever drift, so update both sides together.
 *
 * Framing (see ngs_link.h):
 *
 *     0x00 <COBS( type seq len payload... crc16 )> 0x00
 *
 *   type    u8   message type; a response is the request type | NGS_MSG_RESP
 *   seq     u8   host-chosen sequence number, echoed in the response
 *   len     u16  payload length in bytes, little-endian
 *   payload      len bytes
 *   crc16   u16  CRC-16/CCITT-FALSE over type,seq,len,payload; little-endian
 *
 * COBS removes every 0x00 from the encoded body, so a lone 0x00 is an
 * unambiguous frame delimiter and a receiver can always resynchronise after a
 * dropped byte or a mid-stream reset.
 */

#ifndef NGS_PROTOCOL_H
#define NGS_PROTOCOL_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Bumped on any incompatible change to framing, message ids, or struct layout. */
#define NGS_PROTO_VERSION 1

/* Largest payload either side will emit or accept. Keeps every buffer static. */
#define NGS_MAX_PAYLOAD 512u

/* ---------------------------------------------------------------------------
 * Message types
 *
 * Requests are 0x01..0x7F. The device answers a request with the same type
 * OR'd with NGS_MSG_RESP, or with NGS_MSG_ERROR if it could not be served.
 * 0xF0..0xFD are unsolicited device-initiated messages.
 * ------------------------------------------------------------------------- */
#define NGS_MSG_RESP 0x80u /* response flag OR'd onto the request type */

#define NGS_MSG_PING 0x01u       /* -> (empty)            <- NgsPongPayload   */
#define NGS_MSG_GET_INFO 0x02u   /* -> (empty)            <- NgsInfoPayload   */
#define NGS_MSG_GET_STATUS 0x03u /* -> (empty)            <- NgsStatusPayload */
#define NGS_MSG_RESET 0x04u      /* -> (empty)            <- (empty), then reboot */

#define NGS_MSG_SET_GPIO 0x10u  /* -> NgsGpioSetPayload  <- (empty)          */
#define NGS_MSG_GET_GPIO 0x11u  /* -> NgsGpioGetPayload  <- NgsGpioGetPayload */
#define NGS_MSG_READ_ADC 0x12u  /* -> NgsAdcReadPayload  <- NgsAdcReadPayload */
#define NGS_MSG_WRITE_PWM 0x13u /* -> NgsPwmWritePayload <- (empty)          */

#define NGS_MSG_SET_STREAM 0x20u /* -> NgsStreamCfgPayload <- (empty)        */

#define NGS_MSG_LOG 0xF0u       /* <- ASCII text, no NUL terminator          */
#define NGS_MSG_TELEMETRY 0xF1u /* <- NgsTelemetryPayload                    */
#define NGS_MSG_ERROR 0xFEu     /* <- NgsErrorPayload                        */

/* ---------------------------------------------------------------------------
 * Error codes (payload of NGS_MSG_ERROR)
 * ------------------------------------------------------------------------- */
#define NGS_ERR_NONE 0x00u
#define NGS_ERR_BAD_CRC 0x01u       /* CRC mismatch on a received frame      */
#define NGS_ERR_BAD_LENGTH 0x02u    /* len field disagrees with frame size   */
#define NGS_ERR_UNKNOWN_TYPE 0x03u  /* no handler for this message type      */
#define NGS_ERR_BAD_PAYLOAD 0x04u   /* payload wrong size for this type      */
#define NGS_ERR_BAD_ARGUMENT 0x05u  /* argument out of range (pin, channel)  */
#define NGS_ERR_OVERFLOW 0x06u      /* frame exceeded NGS_MAX_PAYLOAD        */
#define NGS_ERR_NOT_SUPPORTED 0x07u /* valid request, unimplemented here     */
#define NGS_ERR_BUSY 0x08u          /* transient; retry is reasonable        */

/* ---------------------------------------------------------------------------
 * Payload layouts
 *
 * Every struct is explicitly packed and uses only fixed-width types, so the
 * Python `struct` format strings in protocol.py describe them exactly. Keep
 * fields naturally aligned anyway -- Cortex-M7 tolerates unaligned access but
 * it is slower, and packed structs make the intent unambiguous.
 * ------------------------------------------------------------------------- */

#define NGS_PACKED __attribute__((packed))

/* NGS_MSG_PING response. Lets the host measure round-trip latency against the
 * device's own clock without assuming the two are synchronised. */
typedef struct NGS_PACKED {
    uint32_t uptime_us; /* microseconds since boot, wraps every ~71 min */
} NgsPongPayload;

/* NGS_MSG_GET_INFO response. Static identity of the running firmware. */
typedef struct NGS_PACKED {
    uint8_t proto_version; /* == NGS_PROTO_VERSION                        */
    uint8_t fw_major;
    uint8_t fw_minor;
    uint8_t fw_patch;
    uint32_t cpu_hz;      /* F_CPU as actually configured                */
    uint32_t max_payload; /* == NGS_MAX_PAYLOAD                          */
    uint8_t mcu_serial[8]; /* i.MXRT unique id, zero-padded              */
} NgsInfoPayload;

/* NGS_MSG_GET_STATUS response. Cheap health counters -- the first thing to
 * look at when the link misbehaves. */
typedef struct NGS_PACKED {
    uint32_t uptime_us;
    uint32_t rx_frames;    /* frames accepted                             */
    uint32_t tx_frames;    /* frames emitted                              */
    uint32_t rx_crc_errors;
    uint32_t rx_overflows; /* frames dropped for exceeding NGS_MAX_PAYLOAD */
    uint32_t loop_max_us;  /* longest loop() iteration since last status   */
    int32_t temp_mc;       /* die temperature, milli-degrees C             */
} NgsStatusPayload;

/* NGS_MSG_SET_GPIO request. mode selects the pinMode applied before writing. */
typedef struct NGS_PACKED {
    uint8_t pin;
    uint8_t value; /* 0 = LOW, non-zero = HIGH                             */
    uint8_t mode;  /* NGS_PIN_MODE_*                                       */
    uint8_t _pad;
} NgsGpioSetPayload;

#define NGS_PIN_MODE_OUTPUT 0x00u
#define NGS_PIN_MODE_INPUT 0x01u
#define NGS_PIN_MODE_INPUT_PULLUP 0x02u
#define NGS_PIN_MODE_INPUT_PULLDOWN 0x03u

/* NGS_MSG_GET_GPIO. Request sets pin and mode; the response echoes both and
 * fills in value. */
typedef struct NGS_PACKED {
    uint8_t pin;
    uint8_t value;
    uint8_t mode;
    uint8_t _pad;
} NgsGpioGetPayload;

/* NGS_MSG_READ_ADC. Request sets channel and samples; the response echoes
 * both and fills in the averaged raw reading.
 *
 * `samples` averages in firmware to cut noise without paying a round trip per
 * sample. 0 is treated as 1. Raw counts, not volts -- scaling is the host's
 * job so the calibration lives in version control with the analysis code. */
typedef struct NGS_PACKED {
    uint8_t channel;  /* Teensy analog pin index, A0 == 0                  */
    uint8_t samples;  /* 1..255 averaged in firmware                       */
    uint16_t raw;     /* 0..(2^resolution - 1), response only              */
    uint8_t resolution; /* bits, response only                             */
    uint8_t _pad[3];
} NgsAdcReadPayload;

/* NGS_MSG_WRITE_PWM request. */
typedef struct NGS_PACKED {
    uint8_t pin;
    uint8_t _pad;
    uint16_t duty;      /* 0..(2^resolution - 1)                           */
    uint32_t freq_hz;   /* 0 leaves the current frequency alone            */
    uint8_t resolution; /* bits; 0 leaves the current resolution alone     */
    uint8_t _pad2[3];
} NgsPwmWritePayload;

/* NGS_MSG_SET_STREAM request. Turns periodic unsolicited NGS_MSG_TELEMETRY on
 * or off. period_us of 0 with enable=1 is rejected as NGS_ERR_BAD_ARGUMENT. */
typedef struct NGS_PACKED {
    uint8_t enable;
    uint8_t _pad[3];
    uint32_t period_us;
    uint32_t channel_mask; /* bit N = include analog channel N             */
} NgsStreamCfgPayload;

/* NGS_MSG_TELEMETRY, sent unsolicited while streaming is enabled.
 *
 * A header followed by `count` uint16 raw samples, one per set bit in the
 * configured channel_mask, low bit first. `seq` here is the telemetry record
 * counter -- it is independent of the frame header's seq and lets the host
 * detect gaps if it cannot keep up with the stream. */
typedef struct NGS_PACKED {
    uint32_t timestamp_us;
    uint32_t seq;
    uint32_t channel_mask;
    uint8_t count;
    uint8_t resolution;
    uint8_t _pad[2];
    /* uint16_t samples[count] follows */
} NgsTelemetryHeader;

/* NGS_MSG_ERROR payload. `seq` and `type` identify the request that failed,
 * so a host with several requests in flight can attribute the failure. */
typedef struct NGS_PACKED {
    uint8_t code; /* NGS_ERR_*                                             */
    uint8_t seq;  /* seq of the offending request, 0 if unknown            */
    uint8_t type; /* type of the offending request, 0 if unknown           */
    uint8_t _pad;
} NgsErrorPayload;

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* NGS_PROTOCOL_H */
