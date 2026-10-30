/* SPDX-License-Identifier: Apache-2.0 */

/* The Bluetooth side of the Classic (BR/EDR) test peer: a discoverable,
 * connectable board with one Serial Port Profile channel, so two of them can
 * hold a link and push data across it while an Ubertooth listens.
 *
 * Everything the air shows is asked for explicitly. The channel is opened
 * with no security, so a link needs no pairing and a capture holds only the
 * SDP lookup, the connection, and the data. */

#include "peer.h"

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

#define STREAM_PERIOD_MS_DEFAULT 100
#define STREAM_PAYLOAD_DEFAULT   100
#define STREAM_PAYLOAD_MAX       512 /* fits one SPP write with room to spare */
#define SERVER_NAME              "UTESP_SPP"
#define INQUIRY_RESULTS_MAX      16
#define NAME_LEN_MAX             32

/* How long a blocking shell command waits for the controller. An SDP lookup
 * plus a page of a board that is awake takes a second or two; a board that is
 * not there has to time out. */
#define CONNECT_TIMEOUT_MS 15000
#define CLOSE_TIMEOUT_MS   2000
#define INQUIRY_EXTRA_MS   3000
#define SPP_INIT_TIMEOUT_MS 5000

struct inquiry_result {
    esp_bd_addr_t bda;
    int rssi;
    uint32_t cod;
    char name[NAME_LEN_MAX + 1];
};

static struct {
    bool started;
    enum peer_role role;
    char name[NAME_LEN_MAX + 1];
    bool discoverable;
    bool server_on;  /* the SPP record is registered and listening */
    bool connecting; /* an outgoing connection is on its way */
    bool connected;
    uint32_t handle;
    esp_bd_addr_t peer;   /* what "spp peer" set: who the initiator calls */
    esp_bd_addr_t remote; /* the far end of the live link */
    bool peer_set;
    bool congested;
    bool stream_on;
    unsigned period_ms;
    unsigned payload_len;
    uint32_t counter;
    uint32_t writes, write_errors, rx_bytes, connects, disconnects;
    int last_err;

    struct inquiry_result inq[INQUIRY_RESULTS_MAX];
    unsigned inq_count;
} st;

static SemaphoreHandle_t st_lock;     /* guards st against the callbacks */
static SemaphoreHandle_t link_ev;     /* given when a link opens or closes */
static SemaphoreHandle_t inquiry_ev;  /* given when an inquiry ends */
static SemaphoreHandle_t spp_init_ev; /* given once SPP is up */

#define LOCK()   xSemaphoreTake(st_lock, portMAX_DELAY)
#define UNLOCK() xSemaphoreGive(st_lock)

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

/* "aa:bb:cc:dd:ee:ff", either case, colons required. */
static bool addr_parse(const char *s, esp_bd_addr_t out)
{
    unsigned v[6];
    char tail;

    if (sscanf(s, "%2x:%2x:%2x:%2x:%2x:%2x%c", &v[0], &v[1], &v[2], &v[3], &v[4], &v[5],
               &tail) != 6) {
        return false;
    }
    for (int i = 0; i < 6; i++) {
        out[i] = (uint8_t)v[i];
    }
    return true;
}

/* ESP-IDF names the transmit power levels in steps, not dBm. The table is the
 * one in esp_bt.h: -12 dBm to +9 dBm in 3 dB steps. */
static bool tx_power_level(int dbm, esp_power_level_t *out)
{
    if (dbm < -12 || dbm > 9 || (dbm + 12) % 3 != 0) {
        return false;
    }
    *out = (esp_power_level_t)((dbm + 12) / 3);
    return true;
}

/* The inverse, for the status line: what the controller reports back. */
static int tx_power_dbm(esp_power_level_t level)
{
    return (int)level * 3 - 12;
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
 * Reusing one buffer is safe: esp_spp_write() copies the payload into the
 * Bluetooth task's own message before it returns. */
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
        send = st.stream_on && st.connected && !st.congested;
        period = st.period_ms;
        len = st.payload_len;
        handle = st.handle;
        counter = st.counter;
        if (send) {
            st.counter++;
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
                st.writes++;
            } else if (st.connected) {
                /* A write that failed because the link went down in the
                 * meantime is not a write error; only count a real one. */
                st.write_errors++;
                st.last_err = (int)err;
            }
            UNLOCK();
        }
        vTaskDelay(pdMS_TO_TICKS(period ? period : STREAM_PERIOD_MS_DEFAULT));
    }
}

/* ---------------------------------------------------------------- callbacks */

