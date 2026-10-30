/* SPDX-License-Identifier: Apache-2.0 */

/* Reading the board's clocks with HCI_Read_Clock.
 *
 * The implementation is built into ESP-IDF's Bluetooth component rather than
 * into this firmware's own component, because that is what lets it reach
 * Bluedroid's command queue; see utesp_clock.c. This header is the whole of
 * what the firmware uses. */

#pragma once

#include <stdbool.h>
#include <stdint.h>

/* What the controller answered. The clock is a 28-bit counter in units of
 * 312.5 microseconds, so it advances 3200 times a second and wraps about
 * every 23.3 hours (Core 6.0 Vol 4 Part E 7.5.6). */
struct utesp_clock {
    uint8_t status;      /* the HCI status byte: 0 is success */
    uint16_t handle;     /* the connection handle the controller echoed back */
    uint32_t clock;      /* the clock, in units of 312.5 us */
    uint16_t accuracy;   /* ppm; 0xffff means the controller does not know */
    int64_t answered_us; /* microseconds since boot, when the answer arrived */
    int64_t read_us;     /* that moment, less when the command was queued */
};

/* Negative results of utesp_clock_read(). A read that reached the controller
 * returns 0 whatever the controller thought of it, and leaves its verdict in
 * the status field. */
enum {
    UTESP_CLOCK_OK = 0,
    UTESP_CLOCK_NOT_READY = -1, /* utesp_clock_init() has not run */
    UTESP_CLOCK_NO_LINK = -2,   /* no link to that address, so no piconet */
    UTESP_CLOCK_NO_MEMORY = -3,
    UTESP_CLOCK_TIMED_OUT = -4, /* the controller did not answer */
};

/* Makes the semaphores one read needs. Call once, while Bluedroid is
 * enabled, before any read. */
int utesp_clock_init(void);

/* Reads the board's own clock (piconet false), or the clock of the piconet
 * the link to peer_addr belongs to (piconet true, peer_addr the six bytes of
 * that board's address). On the piconet's leader the two are the same
 * counter.
 *
 * Blocks until the controller answers or about a second has passed, so call
 * it from a task that may wait, not from a Bluetooth callback. Bluedroid must
 * be enabled. */
int utesp_clock_read(const uint8_t *peer_addr, bool piconet, struct utesp_clock *out);
