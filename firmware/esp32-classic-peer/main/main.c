/* SPDX-License-Identifier: Apache-2.0 */

/* The serial command shell of the Classic Bluetooth test peer.
 *
 * One command line in, zero or more data lines out, then exactly one "OK" or
 * "ERR <reason>" line. Lines that start with "utesp: " are notices the board
 * sends by itself (the boot banner, a link coming up or going down) and are
 * not part of any answer, so a driver reading a response skips them. The
 * protocol matches the nRF52840 DK role firmware in this repository, which
 * lets the harness keep one shell driver for both kinds of board. */

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "driver/uart.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "peer.h"

#define SHELL_UART     UART_NUM_0
#define SHELL_BAUD     115200
#define SHELL_RX_BUF   1024
#define SHELL_TX_BUF   2048
#define SHELL_LINE_MAX 160 /* longest command line accepted */
#define SHELL_OUT_MAX  1200 /* longest answer line, set by the status JSON,
                             * whose longest possible form is 1070 bytes:
                             * every number at its widest, the longest name,
                             * and the longest channel map reason */
#define SHELL_WORDS    8

static SemaphoreHandle_t out_lock;

void shell_line(const char *fmt, ...)
{
    /* One buffer, held by out_lock, rather than 770 bytes on the caller's
     * stack: the notices below come from the Bluetooth task, whose stack is
     * small. */
    static char buf[SHELL_OUT_MAX + 2];
    va_list ap;
    int n;

    if (out_lock) {
        xSemaphoreTake(out_lock, portMAX_DELAY);
    }
    va_start(ap, fmt);
    n = vsnprintf(buf, sizeof(buf) - 1, fmt, ap);
    va_end(ap);
    if (n >= 0) {
        if (n > (int)sizeof(buf) - 2) {
            n = (int)sizeof(buf) - 2;
        }
        buf[n++] = '\n';
        uart_write_bytes(SHELL_UART, buf, n);
    }
    if (out_lock) {
        xSemaphoreGive(out_lock);
    }
}

static void answer(const char *err)
{
    if (err) {
        shell_line("ERR %s", err);
    } else {
        shell_line("OK");
    }
}

/* Parses "<digits>" strictly: no sign, no trailing text. */
static bool parse_uint(const char *s, unsigned *out)
{
    char *end;
    unsigned long v;

    if (!s || !*s) {
        return false;
    }
    v = strtoul(s, &end, 10);
    if (*end != '\0' || v > 0xffffffffUL) {
        return false;
    }
    *out = (unsigned)v;
    return true;
}

/* "on" or "off". */
static bool parse_onoff(const char *s, bool *out)
{
    if (s && strcmp(s, "on") == 0) {
        *out = true;
        return true;
    }
    if (s && strcmp(s, "off") == 0) {
        *out = false;
        return true;
    }
    return false;
}

static void cmd_help(void)
{
    static const char *const lines[] = {
        "version                       the firmware version",
        "status                        one JSON line of every field",
        "role <acceptor|initiator>     stop, then take this role",
        "disc <on|off>                 discoverable and connectable",
        "spp peer <aa:bb:cc:dd:ee:ff>  the address the initiator calls",
        "spp start                     acceptor: listen; initiator: connect",
        "spp stop                      drop the link and stop listening",
        "stream <on|off>               send payload bytes while connected",
        "stream period <ms>            time between writes, 5-10000",
        "stream payload <bytes>        bytes per write, 8-512",
        "sco start                     open the synchronous audio link",
        "sco stop                      drop it and stop the profile",
        "sco stats                     the controller's packet counters",
        "sco type <ev3|2ev3|default>   the packet type the next link asks for",
        "clock                         the native and piconet clocks",
        "afh <20 hex digits>           hop over this channel map",
        "afh off                       hop over every channel again",
        "defaults                      restore every boot setting",
        "reset                         restart the board",
    };

    for (size_t i = 0; i < sizeof(lines) / sizeof(lines[0]); i++) {
        shell_line("%s", lines[i]);
    }
}

