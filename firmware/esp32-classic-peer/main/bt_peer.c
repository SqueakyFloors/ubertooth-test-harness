/* SPDX-License-Identifier: Apache-2.0 */

/* The Bluetooth side of the Classic (BR/EDR) test peer: a discoverable,
 * connectable board with one Serial Port Profile channel, so two of them can
 * hold a link and push data across it while an Ubertooth listens.
 *
 * Everything the air shows is asked for explicitly. The channel is opened
 * with no security, so a link needs no pairing and a capture holds only the
 * SDP lookup, the connection, and the data.
 *
 * Every command that starts something waits for the controller's event and
 * answers from it, so a shell answer is never ahead of the radio. */

#include "peer.h"

#include <ctype.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_bt.h"
#include "esp_bt_device.h"
#include "esp_bt_main.h"
#include "esp_gap_bt_api.h"
#include "esp_spp_api.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "utesp_clock.h"

#define STREAM_PERIOD_MS_DEFAULT 100
#define STREAM_PAYLOAD_DEFAULT   100
#define STREAM_PAYLOAD_MAX       512 /* fits one SPP write with room to spare */
#define SERVER_NAME              "UTESP_SPP"
#define NAME_LEN_MAX             32

/* How long a blocking shell command waits for the controller. An SDP lookup
 * plus a page of a board that is awake takes a second or two; a board that is
 * not there has to time out. */
#define CONNECT_TIMEOUT_MS  15000
#define CLOSE_TIMEOUT_MS    2000
#define SPP_INIT_TIMEOUT_MS 5000
#define SERVER_TIMEOUT_MS   3000

static struct {
    bool started;
    enum peer_role role;
    char name[NAME_LEN_MAX + 1];
    bool discoverable;
    bool server_on;  /* the SPP record is registered and listening */
    bool connecting; /* an outgoing connection is on its way */
    bool connected;
    uint32_t handle;
    bool acl_up;         /* a baseband link to the peer is up */
    uint16_t acl_handle; /* its connection handle, which the clock read needs */
    esp_bd_addr_t peer;   /* what "spp peer" set: who the initiator calls */
    esp_bd_addr_t remote; /* the far end of the live link */
    bool peer_set;
    bool congested;
    bool write_pending; /* a write is with the stack, not yet reported back */
    bool stream_on;
    unsigned period_ms;
    unsigned payload_len;
    uint32_t counter;
    uint32_t writes, write_errors, rx_bytes, connects, disconnects;
    int last_err;
} st;

static SemaphoreHandle_t st_lock;     /* guards st against the callbacks */
static SemaphoreHandle_t link_ev;     /* given when a link opens or closes */
static SemaphoreHandle_t spp_init_ev; /* given once SPP is up */
static SemaphoreHandle_t server_ev;   /* given when the server starts or stops */

/* The lock may not exist if bring-up ran out of memory. The shell still has
 * to answer then, and with no Bluetooth there is nothing to race. */
#define LOCK()                                        \
    do {                                              \
        if (st_lock) {                                \
            xSemaphoreTake(st_lock, portMAX_DELAY);   \
        }                                             \
    } while (0)
#define UNLOCK()                 \
    do {                         \
        if (st_lock) {           \
            xSemaphoreGive(st_lock); \
        }                        \
    } while (0)

/* Bluedroid has the address only while it is enabled, and returns NULL
 * otherwise. The shell must still answer after a failed start, so fall back
 * to all zeros rather than following that NULL. */
static const uint8_t *own_addr(void)
{
    static const uint8_t none[ESP_BD_ADDR_LEN] = {0};
    const uint8_t *a = esp_bt_dev_get_address();

    return a ? a : none;
}

static void addr_text(const esp_bd_addr_t a, char *out)
{
    sprintf(out, "%02X:%02X:%02X:%02X:%02X:%02X", a[0], a[1], a[2], a[3], a[4], a[5]);
}

/* Exactly "aa:bb:cc:dd:ee:ff", either case. Hand-checked rather than left to
 * scanf, whose %2x takes a sign and a single digit. */
static bool addr_parse(const char *s, esp_bd_addr_t out)
{
    if (strlen(s) != 17) {
        return false;
    }
    for (int i = 0; i < 6; i++) {
        const char *p = s + i * 3;
        if (!isxdigit((unsigned char)p[0]) || !isxdigit((unsigned char)p[1])) {
            return false;
        }
        if (i < 5 && p[2] != ':') {
            return false;
        }
        out[i] = (uint8_t)strtoul((char[3]){p[0], p[1], '\0'}, NULL, 16);
    }
    return true;
}

