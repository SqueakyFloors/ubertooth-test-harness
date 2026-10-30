/* SPDX-License-Identifier: Apache-2.0 */

/* HCI_Read_Clock, sent down Bluedroid's own command queue.
 *
 * The ESP32's controller answers Read Clock, but ESP-IDF's Bluetooth host,
 * Bluedroid, never sends it and gives an application no way to send a chosen
 * opcode: the one public route, esp_ble_gap_vendor_command_send(), ORs the
 * vendor group into whatever opcode it is given. Sending the command around
 * Bluedroid instead, over the VHCI interface, would hand the answer to
 * Bluedroid, which drops an event it has no pending command for.
 *
 * So this file is compiled into ESP-IDF's "bt" component, which the
 * project's top-level CMakeLists.txt arranges, and uses the same interfaces
 * the rest of Bluedroid uses. Nothing in ESP-IDF itself is changed. The
 * command joins the queue every other command goes on, under the same
 * command credits, and the HCI layer matches the Command Complete to it by
 * opcode and calls the callback below; host and controller stay in step.
 *
 * Only this file sends Read Clock, so matching by opcode is unambiguous. */

#include <string.h>

#include "common/bt_target.h"
#include "hci/hci_layer.h"
#include "osi/allocator.h"
#include "stack/bt_types.h"
#include "stack/btm_api.h"
#include "stack/hcidefs.h"
#include "stack/hcimsgs.h"

#include "esp_bt_main.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#include "utesp_clock.h"

/* The command's parameters: a connection handle and Which_Clock. */
#define READ_CLOCK_PARAM_LEN 3
#define WHICH_CLOCK_NATIVE   0
#define WHICH_CLOCK_PICONET  1

/* The event: code, parameter length, command credits, opcode, then status,
 * handle, clock, and accuracy. */
#define EVENT_HEADER_LEN  5
#define RETURN_PARAM_LEN  9
#define EVENT_EXPECTED_LEN (EVENT_HEADER_LEN + RETURN_PARAM_LEN)

/* The controller answers in well under a millisecond. This is only so a
 * controller that has stopped answering cannot hold the shell. */
#define ANSWER_TIMEOUT_MS 1000

/* One read at a time: request_lock makes a reader the owner of the slot
 * below, and answer is given when the Command Complete for its command
 * arrives. slot_lock is held for the moment it takes to write the answer in
 * or to give up on it.
 *
 * Each request gets a number, which rides along in the command's own
 * metadata and comes back with its answer. A read that gave up waiting is
 * what makes this worth doing: its answer can still arrive, and matching the
 * number is what stops that answer being handed to the next reader, which
 * would report a clock from a second ago as a fresh one. */
static bool clock_ready;
static SemaphoreHandle_t request_lock;
static SemaphoreHandle_t answer;
static SemaphoreHandle_t slot_lock;
static struct {
    bool waiting;  /* the reader still wants the answer */
    uint32_t seq;  /* which request it is waiting for; numbering starts at 1 */
    int64_t sent_us;
    struct utesp_clock got;
} slot;

int utesp_clock_init(void)
{
    SemaphoreHandle_t request, slot_guard, arrived;

    if (clock_ready) {
        return UTESP_CLOCK_OK;
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
        return UTESP_CLOCK_NO_MEMORY;
    }
    request_lock = request;
    slot_lock = slot_guard;
    answer = arrived;
    clock_ready = true;
    return UTESP_CLOCK_OK;
}

/* Called from the HCI layer's receive path with the whole event, and owns
 * the buffer from here on. */
static void clock_complete(BT_HDR *response, void *context)
{
    const int64_t now = esp_timer_get_time();
    const uint32_t seq = (uint32_t)(uintptr_t)context;
    uint8_t *stream = response->data + response->offset;
    struct utesp_clock got;
    uint16_t opcode = 0;

    memset(&got, 0, sizeof(got));
    got.answered_us = now;

    if (response->len >= EVENT_EXPECTED_LEN) {
        STREAM_SKIP_UINT8(stream); /* event code, HCI_COMMAND_COMPLETE_EVT */
        STREAM_SKIP_UINT8(stream); /* parameter total length */
        STREAM_SKIP_UINT8(stream); /* command credits, the HCI layer's own */
        STREAM_TO_UINT16(opcode, stream);
        STREAM_TO_UINT8(got.status, stream);
        STREAM_TO_UINT16(got.handle, stream);
        STREAM_TO_UINT32(got.clock, stream);
        STREAM_TO_UINT16(got.accuracy, stream);
        /* The clock is 28 bits and the rest of the word is reserved. */
        got.clock &= 0x0fffffff;
    }

    xSemaphoreTake(slot_lock, portMAX_DELAY);
    if (slot.waiting && slot.seq == seq && opcode == HCI_READ_CLOCK) {
        got.read_us = now - slot.sent_us;
        slot.got = got;
        slot.waiting = false;
        xSemaphoreGive(answer);
    }
    xSemaphoreGive(slot_lock);

    osi_free(response);
}

