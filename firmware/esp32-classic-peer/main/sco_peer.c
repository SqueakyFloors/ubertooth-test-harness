/* SPDX-License-Identifier: Apache-2.0 */

/* The synchronous audio link of the Classic (BR/EDR) test peer, so a capture
 * holds reserved-slot voice traffic as well as the ACL data the SPP side
 * makes.
 *
 * ESP-IDF gives an application synchronous links only through the Hands-Free
 * Profile, so that is how the link is brought up: the initiator runs the
 * audio gateway and pages, which keeps it the piconet master as it is for
 * SPP, and the acceptor runs the hands-free unit. ESP-IDF includes both
 * profiles in one image but says they cannot run at the same time, so only
 * the one the role needs is started. Nothing here starts until "sco start",
 * which leaves a board that only does SPP on the air exactly as it was.
 *
 * Audio goes over HCI rather than the board's PCM pins, and the application
 * supplies it. There is no microphone here, so each frame is a known
 * pattern: a little-endian frame counter followed by a ramp.
 *
 * The two sides clock each other. A synchronous link is symmetric and the
 * controller hands up one frame per interval, so answering each frame with
 * one frame of the same length sends at exactly the link's rate, with no
 * timer here to drift against it. Each side sends a couple of frames when
 * the link comes up to start the exchange.
 *
 * What the controller negotiated for the link - SCO or eSCO, the packet
 * type, the interval and the retransmission window - is not reported by any
 * ESP-IDF API; see the README for where it stops inside the stack. The
 * codec, the frame size, and the controller's own packet counters are
 * reported, and those are what a test can check. */

#include "peer.h"

#include <stdio.h>
#include <string.h>

#include "esp_bt.h"
#include "esp_hf_ag_api.h"
#include "esp_hf_client_api.h"
#include "esp_hf_defs.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

/* How long a blocking shell command waits for the controller. Bringing the
 * service level connection up pages the peer and then exchanges a dozen AT
 * commands, so it is the slowest step. */
#define PROF_TIMEOUT_MS  3000
#define SLC_TIMEOUT_MS   15000
#define AUDIO_TIMEOUT_MS 10000
#define STATS_TIMEOUT_MS 2000

/* The cap on one audio frame. A CVSD link over HCI asks for 60 bytes of
 * 16-bit linear audio; the link's real frame size comes from the audio state
 * event and is checked against this. */
#define FRAME_MAX 240

/* Frames sent when the link comes up: the first starts the far end sending,
 * the second keeps the link fed while that first answer crosses. */
#define PRIME_FRAMES 2

/* At most this many frames may be owed at once. A link that outruns the
 * sender drops credits rather than building a queue, which keeps the audio
 * live instead of late. */
#define FRAME_CREDIT_MAX 4

struct pkt_stats {
    uint32_t rx_total, rx_correct, rx_err, rx_none, rx_lost;
    uint32_t tx_total, tx_discarded;
};

static struct {
    bool profile_up;  /* one of the two HFP profiles is initialized */
    enum peer_role role;
    bool slc;    /* the service level connection is up */
    bool audio;  /* the synchronous link is up */
    uint16_t handle;
    uint16_t frame_len;
    bool msbc; /* the codec the controller settled on */
    uint32_t counter;
    uint32_t tx_frames, tx_errors, rx_frames, rx_bad;
    esp_bd_addr_t peer;
    struct pkt_stats stats;
    bool stats_ok;
    esp_hf_prof_state_t prof_state;
} sc;

static SemaphoreHandle_t sc_lock;
static SemaphoreHandle_t prof_ev;  /* a profile finished starting or stopping */
static SemaphoreHandle_t slc_ev;   /* the service level connection changed */
static SemaphoreHandle_t audio_ev; /* the synchronous link changed */
static SemaphoreHandle_t stats_ev; /* the packet counters arrived */
static SemaphoreHandle_t frame_ev; /* counting: one per frame owed */

