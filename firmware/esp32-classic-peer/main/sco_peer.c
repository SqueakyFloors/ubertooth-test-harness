/* SPDX-License-Identifier: Apache-2.0 */

/* The synchronous audio link of the Classic (BR/EDR) test peer, so a capture
 * holds reserved-slot voice traffic as well as the ACL data the SPP side
 * makes.
 *
 * ESP-IDF gives an application synchronous links only through the Hands-Free
 * Profile, so that is how the link is brought up: the initiator runs the
 * audio gateway and pages, which keeps it the piconet's leader as it is
 * for SPP, and the acceptor runs the hands-free unit. ESP-IDF includes both
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
 * Which packet type the link uses decides whether its payload is on the air
 * at the basic rate or as EDR, so "sco type" chooses it and the status line
 * reports what the controller settled on. Neither goes through an ESP-IDF
 * API, which offers none: the hooks are in utesp_sco_type.c, built into
 * ESP-IDF's own Bluetooth component. */

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
#include "utesp_sco_type.h"

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
 * state event and is checked against it; a CVSD link over HCI asked for 120
 * bytes of 16-bit samples when measured, and this leaves room for a wider
 * one. A link whose frame size is larger than this carries no audio and says
 * so, rather than filling part of a frame. */
#define FRAME_MAX 240

/* How long the sender waits for the next frame to arrive before sending one
 * anyway. Short enough that a live link never goes quiet for long, long
 * enough that it does not outrun a link that is answering normally. */
#define FRAME_WAIT_MS 50

/* How long profile_stop() waits for a frame that is already with the stack,
 * so a buffer is never submitted into a profile being taken apart. */
#define QUIESCE_WAIT_MS 500

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
    bool stats_wanted; /* a packet-counter request is outstanding */
    bool stats_dead;   /* a request went unanswered: see sco_pkt_stats() */
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
 * a ramp, so no two frames of a link are alike and the content is fixed
 * rather than whatever the buffer held.
 *
 * The counter is not readable from the air: the controller codes these
 * samples to CVSD, so the bytes on the air are not these bytes. It is
 * readable by anything that sees the frames before that, which is the far
 * end over HCI, and it would reach the air unchanged in a build whose codec
 * is transparent. Counting frames is what the far end does today. */
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
     * and sending is how it knows the last frame has left. A frame size
     * outside the usable range was reported and zeroed when the link came
     * up, so there is nothing to say about it here. */
    go = sc.audio && !sc.quiesce && len >= 4 && len <= FRAME_MAX;
    role = sc.role;
    handle = sc.handle;
    counter = sc.counter;
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
        /* A send that was refused never took the buffer: every failure
         * return in esp_hf_ag_audio_data_send() (and the client's) is taken
         * before btc_hf_ag_audio_data_send() hands the buffer to the
         * Bluetooth task, so freeing it here is right and necessary. */
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
         * gains a gap. Only if it is still the one this frame carried: a
         * link that came up while the frame was with the stack has reset it
         * to zero, and that restart is what a capture reads. */
        if (sc.counter == counter) {
            sc.counter = counter + 1;
        }
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

/* Whether a profile is this module's right now. The callbacks stay
 * registered across a deinit, and the stack may hand up an event it had
 * already queued, so every handler that writes link state asks this first: a
 * late "connected" must not bring sc.audio back after the profile it
 * belonged to has gone, or the next "sco start" would refuse with "already
 * up" and the sender would push frames at an API that is no longer there. */
static bool profile_is_ours(void)
{
    bool ours;

    LOCK();
    ours = sc.profile_up;
    UNLOCK();
    return ours;
}

/* Both profiles report an incoming frame the same way, and either way the
 * application owns the buffer: both headers say the user "should free audio
 * buffer by calling esp_hf_ag_audio_buff_free" (and the client's equivalent). */