/* The board's own name: fixed for the life of the board, and different from
 * the other one, with no setup step. */
static void default_name(char *out, size_t len)
{
    const uint8_t *a = own_addr();

    snprintf(out, len, "UT-ESP32-%02X%02X%02X", a[3], a[4], a[5]);
}

/* ---------------------------------------------------------------- streaming */

/* Sends payload_len bytes every period_ms while the link is up. The first
 * eight bytes are a magic word and a counter, so a capture can be matched to
 * the run and to the moment inside it; the rest is filler.
 *
 * Only one write is with the stack at a time. esp_spp_write() returns as soon
 * as the write is queued and the outcome arrives later as ESP_SPP_WRITE_EVT,
 * so a second write submitted before that one can be refused where the queue
 * is full. Waiting makes the period a floor rather than a guarantee: a link
 * too slow for it sends fewer, wider-spaced writes instead of reporting
 * writes that never went out.
 *
 * Reusing one buffer is safe either way: esp_spp_write() copies the payload
 * into the Bluetooth task's own message before it returns. */
static void stream_task(void *arg)
{
    static uint8_t buf[STREAM_PAYLOAD_MAX];

    (void)arg;
    memset(buf, 'U', sizeof(buf));

    for (;;) {
        unsigned period, len;
        uint32_t handle, counter;
        bool send;

        LOCK();
        send = st.stream_on && st.connected && !st.congested && !st.write_pending;
        period = st.period_ms;
        len = st.payload_len;
        handle = st.handle;
        counter = st.counter;
        if (send) {
            st.write_pending = true;
        }
        UNLOCK();

        if (send) {
            memcpy(buf, "UTESP", 5);
            buf[5] = (uint8_t)(counter & 0xff);
            buf[6] = (uint8_t)((counter >> 8) & 0xff);
            buf[7] = (uint8_t)((counter >> 16) & 0xff);
            esp_err_t err = esp_spp_write(handle, (int)len, buf);
            LOCK();
            if (err == ESP_OK) {
                /* The counter steps only for a write that was taken, so it
                 * never gains a gap and always matches `writes`. */
                st.counter++;
            } else {
                st.write_pending = false;
                if (st.connected) {
                    /* A write that failed because the link went down in the
                     * meantime is not a write error; only count a real one. */
                    st.write_errors++;
                    st.last_err = (int)err;
                }
            }
            UNLOCK();
        }
        vTaskDelay(pdMS_TO_TICKS(period ? period : STREAM_PERIOD_MS_DEFAULT));
    }
}

/* ---------------------------------------------------------------- callbacks */

static void link_up(uint32_t handle, const esp_bd_addr_t remote)
{
    char text[18];

    LOCK();
    st.connected = true;
    st.connecting = false;
    st.congested = false;
    st.write_pending = false;
    st.handle = handle;
    st.counter = 0;
    memcpy(st.remote, remote, ESP_BD_ADDR_LEN);
    st.connects++;
    UNLOCK();

    addr_text(remote, text);
    shell_line("utesp: spp connected %s", text);
    xSemaphoreGive(link_ev);
}

static void connect_failed(int status)
{
    LOCK();
    st.connecting = false;
    st.last_err = status;
    UNLOCK();
    xSemaphoreGive(link_ev);
}

/* The baseband link under the profiles. Its connection handle is what
 * HCI_Read_Clock needs to name a piconet, and this event is the only place
 * an application is told it; the SPP and audio handles are the profiles'
 * own, not this one. */
static void gap_cb(esp_bt_gap_cb_event_t event, esp_bt_gap_cb_param_t *param)
{
    switch (event) {
    case ESP_BT_GAP_ACL_CONN_CMPL_STAT_EVT:
        if (param->acl_conn_cmpl_stat.stat == ESP_BT_STATUS_SUCCESS) {
            LOCK();
            st.acl_up = true;
            st.acl_handle = param->acl_conn_cmpl_stat.handle;
            UNLOCK();
        }
        break;

    case ESP_BT_GAP_ACL_DISCONN_CMPL_STAT_EVT:
        LOCK();
        /* A board can be left with a second link's close while its own is
         * still up, so only the live one's close clears the handle. */
        if (st.acl_up && st.acl_handle == param->acl_disconn_cmpl_stat.handle) {
            st.acl_up = false;
        }
        UNLOCK();
        break;

    default:
        break;
    }
}

