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
 * The link clocks the sending. A synchronous link is symmetric and the
 * controller hands up one frame per interval, so a frame arriving is the cue
 * to send one, which matches the link's rate without a timer here to drift
 * against it. A fallback timeout sends anyway if nothing arrives, so a
 * dropped cue cannot leave a live link silent.
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
 * commands, so it is the slowest step. The three stopping timeouts are short
 * on purpose: "role" and "defaults" stop the audio link first, and they must
 * not become commands a driver times out on. */
#define PROF_TIMEOUT_MS      3000
#define SLC_TIMEOUT_MS       15000
#define AUDIO_TIMEOUT_MS     10000
#define AUDIO_DOWN_TIMEOUT_MS 3000
#define SLC_DOWN_TIMEOUT_MS   3000
#define STATS_TIMEOUT_MS     2000

/* The cap on one audio frame. The link's real frame size comes from the audio
 * state event and is checked against this; a CVSD link over HCI asked for 120
 * bytes of 16-bit samples when measured, and this leaves room for a wider
 * one without putting a large buffer in static memory. */
#define FRAME_MAX 240

/* How long the sender waits for the next frame to arrive before sending one
 * anyway. Short enough that a live link never goes quiet for long, long
 * enough that it does not outrun a link that is answering normally. */
#define FRAME_WAIT_MS 50

/* How long profile_stop() waits for a frame that is already with the stack,
 * so a buffer is never submitted into a profile being taken apart. */
#define QUIESCE_WAIT_MS 200

struct pkt_stats {
    uint32_t rx_total, rx_correct, rx_err, rx_none, rx_lost;
    uint32_t tx_total, tx_discarded;
};

static struct {
    bool ready;       /* sco_init() succeeded, so the commands can work */
    bool profile_up;  /* one of the two HFP profiles is initialized */
    enum peer_role role;
    bool slc;     /* the service level connection is up */
    bool audio;   /* the synchronous link is up */
    bool quiesce; /* the sender must not start another frame */
    bool sending; /* a frame is with the stack right now */
    uint16_t handle;
    uint16_t frame_len;
    bool msbc; /* the codec the controller settled on */
    uint32_t counter;
    uint32_t tx_frames, tx_errors, rx_frames, rx_bad;
    esp_bd_addr_t peer;   /* who the gateway calls */
    esp_bd_addr_t remote; /* the far end of the live link, either role */
    bool stats_wanted;    /* a packet-counter request is outstanding */
    struct pkt_stats stats;
    bool stats_ok;
    esp_hf_prof_state_t prof_state;
} sc;

static SemaphoreHandle_t sc_lock;
static SemaphoreHandle_t prof_ev;  /* a profile finished starting or stopping */
static SemaphoreHandle_t slc_ev;   /* the service level connection changed */
static SemaphoreHandle_t audio_ev; /* the synchronous link changed */
static SemaphoreHandle_t stats_ev; /* the packet counters arrived */
static SemaphoreHandle_t frame_ev; /* a frame arrived: send one back */

#define LOCK()                                      \
    do {                                            \
        if (sc_lock) {                              \
            xSemaphoreTake(sc_lock, portMAX_DELAY); \
        }                                           \
    } while (0)
#define UNLOCK()                     \
    do {                             \
        if (sc_lock) {               \
            xSemaphoreGive(sc_lock); \
        }                            \
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
    bool go;
    esp_err_t err;

    LOCK();
    len = sc.frame_len;
    /* quiesce is how a stop keeps this out of a profile it is taking apart,
     * and sending is how it knows the last frame has left. */
    go = sc.audio && !sc.quiesce;
    role = sc.role;
    handle = sc.handle;
    counter = sc.counter;
    if (go && (len < 4 || len > FRAME_MAX)) {
        /* An unusable frame size: count it rather than write past a buffer
         * the size asked for, or stop silently. */
        sc.tx_errors++;
        go = false;
    }
    if (go) {
        sc.sending = true;
    }
    UNLOCK();
    if (!go) {
        return;
    }

    buf = (role == PEER_ROLE_INITIATOR) ? esp_hf_ag_audio_buff_alloc(len)
                                        : esp_hf_client_audio_buff_alloc(len);
    if (!buf) {
        LOCK();
        sc.tx_errors++;
        sc.sending = false;
        UNLOCK();
        return;
    }
    fill_frame(buf->data, len, counter);
    buf->data_len = len;

    err = (role == PEER_ROLE_INITIATOR) ? esp_hf_ag_audio_data_send(handle, buf)
                                        : esp_hf_client_audio_data_send(handle, buf);
    if (err != ESP_OK) {
        /* A send that was refused never took the buffer. */
        if (role == PEER_ROLE_INITIATOR) {
            esp_hf_ag_audio_buff_free(buf);
        } else {
            esp_hf_client_audio_buff_free(buf);
        }
    }

    LOCK();
    if (err == ESP_OK) {
        sc.tx_frames++;
        /* The counter steps only for a frame that went out, so it never
         * gains a gap and always matches tx_frames. */
        sc.counter++;
    } else {
        sc.tx_errors++;
    }
    sc.sending = false;
    UNLOCK();
}

