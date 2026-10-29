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
#define SHELL_LINE_MAX 160 /* longest command line accepted */
#define SHELL_OUT_MAX  768 /* longest answer line, set by the status JSON */
#define SHELL_WORDS    8

static SemaphoreHandle_t out_lock;

void shell_line(const char *fmt, ...)
{
    char buf[SHELL_OUT_MAX + 2];
    va_list ap;
    int n;

    va_start(ap, fmt);
    n = vsnprintf(buf, sizeof(buf) - 1, fmt, ap);
    va_end(ap);
    if (n < 0) {
        return;
    }
    if (n > (int)sizeof(buf) - 2) {
        n = (int)sizeof(buf) - 2;
    }
    buf[n++] = '\n';

    /* The notices come from Bluetooth callbacks, so two writers can meet. */
    if (out_lock) {
        xSemaphoreTake(out_lock, portMAX_DELAY);
    }
    uart_write_bytes(SHELL_UART, buf, n);
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

static bool parse_int(const char *s, int *out)
{
    char *end;
    long v;

    if (!s || !*s) {
        return false;
    }
    v = strtol(s, &end, 10);
    if (*end != '\0') {
        return false;
    }
    *out = (int)v;
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
        "name <text>                   Bluetooth device name, 1-32 characters",
        "role <acceptor|initiator>     stop, then take this role",
        "disc <on|off>                 discoverable and connectable",
        "spp peer <aa:bb:cc:dd:ee:ff>  the address the initiator calls",
        "spp start                     acceptor: listen; initiator: connect",
        "spp stop                      drop the link and stop listening",
        "stream <on|off>               send payload bytes while connected",
        "stream period <ms>            time between writes, 5-10000",
        "stream payload <bytes>        bytes per write, 8-512",
        "inquiry <seconds>             one general inquiry, 2-60 seconds",
        "txpower <min> <max>           dBm, one of -12 -9 -6 -3 0 3 6 9",
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
        char json[640];
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
    if (argc == 2 && strcmp(c, "name") == 0) {
        answer(bt_set_name(argv[1]));
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
    if (argc == 2 && strcmp(c, "inquiry") == 0) {
        unsigned secs;
        answer(parse_uint(argv[1], &secs) ? bt_inquiry(secs) : "inquiry takes a number of seconds");
        return;
    }
    if (argc == 3 && strcmp(c, "txpower") == 0) {
        int lo, hi;
        if (!parse_int(argv[1], &lo) || !parse_int(argv[2], &hi)) {
            answer("txpower takes two numbers in dBm");
        } else {
            answer(bt_tx_power(lo, hi));
        }
        return;
    }
    if (argc == 1 && strcmp(c, "defaults") == 0) {
        answer(bt_defaults());
        return;
    }
    if (argc == 1 && strcmp(c, "reset") == 0) {
        answer(NULL);
        vTaskDelay(pdMS_TO_TICKS(100));
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
    ESP_ERROR_CHECK(uart_driver_install(SHELL_UART, SHELL_RX_BUF, 0, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(SHELL_UART, &cfg));

    err = bt_start();
    if (err) {
        shell_line("utesp: %s start failed: %s", UTESP_FW_VERSION, err);
    } else {
        shell_line("utesp: %s ready", UTESP_FW_VERSION);
    }

    xTaskCreate(shell_task, "utesp_shell", 4096, NULL, 4, NULL);
}
