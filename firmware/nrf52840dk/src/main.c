/*
 * Role firmware for the harness's nRF52840 DK test devices: Bluetooth
 * start-up, the identity address, and the `status` command.
 *
 * Copyright (c) 2026 The Ubertooth test harness authors
 * SPDX-License-Identifier: Apache-2.0
 */

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/shell/shell.h>
#include <zephyr/sys/byteorder.h>

#include "roles.h"

static bool bt_ready;
static enum utdk_role active_role = UTDK_ROLE_ADVERTISER;

int utdk_ok(const struct shell *sh)
{
	shell_print(sh, "OK");
	return 0;
}

int utdk_err(const struct shell *sh, const char *fmt, ...)
{
	char text[96];
	va_list args;

	va_start(args, fmt);
	vsnprintf(text, sizeof(text), fmt, args);
	va_end(args);
	shell_print(sh, "ERR %s", text);
	/* The response line already says why; a plain non-zero status keeps
	 * the shell from printing anything more.
	 */
	return -EINVAL;
}

enum utdk_role utdk_role(void)
{
	return active_role;
}

const char *utdk_role_name(enum utdk_role role)
{
	switch (role) {
	case UTDK_ROLE_CENTRAL:
		return "central";
	case UTDK_ROLE_PERIPHERAL:
		return "peripheral";
	default:
		return "advertiser";
	}
}

size_t utdk_mfg_payload(uint8_t *out, uint8_t chmap, uint32_t counter,
			const uint8_t *tag, uint8_t tag_len)
{
	sys_put_le16(UTDK_COMPANY_ID, &out[0]);
	memcpy(&out[2], UTDK_MAGIC, 4);
	out[6] = UTDK_FORMAT;
	out[7] = chmap;
	sys_put_le32(counter, &out[8]);
	if (tag_len > 0) {
		memcpy(&out[UTDK_MFG_FIXED_LEN], tag, tag_len);
	}
	return UTDK_MFG_FIXED_LEN + tag_len;
}

void utdk_address(char *out, size_t len)
{
	bt_addr_le_t addrs[CONFIG_BT_ID_MAX];
	size_t count = ARRAY_SIZE(addrs);

	bt_id_get(addrs, &count);
	if (!bt_ready || count == 0) {
		snprintf(out, len, "unknown");
		return;
	}
	/* bt_addr_t is little-endian; print most significant byte first. */
	const uint8_t *a = addrs[0].a.val;

	snprintf(out, len, "%02X:%02X:%02X:%02X:%02X:%02X",
		 a[5], a[4], a[3], a[2], a[1], a[0]);
}

static int cmd_status(const struct shell *sh, size_t argc, char **argv)
{
	char line[448];
	char address[18];
	int used;

	ARG_UNUSED(argc);
	ARG_UNUSED(argv);
	utdk_address(address, sizeof(address));
	used = snprintf(line, sizeof(line),
			"{\"role\":\"%s\",\"fw\":\"%s\",\"bt\":%s,"
			"\"address\":\"%s\",\"address_type\":\"random static\"",
			utdk_role_name(active_role), UTDK_FW_VERSION,
			bt_ready ? "true" : "false", address);
	if (used > 0 && used < sizeof(line)) {
		/* Every role reports the advertiser's fields, so one status
		 * shape serves them all; the connection fields follow.
		 */
		adv_status(line + used, sizeof(line) - used);
		used += strlen(line + used);
	}
	if (used > 0 && used < sizeof(line)) {
		conn_status(line + used, sizeof(line) - used);
	}
	shell_print(sh, "%s}", line);
	return utdk_ok(sh);
}

SHELL_CMD_ARG_REGISTER(status, NULL, "Print one JSON status line", cmd_status, 1, 0);

static int cmd_role(const struct shell *sh, size_t argc, char **argv)
{
	enum utdk_role wanted;

	ARG_UNUSED(argc);
	if (strcmp(argv[1], "advertiser") == 0) {
		wanted = UTDK_ROLE_ADVERTISER;
	} else if (strcmp(argv[1], "central") == 0) {
		wanted = UTDK_ROLE_CENTRAL;
	} else if (strcmp(argv[1], "peripheral") == 0) {
		wanted = UTDK_ROLE_PERIPHERAL;
	} else {
		return utdk_err(sh, "role must be advertiser, central or peripheral");
	}
	/* Leave both roles idle first, so the board is in a known state
	 * whichever role it was in, then take the new one.
	 */
	conn_leave();
	adv_leave();
	active_role = wanted;
	return utdk_ok(sh);
}

SHELL_CMD_ARG_REGISTER(role, NULL, "<advertiser|central|peripheral> Switch role",
		       cmd_role, 2, 0);

static int cmd_version(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);
	shell_print(sh, "%s", UTDK_FW_VERSION);
	return utdk_ok(sh);
}

SHELL_CMD_ARG_REGISTER(version, NULL, "Print the firmware version", cmd_version, 1, 0);

int main(void)
{
	int err = bt_enable(NULL);

	if (err) {
		printk("utdk: bt_enable failed (%d)\n", err);
		return 0;
	}
	bt_ready = true;
	err = adv_init();
	if (err) {
		printk("utdk: advertiser init failed (%d)\n", err);
	}
	err = conn_init();
	if (err) {
		printk("utdk: connection init failed (%d)\n", err);
	}
	printk("utdk: %s ready\n", UTDK_FW_VERSION);
	return 0;
}