static void frame_in(esp_hf_audio_buff_t *buf, bool bad, enum peer_role role)
{
    bool ours = profile_is_ours();

    if (ours) {
        LOCK();
        sc.rx_frames++;
        if (bad) {
            sc.rx_bad++;
        }
        UNLOCK();
    }

    /* The buffer is freed either way: the stack handed it over, so leaving it
     * would leak whether or not the profile is still ours. */
    if (role == PEER_ROLE_INITIATOR) {
        esp_hf_ag_audio_buff_free(buf);
    } else {
        esp_hf_client_audio_buff_free(buf);
    }
    if (!ours) {
        return;
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

/* The packet types of each link kind, smallest payload first, with the bit
 * that allows each one and the most it can carry. The EDR types are allowed
 * by a bit being CLEAR, which is why each row says which way round its bit
 * reads (Core 6.0 Vol 4 Part E 7.1.26). */
struct type_row {
    const char *name;
    uint16_t bit;
    bool allowed_when_set;
    uint16_t max_payload;
};

static const struct type_row sco_types[] = {
    {"hv1", 0x0001, true, 10},
    {"hv2", 0x0002, true, 20},
    {"hv3", 0x0004, true, 30},
};

static const struct type_row esco_types[] = {
    {"ev3", 0x0008, true, 30},
    {"2-ev3", 0x0040, false, 60},
    {"3-ev3", 0x0080, false, 90},
    {"ev4", 0x0010, true, 120},
    {"ev5", 0x0020, true, 180},
    {"2-ev5", 0x0100, false, 360},
    {"3-ev5", 0x0200, false, 540},
};

/* Names the packet type a live link is using.
 *
 * The Synchronous Connection Complete event carries the link type and the
 * packet length but not the packet type (Core 6.0 Vol 4 Part E 7.7.35), so
 * the type is the one that was allowed for this link and that can carry the
 * length the link settled on. Smallest first, because a type carries up to
 * its maximum and a 64 kbit/s voice link is given that maximum.
 *
 * With no list to go on - a link already going down - the length alone
 * decides, and then two types that could carry it are told apart only by
 * the smaller being named. The status line reports the length and the
 * interval beside this either way, for a reader to check. */
static const char *pkt_type_name(const struct utesp_sco_link *link)
{
    const struct type_row *rows;
    size_t count;

    if (link->link_type == 0) { /* SCO */
        rows = sco_types;
        count = sizeof(sco_types) / sizeof(sco_types[0]);
    } else if (link->link_type == 2) { /* eSCO */
        rows = esco_types;
        count = sizeof(esco_types) / sizeof(esco_types[0]);
    } else {
        return "unknown";
    }
    if (link->tx_pkt_len == 0) {
        return "unknown";
    }
    for (size_t i = 0; i < count; i++) {
        bool set = (link->allowed & rows[i].bit) != 0;
        bool allowed = link->allowed == 0 || set == rows[i].allowed_when_set;

        if (allowed && link->tx_pkt_len <= rows[i].max_payload) {
            return rows[i].name;
        }
    }
    return "unknown";
}

/* Records a synchronous link coming up or going down. The state carries the
 * codec: there is a separate value for an mSBC link. */
static void audio_changed(int state, uint16_t handle, uint16_t frame_len,
                          const esp_bd_addr_t remote)
{
    bool up = (state == ESP_HF_AUDIO_STATE_CONNECTED ||
               state == ESP_HF_AUDIO_STATE_CONNECTED_MSBC);
    bool usable = up && frame_len >= 4 && frame_len <= FRAME_MAX;
    struct utesp_sco_link link;
    bool link_known = false;
    bool was_up;

    if (state == ESP_HF_AUDIO_STATE_CONNECTING) {
        /* Nothing has changed yet, and in particular a link already up must
         * keep its handle and frame size. */
        return;
    }
    if (!profile_is_ours()) {
        return;
    }

    /* What the controller settled on, for the notice below. Not kept: the
     * status line reads it again, so a link whose parameters are
     * renegotiated later is still described as it is rather than as it
     * arrived. */
    if (up) {
        link_known = utesp_sco_link_read(remote, &link) == UTESP_SCO_OK;
    }

    LOCK();
    was_up = sc.audio;
    sc.audio = up;
    /* Keep the handle and the frame size only while there is a link to use
     * them on, so a status line never describes one that has gone. The size
     * is checked here, once, rather than on every frame: a link whose frame
     * size this firmware cannot fill carries no audio, and "sco_frame_len":0
     * in the status line is how that shows. */
    sc.handle = up ? handle : 0;
    sc.frame_len = usable ? frame_len : 0;
    sc.msbc = up && state == ESP_HF_AUDIO_STATE_CONNECTED_MSBC;
    if (up) {
        sc.counter = 0;
        memcpy(sc.remote, remote, ESP_BD_ADDR_LEN);
    }
    UNLOCK();

    /* Only a link that was up can go down, so a connect that the controller
     * refused does not read as a disconnection nobody asked about. The
     * packet type goes in the notice as well as the status line: it is the
     * one fact a capture has to be lined up against, and a run that only
     * reads the log still has it. */
    if (up) {
        shell_line("utesp: sco connected, %s", link_known ? pkt_type_name(&link)
                                                          : "packet type unknown");
    } else if (was_up) {
        shell_line("utesp: sco disconnected");
    }
    if (up && !usable) {
        shell_line("utesp: sco frame size %u unusable, sending no audio", frame_len);
    }
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
        if (!profile_is_ours()) {
            break;
        }
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
        if (!profile_is_ours()) {
            break;
        }
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

/* Records a profile as this module's to stop. Called the moment the init
 * call is accepted and before waiting for its event: the init may land after
 * the wait gives up, and a profile nobody owns is one nothing will ever
 * stop, so the next role would start the other one on top of it, which
 * ESP-IDF says cannot work. Not called any earlier than that, because a
 * profile that was never initialized must not be deinitialized either. */
static void profile_owned(enum peer_role role)
{
    LOCK();
    sc.profile_up = true;
    sc.role = role;
    UNLOCK();
}

/* Starts the profile this role needs. Only one may run at a time, which
 * ESP-IDF's own documentation states, so the role decides. */
static const char *profile_start(enum peer_role role)
{
    const char *err;

    xSemaphoreTake(prof_ev, 0);

    if (role == PEER_ROLE_INITIATOR) {
        if (esp_hf_ag_register_callback(ag_cb) != ESP_OK) {
            return "audio gateway init failed";
        }
        if (esp_hf_ag_init() != ESP_OK) {
            return "audio gateway init failed";
        }
        profile_owned(role);
        if ((err = profile_wait(true)) != NULL) {
            return err;
        }
        if (esp_hf_ag_register_audio_data_callback(ag_frame_in) != ESP_OK) {
            return "audio callback registration failed";
        }
    } else {
        if (esp_hf_client_register_callback(hf_cb) != ESP_OK) {
            return "hands-free init failed";
        }
        if (esp_hf_client_init() != ESP_OK) {
            return "hands-free init failed";
        }
        profile_owned(role);
        if ((err = profile_wait(true)) != NULL) {
            return err;
        }
        if (esp_hf_client_register_audio_data_callback(hf_frame_in) != ESP_OK) {
            return "audio callback registration failed";
        }
    }
    LOCK();
    sc.tx_frames = sc.tx_errors = sc.rx_frames = sc.rx_bad = 0;
    sc.stats_ok = false;
    sc.stats_wanted = false;
    UNLOCK();
    return NULL;
}

/* Stops the sender and waits for a frame already with the stack, so nothing
 * is submitted into a profile that is being taken apart. Says whether the
 * frame did leave: tearing the profile down under one would hand a buffer to
 * a pool being freed.
 *
 * Note that this does not touch sc.audio. Only the controller's own event, or
 * a profile teardown that the stack confirmed, may say the link is down; a
 * stop that failed must leave the status line describing what is really on
 * the air. */
static bool quiesce_sender(void)
{
    LOCK();
    sc.quiesce = true;
    UNLOCK();

    for (int waited = 0; waited < QUIESCE_WAIT_MS; waited += 2) {
        bool busy;
        LOCK();
        busy = sc.sending;
        UNLOCK();
        if (!busy) {
            return true;
        }
        vTaskDelay(pdMS_TO_TICKS(2));
    }
    return false;
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

    if (!quiesce_sender()) {
        LOCK();
        sc.quiesce = false;
        UNLOCK();
        return "a frame is still with the stack";
    }
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

    /* The profile is gone, so anything it was holding up is gone with it. */
    LOCK();
    sc.profile_up = false;
    sc.slc = false;
    sc.audio = false;
    sc.handle = 0;
    sc.frame_len = 0;
    sc.quiesce = false;
    UNLOCK();
    return NULL;
}

/* ---------------------------------------------------------------- commands */

/* Says so when the tidying up after a failed start could not finish. The
 * answer to the command is the reason it failed, which is what a driver
 * needs; this is the second thing it needs, because the profile is still the
 * firmware's and the next "role" or "defaults" will refuse too. */
static void give_up(const char *err)
{
    if (err) {
        shell_line("utesp: sco the profile is still running: %s", err);
    }
}

const char *sco_start(enum peer_role role, const uint8_t *peer_addr)
{
    esp_bd_addr_t peer;
    const char *err;
    bool audio, slc, have_profile;
    unsigned frame_len;

    if (!sc.ready) {
        return "the audio link is not available on this board";
    }

    LOCK();
    audio = sc.audio;
    slc = sc.slc;
    /* The profile this role needs may already be running, from an earlier
     * "sco start" on this board. Keeping it is not just quicker: taking it
     * away would drop a service level connection the other board brought
     * up, so a second "sco start" would destroy the link it is asked for. */
    have_profile = sc.profile_up && sc.role == role;
    UNLOCK();
    if (audio) {
        return "the audio link is already up";
    }

    if (!have_profile) {
        if ((err = sco_stop()) != NULL) {
            return err;
        }
        if ((err = profile_start(role)) != NULL) {
            give_up(profile_stop());
            return err;
        }
        slc = false;
    }

    if (role == PEER_ROLE_ACCEPTOR) {
        /* The hands-free unit waits: the gateway pages, so that it is the
         * piconet's leader here as it is for SPP. */
        return NULL;
    }

    if (!peer_addr) {
        give_up(profile_stop());
        return "set the peer address first with \"spp peer\"";
    }
    memcpy(peer, peer_addr, ESP_BD_ADDR_LEN);
    LOCK();
    memcpy(sc.peer, peer, ESP_BD_ADDR_LEN);
    UNLOCK();

    /* An earlier "sco start" may have left the service level connection up
     * with only the audio link gone; then there is nothing to page for. */
    if (!slc) {
        xSemaphoreTake(slc_ev, 0);
        if (esp_hf_ag_slc_connect(peer) != ESP_OK) {
            give_up(profile_stop());
            return "service level connect failed";
        }
        if (xSemaphoreTake(slc_ev, pdMS_TO_TICKS(SLC_TIMEOUT_MS)) != pdTRUE) {
            give_up(profile_stop());
            return "service level connect timed out";
        }
        LOCK();
        err = sc.slc ? NULL : "the peer refused the service level connection";
        UNLOCK();
        if (err) {
            give_up(profile_stop());
            return err;
        }
    }

    xSemaphoreTake(audio_ev, 0);
    if (esp_hf_ag_audio_connect(peer) != ESP_OK) {
        give_up(profile_stop());
        return "audio connect failed";
    }
    if (xSemaphoreTake(audio_ev, pdMS_TO_TICKS(AUDIO_TIMEOUT_MS)) != pdTRUE) {
        give_up(profile_stop());
        return "audio connect timed out";
    }
    LOCK();
    audio = sc.audio;
    frame_len = sc.frame_len;
    UNLOCK();
    if (!audio) {
        give_up(profile_stop());
        return "the controller did not open a synchronous link";
    }
    /* A link this firmware cannot fill carries no audio pattern, which is
     * the whole point of asking for it, so it is a failed start and not a
     * link left half working. The notice from audio_changed() gives the
     * size. */
    if (frame_len == 0) {
        give_up(sco_stop());
        return "the link asked for a frame size this firmware cannot fill";
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

const char *sco_set_type(const char *name)
{
    enum utesp_sco_type type;
    bool audio;

    /* No check that the audio link machinery came up: this only records a
     * choice for the next link, and "sco start" is where a board that
     * cannot open one says so. */
    if (strcmp(name, "ev3") == 0) {
        type = UTESP_SCO_TYPE_EV3;
    } else if (strcmp(name, "2ev3") == 0) {
        type = UTESP_SCO_TYPE_2EV3;
    } else if (strcmp(name, "default") == 0) {
        type = UTESP_SCO_TYPE_DEFAULT;
    } else {
        return "sco type takes ev3, 2ev3, or default";
    }

    LOCK();
    audio = sc.audio;
    UNLOCK();
    /* The choice reaches the controller only as a link is opened, so taking
     * it under a live link would leave the status line naming one type while
     * the air carried another. */
    if (audio) {
        return "stop the audio link before choosing a packet type";
    }
    utesp_sco_type_set(type);
    return NULL;
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
    bool audio, dead, got;

    LOCK();
    audio = sc.audio;
    handle = sc.handle;
    role = sc.role;
    dead = sc.stats_dead;
    sc.stats_ok = false;
    sc.stats_wanted = audio && !dead;
    UNLOCK();
    if (!audio) {
        return "no audio link to count packets on";
    }
    /* A request that went unanswered may still be in flight, and a reply
     * carries nothing to tell it from the next request's - not even across
     * a new link, since nothing says the stack drops the old one. Rather
     * than risk answering one request with another's counters, the command
     * is refused for the rest of this boot. */
    if (dead) {
        return "a packet-counter request went unanswered; reset the board";
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
        sc.stats_dead = true;
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
    struct utesp_sco_link link;
    esp_bd_addr_t remote;
    bool audio, known = false;

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
    audio = sc.audio;
    memcpy(remote, sc.remote, ESP_BD_ADDR_LEN);
    UNLOCK();

    /* Read now rather than remembered from when the link came up, so a link
     * whose parameters were renegotiated since is described as it is. Done
     * outside the lock: it reaches into the stack, and nothing here needs
     * the two to agree. */
    if (audio) {
        known = utesp_sco_link_read(remote, &link) == UTESP_SCO_OK;
    }
    out->link = !audio ? "none"
                       : (!known ? "unknown" : (link.link_type == 0 ? "sco" : "esco"));
    out->pkt_type = !audio ? "none" : (known ? pkt_type_name(&link) : "unknown");
    out->tx_len = known ? link.tx_pkt_len : 0;
    out->rx_len = known ? link.rx_pkt_len : 0;
    out->interval = known ? link.tx_interval : 0;
    out->retrans = known ? link.retrans_window : 0;
    /* The type asked for lives with the calls that carry it, and the
     * supported mask is the controller's and fixed from its start. */
    out->type = utesp_sco_type_name(utesp_sco_type_get());
    out->supported = utesp_sco_types_supported();
}
