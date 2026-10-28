/*
 * Advertiser role: legacy ADV_NONCONN_IND (or ADV_IND) on the 1M PHY from
 * the identity address, on a chosen advertising channel or all three, with a
 * fixed manufacturer-specific payload carrying a magic, the channel map, a
 * counter, and a tag set over the shell.
 *
 * Copyright (c) 2026 The Ubertooth test harness authors
 * SPDX-License-Identifier: Apache-2.0
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/shell/shell.h>
#include <zephyr/sys/byteorder.h>

#include "roles.h"

/* Bluetooth Core: advertising interval 20 ms to 10.24 s. */
#define INTERVAL_MIN_MS 20
#define INTERVAL_MAX_MS 10240
#define INTERVAL_DEFAULT_MS 100
/* The payload counter steps once a second while advertising. */
#define COUNTER_PERIOD K_SECONDS(1)

static K_MUTEX_DEFINE(lock);

static struct {
	bool on;          /* advertising requested (start .. stop) */
	bool running;     /* the controller is advertising now */
	bool connectable; /* ADV_IND instead of ADV_NONCONN_IND */
	uint8_t chmap;
	uint32_t interval_ms;
	uint32_t counter;
	uint8_t tag[UTDK_TAG_MAX];
	uint8_t tag_len;
	uint32_t starts;
	uint32_t connections;
	int last_err;
} st = {
	.chmap = UTDK_CHAN_ALL,
	.interval_ms = INTERVAL_DEFAULT_MS,
};

static uint8_t mfg[UTDK_MFG_FIXED_LEN + UTDK_TAG_MAX];
static uint8_t flags_nonconn = BT_LE_AD_NO_BREDR;
static uint8_t flags_conn = BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR;
static struct bt_data ad[2];

