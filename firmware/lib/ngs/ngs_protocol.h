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

/* Bumped on any incompatible change to framing, message ids, or struct layout.
 *
 * v2 added the closed-loop pump controller (0x30..0x33) and its payloads. */
#define NGS_PROTO_VERSION 2

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

/* Closed-loop pump control. The loop runs on the device at a fixed period --
 * a control loop paced by USB round trips is at the mercy of host scheduling,
 * which is exactly the jitter a controller must not have. */
#define NGS_MSG_SET_CONTROL 0x30u /* -> NgsControlCfgPayload  <- (empty)      */
#define NGS_MSG_GET_CONTROL 0x31u /* -> (empty) <- NgsControlStatePayload     */
#define NGS_MSG_AUTOTUNE 0x32u    /* -> NgsAutotuneCmdPayload <- (empty)      */
#define NGS_MSG_GET_AUTOTUNE 0x33u /* -> (empty) <- NgsAutotuneResultPayload  */

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

/* ---------------------------------------------------------------------------
 * Closed-loop pump control
 *
 * Floats on the wire, IEEE-754 little-endian: the Cortex-M7 has an FPU and
 * Python's struct speaks the same format, so fixed-point would buy nothing but
 * a class of scaling bugs.
 *
 * The device works in engineering units (mL/min), not ADC counts, so setpoint,
 * gains and autotune results all mean something to an operator. It gets there
 * with the linear calibration the host supplies below -- the calibration still
 * lives in host config, under version control; the device is just told the two
 * numbers it needs to apply it.
 * ------------------------------------------------------------------------- */

#define NGS_PUMP_MODE_MANUAL 0x00u   /* output follows NGS_MSG_WRITE_PWM      */
#define NGS_PUMP_MODE_AUTO 0x01u     /* output follows the controller         */
#define NGS_PUMP_MODE_AUTOTUNE 0x02u /* relay experiment in progress          */

/* NgsControlStatePayload.flags */
#define NGS_CTRL_FLAG_SATURATED 0x01u /* output pinned at a limit             */
#define NGS_CTRL_FLAG_WINDUP 0x02u    /* integration held off this tick       */
#define NGS_CTRL_FLAG_FAULT 0x04u     /* sensor out of range; loop dropped out */
#define NGS_CTRL_FLAG_SLEWING 0x08u   /* setpoint still ramping to target     */

/* NGS_MSG_SET_CONTROL request. Sent whole: partial updates would need a
 * field mask, and re-sending eleven floats costs nothing at these rates. */
/* NgsControlCfgPayload.options */
#define NGS_CTRL_OPT_FAULT_CHECK 0x01u /* enable the `fault_below` trip       */

typedef struct NGS_PACKED {
    uint8_t mode;    /* NGS_PUMP_MODE_MANUAL or _AUTO                        */
    uint8_t channel; /* ADC channel the loop measures                        */
    uint8_t options; /* NGS_CTRL_OPT_*                                       */
    uint8_t _pad;
    float setpoint;     /* engineering units                                 */
    float kp;           /* % output per unit of error                        */
    float ki;           /* % per unit-second                                 */
    float kd;           /* % per (unit/second); 0 disables derivative        */
    float out_min;      /* % -- also the floor the integrator may wind to    */
    float out_max;      /* %                                                 */
    float filter_tau_s; /* measurement low-pass; 0 disables                  */
    float deadband;     /* units; no integration while |error| is under it   */
    float setpoint_slew; /* units/s, 0 = step immediately                    */
    float output_slew;  /* %/s, 0 = unlimited                                */
    float cal_scale;    /* units per ADC count                               */
    float cal_offset;   /* ADC counts reading zero units                     */
    /* Units; below this the sensor is considered dead. Signed and enabled by
     * a flag rather than by being non-zero: on a 4-20 mA loop the meaningful
     * threshold is *negative* (under 4 mA reads below zero flow), so "0 means
     * off" would rule out exactly the value you want. */
    float fault_below;
    uint32_t period_us; /* control interval                                  */
} NgsControlCfgPayload;