static void spp_cb(esp_spp_cb_event_t event, esp_spp_cb_param_t *param)
{
    switch (event) {
    case ESP_SPP_INIT_EVT: {
        /* SPP is up. Register the server unless a role change has already
         * made this board the initiator. */
        bool want_server;
        LOCK();
        want_server = (st.role == PEER_ROLE_ACCEPTOR);
        UNLOCK();
        if (want_server) {
            esp_spp_start_srv(ESP_SPP_SEC_NONE, ESP_SPP_ROLE_SLAVE, 0, SERVER_NAME);
        }
        xSemaphoreGive(spp_init_ev);
        break;
    }

    case ESP_SPP_START_EVT:
        LOCK();
        st.server_on = (param->start.status == ESP_SPP_SUCCESS);
        UNLOCK();
        xSemaphoreGive(server_ev);
        break;

    case ESP_SPP_SRV_STOP_EVT:
        LOCK();
        if (param->srv_stop.status == ESP_SPP_SUCCESS) {
            st.server_on = false;
        }
        UNLOCK();
        xSemaphoreGive(server_ev);
        break;

    case ESP_SPP_DISCOVERY_COMP_EVT: {
        /* The SDP lookup finished. A lookup that timed out, or whose command
         * was given up on, must not still place a call, so check that one is
         * still wanted. */
        esp_bd_addr_t peer;
        bool wanted;

        LOCK();
        wanted = st.connecting;
        memcpy(peer, st.peer, ESP_BD_ADDR_LEN);
        UNLOCK();
        if (!wanted) {
            break;
        }
        if (param->disc_comp.status == ESP_SPP_SUCCESS && param->disc_comp.scn_num > 0) {
            esp_spp_connect(ESP_SPP_SEC_NONE, ESP_SPP_ROLE_MASTER, param->disc_comp.scn[0],
                            peer);
        } else {
            connect_failed((int)param->disc_comp.status);
        }
        break;
    }

    case ESP_SPP_OPEN_EVT:
        /* This event reports a refused connection too, so check it. */
        if (param->open.status == ESP_SPP_SUCCESS) {
            link_up(param->open.handle, param->open.rem_bda);
        } else {
            connect_failed((int)param->open.status);
        }
        break;

    case ESP_SPP_SRV_OPEN_EVT:
        if (param->srv_open.status == ESP_SPP_SUCCESS) {
            link_up(param->srv_open.handle, param->srv_open.rem_bda);
        }
        break;

    case ESP_SPP_CL_INIT_EVT:
        if (param->cl_init.status != ESP_SPP_SUCCESS) {
            connect_failed((int)param->cl_init.status);
        }
        break;

    case ESP_SPP_CLOSE_EVT: {
        bool ours;
        LOCK();
        /* The server stays registered while a link is up, so a second link
         * can close; only the live one's close clears the state. */
        ours = st.connected && param->close.handle == st.handle;
        if (ours) {
            st.connected = false;
            st.connecting = false;
            st.stream_on = false;
            st.write_pending = false;
            st.handle = 0;
            st.disconnects++;
        }
        UNLOCK();
        if (ours) {
            shell_line("utesp: spp disconnected");
            xSemaphoreGive(link_ev);
        }
        break;
    }

    case ESP_SPP_DATA_IND_EVT:
        LOCK();
        st.rx_bytes += param->data_ind.len;
        UNLOCK();
        break;

    case ESP_SPP_WRITE_EVT:
        /* The outcome of the write arrives here, not from esp_spp_write():
         * a refused write is a success there and a failure here. */
        LOCK();
        st.congested = param->write.cong;
        st.write_pending = false;
        if (param->write.status == ESP_SPP_SUCCESS) {
            st.writes++;
        } else {
            st.write_errors++;
            st.last_err = (int)param->write.status;
        }
        UNLOCK();
        break;

    case ESP_SPP_CONG_EVT:
        LOCK();
        st.congested = param->cong.cong;
        UNLOCK();
        break;

    default:
        break;
    }
}

/* --------------------------------------------------------------- bring-up */

