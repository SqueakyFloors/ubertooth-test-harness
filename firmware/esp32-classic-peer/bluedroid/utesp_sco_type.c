/* SPDX-License-Identifier: Apache-2.0 */

/* Choosing the synchronous packet type, so the audio link can carry plain
 * EV3 rather than the EDR 2-EV3 the stack asks for by default.
 *
 * The parameters of a Hands-Free Profile audio link are compile-time tables
 * inside Bluedroid, one for the side that opens a link and one for the side
 * that accepts it, and no esp_hf_* or esp_bt_gap_* function takes
 * synchronous link parameters. Both tables ask for 2-EV3 between two devices
 * that offer the eSCO S4 setting, as these boards do, and the accepting
 * side's table does not allow plain EV3 at all. So each board has to be
 * changed on both paths: one board opens the link and the other accepts it,
 * and they run the same image.
 *
 * Each path ends in one call that carries the list of allowed packet types
 * to the controller, and nothing else of the link's parameters:
 *
 *   - BTM_CreateSco(), for the board that opens the link (and for the
 *     listening registration on the board that accepts one). Its pkt_types
 *     argument becomes the link's whole list; the bandwidths, the latency,
 *     the voice setting, and the retransmission effort come from what the
 *     stack set with BTM_SetEScoMode() and are left alone.
 *
 *   - BTM_EScoConnRsp(), for the board that answers an incoming request.
 *     Only the list in the parameters it is given is changed, because the
 *     rest of what the hands-free unit chose suits the codec it negotiated.
 *
 * Both are private to ESP-IDF's "bt" component, so this file is compiled
 * into that component as the clock and channel-map reads beside it are, and
 * both are replaced at link time with the wrappers below (--wrap in the
 * project's CMakeLists.txt). ESP-IDF's own tree is not changed.
 *
 * Wrapping, rather than setting the stack's parameters from the shell task,
 * is what keeps the choice off a task that does not own the stack's state:
 * each wrapper runs on whichever task the stack called from, and reads one
 * word. Bluedroid does have an override for the opening side's whole
 * parameter row, bta_ag_set_esco_param(), which nothing in ESP-IDF calls,
 * but using it would mean writing a struct the Bluetooth task reads and
 * supplying every field of the row rather than the one that matters. */

#include <string.h>

#include "common/bt_target.h"
#include "stack/bt_types.h"
#include "stack/btm_api.h"
#include "stack/hcidefs.h"

#include "esp_bt_main.h"

#include "utesp_sco_type.h"

/* The list of allowed types the two calls carry. Bits 0 to 5 allow a basic
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

/* 2-EV3 and nothing else, which is the list both of the stack's CVSD S4 rows
 * ask for. Asking for it by name makes a comparison between the two
 * deliberate rather than a comparison against whatever the tables hold. */
#define TWO_EV3_ONLY                                                     \
    (BTM_SCO_PKT_TYPES_MASK_NO_3_EV3 | BTM_SCO_PKT_TYPES_MASK_NO_2_EV5 | \
     BTM_SCO_PKT_TYPES_MASK_NO_3_EV5)

/* Read by the wrappers on whichever task the stack calls them from, written
 * by the shell task. One word, written whole, so a wrapper reads either the
 * type asked for before it or the one asked for after it, never a mixture.
 * The shell refuses the command while a link is up, which is what makes the
 * status line's account of a live link match the air. */
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

uint16_t utesp_sco_types_supported(void)
{
    /* The controller settles this when it starts, so it is read once rather
     * than reaching into the stack on every status line. Only while
     * Bluedroid is enabled: the control block it reads does not exist
     * before that, and a board whose Bluetooth did not start still answers
     * the shell. Nothing is kept from a read that could not be made, so the
     * answer arrives once the stack is up rather than being fixed at 0. */
    static uint16_t cached;
    static bool read_once;

    if (!read_once) {
        if (esp_bluedroid_get_status() != ESP_BLUEDROID_STATUS_ENABLED) {
            return 0;
        }
        cached = BTM_ReadDeviceScoPacketTypes();
        read_once = true;
    }
    return cached;
}

