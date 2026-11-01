/* SPDX-License-Identifier: Apache-2.0 */

/* Choosing the synchronous packet type, so the audio link can carry plain
 * EV3 rather than the EDR 2-EV3 the stack asks for by default.
 *
 * The parameters of a Hands-Free Profile audio link are a compile-time table
 * inside Bluedroid, bta_ag_esco_params[] in bta/hf_ag/bta_ag_sco.c, and no
 * esp_hf_* or esp_bt_gap_* function takes synchronous link parameters. The
 * row the stack picks for a CVSD link between two devices that both offer
 * the eSCO S4 setting asks for 2-EV3, whose payload is carried as EDR.
 *
 * Two hooks change that, and both are private to ESP-IDF's "bt" component,
 * so this file is compiled into that component as the clock and channel-map
 * reads beside it are. ESP-IDF's own tree is not changed.
 *
 *  - The side that opens the link is the audio gateway. Bluedroid already
 *    has an override for it, bta_ag_set_esco_param(), which the stack reads
 *    in bta_ag_create_sco() in place of the table row; nothing in ESP-IDF
 *    calls it.
 *
 *  - The side that accepts the link answers with its own list of types it
 *    will allow, from a second table, bta_hf_client_esco_params[] in
 *    bta/hf_client/bta_hf_client_sco.c. That table is static and const, and
 *    its row for a CVSD S4 link leaves the EV3 bit clear, so an accepting
 *    board would refuse a plain EV3 link however the opening board asked.
 *    There is no override, so the one call that carries the list to the
 *    controller, BTM_EScoConnRsp(), is wrapped at link time (--wrap in the
 *    project's CMakeLists.txt) and the list adjusted on its way past.
 *
 * Both boards run one image and take their role at run time, so each board
 * needs both hooks. Only the packet-type list is changed; the bandwidths,
 * the voice setting, the latency, and the retransmission effort stay as the
 * stack chose them, which keeps this to the one thing that decides whether
 * the payload on the air is basic rate or EDR. */

#include <string.h>

#include "common/bt_target.h"
#include "stack/bt_types.h"
#include "stack/btm_api.h"
#include "stack/hcidefs.h"

#include "bta_ag_int.h"

#include "utesp_sco_type.h"

/* The packet-type masks the two hooks pass down. Bits 0 to 5 allow a basic
 * SCO or eSCO type; bits 6 to 9 forbid an EDR one (Core 6.0 Vol 4 Part E
 * 7.1.26), so a list that names no EDR type still has to forbid each one. */
#define NO_EDR_TYPES                                                     \
    (BTM_SCO_PKT_TYPES_MASK_NO_2_EV3 | BTM_SCO_PKT_TYPES_MASK_NO_3_EV3 | \
     BTM_SCO_PKT_TYPES_MASK_NO_2_EV5 | BTM_SCO_PKT_TYPES_MASK_NO_3_EV5)

/* EV3 and nothing else: no EDR type, and no SCO type either, so a link that
 * cannot be EV3 fails rather than quietly becoming an HV3. That matters
 * because an HV3 and an EV3 share one packet type code on the air, so a
 * capture alone could not tell them apart. */
#define EV3_ONLY (BTM_SCO_PKT_TYPES_MASK_EV3 | NO_EDR_TYPES)

/* 2-EV3 and nothing else, which is the list the stack's CVSD S4 row asks
 * for. Asking for it by name makes a comparison between the two deliberate
 * rather than a comparison against whatever the table happens to hold. */
#define TWO_EV3_ONLY                                                     \
    (BTM_SCO_PKT_TYPES_MASK_NO_3_EV3 | BTM_SCO_PKT_TYPES_MASK_NO_2_EV5 | \
     BTM_SCO_PKT_TYPES_MASK_NO_3_EV5)

/* Read by the Bluetooth task when a link is opened or accepted, written by
 * the shell task. One word, and a link is never up while it changes (the
 * shell refuses it then), so no lock is needed. */
static volatile enum utesp_sco_type wanted = UTESP_SCO_TYPE_DEFAULT;

