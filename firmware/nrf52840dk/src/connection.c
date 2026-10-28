/*
 * Central and peripheral roles: a pair of DKs that connect on the LE 1M PHY
 * with a set connection interval, raise the data length to 251 bytes, send a
 * counter payload on a schedule, and can ask for a connection parameter
 * update. One Ubertooth follows the link; these boards do the transmitting.
 *
 * The central connects to the peripheral's identity address, so no scanning
 * is needed and the CONNECT_IND carries exactly the parameters asked for.
 * Data goes to one characteristic with Write Without Response, which puts
 * the whole payload in one link-layer packet.
 *
 * Copyright (c) 2026 The Ubertooth test harness authors
 * SPDX-License-Identifier: Apache-2.0
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/bluetooth/addr.h>
#include <zephyr/bluetooth/att.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gap.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/shell/shell.h>
#include <zephyr/sys/byteorder.h>

#include "roles.h"

/* Bluetooth Core: connection interval 7.5 ms to 4 s, in 1.25 ms units. */
#define INTERVAL_MIN_MS 8
#define INTERVAL_MAX_MS 4000
#define INTERVAL_DEFAULT_MS 30
#define UNITS_PER_MS 4 / 5 /* ms -> 1.25 ms units */
/* Supervision timeout, in 10 ms units: long enough that a missed packet or
 * two while the Ubertooth starts does not drop the link.
 */
#define TIMEOUT_UNITS 400
/* Write Without Response payload: the ATT MTU less the 3-byte ATT header. */
#define PAYLOAD_MAX (CONFIG_BT_L2CAP_TX_MTU - 3)
#define PAYLOAD_DEFAULT PAYLOAD_MAX
#define PERIOD_MIN_MS 10
#define PERIOD_MAX_MS 10000
#define PERIOD_DEFAULT_MS 100
/* Time for the controller to connect before `conn start` gives up. */
#define CONNECT_TIMEOUT_MS 5000
/* MTU exchange, PHY, data length and discovery before the first write. */
#define SETUP_DELAY K_MSEC(700)

/* One service with one write-only characteristic. The UUIDs are this
 * firmware's own; nothing else uses them.
 */
#define SERVICE_UUID_VAL \
	BT_UUID_128_ENCODE(0x0000da7a, 0x5554, 0x444b, 0xb000, 0x00805f9b34fb)
#define DATA_UUID_VAL \
	BT_UUID_128_ENCODE(0x0001da7a, 0x5554, 0x444b, 0xb000, 0x00805f9b34fb)

static const struct bt_uuid_128 service_uuid = BT_UUID_INIT_128(SERVICE_UUID_VAL);
static const struct bt_uuid_128 data_uuid = BT_UUID_INIT_128(DATA_UUID_VAL);

static K_MUTEX_DEFINE(lock);
static K_SEM_DEFINE(connected_sem, 0, 1);

static struct {
	bool on;               /* the role was started */
	struct bt_conn *conn;  /* the link, while it is up */
	bool have_peer;
	bt_addr_le_t peer;
	uint32_t interval_ms;  /* asked for in the CONNECT_IND */
	uint32_t update_ms;    /* the last parameter update asked for */
	bool dle;              /* ask for 251-byte data length */
	uint16_t payload_len;
	uint32_t period_ms;
	uint16_t handle;       /* the peer's data characteristic */
	uint16_t interval_units, latency, timeout_units;
	uint16_t tx_octets, rx_octets, mtu;
	uint8_t phy;
	uint32_t writes, write_errors, received, updates, connects, disconnects;
	int last_err;
} cn = {
	.interval_ms = INTERVAL_DEFAULT_MS,
	.dle = true,
	.payload_len = PAYLOAD_DEFAULT,
	.period_ms = PERIOD_DEFAULT_MS,
};

static uint8_t payload[PAYLOAD_MAX];

static void setup(struct k_work *work);
static void write_tick(struct k_work *work);
static K_WORK_DEFINE(setup_work, setup);
static K_WORK_DELAYABLE_DEFINE(write_work, write_tick);