const char *bt_start(void)
{
    esp_err_t err;
    esp_bt_controller_config_t cfg = BT_CONTROLLER_INIT_CONFIG_DEFAULT();

    st.role = PEER_ROLE_ACCEPTOR;
    st.period_ms = STREAM_PERIOD_MS_DEFAULT;
    st.payload_len = STREAM_PAYLOAD_DEFAULT;
    strcpy(st.name, "-");

    st_lock = xSemaphoreCreateMutex();
    link_ev = xSemaphoreCreateBinary();
    spp_init_ev = xSemaphoreCreateBinary();
    server_ev = xSemaphoreCreateBinary();
    if (!st_lock || !link_ev || !spp_init_ev || !server_ev) {
        return "out of memory";
    }

    err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        err = nvs_flash_init();
    }
    if (err != ESP_OK) {
        return "nvs init failed";
    }

    /* Classic only: hand the BLE half of the controller's memory back. */
    esp_bt_controller_mem_release(ESP_BT_MODE_BLE);
    cfg.mode = ESP_BT_MODE_CLASSIC_BT;
    if (esp_bt_controller_init(&cfg) != ESP_OK) {
        return "controller init failed";
    }
    if (esp_bt_controller_enable(ESP_BT_MODE_CLASSIC_BT) != ESP_OK) {
        return "controller enable failed";
    }
    if (esp_bluedroid_init() != ESP_OK || esp_bluedroid_enable() != ESP_OK) {
        return "bluedroid enable failed";
    }
    if (esp_spp_register_callback(spp_cb) != ESP_OK) {
        return "callback registration failed";
    }
    if (esp_bt_gap_register_callback(gap_cb) != ESP_OK) {
        return "gap callback registration failed";
    }

    default_name(st.name, sizeof(st.name));
    if (esp_bt_gap_set_device_name(st.name) != ESP_OK) {
        return "set name failed";
    }

    esp_spp_cfg_t spp_cfg = {
        .mode = ESP_SPP_MODE_CB,
        .enable_l2cap_ertm = true,
        .tx_buffer_size = 0, /* callback mode ignores this */
    };
    if (esp_spp_enhanced_init(&spp_cfg) != ESP_OK) {
        return "spp init failed";
    }
    /* Do not let the shell run before SPP is up and the boot role's server
     * is actually listening, so no command races those callbacks and the
     * ready notice means ready. */
    if (xSemaphoreTake(spp_init_ev, pdMS_TO_TICKS(SPP_INIT_TIMEOUT_MS)) != pdTRUE) {
        return "spp did not come up";
    }
    if (xSemaphoreTake(server_ev, pdMS_TO_TICKS(SERVER_TIMEOUT_MS)) != pdTRUE) {
        return "spp server did not start";
    }
    if (!st.server_on) {
        return "spp server refused to start";
    }

    if (esp_bt_gap_set_scan_mode(ESP_BT_CONNECTABLE, ESP_BT_GENERAL_DISCOVERABLE) != ESP_OK) {
        return "set scan mode failed";
    }
    st.discoverable = true;

    if (xTaskCreate(stream_task, "utesp_stream", 3072, NULL, 5, NULL) != pdPASS) {
        return "stream task failed";
    }
    /* Only the machinery, not a profile: nothing of the audio link is on the
     * air until "sco start". The audio link is the optional half of this
     * firmware, so a board whose SPP side came up is reported as working
     * even when this did not; "sco start" then says so. */
    const char *sco_err = sco_init();
    if (sco_err) {
        shell_line("utesp: audio link unavailable: %s", sco_err);
    }
    /* Same again for the clock read: a board whose SPP side works is worth
     * having even if this did not come up, and "clock" says so itself. */
    if (utesp_clock_init() != UTESP_CLOCK_OK) {
        shell_line("utesp: clock read unavailable");
    }
    st.started = true;
    return NULL;
}

/* ---------------------------------------------------------------- commands */

const char *bt_set_role(enum peer_role role)
{
    /* The audio link belongs to a role (gateway or hands-free unit), so it
     * goes with the old one. */
    const char *err = sco_stop();

    if (err) {
        return err;
    }
    if ((err = bt_spp_stop()) != NULL) {
        return err;
    }
    LOCK();
    st.role = role;
    UNLOCK();
    /* The acceptor is defined as a board that is listening, so put the
     * server back; the initiator has none. */
    return role == PEER_ROLE_ACCEPTOR ? bt_spp_start() : NULL;
}

