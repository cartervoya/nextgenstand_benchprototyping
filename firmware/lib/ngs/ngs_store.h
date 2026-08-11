/*
 * ngs_store.h - The control configuration in non-volatile memory.
 *
 * The board is the authority on its own tuning. Gains only mean anything
 * against a particular pump, line and flow meter, so they belong with the
 * board that drives them rather than with whichever PC happens to be plugged
 * in: a fresh checkout, a different laptop, or no host config at all still
 * gets the tuning the rig was actually set up with.
 *
 * Storage goes through ngs_board_nvm_*, so this file stays hardware-free and
 * the tests can drive it against a fake. On the Teensy 4.1 that lands in
 * Teensyduino's EEPROM emulation -- 4284 bytes carved out of the program
 * flash, wear-levelled across 63 sectors. A stored config is 68 bytes of it.
 *
 * What is written is a header plus the payload:
 *
 *     magic  u32   'NGSC', so a blank or foreign region is not read as config
 *     version u16  bumped when the payload layout changes
 *     length  u16  payload bytes that follow
 *     crc16   u16  over the payload
 *     _pad    u16
 *     payload      NgsControlCfgPayload
 *
 * The CRC is the point of the exercise. Flash writes are not atomic and a
 * brownout mid-write leaves a half-updated block; without the check the loop
 * would come up with a plausible-looking configuration made of two different
 * ones, which is far worse than coming up with none.
 */

#ifndef NGS_STORE_H
#define NGS_STORE_H

#include <stdbool.h>
#include <stdint.h>

#include "ngs_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

#define NGS_STORE_MAGIC 0x4353474EuL /* 'NGSC' little-endian */

/* Bumped when NgsControlCfgPayload changes shape. An older stored config is
 * then ignored rather than misread -- the operator retunes, which takes two
 * minutes, instead of running gains reinterpreted through the wrong layout. */
#define NGS_STORE_VERSION 1u

#define NGS_STORE_HEADER_SIZE 12u
#define NGS_STORE_SIZE (NGS_STORE_HEADER_SIZE + sizeof(NgsControlCfgPayload))

/* Where in NVM the block lives. Offset 0; nothing else is stored yet. */
#define NGS_STORE_OFFSET 0u

/* Reads the stored configuration into `out`.
 *
 * Returns true only for a block that is present, the right version, and
 * intact. Every other case -- blank NVM, an older version, a failed CRC --
 * returns false and leaves `out` untouched, so the caller keeps its defaults.
 */
bool ngs_store_load(NgsControlCfgPayload *out);

/* Writes `cfg`. Returns false if the NVM rejected the write.
 *
 * The mode is forced to MANUAL on the way in: a board that powered up already
 * driving a pump, because that is what it was doing when it was last saved,
 * is not a thing this bench should be able to do.
 */
bool ngs_store_save(const NgsControlCfgPayload *cfg);

/* Invalidates the stored block, so the next load falls back to defaults. */
bool ngs_store_erase(void);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* NGS_STORE_H */
