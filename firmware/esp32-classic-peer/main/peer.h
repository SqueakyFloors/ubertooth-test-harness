/* SPDX-License-Identifier: Apache-2.0 */

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* The harness pins this string and reflashes a board that reports anything
 * else, so it changes whenever the shell protocol or the air behavior does. */
#define UTESP_FW_VERSION "utesp-1.5.0"

/* The packet types "sco type" takes, and the two lines the shell refuses a
 * malformed "sco" line with. One definition of each, so the help text, the
 * refusals, and the setter cannot come to disagree about what the command
 * accepts. */
#define SCO_TYPE_NAMES "ev3|2ev3|default"
#define SCO_TYPE_USAGE "sco type takes ev3, 2ev3, or default"
#define SCO_USAGE      "sco takes start, stop, stats, or type <" SCO_TYPE_NAMES ">"

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
const char *bt_set_discoverable(bool on);
const char *bt_set_peer(const char *addr_text);
const char *bt_spp_start(void);
const char *bt_spp_stop(void);
const char *bt_stream(bool on);
const char *bt_stream_period(unsigned ms);
const char *bt_stream_payload(unsigned bytes);
const char *bt_defaults(void);
const char *bt_sco_start(void);

/* Asks the controller to hop over the channels this map marks used, as
 * twenty hex digits: bit b of byte i is channel 8i + b, and the top bit of
 * the last byte is reserved and must be zero. A map the spec does not allow,
 * including one with fewer than 20 used channels, is refused and nothing
 * changes. */
const char *bt_set_afh(const char *map_text);

/* Puts every channel back, which is what a board hops over before any map
 * has been asked for. */
const char *bt_set_afh_all(void);

/* One JSON object, no newline, truncated to fit. */
void bt_status_json(char *out, size_t len);

/* The board's clocks as one JSON object, or a reason the read failed. The
 * piconet fields are null when the board is in no piconet. */
const char *bt_clock_json(char *out, size_t len);

/* ---------------------------------------------- the synchronous audio link */

/* Brings up the Hands-Free Profile machinery the audio link needs: the
 * semaphores, the data path, and the task that sends frames. Called once
 * from bt_start(); nothing is on the air until sco_start(). */
const char *sco_init(void);

/* Starts the profile this role needs, and on the initiator pages the peer
 * and opens the synchronous link. peer_addr is the six-byte address "spp
 * peer" set, or NULL if it has not been set. The shell reaches this through
 * bt_sco_start(), which is where the role and that address live. */
const char *sco_start(enum peer_role role, const uint8_t *peer_addr);

/* Drops the audio link and takes the profile away again. */
const char *sco_stop(void);

/* Chooses the packet type the next audio link asks for: "ev3", "2ev3", or
 * "default". Refused while the audio profile is running, because that is
 * when a link can appear and it is the appearing of a link that carries the
 * choice to the controller. */
const char *sco_set_type(const char *name);

/* Puts the choice back to "default", for "defaults". Cannot fail, so a
 * restore that has already stopped the profile is never left half done. */
void sco_reset_type(void);

/* The controller's own (e)SCO packet counters, as one JSON object. */
const char *sco_pkt_stats(char *out, size_t len);

/* Zeroes the frame counters, for "defaults". */
void sco_zero_counters(void);

/* What the status line reports about the audio link. The strings are fixed
 * literals, so they need no copying. */
struct sco_view {
    const char *state;   /* off, ready, slc, on */
    const char *profile; /* none, ag, hf */
    const char *codec;   /* none, cvsd, msbc */
    unsigned handle;
    unsigned frame_len;
    uint32_t tx_frames, tx_errors, rx_frames, rx_bad;
    /* How many synchronous links this profile has had, and how many of them
     * went down again. A reader that saw a link at one moment and a link at
     * another can tell from these whether it was the same link. */
    uint32_t connects, disconnects;
    /* The packet type asked for, and what the controller settled on. */
    const char *type;      /* default, ev3, 2ev3 */
    const char *link;      /* none, sco, esco, unknown */
    const char *pkt_type;  /* none, ev3, 2-ev3, hv3, ..., unknown */
    unsigned tx_len;       /* bytes per packet, this board sending */
    unsigned rx_len;       /* bytes per packet, this board receiving */
    unsigned interval;     /* Tesco, in slots of 625 microseconds */
    unsigned retrans;      /* the retransmission window, in the same slots */
    unsigned supported;    /* the controller's own packet-type mask */
};

void sco_get(struct sco_view *out);