/* ------------------------------------------------------- the two wrappers */

/* Note that --wrap replaces every call to these two, so the opening side's
 * wrapper also covers the listening registration, and the accepting side's
 * also covers the audio gateway's own accept path. One image takes either
 * role, so that is what is wanted. */

extern tBTM_STATUS __real_BTM_CreateSco(BD_ADDR remote_bda, BOOLEAN is_orig,
                                        UINT16 pkt_types, UINT16 *p_sco_inx,
                                        tBTM_SCO_CB *p_conn_cb, tBTM_SCO_CB *p_disc_cb);

tBTM_STATUS __wrap_BTM_CreateSco(BD_ADDR remote_bda, BOOLEAN is_orig, UINT16 pkt_types,
                                 UINT16 *p_sco_inx, tBTM_SCO_CB *p_conn_cb,
                                 tBTM_SCO_CB *p_disc_cb)
{
    enum utesp_sco_type type = wanted;

    /* BTM reduces the list to a SCO-only one if the stack asked for a SCO
     * link rather than an eSCO one, which for "ev3" leaves no type at all
     * and fails the link. That is the point: a fallback to SCO would put
     * HV packets on the air under a status line naming EV3. */
    if (type != UTESP_SCO_TYPE_DEFAULT) {
        pkt_types = type_mask(type);
    }
    return __real_BTM_CreateSco(remote_bda, is_orig, pkt_types, p_sco_inx, p_conn_cb,
                                p_disc_cb);
}

extern void __real_BTM_EScoConnRsp(UINT16 sco_inx, UINT8 hci_status,
                                   tBTM_ESCO_PARAMS *p_parms);

void __wrap_BTM_EScoConnRsp(UINT16 sco_inx, UINT8 hci_status, tBTM_ESCO_PARAMS *p_parms)
{
    enum utesp_sco_type type = wanted;

    /* Only the list is changed. BTM masks it against the types the
     * controller supports after this returns, so a controller without EV3
     * still refuses rather than being asked for something it cannot do. */
    if (p_parms && hci_status == HCI_SUCCESS && type != UTESP_SCO_TYPE_DEFAULT) {
        p_parms->packet_types = type_mask(type);
    }
    __real_BTM_EScoConnRsp(sco_inx, hci_status, p_parms);
}

/* ---------------------------------------------------- reading a live link */

int utesp_sco_link_read(const uint8_t *remote, struct utesp_sco_link *out)
{
    tBTM_ESCO_DATA data;

    memset(out, 0, sizeof(*out));
    /* There is one synchronous link at a time here, but it is found by
     * address rather than taken as the only one, so a link to some other
     * peer is never read as this one's. The index is wanted anyway, because
     * the list of allowed types is per link. */
    for (uint16_t i = 0; i < BTM_MAX_SCO_LINKS; i++) {
        uint16_t allowed;

        if (BTM_ReadEScoLinkParms(i, &data) != BTM_SUCCESS) {
            continue;
        }
        if (memcmp(data.bd_addr, remote, BD_ADDR_LEN) != 0) {
            continue;
        }
        /* Non-zero only for a link that is fully connected. BTM hands back
         * parameters for one on its way up or down too, and those hold the
         * address this matched on and little else, so a half-made link
         * would otherwise be described as a finished one. */
        if ((allowed = BTM_ReadScoPacketTypes(i)) == 0) {
            continue;
        }
        out->link_type = data.link_type;
        out->tx_pkt_len = data.tx_pkt_len;
        out->rx_pkt_len = data.rx_pkt_len;
        out->tx_interval = data.tx_interval;
        out->retrans_window = data.retrans_window;
        out->allowed = allowed;
        return UTESP_SCO_OK;
    }
    return UTESP_SCO_NO_LINK;
}