static void counter_tick(struct k_work *work);
static void restart(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(counter_work, counter_tick);
static K_WORK_DEFINE(restart_work, restart);

static void build_ad(void)
{
	size_t len = utdk_mfg_payload(mfg, st.chmap, st.counter, st.tag, st.tag_len);

	ad[0].type = BT_DATA_FLAGS;
	ad[0].data_len = 1;
	ad[0].data = st.connectable ? &flags_conn : &flags_nonconn;
	ad[1].type = BT_DATA_MANUFACTURER_DATA;
	ad[1].data_len = len;
	ad[1].data = mfg;
}

/* Called with the lock held. */
static int start_locked(void)
{
	uint32_t options = BT_LE_ADV_OPT_USE_IDENTITY;
	uint32_t units = st.interval_ms * 8 / 5; /* 0.625 ms units */
	int err;

	if (st.connectable) {
		options |= BT_LE_ADV_OPT_CONN;
	}
	if (!(st.chmap & UTDK_CHAN_37)) {
		options |= BT_LE_ADV_OPT_DISABLE_CHAN_37;
	}
	if (!(st.chmap & UTDK_CHAN_38)) {
		options |= BT_LE_ADV_OPT_DISABLE_CHAN_38;
	}
	if (!(st.chmap & UTDK_CHAN_39)) {
		options |= BT_LE_ADV_OPT_DISABLE_CHAN_39;
	}

	const struct bt_le_adv_param param =
		BT_LE_ADV_PARAM_INIT(options, units, units, NULL);

	build_ad();
	err = bt_le_adv_start(&param, ad, ARRAY_SIZE(ad), NULL, 0);
	st.last_err = err;
	if (err) {
		return err;
	}
	st.running = true;
	st.starts++;
	k_work_reschedule(&counter_work, COUNTER_PERIOD);
	return 0;
}

/* Called with the lock held. */
static int stop_locked(void)
{
	int err = 0;

	(void)k_work_cancel_delayable(&counter_work);
	if (st.running) {
		err = bt_le_adv_stop();
		st.last_err = err;
		if (!err) {
			st.running = false;
		}
	}
	return err;
}

static void counter_tick(struct k_work *work)
{
	ARG_UNUSED(work);
	k_mutex_lock(&lock, K_FOREVER);
	if (st.running) {
		st.counter++;
		build_ad();
		st.last_err = bt_le_adv_update_data(ad, ARRAY_SIZE(ad), NULL, 0);
		k_work_reschedule(&counter_work, COUNTER_PERIOD);
	}
	k_mutex_unlock(&lock);
}

/* A connection to ADV_IND ends legacy advertising: drop the link and start
 * again once the connection object is free.
 */
static void restart(struct k_work *work)
{
	ARG_UNUSED(work);
	k_mutex_lock(&lock, K_FOREVER);
	if (st.on && !st.running) {
		(void)start_locked();
	}
	k_mutex_unlock(&lock);
}

static void connected(struct bt_conn *conn, uint8_t err)
{
	/* The callbacks are global; the connection roles keep their links. */
	if (err || utdk_role() != UTDK_ROLE_ADVERTISER) {
		return;
	}
	k_mutex_lock(&lock, K_FOREVER);
	st.connections++;
	st.running = false;
	(void)k_work_cancel_delayable(&counter_work);
	k_mutex_unlock(&lock);
	(void)bt_conn_disconnect(conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
}

static void recycled(void)
{
	if (utdk_role() != UTDK_ROLE_ADVERTISER) {
		return;
	}
	k_work_submit(&restart_work);
}

BT_CONN_CB_DEFINE(conn_callbacks) = {
	.connected = connected,
	.recycled = recycled,
};

int adv_init(void)
{
	k_mutex_lock(&lock, K_FOREVER);
	build_ad();
	k_mutex_unlock(&lock);
	return 0;
}

static const char *chmap_name(uint8_t chmap)
{
	switch (chmap) {
	case UTDK_CHAN_37:
		return "37";
	case UTDK_CHAN_38:
		return "38";
	case UTDK_CHAN_39:
		return "39";
	default:
		return "all";
	}
}

void adv_status(char *buf, size_t len)
{
	char tag[2 * UTDK_TAG_MAX + 1];

	k_mutex_lock(&lock, K_FOREVER);
	for (size_t i = 0; i < st.tag_len; i++) {
		snprintf(&tag[2 * i], 3, "%02x", st.tag[i]);
	}
	tag[2 * st.tag_len] = '\0';
	snprintf(buf, len,
		 ",\"adv\":%s,\"running\":%s,\"type\":\"%s\",\"chmap\":\"%s\","
		 "\"interval_ms\":%u,\"payload\":\"%s\",\"counter\":%u,"
		 "\"starts\":%u,\"connections\":%u,\"last_err\":%d",
		 st.on ? "true" : "false", st.running ? "true" : "false",
		 st.connectable ? "conn" : "nonconn", chmap_name(st.chmap),
		 st.interval_ms, tag, st.counter, st.starts, st.connections,
		 st.last_err);
	k_mutex_unlock(&lock);
}

/* Apply a setting change: stop, change, and start again if it was on. */
typedef void (*change_fn)(void *arg);

static int change(const struct shell *sh, change_fn fn, void *arg)
{
	int err;

	k_mutex_lock(&lock, K_FOREVER);
	err = stop_locked();
	if (err) {
		k_mutex_unlock(&lock);
		return utdk_err(sh, "stop failed (%d)", err);
	}
	fn(arg);
	if (st.on) {
		err = start_locked();
	}
	k_mutex_unlock(&lock);
	if (err) {
		return utdk_err(sh, "start failed (%d)", err);
	}
	return utdk_ok(sh);
}

static int cmd_start(const struct shell *sh, size_t argc, char **argv)
{
	int err = 0;

	ARG_UNUSED(argc);
	ARG_UNUSED(argv);
	k_mutex_lock(&lock, K_FOREVER);
	st.on = true;
	if (!st.running) {
		err = start_locked();
		if (err) {
			st.on = false;
		}
	}
	k_mutex_unlock(&lock);
	if (err) {
		return utdk_err(sh, "start failed (%d)", err);
	}
	return utdk_ok(sh);
}

static int cmd_stop(const struct shell *sh, size_t argc, char **argv)
{
	int err;

	ARG_UNUSED(argc);
	ARG_UNUSED(argv);
	k_mutex_lock(&lock, K_FOREVER);
	st.on = false;
	err = stop_locked();
	k_mutex_unlock(&lock);
	if (err) {
		return utdk_err(sh, "stop failed (%d)", err);
	}
	return utdk_ok(sh);
}

static void set_chmap(void *arg)
{
	st.chmap = *(uint8_t *)arg;
}

static int cmd_chmap(const struct shell *sh, size_t argc, char **argv)
{
	uint8_t chmap;

	ARG_UNUSED(argc);
	if (strcmp(argv[1], "37") == 0) {
		chmap = UTDK_CHAN_37;
	} else if (strcmp(argv[1], "38") == 0) {
		chmap = UTDK_CHAN_38;
	} else if (strcmp(argv[1], "39") == 0) {
		chmap = UTDK_CHAN_39;
	} else if (strcmp(argv[1], "all") == 0) {
		chmap = UTDK_CHAN_ALL;
	} else {
		return utdk_err(sh, "channel map must be 37, 38, 39 or all");
	}
	return change(sh, set_chmap, &chmap);
}

static void set_interval(void *arg)
{
	st.interval_ms = *(uint32_t *)arg;
}

static int cmd_interval(const struct shell *sh, size_t argc, char **argv)
{
	char *end;
	unsigned long value;
	uint32_t interval;

	ARG_UNUSED(argc);
	value = strtoul(argv[1], &end, 10);
	if (argv[1][0] < '0' || argv[1][0] > '9' || *end != '\0' ||
	    value < INTERVAL_MIN_MS || value > INTERVAL_MAX_MS) {
		return utdk_err(sh, "interval must be %d-%d ms", INTERVAL_MIN_MS,
				INTERVAL_MAX_MS);
	}
	interval = value;
	return change(sh, set_interval, &interval);
}

struct tag_value {
	uint8_t bytes[UTDK_TAG_MAX];
	uint8_t len;
};

static void set_tag(void *arg)
{
	const struct tag_value *tag = arg;

	memcpy(st.tag, tag->bytes, tag->len);
	st.tag_len = tag->len;
}

static int hex_digit(char c)
{
	if (c >= '0' && c <= '9') {
		return c - '0';
	}
	if (c >= 'a' && c <= 'f') {
		return c - 'a' + 10;
	}
	if (c >= 'A' && c <= 'F') {
		return c - 'A' + 10;
	}
	return -1;
}

static int cmd_payload(const struct shell *sh, size_t argc, char **argv)
{
	struct tag_value tag = {0};
	const char *text = argv[1];
	size_t digits;

	ARG_UNUSED(argc);
	if (strcmp(text, "none") != 0) {
		digits = strlen(text);
		if (digits == 0 || digits % 2 || digits > 2 * UTDK_TAG_MAX) {
			return utdk_err(sh, "payload must be 1-%d bytes of hex, or none",
					UTDK_TAG_MAX);
		}
		for (size_t i = 0; i < digits; i += 2) {
			int high = hex_digit(text[i]);
			int low = hex_digit(text[i + 1]);

			if (high < 0 || low < 0) {
				return utdk_err(sh, "payload must be hex digits");
			}
			tag.bytes[tag.len++] = (uint8_t)(high << 4 | low);
		}
	}
	return change(sh, set_tag, &tag);
}

static void set_type(void *arg)
{
	st.connectable = *(bool *)arg;
}

static int cmd_type(const struct shell *sh, size_t argc, char **argv)
{
	bool connectable;

	ARG_UNUSED(argc);
	if (strcmp(argv[1], "nonconn") == 0) {
		connectable = false;
	} else if (strcmp(argv[1], "conn") == 0) {
		connectable = true;
	} else {
		return utdk_err(sh, "type must be nonconn (ADV_NONCONN_IND) or conn (ADV_IND)");
	}
	return change(sh, set_type, &connectable);
}

static void set_defaults(void *arg)
{
	ARG_UNUSED(arg);
	st.on = false;
	st.connectable = false;
	st.chmap = UTDK_CHAN_ALL;
	st.interval_ms = INTERVAL_DEFAULT_MS;
	st.tag_len = 0;
}

void adv_leave(void)
{
	k_mutex_lock(&lock, K_FOREVER);
	(void)stop_locked();
	set_defaults(NULL);
	k_mutex_unlock(&lock);
}

static int cmd_defaults(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);
	return change(sh, set_defaults, NULL);
}

SHELL_STATIC_SUBCMD_SET_CREATE(adv_cmds,
	SHELL_CMD_ARG(start, NULL, "Start advertising", cmd_start, 1, 0),
	SHELL_CMD_ARG(stop, NULL, "Stop advertising", cmd_stop, 1, 0),
	SHELL_CMD_ARG(chmap, NULL, "<37|38|39|all> Advertising channels", cmd_chmap, 2, 0),
	SHELL_CMD_ARG(interval, NULL, "<ms> Advertising interval, 20-10240", cmd_interval,
		      2, 0),
	SHELL_CMD_ARG(payload, NULL, "<hex|none> Payload tag, up to 12 bytes", cmd_payload,
		      2, 0),
	SHELL_CMD_ARG(type, NULL, "<nonconn|conn> ADV_NONCONN_IND or ADV_IND", cmd_type, 2,
		      0),
	SHELL_CMD_ARG(defaults, NULL, "Stop and restore the boot settings", cmd_defaults, 1,
		      0),
	SHELL_SUBCMD_SET_END);

/* `adv` alone or with an unknown subcommand: an ERR line, not the help. */
static int cmd_adv(const struct shell *sh, size_t argc, char **argv)
{
	if (argc < 2) {
		return utdk_err(sh, "usage: adv <start|stop|chmap|interval|payload|type|defaults>");
	}
	return utdk_err(sh, "unknown adv command '%s'", argv[1]);
}

SHELL_CMD_ARG_REGISTER(adv, &adv_cmds, "Advertiser control", cmd_adv, 1, 1);
