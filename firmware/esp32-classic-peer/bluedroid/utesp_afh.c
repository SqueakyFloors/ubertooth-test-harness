/* SPDX-License-Identifier: Apache-2.0 */

/* HCI_Read_AFH_Channel_Map, sent down Bluedroid's own command queue.
 *
 * Bluedroid names the opcode and never sends it, and no esp_* function
 * returns an AFH map, so the only way to the controller's answer is the one
 * the clock read next to this file uses: a source file compiled into
 * ESP-IDF's "bt" component, building the command with Bluedroid's own
 * helpers and handing it to the HCI layer, which matches the Command
 * Complete to it by opcode. utesp_clock.c carries the whole argument for
 * why no public route works; this file is the same shape, for one more
 * command.
 *
 * Only this file sends Read AFH Channel Map, so matching by opcode is
 * unambiguous. */

#include <string.h>

#include "common/bt_target.h"
#include "hci/hci_layer.h"
#include "osi/allocator.h"
#include "stack/bt_types.h"
#include "stack/hcidefs.h"
#include "stack/hcimsgs.h"

#include "esp_bt_main.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#include "utesp_afh.h"

/* The command's parameter: the connection handle. */
#define READ_AFH_PARAM_LEN 2

/* The answer: event code, parameter length, command credits, and the opcode,
 * then the status, handle, AFH mode, and the ten map bytes. The HCI layer has
 * already refused anything shorter than the opcode by the time the callback
 * runs. */
#define EVENT_OPCODE_LEN   5
#define EVENT_STATUS_LEN   6
#define EVENT_EXPECTED_LEN (9 + UTESP_AFH_MAP_LEN)

/* The controller answers in well under a millisecond. This is only so a
 * controller that has stopped answering cannot hold the shell. */
#define ANSWER_TIMEOUT_MS 1000

/* One read at a time, the same machinery as the clock read: request_lock
 * makes a reader the owner of the slot below, answer is given when the
 * controller's reply arrives, and slot_lock is held for the moment it takes
 * to write the answer in or to give up on it. Only one command is ever
 * outstanding, which is what lets a reply be matched to a reader without
 * numbering them. */
static bool afh_ready;
static SemaphoreHandle_t request_lock;
static SemaphoreHandle_t answer;
static SemaphoreHandle_t slot_lock;
static struct {
    bool waiting;    /* a reader still wants the answer */
    bool pending;    /* the controller has not replied to the command sent */
    BT_HDR *free_me; /* a command buffer the reader must free; see below */
    struct utesp_afh_map got;
} slot;

/* Takes the command buffer a Command Status left behind, if there is one.
 * Called from the reader, which is the only place it can be freed: the HCI
 * layer is still reading it when afh_status() runs. */
static BT_HDR *take_leftover(void)
{
    BT_HDR *buf;

    xSemaphoreTake(slot_lock, portMAX_DELAY);
    buf = slot.free_me;
    slot.free_me = NULL;
    xSemaphoreGive(slot_lock);
    return buf;
}

int utesp_afh_init(void)
{
    SemaphoreHandle_t request, slot_guard, arrived;

    if (afh_ready) {
        return UTESP_AFH_OK;
    }
    /* Published together, so a failed start cannot leave a read using one
     * semaphore that exists and one that does not. */
    request = xSemaphoreCreateMutex();
    slot_guard = xSemaphoreCreateMutex();
    arrived = xSemaphoreCreateBinary();
    if (!request || !slot_guard || !arrived) {
        if (request) {
            vSemaphoreDelete(request);
        }
        if (slot_guard) {
            vSemaphoreDelete(slot_guard);
        }
        if (arrived) {
            vSemaphoreDelete(arrived);
        }
        return UTESP_AFH_NO_MEMORY;
    }
    request_lock = request;
    slot_lock = slot_guard;
    answer = arrived;
    afh_ready = true;
    return UTESP_AFH_OK;
}

/* Called from the HCI layer's receive path with the whole Command Complete
 * event, and owns that buffer from here on. The layer has already matched
 * the event to this command by its opcode and refused anything too short to
 * hold one. */
static void afh_complete(BT_HDR *response, void *context)
{
    uint8_t *stream = response->data + response->offset;
    struct utesp_afh_map got;
    uint8_t mode = 0;

    (void)context;
    /* The zeroed status is success, which with complete still false reads as
     * a short answer: an event with no status byte in it at all is exactly
     * that, and must not be reported as a refusal the controller never made.
     *
     * A controller that refuses the command may answer with the status
     * alone, so each part is read only once it is known to be there. */
    memset(&got, 0, sizeof(got));
    stream += EVENT_OPCODE_LEN; /* event code, length, credits, and opcode */
    if (response->len >= EVENT_STATUS_LEN) {
        STREAM_TO_UINT8(got.status, stream);
    }
    if (response->len >= EVENT_EXPECTED_LEN) {
        STREAM_TO_UINT16(got.handle, stream);
        got.handle &= 0x0fff; /* 12 bits meaningful, the rest reserved */
        STREAM_TO_UINT8(mode, stream);
        got.enabled = (mode != 0);
        STREAM_TO_ARRAY(got.map, stream, UTESP_AFH_MAP_LEN);
        got.complete = true;
    }

    xSemaphoreTake(slot_lock, portMAX_DELAY);
    slot.pending = false; /* the controller owes nothing now */
    if (slot.waiting) {
        slot.got = got;
        slot.waiting = false;
        xSemaphoreGive(answer);
    }
    xSemaphoreGive(slot_lock);

    osi_free(response);
}