static void run(int argc, char **argv)
{
    const char *c = argv[0];

    if (argc == 1 && strcmp(c, "version") == 0) {
        shell_line("%s", UTESP_FW_VERSION);
        answer(NULL);
        return;
    }
    if (argc == 1 && strcmp(c, "status") == 0) {
        /* static: only the shell task gets here, and the task's stack is
         * better spent on the Bluetooth calls below it. */
        static char json[SHELL_OUT_MAX];
        bt_status_json(json, sizeof(json));
        shell_line("%s", json);
        answer(NULL);
        return;
    }
    if (argc == 1 && strcmp(c, "help") == 0) {
        cmd_help();
        answer(NULL);
        return;
    }
    if (argc == 2 && strcmp(c, "role") == 0) {
        if (strcmp(argv[1], "acceptor") == 0) {
            answer(bt_set_role(PEER_ROLE_ACCEPTOR));
        } else if (strcmp(argv[1], "initiator") == 0) {
            answer(bt_set_role(PEER_ROLE_INITIATOR));
        } else {
            answer("role must be acceptor or initiator");
        }
        return;
    }
    if (argc == 2 && strcmp(c, "disc") == 0) {
        bool on;
        answer(parse_onoff(argv[1], &on) ? bt_set_discoverable(on) : "disc takes on or off");
        return;
    }
    if (strcmp(c, "spp") == 0) {
        if (argc == 3 && strcmp(argv[1], "peer") == 0) {
            answer(bt_set_peer(argv[2]));
        } else if (argc == 2 && strcmp(argv[1], "start") == 0) {
            answer(bt_spp_start());
        } else if (argc == 2 && strcmp(argv[1], "stop") == 0) {
            answer(bt_spp_stop());
        } else {
            answer("spp takes peer <address>, start, or stop");
        }
        return;
    }
    if (strcmp(c, "stream") == 0) {
        bool on;
        unsigned v;
        if (argc == 2 && parse_onoff(argv[1], &on)) {
            answer(bt_stream(on));
        } else if (argc == 3 && strcmp(argv[1], "period") == 0) {
            answer(parse_uint(argv[2], &v) ? bt_stream_period(v) : "period must be a number");
        } else if (argc == 3 && strcmp(argv[1], "payload") == 0) {
            answer(parse_uint(argv[2], &v) ? bt_stream_payload(v) : "payload must be a number");
        } else {
            answer("stream takes on, off, period <ms>, or payload <bytes>");
        }
        return;
    }
    if (strcmp(c, "sco") == 0) {
        if (argc == 2 && strcmp(argv[1], "start") == 0) {
            answer(bt_sco_start());
        } else if (argc == 2 && strcmp(argv[1], "stop") == 0) {
            answer(sco_stop());
        } else if (argc == 2 && strcmp(argv[1], "stats") == 0) {
            /* static for the same reason as the status line above. */
            static char json[SHELL_OUT_MAX];
            const char *err = sco_pkt_stats(json, sizeof(json));
            if (!err) {
                shell_line("%s", json);
            }
            answer(err);
        } else if (argc == 3 && strcmp(argv[1], "type") == 0) {
            answer(sco_set_type(argv[2]));
        } else {
            answer("sco takes start, stop, stats, or type <ev3|2ev3|default>");
        }
        return;
    }
    if (strcmp(c, "afh") == 0) {
        if (argc == 2 && strcmp(argv[1], "off") == 0) {
            answer(bt_set_afh_all());
        } else if (argc == 2) {
            answer(bt_set_afh(argv[1]));
        } else {
            answer("afh takes twenty hex digits or off");
        }
        return;
    }
    if (argc == 1 && strcmp(c, "clock") == 0) {
        /* static for the same reason as the status line above. */
        static char json[SHELL_OUT_MAX];
        const char *err = bt_clock_json(json, sizeof(json));
        if (!err) {
            shell_line("%s", json);
        }
        answer(err);
        return;
    }
    if (argc == 1 && strcmp(c, "defaults") == 0) {
        answer(bt_defaults());
        return;
    }
    if (argc == 1 && strcmp(c, "reset") == 0) {
        answer(NULL);
        /* Let the answer leave the UART before the board goes. */
        uart_wait_tx_done(SHELL_UART, pdMS_TO_TICKS(500));
        esp_restart();
        return;
    }
    answer("unknown command, try \"help\"");
}

static void handle_line(char *line)
{
    char *argv[SHELL_WORDS];
    int argc = 0;
    char *save = NULL;

    for (char *t = strtok_r(line, " \t", &save); t && argc < SHELL_WORDS;
         t = strtok_r(NULL, " \t", &save)) {
        argv[argc++] = t;
    }
    if (argc == 0) {
        return; /* a blank line is not a command and gets no answer */
    }
    run(argc, argv);
}

static void shell_task(void *arg)
{
    char line[SHELL_LINE_MAX + 1];
    size_t len = 0;
    bool overrun = false;

    (void)arg;
    for (;;) {
        uint8_t ch;
        if (uart_read_bytes(SHELL_UART, &ch, 1, portMAX_DELAY) != 1) {
            continue;
        }
        if (ch == '\r' || ch == '\n') {
            if (overrun) {
                shell_line("ERR line too long");
            } else {
                line[len] = '\0';
                handle_line(line);
            }
            len = 0;
            overrun = false;
            continue;
        }
        if (len < SHELL_LINE_MAX) {
            line[len++] = (char)ch;
        } else {
            overrun = true;
        }
    }
}

void app_main(void)
{
    const uart_config_t cfg = {
        .baud_rate = SHELL_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    const char *err;

    out_lock = xSemaphoreCreateMutex();
    ESP_ERROR_CHECK(uart_driver_install(SHELL_UART, SHELL_RX_BUF, SHELL_TX_BUF, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(SHELL_UART, &cfg));

    err = bt_start();
    if (err) {
        shell_line("utesp: %s start failed: %s", UTESP_FW_VERSION, err);
    } else {
        shell_line("utesp: %s ready", UTESP_FW_VERSION);
    }

    xTaskCreate(shell_task, "utesp_shell", 4096, NULL, 4, NULL);
}