/* Sends on its own task rather than inside the callback: the callback runs on
 * the Bluetooth task, and handing work back to that same task from inside it
 * is how a stack deadlocks.
 *
 * The wait has a timeout so that a cue lost while this task was descheduled,
 * or a frame the controller never handed up, costs one quiet interval rather
 * than the rest of the link. */
static void sco_task(void *arg)
{
    (void)arg;
    for (;;) {
        bool up;

        xSemaphoreTake(frame_ev, pdMS_TO_TICKS(FRAME_WAIT_MS));
        LOCK();
        up = sc.audio && !sc.quiesce;
        UNLOCK();
        if (up) {
            send_frame();
        }
    }
}

/* ---------------------------------------------------------------- callbacks */

/* Both profiles report an incoming frame the same way, and either way the
 * application owns the buffer: both headers say the user "should free audio
 * buffer by calling esp_hf_ag_audio_buff_free" (and the client's equivalent). */
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
    /* The cue to send one of ours, which is what keeps the sending rate equal
     * to the link's. A binary semaphore on purpose: this is "there is a frame
     * to answer", not a count to work through, so a cue given while the
     * sender is busy is dropped rather than letting a backlog build. */
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
static void audio_changed(int state, uint16_t handle, uint16_t frame_len,
                          const esp_bd_addr_t remote)
{
    bool up = (state == ESP_HF_AUDIO_STATE_CONNECTED ||
               state == ESP_HF_AUDIO_STATE_CONNECTED_MSBC);

    LOCK();
    sc.audio = up;
    /* Keep the handle and the frame size only while there is a link to use
     * them on, so a status line never describes one that has gone. */
    sc.handle = up ? handle : 0;
    sc.frame_len = up ? frame_len : 0;
    sc.msbc = up && state == ESP_HF_AUDIO_STATE_CONNECTED_MSBC;
    if (up) {
        sc.counter = 0;
        memcpy(sc.remote, remote, ESP_BD_ADDR_LEN);
    }
    UNLOCK();

    if (state == ESP_HF_AUDIO_STATE_CONNECTING) {
        return; /* not an answer to anything yet */
    }
    shell_line("utesp: sco %s", up ? "connected" : "disconnected");
    xSemaphoreGive(audio_ev);
}

static void record_stats(const struct pkt_stats *s)
{
    LOCK();
    /* Only while a request is outstanding: a reply that arrives after one
     * timed out must not be handed to the next request. */
    if (sc.stats_wanted) {
        sc.stats = *s;
        sc.stats_ok = true;
        sc.stats_wanted = false;
        UNLOCK();
        xSemaphoreGive(stats_ev);
        return;
    }
    UNLOCK();
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
        if (sc.slc) {
            memcpy(sc.remote, param->conn_stat.remote_bda, ESP_BD_ADDR_LEN);
        }
        UNLOCK();
        if (param->conn_stat.state == ESP_HF_CONNECTION_STATE_SLC_CONNECTED ||
            param->conn_stat.state == ESP_HF_CONNECTION_STATE_DISCONNECTED) {
            xSemaphoreGive(slc_ev);
        }
        break;

    case ESP_HF_AUDIO_STATE_EVT:
        audio_changed(param->audio_stat.state, param->audio_stat.sync_conn_handle,
                      param->audio_stat.preferred_frame_size, param->audio_stat.remote_addr);
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

    case ESP_HF_PKT_STAT_NUMS_GET_EVT: {
        const struct pkt_stats s = {
            param->pkt_nums.rx_total,  param->pkt_nums.rx_correct,
            param->pkt_nums.rx_err,    param->pkt_nums.rx_none,
            param->pkt_nums.rx_lost,   param->pkt_nums.tx_total,
            param->pkt_nums.tx_discarded,
        };
        record_stats(&s);
        break;
    }

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
        if (sc.slc) {
            memcpy(sc.remote, param->conn_stat.remote_bda, ESP_BD_ADDR_LEN);
        }
        UNLOCK();
        /* The same two states the gateway signals on, so an acceptor-side
         * wait added later behaves the same way. */
        if (param->conn_stat.state == ESP_HF_CLIENT_CONNECTION_STATE_SLC_CONNECTED ||
            param->conn_stat.state == ESP_HF_CLIENT_CONNECTION_STATE_DISCONNECTED) {
            xSemaphoreGive(slc_ev);
        }
        break;

    case ESP_HF_CLIENT_AUDIO_STATE_EVT:
        audio_changed(param->audio_stat.state, param->audio_stat.sync_conn_handle,
                      param->audio_stat.preferred_frame_size, param->audio_stat.remote_bda);
        break;

    case ESP_HF_CLIENT_PKT_STAT_NUMS_GET_EVT: {
        const struct pkt_stats s = {
            param->pkt_nums.rx_total,  param->pkt_nums.rx_correct,
            param->pkt_nums.rx_err,    param->pkt_nums.rx_none,
            param->pkt_nums.rx_lost,   param->pkt_nums.tx_total,
            param->pkt_nums.tx_discarded,
        };
        record_stats(&s);
        break;
    }

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
    frame_ev = xSemaphoreCreateBinary();
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
    sc.ready = true;
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
    sc.stats_wanted = false;
    UNLOCK();
    return NULL;
}