static bool role_is_connection(void)
{
	return utdk_role() == UTDK_ROLE_CENTRAL || utdk_role() == UTDK_ROLE_PERIPHERAL;
}

/* --------------------------------------------------------------------- */
/* The peripheral's service: it only counts what arrives.                */

static ssize_t on_write(struct bt_conn *conn, const struct bt_gatt_attr *attr,
			const void *buf, uint16_t len, uint16_t offset, uint8_t flags)
{
	ARG_UNUSED(conn);
	ARG_UNUSED(attr);
	ARG_UNUSED(buf);
	ARG_UNUSED(flags);
	if (offset != 0) {
		return BT_GATT_ERR(BT_ATT_ERR_INVALID_OFFSET);
	}
	k_mutex_lock(&lock, K_FOREVER);
	cn.received += len;
	k_mutex_unlock(&lock);
	return len;
}

BT_GATT_SERVICE_DEFINE(utdk_service,
	BT_GATT_PRIMARY_SERVICE(&service_uuid),
	BT_GATT_CHARACTERISTIC(&data_uuid.uuid, BT_GATT_CHRC_WRITE_WITHOUT_RESP,
			       BT_GATT_PERM_WRITE, NULL, on_write, NULL));

/* --------------------------------------------------------------------- */
/* Connection set-up and data                                            */

static void mtu_done(struct bt_conn *conn, uint8_t err,
		     struct bt_gatt_exchange_params *params)
{
	ARG_UNUSED(params);
	if (err) {
		return;
	}
	k_mutex_lock(&lock, K_FOREVER);
	cn.mtu = bt_gatt_get_mtu(conn);
	k_mutex_unlock(&lock);
}

static struct bt_gatt_exchange_params mtu_params = { .func = mtu_done };

static uint8_t discovered(struct bt_conn *conn, const struct bt_gatt_attr *attr,
			  struct bt_gatt_discover_params *params)
{
	const struct bt_gatt_chrc *chrc;

	ARG_UNUSED(conn);
	if (attr == NULL) {
		return BT_GATT_ITER_STOP;
	}
	if (params->type != BT_GATT_DISCOVER_CHARACTERISTIC) {
		return BT_GATT_ITER_STOP;
	}
	chrc = attr->user_data;
	k_mutex_lock(&lock, K_FOREVER);
	cn.handle = chrc->value_handle;
	k_mutex_unlock(&lock);
	return BT_GATT_ITER_STOP;
}

static struct bt_gatt_discover_params discover_params = {
	.uuid = &data_uuid.uuid,
	.func = discovered,
	.start_handle = BT_ATT_FIRST_ATTRIBUTE_HANDLE,
	.end_handle = BT_ATT_LAST_ATTRIBUTE_HANDLE,
	.type = BT_GATT_DISCOVER_CHARACTERISTIC,
};

/* Raise the data length on this side; either side may ask. */
static void request_data_len(struct bt_conn *conn)
{
	int err = bt_conn_le_data_len_update(conn, BT_LE_DATA_LEN_PARAM_MAX);

	if (err && err != -EALREADY) {
		cn.last_err = err;
	}
}

/*
 * Runs once the link is up, off the Bluetooth callback thread: raise the data
 * length, pin the PHY, learn the MTU, and (as central) find the peer's
 * characteristic. The first write waits SETUP_DELAY for all of it.
 */
static void setup(struct k_work *work)
{
	struct bt_conn *conn;
	bool central;
	bool dle;

	ARG_UNUSED(work);
	k_mutex_lock(&lock, K_FOREVER);
	conn = cn.conn ? bt_conn_ref(cn.conn) : NULL;
	dle = cn.dle;
	k_mutex_unlock(&lock);
	if (conn == NULL) {
		return;
	}
	central = utdk_role() == UTDK_ROLE_CENTRAL;
	if (dle) {
		request_data_len(conn);
	}
	/* The 2M and coded PHYs are not built in, so this only confirms 1M. */
	(void)bt_conn_le_phy_update(conn, BT_CONN_LE_PHY_PARAM_1M);
	(void)bt_gatt_exchange_mtu(conn, &mtu_params);
	if (central) {
		if (bt_gatt_discover(conn, &discover_params) == 0) {
			k_work_reschedule(&write_work, SETUP_DELAY);
		}
	}
	bt_conn_unref(conn);
}