static uint16_t type_mask(enum utesp_sco_type type)
{
    switch (type) {
    case UTESP_SCO_TYPE_EV3:
        return EV3_ONLY;
    case UTESP_SCO_TYPE_2EV3:
        return TWO_EV3_ONLY;
    default:
        return 0;
    }
}

void utesp_sco_type_set(enum utesp_sco_type type)
{
    wanted = type;
}

enum utesp_sco_type utesp_sco_type_get(void)
{
    return wanted;
}

const char *utesp_sco_type_name(enum utesp_sco_type type)
{
    switch (type) {
    case UTESP_SCO_TYPE_EV3:
        return "ev3";
    case UTESP_SCO_TYPE_2EV3:
        return "2ev3";
    default:
        return "default";
    }
}

void utesp_sco_type_apply(void)
{
    enum utesp_sco_type type = wanted;
    tBTM_ESCO_PARAMS params;

    if (type == UTESP_SCO_TYPE_DEFAULT) {
        bta_ag_set_esco_param(FALSE, NULL);
        return;
    }

    /* The override replaces the whole row, so the other fields are the CVSD
     * S4 row's own values: 64 kbit/s each way, a 12 ms bound on the link's
     * latency, linear 16-bit input coded to CVSD on the air, and one
     * retransmission for quality. An EV3 link carries 30 bytes every six
     * slots, which is the same 64 kbit/s and well inside that latency. */
    memset(&params, 0, sizeof(params));
    params.tx_bw = BTM_64KBITS_RATE;
    params.rx_bw = BTM_64KBITS_RATE;
    params.max_latency = 12;
    params.voice_contfmt = BTM_VOICE_SETTING_CVSD;
    params.packet_types = type_mask(type);
    params.retrans_effort = BTM_ESCO_RETRANS_QUALITY;
    bta_ag_set_esco_param(TRUE, &params);
}

uint16_t utesp_sco_types_supported(void)
{
    return BTM_ReadDeviceScoPacketTypes();
}

/* The accepting side's list of allowed types, on its way to the controller.
 * Only the list is changed: the rest of what the hands-free unit chose
 * (bandwidths, voice setting, latency, retransmission effort) is what suits
 * the codec it negotiated, and this must not second-guess it.
 *
 * BTM masks the list against the types the controller supports after this
 * returns, so a controller without EV3 still refuses rather than being
 * asked for something it cannot do. */
extern void __real_BTM_EScoConnRsp(UINT16 sco_inx, UINT8 hci_status,
                                   tBTM_ESCO_PARAMS *p_parms);

void __wrap_BTM_EScoConnRsp(UINT16 sco_inx, UINT8 hci_status, tBTM_ESCO_PARAMS *p_parms)
{
    enum utesp_sco_type type = wanted;

    if (p_parms && hci_status == HCI_SUCCESS && type != UTESP_SCO_TYPE_DEFAULT) {
        p_parms->packet_types = type_mask(type);
    }
    __real_BTM_EScoConnRsp(sco_inx, hci_status, p_parms);
}

int utesp_sco_link_read(const uint8_t *remote, struct utesp_sco_link *out)
{
    tBTM_ESCO_DATA data;

    if (!out || !remote) {
        return UTESP_SCO_NO_LINK;
    }
    memset(out, 0, sizeof(*out));
    /* There is only ever one synchronous link here, so the first active
     * index is it; the address the controller reports is checked all the
     * same, so a link to some other peer is never read as this one's. */
    if (BTM_ReadEScoLinkParms(BTM_FIRST_ACTIVE_SCO_INDEX, &data) != BTM_SUCCESS) {
        return UTESP_SCO_NO_LINK;
    }
    if (memcmp(data.bd_addr, remote, BD_ADDR_LEN) != 0) {
        return UTESP_SCO_WRONG_LINK;
    }
    out->link_type = data.link_type;
    out->tx_pkt_len = data.tx_pkt_len;
    out->rx_pkt_len = data.rx_pkt_len;
    out->tx_interval = data.tx_interval;
    out->retrans_window = data.retrans_window;
    out->air_mode = data.air_mode;
    return UTESP_SCO_OK;
}
