/* SPDX-License-Identifier: Apache-2.0 */

/* The serial command shell of the Classic Bluetooth test peer.
 *
 * One command line in, zero or more data lines out, then exactly one "OK" or
 * "ERR <reason>" line. Lines that start with "utesp: " are notices the board
 * sends by itself (the boot banner, a link coming up or going down) and are
 * not part of any answer, so a driver reading a response skips them. The
 * protocol matches the nRF52840 DK role firmware in this repository, which
 * lets the harness keep one shell driver for both kinds of board.
 *
 * "frame on" puts a length and a CRC in front of every line this file
 * writes. The bridge to the host loses runs of bytes when the host is busy,
 * and a line that lost some from its middle still arrives with its newline,
 * so nothing about the line itself says it is broken: a status line that
 * lost a digit is still a status line. With the frame, the host knows
 * (harness #310). */

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
                             * whose longest possible form is 1124 bytes:
                             * every number at its widest, the longest name,
                             * and the longest channel map reason */
#define SHELL_FRAME_MAX 16  /* room for "#<len>,<crc>:", which at its widest
                             * is "#1199,ffff:", eleven bytes */
#define SHELL_WORDS    8

static SemaphoreHandle_t out_lock;
/* Whether every line this file writes carries its own length and CRC. Off
 * at boot, and a reset puts it back: a driver asks for it once it has read
 * the boot notice, which is how it knows the image has the command at all. */
static bool framing;

/* CRC-16/CCITT-FALSE, bit by bit. It runs on at most 1200 bytes per line at
 * 115200 baud, where one line takes 85 milliseconds to send, so the few
 * microseconds this costs are free; a table would be 512 bytes of RAM for
 * nothing. The harness computes the same thing in esp32peer.crc16(). */
static uint16_t shell_crc16(const char *data, int len)
{
    uint16_t crc = 0xffff;

    for (int i = 0; i < len; i++) {
        crc ^= (uint16_t)((uint8_t)data[i]) << 8;
        for (int bit = 0; bit < 8; bit++) {
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021)
                                 : (uint16_t)(crc << 1);
        }
    }
    return crc;
}