/* Stops the sender and waits for a frame already with the stack, so nothing
 * is submitted into a profile that is being taken apart. */
static void quiesce_sender(void)
{
    LOCK();
    sc.quiesce = true;
    sc.audio = false;
    UNLOCK();

    for (int waited = 0; waited < QUIESCE_WAIT_MS; waited += 2) {
        bool busy;
        LOCK();
        busy = sc.sending;
        UNLOCK();
        if (!busy) {
            return;
        }
        vTaskDelay(pdMS_TO_TICKS(2));
    }
}

static const char *profile_stop(void)
{
    enum peer_role role;
    const char *err;
    bool up;

    LOCK();
    up = sc.profile_up;
    role = sc.role;
    UNLOCK();
    if (!up) {
        return NULL;
    }

    quiesce_sender();
    xSemaphoreTake(prof_ev, 0);
    if ((role == PEER_ROLE_INITIATOR ? esp_hf_ag_deinit() : esp_hf_client_deinit()) != ESP_OK) {
        LOCK();
        sc.quiesce = false;
        UNLOCK();
        return "stopping the profile was refused";
    }
    /* Only report it stopped once the profile says so. Leaving profile_up set
     * on a failure is what makes the next "sco start" try the stop again
     * rather than start the other role on top of this one. */
    if ((err = profile_wait(false)) != NULL) {
        LOCK();
        sc.quiesce = false;
        UNLOCK();
        return err;
    }

    LOCK();
    sc.profile_up = false;
    sc.slc = false;
    sc.handle = 0;
    sc.frame_len = 0;
    sc.quiesce = false;
    UNLOCK();
    return NULL;
}

/* ---------------------------------------------------------------- commands */

const char *sco_start(enum peer_role role, const uint8_t *peer_addr)
{
    const char *err;
    bool audio;

    if (!sc.ready) {
        return "the audio link is not available on this board";
    }

    LOCK();
    audio = sc.audio;
    UNLOCK();
    if (audio) {
        return "the audio link is already up";
    }

    if ((err = sco_stop()) != NULL) {
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
    esp_bd_addr_t remote;

    LOCK();
    up = sc.profile_up;
    audio = sc.audio;
    slc = sc.slc;
    role = sc.role;
    memcpy(remote, sc.remote, ESP_BD_ADDR_LEN);
    UNLOCK();
    if (!up) {
        return NULL;
    }

    /* Close the link from whichever side is asked, rather than leaving
     * reserved slots on the air for the profile teardown to deal with. */
    if (audio) {
        xSemaphoreTake(audio_ev, 0);
        if ((role == PEER_ROLE_INITIATOR ? esp_hf_ag_audio_disconnect(remote)
                                         : esp_hf_client_disconnect_audio(remote)) == ESP_OK) {
            xSemaphoreTake(audio_ev, pdMS_TO_TICKS(AUDIO_DOWN_TIMEOUT_MS));
        }
    }
    if (slc) {
        xSemaphoreTake(slc_ev, 0);
        if ((role == PEER_ROLE_INITIATOR ? esp_hf_ag_slc_disconnect(remote)
                                         : esp_hf_client_disconnect(remote)) == ESP_OK) {
            xSemaphoreTake(slc_ev, pdMS_TO_TICKS(SLC_DOWN_TIMEOUT_MS));
        }
    }
    /* Taking the profile away drops anything the two steps above left. */
    return profile_stop();
}

void sco_zero_counters(void)
{
    LOCK();
    sc.tx_frames = sc.tx_errors = sc.rx_frames = sc.rx_bad = 0;
    sc.counter = 0;
    UNLOCK();
}

const char *sco_pkt_stats(char *out, size_t len)
{
    struct pkt_stats s;
    uint16_t handle;
    enum peer_role role;
    bool audio, got;

    LOCK();
    audio = sc.audio;
    handle = sc.handle;
    role = sc.role;
    sc.stats_ok = false;
    sc.stats_wanted = audio;
    UNLOCK();
    if (!audio) {
        return "no audio link to count packets on";
    }

    xSemaphoreTake(stats_ev, 0);
    if ((role == PEER_ROLE_INITIATOR ? esp_hf_ag_pkt_stat_nums_get(handle)
                                     : esp_hf_client_pkt_stat_nums_get(handle)) != ESP_OK) {
        LOCK();
        sc.stats_wanted = false;
        UNLOCK();
        return "the packet counters were refused";
    }
    if (xSemaphoreTake(stats_ev, pdMS_TO_TICKS(STATS_TIMEOUT_MS)) != pdTRUE) {
        LOCK();
        sc.stats_wanted = false;
        UNLOCK();
        return "the packet counters did not arrive";
    }
    LOCK();
    s = sc.stats;
    got = sc.stats_ok;
    UNLOCK();
    if (!got) {
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
