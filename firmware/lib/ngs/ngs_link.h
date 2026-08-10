/*
 * ngs_link.h - COBS framing + CRC-16 for the NGS wire protocol.
 *
 * Pure C, no allocation, no I/O. The encoder renders a frame into a caller
 * supplied buffer; the decoder is a byte-at-a-time state machine so it can be
 * driven straight from a USB CDC read loop without buffering a whole frame
 * first. Both are transport agnostic -- see ngs_board.h for the I/O seam.
 *
 * Wire format is documented in ngs_protocol.h.
 */

#ifndef NGS_LINK_H
#define NGS_LINK_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "ngs_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Bytes on the wire before COBS: type, seq, len(2), payload, crc(2). */
#define NGS_HEADER_SIZE 4u
#define NGS_CRC_SIZE 2u
#define NGS_FRAME_RAW_MAX (NGS_HEADER_SIZE + NGS_MAX_PAYLOAD + NGS_CRC_SIZE)

/* COBS adds one code byte per 254-byte run, plus one to open the first run.
 * The +2 is headroom so an off-by-one here can never overrun a buffer. */
#define NGS_COBS_MAX_ENCODED(n) ((n) + ((n) / 254u) + 2u)

/* Encoded frame plus its trailing 0x00 delimiter. Size your tx buffer to this. */
#define NGS_FRAME_WIRE_MAX (NGS_COBS_MAX_ENCODED(NGS_FRAME_RAW_MAX) + 1u)

/* Returned by the COBS helpers when the input is not valid COBS or the
 * destination is too small. Chosen so it can never be a real length. */
#define NGS_COBS_ERROR ((size_t)-1)

/* ---------------------------------------------------------------------------
 * CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no final XOR.
 * ------------------------------------------------------------------------- */
uint16_t ngs_crc16_update(uint16_t crc, const uint8_t *data, size_t len);
uint16_t ngs_crc16(const uint8_t *data, size_t len);

/* ---------------------------------------------------------------------------
 * COBS. `dst` must hold NGS_COBS_MAX_ENCODED(len) bytes for encode.
 * Both return the number of bytes written, or NGS_COBS_ERROR.
 * ------------------------------------------------------------------------- */
size_t ngs_cobs_encode(const uint8_t *src, size_t len, uint8_t *dst, size_t dst_cap);
size_t ngs_cobs_decode(const uint8_t *src, size_t len, uint8_t *dst, size_t dst_cap);

/* ---------------------------------------------------------------------------
 * Encoding
 *
 * Renders one complete frame -- COBS body plus trailing delimiter -- into
 * `out`. Returns bytes written, or 0 if `len` exceeds NGS_MAX_PAYLOAD or `out`
 * is smaller than NGS_FRAME_WIRE_MAX would require. `payload` may be NULL when
 * `len` is 0.
 * ------------------------------------------------------------------------- */
size_t ngs_frame_encode(uint8_t type, uint8_t seq, const void *payload, uint16_t len,
                        uint8_t *out, size_t out_cap);

/* ---------------------------------------------------------------------------
 * Decoding
 * ------------------------------------------------------------------------- */

/* A decoded frame. `payload` points into the decoder's own buffer and stays
 * valid only until the next ngs_decoder_push() call -- copy anything you need
 * to keep. */
typedef struct {
    uint8_t type;
    uint8_t seq;
    uint16_t len;
    const uint8_t *payload;
} NgsFrame;

typedef struct {
    /* Accumulates the COBS-encoded body between delimiters. */
    uint8_t enc[NGS_COBS_MAX_ENCODED(NGS_FRAME_RAW_MAX)];
    size_t enc_len;
    /* Receives the COBS-decoded body. */
    uint8_t raw[NGS_FRAME_RAW_MAX];
    /* Set when the current frame already exceeded `enc`; we keep consuming
     * bytes until the next delimiter, then report a single overflow rather
     * than emitting a burst of garbage frames. */
    bool overrun;
    uint8_t last_error; /* NGS_ERR_* for the most recent NGS_DECODE_ERROR */

    /* Counters surfaced by NGS_MSG_GET_STATUS. */
    uint32_t frames;
    uint32_t crc_errors;
    uint32_t overflows;
} NgsDecoder;

#define NGS_DECODE_MORE 0  /* need more bytes                              */
#define NGS_DECODE_FRAME 1 /* *frame is valid                              */
#define NGS_DECODE_ERROR 2 /* frame rejected; see decoder->last_error      */

void ngs_decoder_init(NgsDecoder *d);

/* Feed one received byte. A 0x00 terminates the current frame; runs of 0x00
 * and leading 0x00s are ignored, so the decoder resynchronises on its own
 * after a dropped byte or a device reset mid-frame. */
int ngs_decoder_push(NgsDecoder *d, uint8_t byte, NgsFrame *frame);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* NGS_LINK_H */