const char *bt_set_discoverable(bool on)
{
    esp_bt_connection_mode_t c = on ? ESP_BT_CONNECTABLE : ESP_BT_NON_CONNECTABLE;
    esp_bt_discovery_mode_t d = on ? ESP_BT_GENERAL_DISCOVERABLE : ESP_BT_NON_DISCOVERABLE;

    if (esp_bt_gap_set_scan_mode(c, d) != ESP_OK) {
        return "set scan mode failed";
    }
    LOCK();
    st.discoverable = on;
    UNLOCK();
    return NULL;
}

const char *bt_set_peer(const char *addr_text_in)
{
    esp_bd_addr_t a;

    if (!addr_parse(addr_text_in, a)) {
        return "address must be aa:bb:cc:dd:ee:ff";
    }
    LOCK();
    memcpy(st.peer, a, ESP_BD_ADDR_LEN);
    st.peer_set = true;
    UNLOCK();
    return NULL;
}

/* Registers the server and waits for the controller to say it is listening. */
static const char *server_start(void)
{
    bool on;

    LOCK();
    on = st.server_on;
    UNLOCK();
    if (on) {
        return NULL;
    }

    xSemaphoreTake(server_ev, 0);
    if (esp_spp_start_srv(ESP_SPP_SEC_NONE, ESP_SPP_ROLE_SLAVE, 0, SERVER_NAME) != ESP_OK) {
        return "start server failed";
    }
    if (xSemaphoreTake(server_ev, pdMS_TO_TICKS(SERVER_TIMEOUT_MS)) != pdTRUE) {
        return "the server did not start";
    }
    LOCK();
    on = st.server_on;
    UNLOCK();
    return on ? NULL : "the server refused to start";
}

const char *bt_spp_start(void)
{
    enum peer_role role;
    bool connected, peer_set, ok;
    esp_bd_addr_t peer;

    LOCK();
    role = st.role;
    connected = st.connected;
    peer_set = st.peer_set;
    memcpy(peer, st.peer, ESP_BD_ADDR_LEN);
    UNLOCK();

    if (role == PEER_ROLE_ACCEPTOR) {
        /* The server is registered at boot; starting it again is how a test
         * recovers after "spp stop". */
        return server_start();
    }

    if (!peer_set) {
        return "set the peer address first with \"spp peer\"";
    }
    if (connected) {
        return "already connected";
    }

    LOCK();
    st.connecting = true;
    UNLOCK();
    xSemaphoreTake(link_ev, 0); /* drop a stale signal */

    /* Ask the peer over SDP which channel its server is on, then call it. */
    if (esp_spp_start_discovery(peer) != ESP_OK) {
        LOCK();
        st.connecting = false;
        UNLOCK();
        return "sdp lookup failed";
    }
    if (xSemaphoreTake(link_ev, pdMS_TO_TICKS(CONNECT_TIMEOUT_MS)) != pdTRUE) {
        LOCK();
        st.connecting = false;
        UNLOCK();
        return "connect timed out";
    }

    /* An incoming link from somewhere else also signals link_ev, so only
     * a link to the address asked for counts as success. */
    LOCK();
    ok = st.connected && memcmp(st.remote, peer, ESP_BD_ADDR_LEN) == 0;
    UNLOCK();
    return ok ? NULL : "connect refused";
}

const char *bt_spp_stop(void)
{
    bool connected, server_on;
    uint32_t handle;

    LOCK();
    st.stream_on = false;
    connected = st.connected;
    server_on = st.server_on;
    handle = st.handle;
    UNLOCK();

    if (connected) {
        xSemaphoreTake(link_ev, 0); /* the link coming up left a signal */
        if (esp_spp_disconnect(handle) != ESP_OK) {
            return "disconnect failed";
        }
        /* Wait for the close so "status" right after this is honest, and say
         * so rather than claim a stop that did not happen. */
        xSemaphoreTake(link_ev, pdMS_TO_TICKS(CLOSE_TIMEOUT_MS));
        LOCK();
        connected = st.connected;
        UNLOCK();
        if (connected) {
            return "the link did not close";
        }
    }

    if (server_on) {
        xSemaphoreTake(server_ev, 0);
        if (esp_spp_stop_srv() != ESP_OK) {
            return "stop server failed";
        }
        if (xSemaphoreTake(server_ev, pdMS_TO_TICKS(SERVER_TIMEOUT_MS)) != pdTRUE) {
            return "the server did not stop";
        }
        LOCK();
        server_on = st.server_on;
        UNLOCK();
        if (server_on) {
            return "the server refused to stop";
        }
    }
    return NULL;
}