static void gap_cb(esp_bt_gap_cb_event_t event, esp_bt_gap_cb_param_t *param)
{
    switch (event) {
    case ESP_BT_GAP_DISC_RES_EVT: {
        struct inquiry_result *r = NULL;

        LOCK();
        for (unsigned i = 0; i < st.inq_count; i++) {
            if (memcmp(st.inq[i].bda, param->disc_res.bda, ESP_BD_ADDR_LEN) == 0) {
                r = &st.inq[i];
                break;
            }
        }
        if (!r && st.inq_count < INQUIRY_RESULTS_MAX) {
            r = &st.inq[st.inq_count++];
            memset(r, 0, sizeof(*r));
            memcpy(r->bda, param->disc_res.bda, ESP_BD_ADDR_LEN);
        }
        if (r) {
            for (int i = 0; i < param->disc_res.num_prop; i++) {
                esp_bt_gap_dev_prop_t *p = &param->disc_res.prop[i];
                if (p->type == ESP_BT_GAP_DEV_PROP_RSSI) {
                    r->rssi = *(int8_t *)p->val;
                } else if (p->type == ESP_BT_GAP_DEV_PROP_COD) {
                    r->cod = *(uint32_t *)p->val;
                } else if (p->type == ESP_BT_GAP_DEV_PROP_BDNAME) {
                    int n = p->len < NAME_LEN_MAX ? p->len : NAME_LEN_MAX;
                    memcpy(r->name, p->val, n);
                    r->name[n] = '\0';
                }
            }
        }
        UNLOCK();
        break;
    }
    case ESP_BT_GAP_DISC_STATE_CHANGED_EVT:
        if (param->disc_st_chg.state == ESP_BT_GAP_DISCOVERY_STOPPED) {
            xSemaphoreGive(inquiry_ev);
        }
        break;
    default:
        break;
    }
}