static void write_tick(struct k_work *work)
{
	struct bt_conn *conn;
	uint16_t handle, len;
	int err;

	ARG_UNUSED(work);
	k_mutex_lock(&lock, K_FOREVER);
	conn = (cn.on && cn.conn) ? bt_conn_ref(cn.conn) : NULL;
	handle = cn.handle;
	len = cn.payload_len;
	if (conn != NULL && handle != 0) {
		/* A counter in the first four bytes, so a capture shows which
		 * write each packet carried; the rest is a fixed pattern.
		 */
		sys_put_le32(cn.writes, payload);
		memset(&payload[4], 0xA5, sizeof(payload) - 4);
	}
	k_mutex_unlock(&lock);
	if (conn == NULL) {
		return;
	}
	if (handle != 0) {
		err = bt_gatt_write_without_response(conn, handle, payload, len, false);
		k_mutex_lock(&lock, K_FOREVER);
		if (err) {
			cn.write_errors++;
			cn.last_err = err;
		} else {
			cn.writes++;
		}
		k_mutex_unlock(&lock);
	}
	bt_conn_unref(conn);
	k_work_reschedule(&write_work, K_MSEC(cn.period_ms));
}

static void read_info(struct bt_conn *conn)
{
	struct bt_conn_info info;

	if (bt_conn_get_info(conn, &info) != 0 || info.type != BT_CONN_TYPE_LE) {
		return;
	}
	/* interval_us replaces the deprecated interval field; the connection
	 * interval on air is in 1.25 ms units, which is what a capture shows.
	 */
	cn.interval_units = info.le.interval_us / 1250;
	cn.latency = info.le.latency;
	cn.timeout_units = info.le.timeout;
	if (info.le.data_len != NULL) {
		cn.tx_octets = info.le.data_len->tx_max_len;
		cn.rx_octets = info.le.data_len->rx_max_len;
	}
	if (info.le.phy != NULL) {
		cn.phy = info.le.phy->tx_phy;
	}
}

static void connected(struct bt_conn *conn, uint8_t err)
{
	if (!role_is_connection()) {
		return;
	}
	k_mutex_lock(&lock, K_FOREVER);
	if (err) {
		cn.last_err = -err;
		if (cn.conn != NULL) {
			bt_conn_unref(cn.conn);
			cn.conn = NULL;
		}
		k_mutex_unlock(&lock);
		k_sem_give(&connected_sem);
		return;
	}
	if (cn.conn == NULL) {
		cn.conn = bt_conn_ref(conn);
	}
	cn.connects++;
	cn.handle = 0;
	cn.mtu = bt_gatt_get_mtu(conn);
	read_info(conn);
	k_mutex_unlock(&lock);
	k_work_submit(&setup_work);
	k_sem_give(&connected_sem);
}

static void disconnected(struct bt_conn *conn, uint8_t reason)
{
	if (!role_is_connection()) {
		return;
	}
	(void)k_work_cancel_delayable(&write_work);
	k_mutex_lock(&lock, K_FOREVER);
	cn.disconnects++;
	cn.last_err = -reason;
	cn.handle = 0;
	if (cn.conn == conn) {
		bt_conn_unref(cn.conn);
		cn.conn = NULL;
	}
	k_mutex_unlock(&lock);
}

static void param_updated(struct bt_conn *conn, uint16_t interval, uint16_t latency,
			  uint16_t timeout)
{
	if (!role_is_connection()) {
		return;
	}
	ARG_UNUSED(conn);
	k_mutex_lock(&lock, K_FOREVER);
	cn.interval_units = interval;
	cn.latency = latency;
	cn.timeout_units = timeout;
	cn.updates++;
	k_mutex_unlock(&lock);
}