#define LOCK()                                      \
    do {                                            \
        if (sc_lock) {                              \
            xSemaphoreTake(sc_lock, portMAX_DELAY); \
        }                                           \
    } while (0)
#define UNLOCK()                 \
    do {                         \
        if (sc_lock) {           \
            xSemaphoreGive(sc_lock); \
        }                        \
    } while (0)

/* ------------------------------------------------------------- the pattern */

/* One frame of the known pattern: a little-endian 32-bit frame counter, then
 * a ramp. A test can tell one frame from the next, and a gap from a loss.
 *
 * CVSD is a lossy air format, so the far end counts frames rather than
 * comparing bytes; the counter is there for a capture to read. */
static void fill_frame(uint8_t *out, uint16_t len, uint32_t counter)
{
    out[0] = (uint8_t)(counter & 0xff);
    out[1] = (uint8_t)((counter >> 8) & 0xff);
    out[2] = (uint8_t)((counter >> 16) & 0xff);
    out[3] = (uint8_t)((counter >> 24) & 0xff);
    for (uint16_t i = 4; i < len; i++) {
        out[i] = (uint8_t)i;
    }
}

/* Hands one frame to the stack. The two profiles have separate buffer pools,
 * so which one is in use decides every call here. */
static void send_frame(void)
{
    enum peer_role role;
    esp_hf_audio_buff_t *buf;
    uint16_t handle, len;
    uint32_t counter;
    bool audio;
    esp_err_t err;

    LOCK();
    audio = sc.audio;
    role = sc.role;
    handle = sc.handle;
    len = sc.frame_len;
    counter = sc.counter;
    if (audio) {
        sc.counter++;
    }
    UNLOCK();

    if (!audio || len == 0 || len > FRAME_MAX) {
        return;
    }

    buf = (role == PEER_ROLE_INITIATOR) ? esp_hf_ag_audio_buff_alloc(len)
                                        : esp_hf_client_audio_buff_alloc(len);
    if (!buf) {
        LOCK();
        sc.tx_errors++;
        UNLOCK();
        return;
    }
    fill_frame(buf->data, len, counter);
    buf->data_len = len;

    err = (role == PEER_ROLE_INITIATOR) ? esp_hf_ag_audio_data_send(handle, buf)
                                        : esp_hf_client_audio_data_send(handle, buf);
    LOCK();
    if (err == ESP_OK) {
        sc.tx_frames++;
    } else {
        sc.tx_errors++;
    }
    UNLOCK();
    if (err != ESP_OK) {
        /* A send that was refused never took the buffer. */
        if (role == PEER_ROLE_INITIATOR) {
            esp_hf_ag_audio_buff_free(buf);
        } else {
            esp_hf_client_audio_buff_free(buf);
        }
    }
}

/* Sends on its own task rather than inside the callback: the callback runs on
 * the Bluetooth task, and handing work back to that same task from inside it
 * is how a stack deadlocks. */
static void sco_task(void *arg)
{
    (void)arg;
    for (;;) {
        xSemaphoreTake(frame_ev, portMAX_DELAY);
        send_frame();
    }
}

static void owe_frames(unsigned n)
{
    for (unsigned i = 0; i < n; i++) {
        xSemaphoreGive(frame_ev);
    }
}

/* ---------------------------------------------------------------- callbacks */

/* Both profiles report an incoming frame the same way, and either way the
 * application owns the buffer and frees it. */
static void frame_in(esp_hf_audio_buff_t *buf, bool bad, enum peer_role role)
{
    LOCK();
    sc.rx_frames++;
    if (bad) {
        sc.rx_bad++;
    }
    UNLOCK();

    if (role == PEER_ROLE_INITIATOR) {
        esp_hf_ag_audio_buff_free(buf);
    } else {
        esp_hf_client_audio_buff_free(buf);
    }
    /* Answer this frame with one of ours, which is what keeps the sending
     * rate equal to the link's. */
    xSemaphoreGive(frame_ev);
}

