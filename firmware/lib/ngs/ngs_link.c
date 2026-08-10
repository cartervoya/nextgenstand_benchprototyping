/*
 * ngs_link.c - COBS framing + CRC-16 implementation. See ngs_link.h.
 */

#include "ngs_link.h"

#include <string.h>

/* --------------------------------------------------------------------------
 * CRC-16/CCITT-FALSE
 *
 * Bitwise rather than table-driven: at USB CDC frame rates this is nowhere
 * near the bottleneck, and it keeps 512 bytes of flash free for a table we
 * would only need if we started CRCing megabytes.
 * ------------------------------------------------------------------------ */
uint16_t ngs_crc16_update(uint16_t crc, const uint8_t *data, size_t len)
{
    for (size_t i = 0; i < len; i++) {
        crc ^= (uint16_t)data[i] << 8;
        for (uint8_t bit = 0; bit < 8; bit++) {
            crc = (crc & 0x8000u) ? (uint16_t)((crc << 1) ^ 0x1021u) : (uint16_t)(crc << 1);
        }
    }
    return crc;
}

uint16_t ngs_crc16(const uint8_t *data, size_t len)
{
    return ngs_crc16_update(0xFFFFu, data, len);
}

/* --------------------------------------------------------------------------
 * COBS
 * ------------------------------------------------------------------------ */
size_t ngs_cobs_encode(const uint8_t *src, size_t len, uint8_t *dst, size_t dst_cap)
{
    if (dst_cap < NGS_COBS_MAX_ENCODED(len)) {
        return NGS_COBS_ERROR;
    }

    size_t read = 0;
    size_t code_idx = 0; /* slot reserved for the current run's length */
    size_t write = 1;    /* first payload byte goes after that slot     */
    uint8_t code = 1;

    while (read < len) {
        uint8_t b = src[read++];
        if (b != 0) {
            dst[write++] = b;
            code++;
        }
        /* A zero ends the run (it is implied by the code byte), and a full
         * 254-byte run has to be split because the code byte caps at 0xFF. */
        if (b == 0 || code == 0xFFu) {
            dst[code_idx] = code;
            code_idx = write++;
            code = 1;
        }
    }

    dst[code_idx] = code;
    return write;
}

size_t ngs_cobs_decode(const uint8_t *src, size_t len, uint8_t *dst, size_t dst_cap)
{
    size_t read = 0;
    size_t write = 0;

    while (read < len) {
        uint8_t code = src[read++];
        if (code == 0) {
            return NGS_COBS_ERROR; /* 0x00 cannot appear inside a COBS body */
        }

        size_t run = (size_t)code - 1u;
        if (read + run > len || write + run > dst_cap) {
            return NGS_COBS_ERROR;
        }

        memcpy(&dst[write], &src[read], run);
        read += run;
        write += run;

        /* A code < 0xFF means the run was terminated by a zero -- unless we
         * just consumed the last byte of input, where the zero was the frame
         * delimiter and is not part of the data. */
        if (code != 0xFFu && read < len) {
            if (write >= dst_cap) {
                return NGS_COBS_ERROR;
            }
            dst[write++] = 0;
        }
    }

    return write;
}

/* --------------------------------------------------------------------------
 * Encoding
 * ------------------------------------------------------------------------ */
size_t ngs_frame_encode(uint8_t type, uint8_t seq, const void *payload, uint16_t len,
                        uint8_t *out, size_t out_cap)
{
    if (len > NGS_MAX_PAYLOAD) {
        return 0;
    }

    uint8_t raw[NGS_FRAME_RAW_MAX];
    size_t n = 0;

    raw[n++] = type;
    raw[n++] = seq;
    raw[n++] = (uint8_t)(len & 0xFFu); /* little-endian, matches the host */
    raw[n++] = (uint8_t)(len >> 8);
    if (len > 0) {
        memcpy(&raw[n], payload, len);
        n += len;
    }

    uint16_t crc = ngs_crc16(raw, n);
    raw[n++] = (uint8_t)(crc & 0xFFu);
    raw[n++] = (uint8_t)(crc >> 8);

    /* Reserve one byte for the trailing delimiter. */
    if (out_cap == 0) {
        return 0;
    }
    size_t enc = ngs_cobs_encode(raw, n, out, out_cap - 1u);
    if (enc == NGS_COBS_ERROR) {
        return 0;
    }

    out[enc] = 0x00;
    return enc + 1u;
}

/* --------------------------------------------------------------------------
 * Decoding
 * ------------------------------------------------------------------------ */
void ngs_decoder_init(NgsDecoder *d)
{
    memset(d, 0, sizeof(*d));
}

int ngs_decoder_push(NgsDecoder *d, uint8_t byte, NgsFrame *frame)
{
    if (byte != 0x00) {
        if (d->enc_len < sizeof(d->enc)) {
            d->enc[d->enc_len++] = byte;
        } else {
            /* Latch the overrun and keep draining to the next delimiter. */
            d->overrun = true;
        }
        return NGS_DECODE_MORE;
    }

    /* byte == 0x00: end of frame. */
    size_t enc_len = d->enc_len;
    bool overrun = d->overrun;
    d->enc_len = 0;
    d->overrun = false;

    if (overrun) {
        d->overflows++;
        d->last_error = NGS_ERR_OVERFLOW;
        return NGS_DECODE_ERROR;
    }

    /* Runs of delimiters, or a leading one after a reset. Not an error. */
    if (enc_len == 0) {
        return NGS_DECODE_MORE;
    }

    size_t raw_len = ngs_cobs_decode(d->enc, enc_len, d->raw, sizeof(d->raw));
    if (raw_len == NGS_COBS_ERROR || raw_len < NGS_HEADER_SIZE + NGS_CRC_SIZE) {
        d->crc_errors++;
        d->last_error = NGS_ERR_BAD_LENGTH;
        return NGS_DECODE_ERROR;
    }

    size_t body_len = raw_len - NGS_CRC_SIZE;
    uint16_t got = (uint16_t)d->raw[body_len] | (uint16_t)((uint16_t)d->raw[body_len + 1] << 8);
    if (got != ngs_crc16(d->raw, body_len)) {
        d->crc_errors++;
        d->last_error = NGS_ERR_BAD_CRC;
        return NGS_DECODE_ERROR;
    }

    uint16_t declared = (uint16_t)d->raw[2] | (uint16_t)((uint16_t)d->raw[3] << 8);
    if (declared != body_len - NGS_HEADER_SIZE) {
        /* CRC passed, so this is a genuine sender bug rather than corruption. */
        d->last_error = NGS_ERR_BAD_LENGTH;
        return NGS_DECODE_ERROR;
    }

    frame->type = d->raw[0];
    frame->seq = d->raw[1];
    frame->len = declared;
    frame->payload = &d->raw[NGS_HEADER_SIZE];

    d->frames++;
    return NGS_DECODE_FRAME;
}
