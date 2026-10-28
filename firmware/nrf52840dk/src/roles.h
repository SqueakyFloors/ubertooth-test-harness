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
#define UTDK_FW_VERSION "utdk-1.2.0"

/* Payload magic and format: see README.md, "Advertising payload". */
#define UTDK_COMPANY_ID 0xFFFF
#define UTDK_MAGIC "UTAD"
#define UTDK_FORMAT 1
#define UTDK_TAG_MAX 12

/* Company ID (2), magic (4), format (1), channel map (1), counter (4). */
#define UTDK_MFG_FIXED_LEN 12

/* Advertising channel map bits, as in the payload's channel map byte. */
#define UTDK_CHAN_37 0x01
#define UTDK_CHAN_38 0x02
#define UTDK_CHAN_39 0x04
#define UTDK_CHAN_ALL (UTDK_CHAN_37 | UTDK_CHAN_38 | UTDK_CHAN_39)

/*
 * The roles one image holds. One is active at a time; `role <name>` switches,
 * and `status` names the active one. The boot role is the advertiser, so a
 * board that was reset or power-cycled is back to a known state.
 */
enum utdk_role {
	UTDK_ROLE_ADVERTISER,
	UTDK_ROLE_CENTRAL,
	UTDK_ROLE_PERIPHERAL,
};

enum utdk_role utdk_role(void);
const char *utdk_role_name(enum utdk_role role);

/* Responses: zero or more data lines, then exactly one "OK" or "ERR <why>". */
int utdk_ok(const struct shell *sh);
int utdk_err(const struct shell *sh, const char *fmt, ...);

/* The identity (FICR-derived static random) address, "XX:XX:XX:XX:XX:XX". */
void utdk_address(char *out, size_t len);

/*
 * The harness's manufacturer-specific payload (README.md, "Advertising
 * payload") in out, which holds UTDK_MFG_FIXED_LEN + UTDK_TAG_MAX bytes.
 * Returns its length.
 */
size_t utdk_mfg_payload(uint8_t *out, uint8_t chmap, uint32_t counter,
			const uint8_t *tag, uint8_t tag_len);

/* Advertiser role (advertiser.c). */
int adv_init(void);
/* Appends the role's status fields (",\"key\":value...") to buf. */
void adv_status(char *buf, size_t len);
/* Leaves the role idle: advertising off, boot settings restored. */
void adv_leave(void);

/* Central and peripheral roles (connection.c). */
int conn_init(void);
void conn_status(char *buf, size_t len);
void conn_leave(void);

#endif /* UTDK_ROLES_H_ */