static void ag_frame_in(esp_hf_sync_conn_hdl_t hdl, esp_hf_audio_buff_t *buf, bool bad)
{
    (void)hdl;
    frame_in(buf, bad, PEER_ROLE_INITIATOR);
}

static void hf_frame_in(esp_hf_sync_conn_hdl_t hdl, esp_hf_audio_buff_t *buf, bool bad)
{
    (void)hdl;
    frame_in(buf, bad, PEER_ROLE_ACCEPTOR);
}

/* Records a synchronous link coming up or going down. The state carries the
 * codec: there is a separate value for an mSBC link. */
static void audio_changed(int state, uint16_t handle, uint16_t frame_len)
{
    bool up = (state == ESP_HF_AUDIO_STATE_CONNECTED ||
               state == ESP_HF_AUDIO_STATE_CONNECTED_MSBC);

    LOCK();
    sc.audio = up;
    sc.handle = handle;
    sc.msbc = (state == ESP_HF_AUDIO_STATE_CONNECTED_MSBC);
    if (up) {
        sc.frame_len = frame_len;
        sc.counter = 0;
    } else {
        sc.frame_len = 0;
    }
    UNLOCK();

    if (state == ESP_HF_AUDIO_STATE_CONNECTING) {
        return; /* not an answer to anything yet */
    }
    shell_line("utesp: sco %s", up ? "connected" : "disconnected");
    if (up) {
        owe_frames(PRIME_FRAMES);
    }
    xSemaphoreGive(audio_ev);
}

static void ag_cb(esp_hf_cb_event_t event, esp_hf_cb_param_t *param)
{
    switch (event) {
    case ESP_HF_PROF_STATE_EVT:
        LOCK();
        sc.prof_state = param->prof_stat.state;
        UNLOCK();
        xSemaphoreGive(prof_ev);
        break;

    case ESP_HF_CONNECTION_STATE_EVT:
        LOCK();
        sc.slc = (param->conn_stat.state == ESP_HF_CONNECTION_STATE_SLC_CONNECTED);
        UNLOCK();
        if (param->conn_stat.state == ESP_HF_CONNECTION_STATE_SLC_CONNECTED ||
            param->conn_stat.state == ESP_HF_CONNECTION_STATE_DISCONNECTED) {
            xSemaphoreGive(slc_ev);
        }
        break;

    case ESP_HF_AUDIO_STATE_EVT:
        audio_changed(param->audio_stat.state, param->audio_stat.sync_conn_handle,
                      param->audio_stat.preferred_frame_size);
        break;

    /* The hands-free unit asks for these while the service level connection
     * comes up, and the profile does not come up until they are answered.
     * The answers describe an idle phone with full signal: no call, no
     * roaming, nothing that would make the far end ask for more. */
    case ESP_HF_CIND_RESPONSE_EVT:
        esp_hf_ag_cind_response(param->cind_rep.remote_addr, ESP_HF_CALL_STATUS_NO_CALLS,
                                ESP_HF_CALL_SETUP_STATUS_IDLE,
                                ESP_HF_NETWORK_STATE_AVAILABLE, 5,
                                ESP_HF_ROAMING_STATUS_INACTIVE, 5,
                                ESP_HF_CALL_HELD_STATUS_NONE);
        break;

    case ESP_HF_COPS_RESPONSE_EVT:
        esp_hf_ag_cops_response(param->cops_rep.remote_addr, "UT-ESP32");
        break;

    case ESP_HF_UNAT_RESPONSE_EVT:
        /* An AT command this gateway does not know. A NULL answer is the
         * plain "OK", which keeps the far end moving. */
        esp_hf_ag_unknown_at_send(param->unat_rep.remote_addr, NULL);
        break;

    case ESP_HF_BCS_RESPONSE_EVT:
        LOCK();
        sc.msbc = (param->bcs_rep.mode == ESP_HF_WBS_YES);
        UNLOCK();
        break;

    case ESP_HF_PKT_STAT_NUMS_GET_EVT:
        LOCK();
        sc.stats.rx_total = param->pkt_nums.rx_total;
        sc.stats.rx_correct = param->pkt_nums.rx_correct;
        sc.stats.rx_err = param->pkt_nums.rx_err;
        sc.stats.rx_none = param->pkt_nums.rx_none;
        sc.stats.rx_lost = param->pkt_nums.rx_lost;
        sc.stats.tx_total = param->pkt_nums.tx_total;
        sc.stats.tx_discarded = param->pkt_nums.tx_discarded;
        sc.stats_ok = true;
        UNLOCK();
        xSemaphoreGive(stats_ev);
        break;

    default:
        break;
    }
}