/* NGS_MSG_GET_CONTROL response. Everything needed to draw the loop and to
 * explain what it is doing -- the split P/I/D terms are what turn "the output
 * is 40 %" into "the integrator is carrying it". */
typedef struct NGS_PACKED {
    uint8_t mode;
    uint8_t flags;          /* NGS_CTRL_FLAG_*                               */
    uint8_t autotune_state; /* NGS_AT_*                                      */
    uint8_t _pad;
    float setpoint;         /* the slew-limited setpoint actually in use     */
    float setpoint_target;  /* what was last commanded                       */
    float measurement;      /* filtered                                      */
    float measurement_raw;  /* same sample, unfiltered                       */
    float output;           /* % duty currently applied                      */
    float p_term;
    float i_term;
    float d_term;
    uint32_t updates;       /* control ticks since the loop was enabled      */
    uint32_t fault_count;   /* times the loop dropped out on a sensor fault  */
} NgsControlStatePayload;

/* NGS_MSG_AUTOTUNE request.
 *
 * Relay feedback (Astrom-Hagglund): drive the output between two levels around
 * an operating point and let the process oscillate. The amplitude and period
 * of that limit cycle give the ultimate gain and period directly, without
 * needing a model and without ever pushing the loop unstable on purpose. */
#define NGS_AT_ACTION_ABORT 0x00u
#define NGS_AT_ACTION_START 0x01u

/* Tuning rule applied to the measured Ku/Tu. */
#define NGS_AT_RULE_TYREUS_LUYBEN 0x00u /* conservative; the default         */
#define NGS_AT_RULE_ZIEGLER_NICHOLS 0x01u
#define NGS_AT_RULE_PESSEN 0x02u /* faster, less damped                      */

typedef struct NGS_PACKED {
    uint8_t action; /* NGS_AT_ACTION_*                                       */
    uint8_t cycles; /* limit cycles to average before deciding               */
    uint8_t rule;   /* NGS_AT_RULE_*                                         */
    uint8_t _pad;
    float setpoint;   /* operating point to oscillate about                  */
    float amplitude;  /* relay step, % output, either side of the bias       */
    float hysteresis; /* units; must clear the sensor noise or the relay will
                       * chatter on noise instead of on the process          */
    uint32_t timeout_ms;
} NgsAutotuneCmdPayload;

#define NGS_AT_IDLE 0x00u
#define NGS_AT_SETTLING 0x01u
#define NGS_AT_RELAY 0x02u
#define NGS_AT_DONE 0x03u
#define NGS_AT_FAILED 0x04u

/* Why an autotune stopped short. */
#define NGS_AT_FAIL_NONE 0x00u
#define NGS_AT_FAIL_TIMEOUT 0x01u    /* never completed enough cycles        */
#define NGS_AT_FAIL_NO_SWING 0x02u   /* amplitude under the hysteresis band  */
#define NGS_AT_FAIL_SENSOR 0x03u     /* sensor faulted mid-experiment        */
#define NGS_AT_FAIL_ABORTED 0x04u    /* operator stopped it                  */
#define NGS_AT_FAIL_INCONSISTENT 0x05u /* cycle periods too scattered to use */

typedef struct NGS_PACKED {
    uint8_t state;       /* NGS_AT_*                                         */
    uint8_t fail_reason; /* NGS_AT_FAIL_*                                    */
    uint8_t cycles_done;
    uint8_t rule;
    float ku;       /* ultimate gain, from the limit cycle                   */
    float tu;       /* ultimate period, seconds                              */
    float amplitude; /* measured process swing, units peak-to-peak           */
    float kp;       /* suggested gains, from `rule`                          */
    float ki;
    float kd;
    float spread;   /* worst period deviation, as a fraction -- how much to
                     * trust the numbers above                               */
} NgsAutotuneResultPayload;

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
