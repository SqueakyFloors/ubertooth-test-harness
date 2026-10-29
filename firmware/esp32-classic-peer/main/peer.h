/* SPDX-License-Identifier: Apache-2.0 */

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* The harness pins this string and reflashes a board that reports anything
 * else, so it changes whenever the shell protocol or the air behavior does. */
#define UTESP_FW_VERSION "utesp-1.0.0"

enum peer_role {
    PEER_ROLE_ACCEPTOR = 0, /* the boot role: wait for the other board */
    PEER_ROLE_INITIATOR,    /* look the peer up over SDP and connect */
};

/* Writes one line to the shell UART, adding the newline. Safe to call from a
 * Bluetooth callback as well as from the shell task. */
void shell_line(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

/* Brings up the controller, Bluedroid, GAP, and SPP. Returns NULL on success
 * or a short reason the shell can put after "ERR ". Every bt_* function below
 * follows that convention. */
const char *bt_start(void);

const char *bt_set_role(enum peer_role role);
const char *bt_set_name(const char *name);
const char *bt_set_discoverable(bool on);
const char *bt_set_peer(const char *addr_text);
const char *bt_spp_start(void);
const char *bt_spp_stop(void);
const char *bt_stream(bool on);
const char *bt_stream_period(unsigned ms);
const char *bt_stream_payload(unsigned bytes);
const char *bt_tx_power(int min_dbm, int max_dbm);
const char *bt_defaults(void);

/* Runs one general inquiry, prints a data line per device found, and returns
 * when the controller reports the inquiry over. */
const char *bt_inquiry(unsigned seconds);

/* One JSON object, no newline, truncated to fit. */
void bt_status_json(char *out, size_t len);