static void hf_cb(esp_hf_client_cb_event_t event, esp_hf_client_cb_param_t *param)
{
    switch (event) {
    case ESP_HF_CLIENT_PROF_STATE_EVT:
        LOCK();
        sc.prof_state = param->prof_stat.state;
        UNLOCK();
        xSemaphoreGive(prof_ev);
        break;

    case ESP_HF_CLIENT_CONNECTION_STATE_EVT:
        LOCK();
        sc.slc = (param->conn_stat.state == ESP_HF_CLIENT_CONNECTION_STATE_SLC_CONNECTED);
        UNLOCK();
        xSemaphoreGive(slc_ev);
        break;

    case ESP_HF_CLIENT_AUDIO_STATE_EVT:
        audio_changed(param->audio_stat.state, param->audio_stat.sync_conn_handle,
                      param->audio_stat.preferred_frame_size);
        break;

    case ESP_HF_CLIENT_PKT_STAT_NUMS_GET_EVT:
        LOCK();
        sc.stats.rx_total = param->pkt_nums.rx_total;
        sc.stats.rx_correct = param->pkt_nums.rx_correct;
        sc.stats.rx_err = param->pkt_nums.rx_err;
        sc.stats.rx_none = param->pkt_nums.rx_none;
        sc.stats.rx_lost = param->pkt_nums.rx_lost;
        sc.stats.tx_total = param->pkt_nums.tx_total;
        sc.stats.tx_discarded = param->pkt_nums.tx_discarded;
        sc.stats_ok = true;
        UNLOCK();
        xSemaphoreGive(stats_ev);
        break;

    default:
        break;
    }
}

/* ---------------------------------------------------------------- bring-up */

const char *sco_init(void)
{
    sc_lock = xSemaphoreCreateMutex();
    prof_ev = xSemaphoreCreateBinary();
    slc_ev = xSemaphoreCreateBinary();
    audio_ev = xSemaphoreCreateBinary();
    stats_ev = xSemaphoreCreateBinary();
    frame_ev = xSemaphoreCreateCounting(FRAME_CREDIT_MAX, 0);
    if (!sc_lock || !prof_ev || !slc_ev || !audio_ev || !stats_ev || !frame_ev) {
        return "out of memory";
    }
    /* Audio over HCI, not the PCM pins: there is no codec chip on these
     * boards, and the frames have to reach the application. */
    if (esp_bredr_sco_datapath_set(ESP_SCO_DATA_PATH_HCI) != ESP_OK) {
        return "sco data path failed";
    }
    if (xTaskCreate(sco_task, "utesp_sco", 3072, NULL, 6, NULL) != pdPASS) {
        return "sco task failed";
    }
    return NULL;
}

/* Waits for the profile's own init or deinit report. */
static const char *profile_wait(bool starting)
{
    esp_hf_prof_state_t state;

    if (xSemaphoreTake(prof_ev, pdMS_TO_TICKS(PROF_TIMEOUT_MS)) != pdTRUE) {
        return starting ? "the profile did not start" : "the profile did not stop";
    }
    LOCK();
    state = sc.prof_state;
    UNLOCK();
    if (starting) {
        return (state == ESP_HF_INIT_SUCCESS || state == ESP_HF_INIT_ALREADY)
                   ? NULL
                   : "the profile refused to start";
    }
    return (state == ESP_HF_DEINIT_SUCCESS || state == ESP_HF_DEINIT_ALREADY)
               ? NULL
               : "the profile refused to stop";
}