/* Read AFH Channel Map is answered with a Command Complete (Core 6.0 Vol 4
 * Part E 7.5.5), so this runs only if a controller answers out of spec.
 * Without it the HCI layer would call nothing, and every later read would say
 * the controller is silent.
 *
 * It must not free the command buffer: the layer reads the metadata in it
 * after this returns, to decide what to free itself. So the buffer is handed
 * to the reader, which frees it once it is awake. */
static void afh_status(uint8_t status, BT_HDR *command, void *context)
{
    struct utesp_afh_map got;

    (void)context;
    memset(&got, 0, sizeof(got));
    got.status = status;

    xSemaphoreTake(slot_lock, portMAX_DELAY);
    slot.pending = false;
    slot.free_me = command;
    if (slot.waiting) {
        slot.got = got;
        slot.waiting = false;
        xSemaphoreGive(answer);
    }
    xSemaphoreGive(slot_lock);
}

/* Builds the command and waits for its answer. The caller holds
 * request_lock. */
static int send_read(uint16_t handle, struct utesp_afh_map *out)
{
    BT_HDR *cmd;
    hci_cmd_metadata_t *metadata;
    uint8_t *pp;
    BT_HDR *free_me;
    bool owed, answered;

    /* A command the controller never replied to still holds its place in the
     * HCI layer's pending list, its buffer, and one of the command credits.
     * Sending more would pile those up, so until it is answered there is
     * nothing useful to do but say the controller is not answering. A board
     * in that state is recovered by "reset". */
    xSemaphoreTake(slot_lock, portMAX_DELAY);
    owed = slot.pending;
    xSemaphoreGive(slot_lock);
    if (owed) {
        return UTESP_AFH_TIMED_OUT;
    }

    /* One left by a read that had already given up waiting, so a second one
     * cannot push the first out of the slot unfreed. */
    if ((free_me = take_leftover()) != NULL) {
        HCI_FREE_CMD_BUF(free_me);
    }

    if ((cmd = HCI_GET_CMD_BUF(READ_AFH_PARAM_LEN)) == NULL) {
        return UTESP_AFH_NO_MEMORY;
    }
    pp = (uint8_t *)(cmd + 1);
    cmd->len = HCIC_PREAMBLE_SIZE + READ_AFH_PARAM_LEN;
    cmd->offset = 0;
    UINT16_TO_STREAM(pp, HCI_READ_AFH_CH_MAP);
    UINT8_TO_STREAM(pp, READ_AFH_PARAM_LEN);
    UINT16_TO_STREAM(pp, handle);

    /* The HCI layer reads the callbacks and the opcode out of the buffer's
     * metadata rather than out of the arguments to transmit_command(), so
     * they are set here, as Bluedroid's own btu_hcif_send_cmd() sets them. */
    metadata = HCI_GET_CMD_METAMSG(cmd);
    metadata->opcode = HCI_READ_AFH_CH_MAP;
    metadata->command_complete_cb = afh_complete;
    metadata->command_status_cb = afh_status;

    /* Any leftover from a read that timed out, so the wait below is this
     * command's. */
    xSemaphoreTake(answer, 0);
    xSemaphoreTake(slot_lock, portMAX_DELAY);
    slot.pending = true;
    slot.waiting = true;
    xSemaphoreGive(slot_lock);

    hci_layer_get_interface()->transmit_command(cmd, afh_complete, afh_status, NULL);

    xSemaphoreTake(answer, pdMS_TO_TICKS(ANSWER_TIMEOUT_MS));

    /* The answer, not the semaphore, decides: one that arrived just as the
     * wait ran out is still this read's answer. After this nothing is
     * expected, so a later one is dropped rather than kept for the next
     * read. */
    xSemaphoreTake(slot_lock, portMAX_DELAY);
    answered = !slot.waiting;
    if (answered) {
        *out = slot.got;
    }
    slot.waiting = false;
    xSemaphoreGive(slot_lock);

    if ((free_me = take_leftover()) != NULL) {
        HCI_FREE_CMD_BUF(free_me);
    }

    if (!answered) {
        return UTESP_AFH_TIMED_OUT;
    }
    if (out->status == HCI_SUCCESS && !out->complete) {
        return UTESP_AFH_BAD_ANSWER;
    }
    /* A map for a link other than the one asked about would be worse than no
     * map at all. The handle is only meaningful in an answer that
     * succeeded. */
    if (out->status == HCI_SUCCESS && out->handle != handle) {
        return UTESP_AFH_WRONG_LINK;
    }
    return UTESP_AFH_OK;
}

int utesp_afh_read(uint16_t handle, struct utesp_afh_map *out)
{
    int result;

    /* The queue the command goes on exists only while Bluedroid is enabled,
     * so check that here rather than trust every caller to. */
    if (!afh_ready || !out || esp_bluedroid_get_status() != ESP_BLUEDROID_STATUS_ENABLED) {
        return UTESP_AFH_NOT_READY;
    }

    xSemaphoreTake(request_lock, portMAX_DELAY);
    result = send_read(handle, out);
    xSemaphoreGive(request_lock);

    /* The link can go down between the caller learning its handle and the
     * command reaching the controller, which then says it does not know the
     * handle. That is the same news as having no link at all. */
    if (result == UTESP_AFH_OK && out->status == HCI_ERR_NO_CONNECTION) {
        return UTESP_AFH_NO_LINK;
    }
    return result;
}