void shell_line(const char *fmt, ...)
{
    /* One buffer, held by out_lock, rather than 770 bytes on the caller's
     * stack: the notices below come from the Bluetooth task, whose stack is
     * small. The line is built SHELL_FRAME_MAX bytes in, so a frame can be
     * written in front of it without a second buffer or a copy. */
    static char buf[SHELL_FRAME_MAX + SHELL_OUT_MAX + 2];
    char *line = buf + SHELL_FRAME_MAX;
    char *start = line;
    char head[SHELL_FRAME_MAX + 1];
    va_list ap;
    int n, head_len;

    if (out_lock) {
        xSemaphoreTake(out_lock, portMAX_DELAY);
    }
    va_start(ap, fmt);
    n = vsnprintf(line, SHELL_OUT_MAX, fmt, ap);
    va_end(ap);
    if (n >= 0) {
        if (n > SHELL_OUT_MAX - 1) {
            n = SHELL_OUT_MAX - 1; /* vsnprintf's count is what it wanted */
        }
        if (framing) {
            head_len = snprintf(head, sizeof(head), "#%d,%04x:", n,
                                shell_crc16(line, n));
            if (head_len > 0 && head_len <= SHELL_FRAME_MAX) {
                start = line - head_len;
                memcpy(start, head, (size_t)head_len);
                n += head_len;
            }
        }
        start[n++] = '\n';
        uart_write_bytes(SHELL_UART, start, n);
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

/* The nth word of the command line, or "" when the line has no nth word.
 *
 * handle_line() fills only the words a line really had, so argv beyond argc
 * holds whatever was last on the shell task's stack. Every branch of run()
 * reads its operands through this, which is what keeps a line shorter than
 * the branch expects from reading one of those: a branch that tested an
 * operand before testing argc used to read an uninitialized pointer
 * (#262), and no arrangement of argc tests can be relied on not to grow
 * that back. "" matches no subcommand and parses as no number, so a short
 * line falls through to the usage answer by itself. */
static const char *word(int argc, char **argv, int n)
{
    return n < argc ? argv[n] : "";
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
        "sco type <" SCO_TYPE_NAMES ">   the packet type the next link asks for",
        "clock                         the native and piconet clocks",
        "afh <20 hex digits>           hop over this channel map",
        "afh off                       hop over every channel again",
        "frame <on|off>                length and CRC on every line out",
        "defaults                      restore every boot setting",
        "reset                         restart the board",
    };

    for (size_t i = 0; i < sizeof(lines) / sizeof(lines[0]); i++) {
        shell_line("%s", lines[i]);
    }
}

static void run(int argc, char **argv)
{
    /* The command and its two operands. argv[0] is always there, because
     * handle_line() answers nothing at all for a blank line; the operands
     * come through word(), so a branch cannot read one the line did not
     * carry. Nothing below indexes argv itself, and a unit test checks
     * that it stays that way. */
    const char *c = argv[0];
    const char *a1 = word(argc, argv, 1);
    const char *a2 = word(argc, argv, 2);

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
        if (strcmp(a1, "acceptor") == 0) {
            answer(bt_set_role(PEER_ROLE_ACCEPTOR));
        } else if (strcmp(a1, "initiator") == 0) {
            answer(bt_set_role(PEER_ROLE_INITIATOR));
        } else {
            answer("role must be acceptor or initiator");
        }
        return;
    }
    if (argc == 2 && strcmp(c, "disc") == 0) {
        bool on;
        answer(parse_onoff(a1, &on) ? bt_set_discoverable(on) : "disc takes on or off");
        return;
    }
    if (strcmp(c, "spp") == 0) {
        if (argc == 3 && strcmp(a1, "peer") == 0) {
            answer(bt_set_peer(a2));
        } else if (argc == 2 && strcmp(a1, "start") == 0) {
            answer(bt_spp_start());
        } else if (argc == 2 && strcmp(a1, "stop") == 0) {
            answer(bt_spp_stop());
        } else {
            answer("spp takes peer <address>, start, or stop");
        }
        return;
    }
    if (strcmp(c, "stream") == 0) {
        bool on;
        unsigned v;
        if (argc == 2 && parse_onoff(a1, &on)) {
            answer(bt_stream(on));
        } else if (argc == 3 && strcmp(a1, "period") == 0) {
            answer(parse_uint(a2, &v) ? bt_stream_period(v) : "period must be a number");
        } else if (argc == 3 && strcmp(a1, "payload") == 0) {
            answer(parse_uint(a2, &v) ? bt_stream_payload(v) : "payload must be a number");
        } else {
            answer("stream takes on, off, period <ms>, or payload <bytes>");
        }
        return;
    }
    if (strcmp(c, "sco") == 0) {
        if (argc == 2 && strcmp(a1, "start") == 0) {
            answer(bt_sco_start());
        } else if (argc == 2 && strcmp(a1, "stop") == 0) {
            answer(sco_stop());
        } else if (argc == 2 && strcmp(a1, "stats") == 0) {
            /* static for the same reason as the status line above. */
            static char json[SHELL_OUT_MAX];
            const char *err = sco_pkt_stats(json, sizeof(json));
            if (!err) {
                shell_line("%s", json);
            }
            answer(err);
        } else if (strcmp(a1, "type") == 0) {
            /* The type's own usage line, which names the three it takes;
             * every other malformed "sco" line gets the command's. Bare
             * "sco" has no first word to match "type", so it goes below. */
            answer(argc == 3 ? sco_set_type(a2) : SCO_TYPE_USAGE);
        } else {
            answer(SCO_USAGE);
        }
        return;
    }
    if (strcmp(c, "afh") == 0) {
        if (argc == 2 && strcmp(a1, "off") == 0) {
            answer(bt_set_afh_all());
        } else if (argc == 2) {
            answer(bt_set_afh(a1));
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
    if (argc == 2 && strcmp(c, "frame") == 0) {
        bool on;
        if (!parse_onoff(a1, &on)) {
            answer("frame takes on or off");
            return;
        }
        /* The answer comes first, so it is in the form that was in force
         * when the command arrived and the new form starts with the line
         * after it. Either way round there is no line whose form the host
         * cannot predict, which there would be if the switch happened in
         * the middle of answering.
         *
         * Nothing else touches this: it is about the wire, not the board,
         * so "defaults" leaves it alone. A reset clears it, because the
         * board that comes back has not been asked. */
        answer(NULL);
        framing = on;
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

/* Why the chip restarted, in words.
 *
 * The ROM prints its own "rst:0x..." line before any of this runs, which is
 * the only evidence from a board that crashed before its code started. This
 * is the same news read the other way, through esp_reset_reason(), which
 * names a panic, a watchdog and a brownout apart where the ROM's number
 * lumps the first two together. A board that restarted under a capture is
 * harness #311, and a run that cannot say why cannot chase it. */
static const char *reset_reason(void)
{
    switch (esp_reset_reason()) {
    case ESP_RST_POWERON:  return "power on";
    case ESP_RST_EXT:      return "the reset pin";
    case ESP_RST_SW:       return "software, which is what \"reset\" does";
    case ESP_RST_PANIC:    return "a panic or an abort in the firmware";
    case ESP_RST_INT_WDT:  return "the interrupt watchdog";
    case ESP_RST_TASK_WDT: return "the task watchdog";
    case ESP_RST_WDT:      return "another watchdog";
    case ESP_RST_DEEPSLEEP: return "waking from deep sleep";
    case ESP_RST_BROWNOUT: return "the brownout detector, so the supply dipped";
    case ESP_RST_SDIO:     return "SDIO";
    default:               return "a reason the chip does not report";
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

    /* Before Bluetooth, because bringing it up takes over a second and a
     * host reading the port wants to know a restart happened as early as it
     * can. It comes before the ready notice, which is the line that ends
     * the boot, so a driver reads the reason and then the version. */
    shell_line("utesp: boot reason %s", reset_reason());

    err = bt_start();
    if (err) {
        shell_line("utesp: %s start failed: %s", UTESP_FW_VERSION, err);
    } else {
        shell_line("utesp: %s ready", UTESP_FW_VERSION);
    }

    xTaskCreate(shell_task, "utesp_shell", 4096, NULL, 4, NULL);
}