static void data_len_updated(struct bt_conn *conn,
			     struct bt_conn_le_data_len_info *info)
{
	if (!role_is_connection()) {
		return;
	}
	ARG_UNUSED(conn);
	k_mutex_lock(&lock, K_FOREVER);
	cn.tx_octets = info->tx_max_len;
	cn.rx_octets = info->rx_max_len;
	k_mutex_unlock(&lock);
}

static void phy_updated(struct bt_conn *conn, struct bt_conn_le_phy_info *info)
{
	if (!role_is_connection()) {
		return;
	}
	ARG_UNUSED(conn);
	k_mutex_lock(&lock, K_FOREVER);
	cn.phy = info->tx_phy;
	k_mutex_unlock(&lock);
}

BT_CONN_CB_DEFINE(connection_callbacks) = {
	.connected = connected,
	.disconnected = disconnected,
	.le_param_updated = param_updated,
	.le_data_len_updated = data_len_updated,
	.le_phy_updated = phy_updated,
};

/* --------------------------------------------------------------------- */
/* Starting and stopping                                                 */

/* Called with the lock held. */
static int start_peripheral_locked(void)
{
	static uint8_t mfg[UTDK_MFG_FIXED_LEN + UTDK_TAG_MAX];
	static uint8_t flags = BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR;
	static struct bt_data ad[2];
	size_t len = utdk_mfg_payload(mfg, UTDK_CHAN_ALL, 0, NULL, 0);
	const struct bt_le_adv_param param = BT_LE_ADV_PARAM_INIT(
		BT_LE_ADV_OPT_CONN | BT_LE_ADV_OPT_USE_IDENTITY,
		BT_GAP_ADV_FAST_INT_MIN_2, BT_GAP_ADV_FAST_INT_MAX_2, NULL);

	ad[0].type = BT_DATA_FLAGS;
	ad[0].data_len = 1;
	ad[0].data = &flags;
	ad[1].type = BT_DATA_MANUFACTURER_DATA;
	ad[1].data_len = len;
	ad[1].data = mfg;
	cn.last_err = bt_le_adv_start(&param, ad, ARRAY_SIZE(ad), NULL, 0);
	return cn.last_err;
}

/* Called with the lock held; the connection completes in the callback. */
static int start_central_locked(void)
{
	uint16_t units = cn.interval_ms * UNITS_PER_MS;
	const struct bt_le_conn_param conn_param =
		BT_LE_CONN_PARAM_INIT(units, units, 0, TIMEOUT_UNITS);
	struct bt_conn *conn = NULL;
	int err;

	if (!cn.have_peer) {
		return -EDESTADDRREQ;
	}
	err = bt_conn_le_create(&cn.peer, BT_CONN_LE_CREATE_CONN, &conn_param, &conn);
	cn.last_err = err;
	if (err == 0) {
		cn.conn = conn;
	}
	return err;
}