static void link_up(uint32_t handle, const esp_bd_addr_t remote)
{
    char text[18];

    LOCK();
    st.connected = true;
    st.connecting = false;
    st.congested = false;
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
        break;

    case ESP_SPP_DISCOVERY_COMP_EVT:
        /* The SDP lookup finished. Call the first channel it offered. */
        if (param->disc_comp.status == ESP_SPP_SUCCESS && param->disc_comp.scn_num > 0) {
            esp_bd_addr_t peer;
            LOCK();
            memcpy(peer, st.peer, ESP_BD_ADDR_LEN);
            UNLOCK();
            esp_spp_connect(ESP_SPP_SEC_NONE, ESP_SPP_ROLE_MASTER, param->disc_comp.scn[0],
                            peer);
        } else {
            connect_failed((int)param->disc_comp.status);
        }
        break;

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
        LOCK();
        st.congested = param->write.cong;
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

    st_lock = xSemaphoreCreateMutex();
    link_ev = xSemaphoreCreateBinary();
    inquiry_ev = xSemaphoreCreateBinary();
    spp_init_ev = xSemaphoreCreateBinary();
    if (!st_lock || !link_ev || !inquiry_ev || !spp_init_ev) {
        return "out of memory";
    }

    st.role = PEER_ROLE_ACCEPTOR;
    st.period_ms = STREAM_PERIOD_MS_DEFAULT;
    st.payload_len = STREAM_PAYLOAD_DEFAULT;
    strcpy(st.name, "-");

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
    if (esp_bt_gap_register_callback(gap_cb) != ESP_OK ||
        esp_spp_register_callback(spp_cb) != ESP_OK) {
        return "callback registration failed";
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
    /* Do not let the shell run before SPP is up, so no command races the
     * init callback. */
    if (xSemaphoreTake(spp_init_ev, pdMS_TO_TICKS(SPP_INIT_TIMEOUT_MS)) != pdTRUE) {
        return "spp did not come up";
    }

    if (esp_bt_gap_set_scan_mode(ESP_BT_CONNECTABLE, ESP_BT_GENERAL_DISCOVERABLE) != ESP_OK) {
        return "set scan mode failed";
    }
    st.discoverable = true;
    st.started = true;

    if (xTaskCreate(stream_task, "utesp_stream", 3072, NULL, 5, NULL) != pdPASS) {
        return "stream task failed";
    }
    return NULL;
}

/* ---------------------------------------------------------------- commands */

const char *bt_set_role(enum peer_role role)
{
    const char *err = bt_spp_stop();

    if (err) {
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

const char *bt_spp_start(void)
{
    enum peer_role role;
    bool connected, peer_set, server_on;
    esp_bd_addr_t peer;

    LOCK();
    role = st.role;
    connected = st.connected;
    peer_set = st.peer_set;
    server_on = st.server_on;
    memcpy(peer, st.peer, ESP_BD_ADDR_LEN);
    UNLOCK();

    if (role == PEER_ROLE_ACCEPTOR) {
        /* The server is registered at boot; starting it again is how a test
         * recovers after "spp stop". */
        if (server_on) {
            return NULL;
        }
        if (esp_spp_start_srv(ESP_SPP_SEC_NONE, ESP_SPP_ROLE_SLAVE, 0, SERVER_NAME) != ESP_OK) {
            return "start server failed";
        }
        return NULL;
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
    bool ok;
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
        esp_spp_disconnect(handle);
        /* Wait for the close so "status" right after this is honest. */
        xSemaphoreTake(link_ev, pdMS_TO_TICKS(CLOSE_TIMEOUT_MS));
    }
    if (server_on) {
        if (esp_spp_stop_srv() != ESP_OK) {
            return "stop server failed";
        }
        LOCK();
        st.server_on = false;
        UNLOCK();
    }
    return NULL;
}

const char *bt_stream(bool on)
{
    bool connected;

    LOCK();
    connected = st.connected;
    UNLOCK();

    if (on && !connected) {
        return "no connection to stream on";
    }
    LOCK();
    st.stream_on = on;
    UNLOCK();
    return NULL;
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

const char *bt_tx_power(int min_dbm, int max_dbm)
{
    esp_power_level_t min_level, max_level;

    if (!tx_power_level(min_dbm, &min_level) || !tx_power_level(max_dbm, &max_level)) {
        return "power must be -12, -9, -6, -3, 0, 3, 6, or 9 dBm";
    }
    if (min_dbm > max_dbm) {
        return "minimum above maximum";
    }
    if (esp_bredr_tx_power_set(min_level, max_level) != ESP_OK) {
        return "set transmit power failed";
    }
    return NULL;
}

const char *bt_defaults(void)
{
    char name[NAME_LEN_MAX + 1];
    const char *err = bt_spp_stop();

    if (err) {
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
    if (esp_bredr_tx_power_set(ESP_PWR_LVL_N12, ESP_PWR_LVL_P9) != ESP_OK) {
        return "set transmit power failed";
    }
    return bt_spp_start();
}

const char *bt_inquiry(unsigned seconds)
{
    /* The controller counts the inquiry length in 1.28 s units. */
    uint8_t units;

    if (seconds < 2 || seconds > 60) {
        return "inquiry length must be 2 to 60 seconds";
    }
    units = (uint8_t)((seconds * 100 + 127) / 128);
    if (units < 1) {
        units = 1;
    }

    LOCK();
    st.inq_count = 0;
    UNLOCK();
    xSemaphoreTake(inquiry_ev, 0);

    if (esp_bt_gap_start_discovery(ESP_BT_INQ_MODE_GENERAL_INQUIRY, units, 0) != ESP_OK) {
        return "start inquiry failed";
    }
    if (xSemaphoreTake(inquiry_ev, pdMS_TO_TICKS(seconds * 1000 + INQUIRY_EXTRA_MS)) != pdTRUE) {
        esp_bt_gap_cancel_discovery();
        return "inquiry did not finish";
    }

    LOCK();
    unsigned n = st.inq_count;
    struct inquiry_result results[INQUIRY_RESULTS_MAX];
    memcpy(results, st.inq, sizeof(results));
    UNLOCK();

    for (unsigned i = 0; i < n; i++) {
        char text[18];
        addr_text(results[i].bda, text);
        shell_line("%s rssi=%d cod=0x%06" PRIx32 " name=%s", text, results[i].rssi,
                   results[i].cod, results[i].name[0] ? results[i].name : "-");
    }
    return NULL;
}

void bt_status_json(char *out, size_t len)
{
    char self[18], peer[18], remote[18];
    const uint8_t *a = own_addr();
    esp_power_level_t got_min = ESP_PWR_LVL_N12, got_max = ESP_PWR_LVL_P9;
    int n;

    esp_bredr_tx_power_get(&got_min, &got_max);
    addr_text(a, self);
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
                 "\"connects\":%" PRIu32 ",\"disconnects\":%" PRIu32 ","
                 "\"tx_power_dbm\":[%d,%d],\"last_err\":%d}",
                 st.role == PEER_ROLE_ACCEPTOR ? "acceptor" : "initiator", UTESP_FW_VERSION,
                 st.started ? "true" : "false", self, a[3], a[4], a[5], a[2], a[0], a[1],
                 st.name, st.discoverable ? "true" : "false", st.server_on ? "true" : "false",
                 st.connecting ? "true" : "false", st.connected ? "true" : "false",
                 st.peer_set ? peer : "", st.connected ? remote : "",
                 st.stream_on ? "true" : "false", st.period_ms, st.payload_len, st.counter,
                 st.writes, st.write_errors, st.rx_bytes, st.connects, st.disconnects,
                 tx_power_dbm(got_min), tx_power_dbm(got_max), st.last_err);
    UNLOCK();

    /* Truncated JSON would be a line a driver silently mis-parses. Say so
     * instead, in JSON a driver can still read. */
    if (n < 0 || (size_t)n >= len) {
        snprintf(out, len, "{\"fw\":\"%s\",\"error\":\"status line too long\"}",
                 UTESP_FW_VERSION);
    }
}