/* Starts the profile this role needs. Only one may run at a time, which
 * ESP-IDF's own documentation states, so the role decides. */
static const char *profile_start(enum peer_role role)
{
    const char *err;

    xSemaphoreTake(prof_ev, 0);
    if (role == PEER_ROLE_INITIATOR) {
        if (esp_hf_ag_register_callback(ag_cb) != ESP_OK ||
            esp_hf_ag_init() != ESP_OK) {
            return "audio gateway init failed";
        }
        if ((err = profile_wait(true)) != NULL) {
            return err;
        }
        if (esp_hf_ag_register_audio_data_callback(ag_frame_in) != ESP_OK) {
            return "audio callback registration failed";
        }
    } else {
        if (esp_hf_client_register_callback(hf_cb) != ESP_OK ||
            esp_hf_client_init() != ESP_OK) {
            return "hands-free init failed";
        }
        if ((err = profile_wait(true)) != NULL) {
            return err;
        }
        if (esp_hf_client_register_audio_data_callback(hf_frame_in) != ESP_OK) {
            return "audio callback registration failed";
        }
    }
    LOCK();
    sc.profile_up = true;
    sc.role = role;
    sc.tx_frames = sc.tx_errors = sc.rx_frames = sc.rx_bad = 0;
    sc.stats_ok = false;
    UNLOCK();
    return NULL;
}

static const char *profile_stop(void)
{
    enum peer_role role;
    bool up;

    LOCK();
    up = sc.profile_up;
    role = sc.role;
    UNLOCK();
    if (!up) {
        return NULL;
    }

    xSemaphoreTake(prof_ev, 0);
    if (role == PEER_ROLE_INITIATOR) {
        esp_hf_ag_deinit();
    } else {
        esp_hf_client_deinit();
    }
    /* Whatever the profile says, it is no longer ours to drive. */
    LOCK();
    sc.profile_up = false;
    sc.slc = false;
    sc.audio = false;
    sc.handle = 0;
    sc.frame_len = 0;
    UNLOCK();
    return profile_wait(false);
}

/* ---------------------------------------------------------------- commands */

const char *sco_start(enum peer_role role, const uint8_t *peer_addr)
{
    const char *err;
    bool audio;

    LOCK();
    audio = sc.audio;
    UNLOCK();
    if (audio) {
        return "the audio link is already up";
    }

    if ((err = profile_stop()) != NULL) {
        return err;
    }
    if ((err = profile_start(role)) != NULL) {
        profile_stop();
        return err;
    }

    if (role == PEER_ROLE_ACCEPTOR) {
        /* The hands-free unit waits: the gateway pages, so that it is the
         * piconet master here as it is for SPP. */
        return NULL;
    }

    if (!peer_addr) {
        profile_stop();
        return "set the peer address first with \"spp peer\"";
    }
    LOCK();
    memcpy(sc.peer, peer_addr, ESP_BD_ADDR_LEN);
    UNLOCK();

    xSemaphoreTake(slc_ev, 0);
    if (esp_hf_ag_slc_connect(sc.peer) != ESP_OK) {
        profile_stop();
        return "service level connect failed";
    }
    if (xSemaphoreTake(slc_ev, pdMS_TO_TICKS(SLC_TIMEOUT_MS)) != pdTRUE) {
        profile_stop();
        return "service level connect timed out";
    }
    LOCK();
    err = sc.slc ? NULL : "the peer refused the service level connection";
    UNLOCK();
    if (err) {
        profile_stop();
        return err;
    }

    xSemaphoreTake(audio_ev, 0);
    if (esp_hf_ag_audio_connect(sc.peer) != ESP_OK) {
        profile_stop();
        return "audio connect failed";
    }
    if (xSemaphoreTake(audio_ev, pdMS_TO_TICKS(AUDIO_TIMEOUT_MS)) != pdTRUE) {
        profile_stop();
        return "audio connect timed out";
    }
    LOCK();
    audio = sc.audio;
    UNLOCK();
    if (!audio) {
        profile_stop();
        return "the controller did not open a synchronous link";
    }
    return NULL;
}