/* Builds the command and waits for its answer. The caller holds
 * request_lock. */
static int read_clock(uint16_t handle, uint8_t which, struct utesp_clock *out)
{
    BT_HDR *cmd;
    hci_cmd_metadata_t *metadata;
    uint8_t *pp;
    uint32_t seq;
    bool answered;

    if ((cmd = HCI_GET_CMD_BUF(READ_CLOCK_PARAM_LEN)) == NULL) {
        return UTESP_CLOCK_NO_MEMORY;
    }
    pp = (uint8_t *)(cmd + 1);
    cmd->len = HCIC_PREAMBLE_SIZE + READ_CLOCK_PARAM_LEN;
    cmd->offset = 0;
    UINT16_TO_STREAM(pp, HCI_READ_CLOCK);
    UINT8_TO_STREAM(pp, READ_CLOCK_PARAM_LEN);
    UINT16_TO_STREAM(pp, handle);
    UINT8_TO_STREAM(pp, which);

    /* The HCI layer reads the callbacks and the opcode out of the buffer's
     * metadata rather than out of the arguments to transmit_command(), so
     * they are set here, as Bluedroid's own btu_hcif_send_cmd() sets them.
     * Read Clock answers with a Command Complete, never a Command Status. */
    metadata = HCI_GET_CMD_METAMSG(cmd);
    metadata->opcode = HCI_READ_CLOCK;
    metadata->command_complete_cb = clock_complete;
    metadata->command_status_cb = NULL;

    /* Any leftover from a read that timed out, so the wait below is this
     * command's. */
    xSemaphoreTake(answer, 0);
    xSemaphoreTake(slot_lock, portMAX_DELAY);
    seq = ++slot.seq;
    slot.waiting = true;
    slot.sent_us = esp_timer_get_time();
    xSemaphoreGive(slot_lock);
    metadata->context = (void *)(uintptr_t)seq;

    hci_layer_get_interface()->transmit_command(cmd, clock_complete, NULL, NULL);

    answered = xSemaphoreTake(answer, pdMS_TO_TICKS(ANSWER_TIMEOUT_MS)) == pdTRUE;

    xSemaphoreTake(slot_lock, portMAX_DELAY);
    if (answered) {
        *out = slot.got;
    }
    /* Whether it answered or not, nothing is expected now: an answer that
     * arrives after this is dropped rather than kept for the next read. */
    slot.waiting = false;
    xSemaphoreGive(slot_lock);

    return answered ? UTESP_CLOCK_OK : UTESP_CLOCK_TIMED_OUT;
}

int utesp_clock_read(const uint8_t *peer_addr, bool piconet, struct utesp_clock *out)
{
    uint16_t handle = 0;
    int result;

    /* The queue the command goes on exists only while Bluedroid is enabled,
     * so check that here rather than trust every caller to. */
    if (!clock_ready || !out || esp_bluedroid_get_status() != ESP_BLUEDROID_STATUS_ENABLED) {
        return UTESP_CLOCK_NOT_READY;
    }
    if (piconet) {
        BD_ADDR bda;

        if (!peer_addr) {
            return UTESP_CLOCK_NO_LINK;
        }
        memcpy(bda, peer_addr, sizeof(bda));
        handle = BTM_GetHCIConnHandle(bda, BT_TRANSPORT_BR_EDR);
        if (handle == 0xffff) {
            return UTESP_CLOCK_NO_LINK;
        }
    }
    /* The handle is ignored for the board's own clock, so 0 goes out there. */

    xSemaphoreTake(request_lock, portMAX_DELAY);
    result = read_clock(handle, piconet ? WHICH_CLOCK_PICONET : WHICH_CLOCK_NATIVE, out);
    xSemaphoreGive(request_lock);
    return result;
}