/* Called with the lock held. */
static void stop_locked(void)
{
	struct bt_conn *conn = cn.conn;

	cn.on = false;
	cn.handle = 0;
	if (utdk_role() == UTDK_ROLE_PERIPHERAL) {
		(void)bt_le_adv_stop();
	}
	if (conn != NULL) {
		/* The callback drops the reference when the link is down. */
		(void)bt_conn_disconnect(conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
	}
}

int conn_init(void)
{
	return 0;
}

void conn_leave(void)
{
	(void)k_work_cancel_delayable(&write_work);
	k_mutex_lock(&lock, K_FOREVER);
	stop_locked();
	cn.interval_ms = INTERVAL_DEFAULT_MS;
	cn.update_ms = 0;
	cn.dle = true;
	cn.payload_len = PAYLOAD_DEFAULT;
	cn.period_ms = PERIOD_DEFAULT_MS;
	k_mutex_unlock(&lock);
	/* Give the disconnect time to complete, so the next role starts from
	 * no link rather than racing the teardown.
	 */
	if (cn.conn != NULL) {
		k_sleep(K_MSEC(200));
	}
}

static const char *phy_name(uint8_t phy)
{
	switch (phy) {
	case BT_GAP_LE_PHY_1M:
		return "1M";
	case BT_GAP_LE_PHY_2M:
		return "2M";
	case BT_GAP_LE_PHY_CODED:
		return "coded";
	default:
		return "none";
	}
}

void conn_status(char *buf, size_t len)
{
	char peer[BT_ADDR_LE_STR_LEN] = "";

	k_mutex_lock(&lock, K_FOREVER);
	if (cn.have_peer) {
		const uint8_t *a = cn.peer.a.val;

		snprintf(peer, sizeof(peer), "%02X:%02X:%02X:%02X:%02X:%02X",
			 a[5], a[4], a[3], a[2], a[1], a[0]);
	}
	snprintf(buf, len,
		 ",\"conn_on\":%s,\"connected\":%s,\"peer\":\"%s\","
		 "\"conn_interval_ms\":%u,\"update_ms\":%u,\"dle\":%s,"
		 "\"interval_units\":%u,\"latency\":%u,\"timeout_units\":%u,"
		 "\"tx_octets\":%u,\"rx_octets\":%u,\"mtu\":%u,\"phy\":\"%s\","
		 "\"payload_len\":%u,\"period_ms\":%u,\"writes\":%u,"
		 "\"write_errors\":%u,\"received\":%u,\"updates\":%u,"
		 "\"connects\":%u,\"disconnects\":%u,\"conn_err\":%d",
		 cn.on ? "true" : "false", cn.conn ? "true" : "false", peer,
		 cn.interval_ms, cn.update_ms, cn.dle ? "true" : "false",
		 cn.interval_units, cn.latency, cn.timeout_units,
		 cn.tx_octets, cn.rx_octets, cn.mtu, phy_name(cn.phy),
		 cn.payload_len, cn.period_ms, cn.writes, cn.write_errors,
		 cn.received, cn.updates, cn.connects, cn.disconnects,
		 cn.last_err);
	k_mutex_unlock(&lock);
}

/* --------------------------------------------------------------------- */
/* Shell                                                                 */

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

/* "C0:7D:CD:95:52:86" (most significant byte first) as a random address. */
static int parse_address(const char *text, bt_addr_le_t *out)
{
	if (strlen(text) != 17) {
		return -EINVAL;
	}
	out->type = BT_ADDR_LE_RANDOM;
	for (int byte = 0; byte < 6; byte++) {
		int high = hex_digit(text[3 * byte]);
		int low = hex_digit(text[3 * byte + 1]);

		if (high < 0 || low < 0) {
			return -EINVAL;
		}
		if (byte < 5 && text[3 * byte + 2] != ':') {
			return -EINVAL;
		}
		out->a.val[5 - byte] = (uint8_t)(high << 4 | low);
	}
	return 0;
}

static int require_role(const struct shell *sh)
{
	if (!role_is_connection()) {
		return utdk_err(sh, "role is %s; run 'role central' or 'role peripheral'",
				utdk_role_name(utdk_role()));
	}
	return 0;
}

/* A setting may only change while the role is idle, so the parameters a
 * capture shows are the ones that were asked for.
 */
static int require_idle(const struct shell *sh)
{
	int err = require_role(sh);

	if (err) {
		return err;
	}
	if (cn.on) {
		return utdk_err(sh, "stop the connection before changing settings");
	}
	return 0;
}

static int cmd_peer(const struct shell *sh, size_t argc, char **argv)
{
	bt_addr_le_t address;
	int err = require_idle(sh);

	ARG_UNUSED(argc);
	if (err) {
		return err;
	}
	if (parse_address(argv[1], &address) != 0) {
		return utdk_err(sh, "peer must look like C0:11:22:33:44:55");
	}
	k_mutex_lock(&lock, K_FOREVER);
	cn.peer = address;
	cn.have_peer = true;
	k_mutex_unlock(&lock);
	return utdk_ok(sh);
}

static int number(const char *text, unsigned long *out)
{
	char *end;

	if (text[0] < '0' || text[0] > '9') {
		return -EINVAL;
	}
	*out = strtoul(text, &end, 10);
	return *end == '\0' ? 0 : -EINVAL;
}

static int cmd_interval(const struct shell *sh, size_t argc, char **argv)
{
	unsigned long value;
	int err = require_idle(sh);

	ARG_UNUSED(argc);
	if (err) {
		return err;
	}
	if (number(argv[1], &value) != 0 || value < INTERVAL_MIN_MS ||
	    value > INTERVAL_MAX_MS) {
		return utdk_err(sh, "interval must be %d-%d ms", INTERVAL_MIN_MS,
				INTERVAL_MAX_MS);
	}
	k_mutex_lock(&lock, K_FOREVER);
	cn.interval_ms = value;
	k_mutex_unlock(&lock);
	return utdk_ok(sh);
}

static int cmd_dle(const struct shell *sh, size_t argc, char **argv)
{
	bool on;
	int err = require_idle(sh);

	ARG_UNUSED(argc);
	if (err) {
		return err;
	}
	if (strcmp(argv[1], "on") == 0) {
		on = true;
	} else if (strcmp(argv[1], "off") == 0) {
		on = false;
	} else {
		return utdk_err(sh, "dle must be on or off");
	}
	k_mutex_lock(&lock, K_FOREVER);
	cn.dle = on;
	k_mutex_unlock(&lock);
	return utdk_ok(sh);
}

static int cmd_payload(const struct shell *sh, size_t argc, char **argv)
{
	unsigned long value;
	int err = require_idle(sh);

	ARG_UNUSED(argc);
	if (err) {
		return err;
	}
	if (number(argv[1], &value) != 0 || value < 1 || value > PAYLOAD_MAX) {
		return utdk_err(sh, "payload must be 1-%d bytes", PAYLOAD_MAX);
	}
	k_mutex_lock(&lock, K_FOREVER);
	cn.payload_len = value;
	k_mutex_unlock(&lock);
	return utdk_ok(sh);
}

static int cmd_period(const struct shell *sh, size_t argc, char **argv)
{
	unsigned long value;
	int err = require_idle(sh);

	ARG_UNUSED(argc);
	if (err) {
		return err;
	}
	if (number(argv[1], &value) != 0 || value < PERIOD_MIN_MS ||
	    value > PERIOD_MAX_MS) {
		return utdk_err(sh, "period must be %d-%d ms", PERIOD_MIN_MS,
				PERIOD_MAX_MS);
	}
	k_mutex_lock(&lock, K_FOREVER);
	cn.period_ms = value;
	k_mutex_unlock(&lock);
	return utdk_ok(sh);
}

static int cmd_start(const struct shell *sh, size_t argc, char **argv)
{
	bool central;
	int err = require_role(sh);

	ARG_UNUSED(argc);
	ARG_UNUSED(argv);
	if (err) {
		return err;
	}
	central = utdk_role() == UTDK_ROLE_CENTRAL;
	k_sem_reset(&connected_sem);
	k_mutex_lock(&lock, K_FOREVER);
	if (cn.on) {
		k_mutex_unlock(&lock);
		return utdk_err(sh, "already started");
	}
	cn.on = true;
	cn.writes = 0;
	cn.write_errors = 0;
	cn.received = 0;
	cn.updates = 0;
	err = central ? start_central_locked() : start_peripheral_locked();
	if (err) {
		cn.on = false;
	}
	k_mutex_unlock(&lock);
	if (err == -EDESTADDRREQ) {
		return utdk_err(sh, "no peer address; run 'conn peer <address>' first");
	}
	if (err) {
		return utdk_err(sh, "%s start failed (%d)",
				central ? "central" : "peripheral", err);
	}
	if (!central) {
		/* The peripheral is advertising; the central connects when it
		 * is told to, so do not wait for a link here.
		 */
		return utdk_ok(sh);
	}
	if (k_sem_take(&connected_sem, K_MSEC(CONNECT_TIMEOUT_MS)) != 0 ||
	    cn.conn == NULL) {
		k_mutex_lock(&lock, K_FOREVER);
		stop_locked();
		k_mutex_unlock(&lock);
		return utdk_err(sh, "no connection within %d ms (is the peripheral "
				"advertising?)", CONNECT_TIMEOUT_MS);
	}
	return utdk_ok(sh);
}

static int cmd_stop(const struct shell *sh, size_t argc, char **argv)
{
	int err = require_role(sh);

	ARG_UNUSED(argc);
	ARG_UNUSED(argv);
	if (err) {
		return err;
	}
	(void)k_work_cancel_delayable(&write_work);
	k_mutex_lock(&lock, K_FOREVER);
	stop_locked();
	k_mutex_unlock(&lock);
	return utdk_ok(sh);
}

static int cmd_update(const struct shell *sh, size_t argc, char **argv)
{
	unsigned long value;
	struct bt_conn *conn;
	uint16_t units;
	int err = require_role(sh);

	ARG_UNUSED(argc);
	if (err) {
		return err;
	}
	if (number(argv[1], &value) != 0 || value < INTERVAL_MIN_MS ||
	    value > INTERVAL_MAX_MS) {
		return utdk_err(sh, "interval must be %d-%d ms", INTERVAL_MIN_MS,
				INTERVAL_MAX_MS);
	}
	units = value * UNITS_PER_MS;

	const struct bt_le_conn_param param =
		BT_LE_CONN_PARAM_INIT(units, units, 0, TIMEOUT_UNITS);

	k_mutex_lock(&lock, K_FOREVER);
	conn = cn.conn ? bt_conn_ref(cn.conn) : NULL;
	k_mutex_unlock(&lock);
	if (conn == NULL) {
		return utdk_err(sh, "no connection to update");
	}
	err = bt_conn_le_param_update(conn, &param);
	bt_conn_unref(conn);
	k_mutex_lock(&lock, K_FOREVER);
	cn.last_err = err;
	if (!err) {
		cn.update_ms = value;
	}
	k_mutex_unlock(&lock);
	if (err) {
		return utdk_err(sh, "parameter update failed (%d)", err);
	}
	return utdk_ok(sh);
}

static int cmd_defaults(const struct shell *sh, size_t argc, char **argv)
{
	int err = require_role(sh);

	ARG_UNUSED(argc);
	ARG_UNUSED(argv);
	if (err) {
		return err;
	}
	conn_leave();
	return utdk_ok(sh);
}

SHELL_STATIC_SUBCMD_SET_CREATE(conn_cmds,
	SHELL_CMD_ARG(peer, NULL, "<address> The peripheral the central connects to",
		      cmd_peer, 2, 0),
	SHELL_CMD_ARG(interval, NULL, "<ms> Connection interval, 8-4000", cmd_interval,
		      2, 0),
	SHELL_CMD_ARG(dle, NULL, "<on|off> Ask for 251-byte data length", cmd_dle, 2, 0),
	SHELL_CMD_ARG(payload, NULL, "<bytes> Bytes per write", cmd_payload, 2, 0),
	SHELL_CMD_ARG(period, NULL, "<ms> Time between writes, 10-10000", cmd_period,
		      2, 0),
	SHELL_CMD_ARG(start, NULL, "Connect (central) or advertise (peripheral)",
		      cmd_start, 1, 0),
	SHELL_CMD_ARG(stop, NULL, "Disconnect and stop", cmd_stop, 1, 0),
	SHELL_CMD_ARG(update, NULL, "<ms> Ask for a connection parameter update",
		      cmd_update, 2, 0),
	SHELL_CMD_ARG(defaults, NULL, "Stop and restore the boot settings", cmd_defaults,
		      1, 0),
	SHELL_SUBCMD_SET_END);

/* `conn` alone or with an unknown subcommand: an ERR line, not the help. */
static int cmd_conn(const struct shell *sh, size_t argc, char **argv)
{
	if (argc < 2) {
		return utdk_err(sh, "usage: conn <peer|interval|dle|payload|period|start|"
				"stop|update|defaults>");
	}
	return utdk_err(sh, "unknown conn command '%s'", argv[1]);
}

SHELL_CMD_ARG_REGISTER(conn, &conn_cmds, "Central and peripheral control", cmd_conn, 1, 1);
