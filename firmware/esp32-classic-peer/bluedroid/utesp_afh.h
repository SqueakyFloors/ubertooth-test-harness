/* SPDX-License-Identifier: Apache-2.0 */

/* Reading the AFH channel map a link is hopping over, with
 * HCI_Read_AFH_Channel_Map.
 *
 * Like the clock read next to it, this is built into ESP-IDF's Bluetooth
 * component rather than into this firmware's own component, because that is
 * what lets it reach Bluedroid's command queue; see utesp_clock.c for why no
 * public route works. This header is the whole of what the firmware uses. */

#pragma once

#include <stdbool.h>
#include <stdint.h>

/* Ten bytes, 79 channels: bit b of byte i is channel 8i + b, and the top bit
 * of the last byte is reserved. That is the layout of every AFH map in the
 * spec, of esp_bt_gap_set_afh_channels(), and of the map libbtbb prints. */
#define UTESP_AFH_MAP_LEN 10

/* What the controller answered. */
struct utesp_afh_map {
    uint8_t status;  /* the HCI status byte: 0 is success */
    bool complete;   /* the answer carried all of the fields below */
    uint16_t handle; /* the connection handle the controller echoed back */
    bool enabled;    /* AFH_Mode: whether the link hops over a map at all.
                      * The map below means nothing while this is false. */
    uint8_t map[UTESP_AFH_MAP_LEN];
};

/* Negative results of utesp_afh_read(). A read the controller answered
 * returns 0 whatever it thought of the command, and leaves its verdict in
 * the status field. */
enum {
    UTESP_AFH_OK = 0,
    UTESP_AFH_NOT_READY = -1,  /* no init, or Bluedroid is not enabled */
    UTESP_AFH_NO_LINK = -2,    /* the controller does not know the handle */
    UTESP_AFH_NO_MEMORY = -3,
    UTESP_AFH_TIMED_OUT = -4,  /* the controller did not answer */
    UTESP_AFH_BAD_ANSWER = -5, /* it answered success, but short */
};

/* Makes the semaphores one read needs. Call once, while Bluedroid is
 * enabled, before any read. */
int utesp_afh_init(void);

/* Reads the AFH channel map of the ACL connection named by handle.
 *
 * Blocks until the controller answers or about a second has passed, so call
 * it from a task that may wait, not from a Bluetooth callback. Bluedroid
 * must be enabled. */
int utesp_afh_read(uint16_t handle, struct utesp_afh_map *out);
