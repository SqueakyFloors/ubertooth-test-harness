/* SPDX-License-Identifier: Apache-2.0 */

/* Choosing the synchronous (eSCO) packet type, and reading back what the
 * controller settled on.
 *
 * Like the clock and channel-map reads next to it, this is built into
 * ESP-IDF's Bluetooth component rather than into this firmware's own
 * component, because the calls it needs are private to that component; see
 * utesp_sco_type.c. This header is the whole of what the firmware uses. */

#pragma once

#include <stdbool.h>
#include <stdint.h>

/* Which packet type the board asks for on its next synchronous link.
 *
 * EV3 carries its payload at the basic rate, so a receiver that can decode
 * basic-rate packets can read it. 2-EV3 carries the same payload as EDR,
 * which is a different modulation. */
enum utesp_sco_type {
    UTESP_SCO_TYPE_DEFAULT = 0, /* leave the stack's own table alone */
    UTESP_SCO_TYPE_EV3,         /* EV3 alone: no EDR type and no SCO type */
    UTESP_SCO_TYPE_2EV3,        /* 2-EV3 alone, which is what the table asks */
};

/* The type the next link will ask for, and the type asked for now. Setting
 * it changes nothing about a link that is already up: the choice is read as
 * a link is opened or accepted. */
void utesp_sco_type_set(enum utesp_sco_type type);
enum utesp_sco_type utesp_sco_type_get(void);

/* Its name, as the shell and the status line spell it: "default", "ev3", or
 * "2ev3". */
const char *utesp_sco_type_name(enum utesp_sco_type type);

/* The synchronous packet types the controller says it supports, in the mask
 * HCI uses: bits 0 to 5 are HV1, HV2, HV3, EV3, EV4, and EV5, set when the
 * type may be used, and bits 6 to 9 are 2-EV3, 3-EV3, 2-EV5, and 3-EV5, set
 * when the type may NOT be used (Core 6.0 Vol 4 Part E 7.1.26). A request
 * for a type the controller does not support is dropped before it reaches
 * the air, so this says whether asking for EV3 can work at all. The
 * controller settles it when it starts, so the answer does not change. */
uint16_t utesp_sco_types_supported(void);

/* What the controller settled on for the synchronous link that is up. The
 * first six are the parameters of the Synchronous Connection Complete event
 * (Core 6.0 Vol 4 Part E 7.7.35), which does not carry the packet type
 * itself. */
struct utesp_sco_link {
    uint8_t link_type;      /* 0 is SCO, 2 is eSCO */
    uint16_t tx_pkt_len;    /* bytes per packet, this board sending */
    uint16_t rx_pkt_len;    /* bytes per packet, this board receiving */
    uint8_t tx_interval;    /* Tesco, in slots of 625 microseconds */
    uint8_t retrans_window; /* in the same slots; 0 is no retransmission */
    /* The packet types this board allowed for this link, in the mask above,
     * as the host sent them to the controller. With the packet length it
     * names the type: the link uses a type that was allowed and that can
     * carry that length. 0 if the link was already going down when this was
     * read, which leaves the length on its own. */
    uint16_t allowed;
};

enum {
    UTESP_SCO_OK = 0,
    UTESP_SCO_NO_LINK = -1, /* no synchronous link to this peer is up */
};

/* Reads the parameters of the link to remote, a six-byte address.
 *
 * This reads state the Bluetooth task owns, so a read taken in the moment a
 * link comes up, changes, or goes down can mix parameters from either side
 * of that change. Between changes the numbers hold still, and a link that
 * has gone reads as no link. */
int utesp_sco_link_read(const uint8_t *remote, struct utesp_sco_link *out);