const char *bt_stream(bool on)
{
    bool connected;

    /* One lock for both: a close between a check and the arming would leave
     * streaming armed for whatever link comes next. */
    LOCK();
    connected = st.connected;
    if (connected || !on) {
        st.stream_on = on;
    }
    UNLOCK();
    return (on && !connected) ? "no connection to stream on" : NULL;
}

const char *bt_stream_period(unsigned ms)
{
    if (ms < 5 || ms > 10000) {
        return "period must be 5 to 10000 ms";
    }
    LOCK();
    st.period_ms = ms;
    UNLOCK();
    return NULL;
}

const char *bt_stream_payload(unsigned bytes)
{
    if (bytes < 8 || bytes > STREAM_PAYLOAD_MAX) {
        return "payload must be 8 to 512 bytes";
    }
    LOCK();
    st.payload_len = bytes;
    UNLOCK();
    return NULL;
}

const char *bt_defaults(void)
{
    char name[NAME_LEN_MAX + 1];
    const char *err = sco_stop();

    if (err) {
        return err;
    }
    if ((err = bt_spp_stop()) != NULL) {
        return err;
    }
    LOCK();
    st.role = PEER_ROLE_ACCEPTOR;
    st.period_ms = STREAM_PERIOD_MS_DEFAULT;
    st.payload_len = STREAM_PAYLOAD_DEFAULT;
    st.peer_set = false;
    memset(st.peer, 0, sizeof(st.peer));
    st.writes = st.write_errors = st.rx_bytes = 0;
    st.connects = st.disconnects = 0;
    st.counter = 0;
    st.last_err = 0;
    UNLOCK();
    sco_zero_counters();

    default_name(name, sizeof(name));
    if (esp_bt_gap_set_device_name(name) != ESP_OK) {
        return "set name failed";
    }
    LOCK();
    strcpy(st.name, name);
    UNLOCK();

    if ((err = bt_set_discoverable(true)) != NULL) {
        return err;
    }
    return bt_spp_start();
}

/* The audio link needs the role and the address "spp peer" set, and both
 * live here under the lock, so the shell comes through this. */
const char *bt_sco_start(void)
{
    enum peer_role role;
    esp_bd_addr_t peer;
    bool peer_set;

    LOCK();
    role = st.role;
    peer_set = st.peer_set;
    memcpy(peer, st.peer, ESP_BD_ADDR_LEN);
    UNLOCK();

    return sco_start(role, peer_set ? peer : NULL);
}

void bt_status_json(char *out, size_t len)
{
    char self[18], peer[18], remote[18];
    const uint8_t *a = own_addr();
    struct sco_view sco;
    int n;

    addr_text(a, self);
    /* Read before taking this module's lock: sco_get() takes its own. */
    sco_get(&sco);
    LOCK();
    addr_text(st.peer, peer);
    addr_text(st.remote, remote);
    n = snprintf(out, len,
                 "{\"role\":\"%s\",\"fw\":\"%s\",\"bt\":%s,\"address\":\"%s\","
                 "\"lap\":\"%02x%02x%02x\",\"uap\":\"%02x\",\"nap\":\"%02x%02x\","
                 "\"name\":\"%s\",\"discoverable\":%s,\"spp_server\":%s,"
                 "\"connecting\":%s,\"connected\":%s,\"peer\":\"%s\",\"remote\":\"%s\","
                 "\"stream\":%s,\"period_ms\":%u,\"payload_len\":%u,\"counter\":%" PRIu32 ","
                 "\"writes\":%" PRIu32 ",\"write_errors\":%" PRIu32 ",\"rx_bytes\":%" PRIu32 ","
                 "\"connects\":%" PRIu32 ",\"disconnects\":%" PRIu32 ",\"last_err\":%d,"
                 "\"sco\":\"%s\",\"sco_profile\":\"%s\",\"sco_codec\":\"%s\","
                 "\"sco_handle\":%u,\"sco_frame_len\":%u,\"sco_tx_frames\":%" PRIu32 ","
                 "\"sco_tx_errors\":%" PRIu32 ",\"sco_rx_frames\":%" PRIu32 ","
                 "\"sco_rx_bad\":%" PRIu32 "}",
                 st.role == PEER_ROLE_ACCEPTOR ? "acceptor" : "initiator", UTESP_FW_VERSION,
                 st.started ? "true" : "false", self, a[3], a[4], a[5], a[2], a[0], a[1],
                 st.name, st.discoverable ? "true" : "false", st.server_on ? "true" : "false",
                 st.connecting ? "true" : "false", st.connected ? "true" : "false",
                 st.peer_set ? peer : "", st.connected ? remote : "",
                 st.stream_on ? "true" : "false", st.period_ms, st.payload_len, st.counter,
                 st.writes, st.write_errors, st.rx_bytes, st.connects, st.disconnects,
                 st.last_err, sco.state, sco.profile, sco.codec, sco.handle,
                 sco.frame_len, sco.tx_frames, sco.tx_errors, sco.rx_frames, sco.rx_bad);
    UNLOCK();

    /* Truncated JSON would be a line a driver silently mis-parses. Say so
     * instead, in JSON a driver can still read. */
    if (n < 0 || (size_t)n >= len) {
        snprintf(out, len, "{\"fw\":\"%s\",\"error\":\"status line too long\"}",
                 UTESP_FW_VERSION);
    }
}