const char *sco_stop(void)
{
    bool audio, slc, up;
    enum peer_role role;
    esp_bd_addr_t peer;

    LOCK();
    up = sc.profile_up;
    audio = sc.audio;
    slc = sc.slc;
    role = sc.role;
    memcpy(peer, sc.peer, ESP_BD_ADDR_LEN);
    UNLOCK();
    if (!up) {
        return NULL;
    }

    if (role == PEER_ROLE_INITIATOR && audio) {
        xSemaphoreTake(audio_ev, 0);
        if (esp_hf_ag_audio_disconnect(peer) == ESP_OK) {
            xSemaphoreTake(audio_ev, pdMS_TO_TICKS(AUDIO_TIMEOUT_MS));
        }
    }
    if (role == PEER_ROLE_INITIATOR && slc) {
        xSemaphoreTake(slc_ev, 0);
        if (esp_hf_ag_slc_disconnect(peer) == ESP_OK) {
            xSemaphoreTake(slc_ev, pdMS_TO_TICKS(SLC_TIMEOUT_MS));
        }
    }
    /* Taking the profile away drops anything the two steps above left. */
    return profile_stop();
}

const char *sco_pkt_stats(char *out, size_t len)
{
    struct pkt_stats s;
    uint16_t handle;
    enum peer_role role;
    bool audio;
    esp_err_t err;

    LOCK();
    audio = sc.audio;
    handle = sc.handle;
    role = sc.role;
    sc.stats_ok = false;
    UNLOCK();
    if (!audio) {
        return "no audio link to count packets on";
    }

    xSemaphoreTake(stats_ev, 0);
    err = (role == PEER_ROLE_INITIATOR) ? esp_hf_ag_pkt_stat_nums_get(handle)
                                        : esp_hf_client_pkt_stat_nums_get(handle);
    if (err != ESP_OK) {
        return "the packet counters were refused";
    }
    if (xSemaphoreTake(stats_ev, pdMS_TO_TICKS(STATS_TIMEOUT_MS)) != pdTRUE) {
        return "the packet counters did not arrive";
    }
    LOCK();
    s = sc.stats;
    audio = sc.stats_ok;
    UNLOCK();
    if (!audio) {
        return "the packet counters did not arrive";
    }

    if (snprintf(out, len,
                 "{\"rx_total\":%u,\"rx_correct\":%u,\"rx_err\":%u,\"rx_none\":%u,"
                 "\"rx_lost\":%u,\"tx_total\":%u,\"tx_discarded\":%u}",
                 (unsigned)s.rx_total, (unsigned)s.rx_correct, (unsigned)s.rx_err,
                 (unsigned)s.rx_none, (unsigned)s.rx_lost, (unsigned)s.tx_total,
                 (unsigned)s.tx_discarded) >= (int)len) {
        return "the counters do not fit one line";
    }
    return NULL;
}

void sco_get(struct sco_view *out)
{
    LOCK();
    out->state = sc.audio ? "on" : (sc.slc ? "slc" : (sc.profile_up ? "ready" : "off"));
    out->profile = !sc.profile_up ? "none"
                                  : (sc.role == PEER_ROLE_INITIATOR ? "ag" : "hf");
    out->codec = sc.audio ? (sc.msbc ? "msbc" : "cvsd") : "none";
    out->handle = sc.handle;
    out->frame_len = sc.frame_len;
    out->tx_frames = sc.tx_frames;
    out->tx_errors = sc.tx_errors;
    out->rx_frames = sc.rx_frames;
    out->rx_bad = sc.rx_bad;
    UNLOCK();
}
