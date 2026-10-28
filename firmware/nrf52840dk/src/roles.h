/*
 * Role firmware for the harness's nRF52840 DK test devices.
 *
 * Copyright (c) 2026 The Ubertooth test harness authors
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef UTDK_ROLES_H_
#define UTDK_ROLES_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <zephyr/shell/shell.h>

/*
 * Firmware version reported by `status`. The harness pins it
 * (ubertooth_harness/nrfdk.py FIRMWARE_VERSION) and flashes the planned image
 * when a board reports anything else, so bump it with every change to the
 * firmware's behaviour or protocol.
 */
#define UTDK_FW_VERSION "utdk-1.0.0"

/* Payload magic and format: see README.md, "Advertising payload". */
#define UTDK_COMPANY_ID 0xFFFF
#define UTDK_MAGIC "UTAD"
#define UTDK_FORMAT 1
#define UTDK_TAG_MAX 12

/* Advertising channel map bits, as in the payload's channel map byte. */
#define UTDK_CHAN_37 0x01
#define UTDK_CHAN_38 0x02
#define UTDK_CHAN_39 0x04
#define UTDK_CHAN_ALL (UTDK_CHAN_37 | UTDK_CHAN_38 | UTDK_CHAN_39)

/* Responses: zero or more data lines, then exactly one "OK" or "ERR <why>". */
int utdk_ok(const struct shell *sh);
int utdk_err(const struct shell *sh, const char *fmt, ...);

/* The identity (FICR-derived static random) address, "XX:XX:XX:XX:XX:XX". */
void utdk_address(char *out, size_t len);

/* Advertiser role (advertiser.c). */
int adv_init(void);
/* Appends the role's status fields (",\"key\":value...") to buf. */
void adv_status(char *buf, size_t len);

#endif /* UTDK_ROLES_H_ */