/* ------------------------------------------------------------- the clocks */

static const char *read_failure(int result)
{
    switch (result) {
    case UTESP_CLOCK_NOT_READY:
        return "the clock read is not available";
    case UTESP_CLOCK_NO_MEMORY:
        return "out of memory";
    case UTESP_CLOCK_TIMED_OUT:
        return "the controller did not answer the clock read";
    case UTESP_CLOCK_BAD_ANSWER:
        return "the controller's clock answer was short";
    default:
        return "the clock read failed";
    }
}

/* A controller that refuses the read is reported with the status it gave,
 * rather than as an empty answer. Only the shell task gets here, so the one
 * buffer is enough. */
static const char *refused(uint8_t status)
{
    static char reason[64];

    snprintf(reason, sizeof(reason), "the controller refused the clock read, status 0x%02x",
             status);
    return reason;
}

const char *bt_clock_json(char *out, size_t len)
{
    struct utesp_clock native, piconet;
    const struct utesp_clock *reported;
    char piconet_fields[80];
    bool started, in_piconet;
    uint16_t acl_handle;
    int result;
    int n;

    LOCK();
    started = st.started;
    in_piconet = st.acl_up;
    acl_handle = st.acl_handle;
    UNLOCK();
    if (!started) {
        return "bluetooth is not up";
    }

    if ((result = utesp_clock_read(0, false, &native)) != UTESP_CLOCK_OK) {
        return read_failure(result);
    }
    if (native.status != 0) {
        return refused(native.status);
    }

    if (in_piconet) {
        result = utesp_clock_read(acl_handle, true, &piconet);
        if (result == UTESP_CLOCK_NO_LINK) {
            /* The link went down between the two reads. */
            in_piconet = false;
        } else if (result != UTESP_CLOCK_OK) {
            return read_failure(result);
        } else if (piconet.status != 0) {
            return refused(piconet.status);
        }
    }

    if (in_piconet) {
        snprintf(piconet_fields, sizeof(piconet_fields),
                 "\"piconet\":%" PRIu32 ",\"accuracy\":%u,\"handle\":%u", piconet.clock,
                 piconet.accuracy, piconet.handle);
    } else {
        snprintf(piconet_fields, sizeof(piconet_fields),
                 "\"piconet\":null,\"accuracy\":null,\"handle\":null");
    }

    /* The two reads are separate commands, so the timestamp and the latency
     * are those of the piconet read, the one a test lines up against a
     * capture; with no piconet they are the native read's. */
    reported = in_piconet ? &piconet : &native;
    n = snprintf(out, len,
                 "{\"native\":%" PRIu32 ",%s,\"t_us\":%" PRId64 ",\"read_us\":%" PRId64 "}",
                 native.clock, piconet_fields, reported->answered_us, reported->read_us);
    if (n < 0 || (size_t)n >= len) {
        return "the clock line does not fit";
    }
    return NULL;
}
