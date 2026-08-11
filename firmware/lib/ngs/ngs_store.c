/*
 * ngs_store.c - The control configuration in NVM. See ngs_store.h.
 */

#include "ngs_store.h"

#include <string.h>

#include "ngs_board.h"
#include "ngs_link.h" /* ngs_crc16 */

/* Little-endian accessors. The Cortex-M7 is little-endian and so is the wire
 * format, but reading a stored block byte by byte keeps this independent of
 * both -- an NVM image is the one thing that outlives a firmware change. */
static uint32_t get_u32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

static uint16_t get_u16(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

static void put_u32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)((v >> 8) & 0xFFu);
    p[2] = (uint8_t)((v >> 16) & 0xFFu);
    p[3] = (uint8_t)((v >> 24) & 0xFFu);
}

static void put_u16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)((v >> 8) & 0xFFu);
}

bool ngs_store_load(NgsControlCfgPayload *out)
{
    uint8_t buf[NGS_STORE_SIZE];

    if (!ngs_board_nvm_read(NGS_STORE_OFFSET, buf, sizeof(buf))) {
        return false;
    }

    if (get_u32(&buf[0]) != NGS_STORE_MAGIC) {
        return false; /* blank, or something else entirely */
    }
    if (get_u16(&buf[4]) != NGS_STORE_VERSION) {
        return false; /* written by a firmware with a different layout */
    }

    uint16_t length = get_u16(&buf[6]);
    if (length != (uint16_t)sizeof(NgsControlCfgPayload)) {
        return false;
    }

    uint16_t stored_crc = get_u16(&buf[8]);
    if (stored_crc != ngs_crc16(&buf[NGS_STORE_HEADER_SIZE], length)) {
        /* Half-written, or the flash has decayed. Defaults are better than a
         * configuration made of two different ones. */
        return false;
    }

    memcpy(out, &buf[NGS_STORE_HEADER_SIZE], sizeof(*out));
    return true;
}

bool ngs_store_save(const NgsControlCfgPayload *cfg)
{
    uint8_t buf[NGS_STORE_SIZE];
    NgsControlCfgPayload safe = *cfg;

    /* Never store AUTO. Powering up already driving a pump, because that is
     * what it was doing when it was saved, is not a thing this bench does. */
    safe.mode = NGS_PUMP_MODE_MANUAL;
    safe.setpoint = 0.0f;

    memcpy(&buf[NGS_STORE_HEADER_SIZE], &safe, sizeof(safe));

    put_u32(&buf[0], NGS_STORE_MAGIC);
    put_u16(&buf[4], NGS_STORE_VERSION);
    put_u16(&buf[6], (uint16_t)sizeof(safe));
    put_u16(&buf[8], ngs_crc16(&buf[NGS_STORE_HEADER_SIZE], (uint16_t)sizeof(safe)));
    put_u16(&buf[10], 0);

    return ngs_board_nvm_write(NGS_STORE_OFFSET, buf, sizeof(buf));
}

bool ngs_store_erase(void)
{
    /* Only the magic is cleared. Erasing all 68 bytes would cost the same
     * number of flash writes for no benefit -- a block whose magic does not
     * match is already indistinguishable from a blank one. */
    uint8_t zeros[4] = {0, 0, 0, 0};
    return ngs_board_nvm_write(NGS_STORE_OFFSET, zeros, sizeof(zeros));
}
