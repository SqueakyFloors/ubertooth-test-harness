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
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#include "utesp_afh.h"

/* The command's parameter: the connection handle. */
#define READ_AFH_PARAM_LEN 2

/* The answer: event code, parameter length, command credits, and the opcode,
 * then the status, handle, AFH mode, and the ten map bytes. The HCI layer
 * has already refused anything shorter than the opcode by the time the
 * callback runs. */
#define EVENT_OPCODE_LEN   5
#define EVENT_STATUS_LEN   6
#define EVENT_EXPECTED_LEN (9 + UTESP_AFH_MAP_LEN)

/* The controller answers in well under a millisecond. This is only so a
 * controller that has stopped answering cannot hold the shell. */
#define ANSWER_TIMEOUT_MS 1000

/* One read at a time, the same machinery as the clock read: request_lock
 * makes a reader the owner of the slot below, answer is given when the reply
 * to its command arrives, and slot_lock is held for the moment it takes to
 * write the answer in or to give up on it.
 *
 * Each request gets a number, which rides along in the command's own
 * metadata and comes back with its reply, so the late answer to a read that
 * gave up waiting is dropped rather than handed to the next reader as a
 * fresh one. */
static bool afh_ready;
static SemaphoreHandle_t request_lock;
static SemaphoreHandle_t answer;
static SemaphoreHandle_t slot_lock;
static struct {
    bool waiting;     /* the reader still wants the answer */
    uint32_t seq;     /* the newest request; numbering starts at 1 */
    uint32_t pending; /* a request the controller has not replied to, or 0 */
    struct utesp_afh_map got;
} slot;

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

/* Hands an answer to the reader waiting for request seq, if it is still
 * waiting, and in any case notes that the controller has replied to that
 * request. */
static void deliver(uint32_t seq, const struct utesp_afh_map *got)
{
    xSemaphoreTake(slot_lock, portMAX_DELAY);
    if (slot.pending == seq) {
        slot.pending = 0; /* the controller owes nothing now */
    }
    if (slot.waiting && slot.seq == seq) {
        slot.got = *got;
        slot.waiting = false;
        xSemaphoreGive(answer);
    }
    xSemaphoreGive(slot_lock);
}

/* Called from the HCI layer's receive path with the whole Command Complete
 * event, and owns that buffer from here on. */
static void afh_complete(BT_HDR *response, void *context)
{
    const uint32_t seq = (uint32_t)(uintptr_t)context;
    uint8_t *stream = response->data + response->offset;
    struct utesp_afh_map got;
    uint16_t opcode = 0;
    uint8_t mode = 0;

    memset(&got, 0, sizeof(got));
    got.status = HCI_ERR_UNSPECIFIED; /* until the event says otherwise */

    /* A controller that refuses the command may answer with the status
     * alone, so each part is read only once it is known to be there. */
    if (response->len >= EVENT_OPCODE_LEN) {
        STREAM_SKIP_UINT8(stream); /* event code, HCI_COMMAND_COMPLETE_EVT */
        STREAM_SKIP_UINT8(stream); /* parameter total length */
        STREAM_SKIP_UINT8(stream); /* command credits, the HCI layer's own */
        STREAM_TO_UINT16(opcode, stream);
    }
    if (response->len >= EVENT_STATUS_LEN) {
        STREAM_TO_UINT8(got.status, stream);
    }
    if (response->len >= EVENT_EXPECTED_LEN) {
        STREAM_TO_UINT16(got.handle, stream);
        STREAM_TO_UINT8(mode, stream);
        got.enabled = (mode != 0);
        STREAM_TO_ARRAY(got.map, stream, UTESP_AFH_MAP_LEN);
        got.complete = true;
    }

    if (opcode == HCI_READ_AFH_CH_MAP) {
        deliver(seq, &got);
    }
    osi_free(response);
}

/* A Command Status for this command means the controller would not run it.
 * With this callback set the HCI layer leaves the command buffer to it, as
 * Bluedroid's own status handler does. */
static void afh_refused(uint8_t status, BT_HDR *command, void *context)
{
    struct utesp_afh_map got;

    memset(&got, 0, sizeof(got));
    got.status = status;
    /* The HCI layer stops tracking a command once it has seen any reply to
     * it, so this is the only reply there will be, whatever the status says,
     * and the reader is told now rather than left to time out. */
    deliver((uint32_t)(uintptr_t)context, &got);
    HCI_FREE_CMD_BUF(command);
}

/* Builds the command and waits for its answer. The caller holds
 * request_lock. */
static int send_read(uint16_t handle, struct utesp_afh_map *out)
{
    BT_HDR *cmd;
    hci_cmd_metadata_t *metadata;
    uint8_t *pp;
    uint32_t seq;
    bool owed, answered;

    /* A command the controller never replied to still holds its place in the
     * HCI layer's pending list, its buffer, and one of the command credits.
     * Sending more would pile those up, so until it is answered there is
     * nothing useful to do but say the controller is not answering. A board
     * in that state is recovered by "reset". */
    xSemaphoreTake(slot_lock, portMAX_DELAY);
    owed = slot.pending != 0;
    xSemaphoreGive(slot_lock);
    if (owed) {
        return UTESP_AFH_TIMED_OUT;
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
    metadata->command_status_cb = afh_refused;

    /* Any leftover from a read that timed out, so the wait below is this
     * command's. */
    xSemaphoreTake(answer, 0);
    xSemaphoreTake(slot_lock, portMAX_DELAY);
    seq = ++slot.seq;
    slot.pending = seq;
    slot.waiting = true;
    xSemaphoreGive(slot_lock);
    metadata->context = (void *)(uintptr_t)seq;

    hci_layer_get_interface()->transmit_command(cmd, afh_complete, afh_refused,
                                                metadata->context);

    answered = xSemaphoreTake(answer, pdMS_TO_TICKS(ANSWER_TIMEOUT_MS)) == pdTRUE;

    xSemaphoreTake(slot_lock, portMAX_DELAY);
    if (answered) {
        *out = slot.got;
    }
    /* Whether it answered or not, nothing is expected now: an answer that
     * arrives after this is dropped rather than kept for the next read. */
    slot.waiting = false;
    xSemaphoreGive(slot_lock);

    if (!answered) {
        return UTESP_AFH_TIMED_OUT;
    }
    if (out->status == HCI_SUCCESS && !out->complete) {
        return UTESP_AFH_BAD_ANSWER;
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
