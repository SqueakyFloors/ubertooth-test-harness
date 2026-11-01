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
/* Once that is done, how long before the automatic parameter update. */
#define AUTOUPDATE_DELAY K_MSEC(1200)
/* How long the central waits before connecting again, when it cycles. */
#define RECONNECT_GAP K_MSEC(300)
/* How long a connection lasts when the central cycles, in ms. */
#define CYCLE_MIN_MS 1000
#define CYCLE_MAX_MS 600000
/* The connection's data channel map: 37 channels, one bit each, in the five
 * bytes a CONNECT_IND and an LL_CHANNEL_MAP_IND carry (bit b of byte i is
 * channel 8i + b).
 */
#define DATA_CHANNELS 37
#define DATA_MAP_BYTES 5
/* A connection's channel map has to leave at least two channels used
 * (Bluetooth Core, HCI LE Set Host Channel Classification: at least two
 * channels shall be marked unknown). The controller here refuses a
 * one-channel map with -EINVAL, measured on the bench, so the firmware
 * refuses it first and says why.
 */
#define DATA_CHANNELS_MIN 2
/* Two channel classifications must be at least a second apart (Bluetooth
 * Core, HCI LE Set Host Channel Classification), so a second one sooner is
 * refused rather than quietly dropped by the controller.
 */
#define DATA_MAP_GAP_MS 1000
/* Leaving the role waits for the link to come down: one connection event at
 * the longest interval the shell accepts, plus the terminate exchange.
 */
#define LEAVE_WAIT_STEP K_MSEC(100)
#define LEAVE_WAIT_STEPS 60

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
	/* The central drops the link after this long and connects again, so a
	 * capture started at any time sees a whole connection: CONNECT_IND,
	 * the data length exchange, the parameter update and the data. 0 keeps
	 * one connection up.
	 */
	uint32_t cycle_ms;
	/* The interval the central asks for once connected (0 = no update). */
	uint32_t autoupdate_ms;
	/* Advertising channels the peripheral uses. The central connects on
	 * the channel it hears the peripheral on, so one channel puts the
	 * CONNECT_IND where a receiver is listening.
	 */
	uint8_t chmap;
	/* The data channel map the central has the controller classify, which
	 * the controller carries to the live link as an LL_CHANNEL_MAP_IND.
	 * All 37 channels until a test narrows it.
	 */
	uint8_t data_map[DATA_MAP_BYTES];
	uint32_t data_map_sets;  /* times it was set since boot */
	int64_t data_map_at;     /* uptime of the last set, in ms */
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
	.chmap = UTDK_CHAN_37,
	.data_map = { 0xFF, 0xFF, 0xFF, 0xFF, 0x1F },
};

static uint8_t payload[PAYLOAD_MAX];

static void setup(struct k_work *work);
static void write_tick(struct k_work *work);
static void cycle_tick(struct k_work *work);
static void reconnect(struct k_work *work);
static void autoupdate(struct k_work *work);
static void readvertise(struct k_work *work);
static K_WORK_DEFINE(setup_work, setup);
static K_WORK_DELAYABLE_DEFINE(write_work, write_tick);
static K_WORK_DELAYABLE_DEFINE(cycle_work, cycle_tick);
static K_WORK_DELAYABLE_DEFINE(reconnect_work, reconnect);
static K_WORK_DELAYABLE_DEFINE(autoupdate_work, autoupdate);
static K_WORK_DEFINE(readvertise_work, readvertise);

static bool role_is_connection(void)
{
	return utdk_role() == UTDK_ROLE_CENTRAL || utdk_role() == UTDK_ROLE_PERIPHERAL;
}

/* --------------------------------------------------------------------- */
/* The connection's data channel map                                     */

static void data_map_all(uint8_t *map)
{
	map[0] = map[1] = map[2] = map[3] = 0xFF;
	map[4] = 0x1F; /* channels 32-36 */
}

static uint8_t data_map_used(const uint8_t *map)
{
	uint8_t used = 0;

	for (int channel = 0; channel < DATA_CHANNELS; channel++) {
		if (map[channel / 8] & BIT(channel % 8)) {
			used++;
		}
	}
	return used;
}

/* "all", or a list of data channels such as "0,12,36", into a channel map.
 * A map that leaves fewer than DATA_CHANNELS_MIN channels used is refused.
 */
static int parse_data_map(const char *text, uint8_t *map)
{
	const char *at = text;

	memset(map, 0, DATA_MAP_BYTES);
	if (strcmp(text, "all") == 0) {
		data_map_all(map);
		return 0;
	}
	while (*at != '\0') {
		unsigned int channel = 0;
		int digits = 0;

		while (*at >= '0' && *at <= '9') {
			channel = channel * 10 + (unsigned int)(*at++ - '0');
			if (++digits > 2) {
				return -EINVAL;
			}
		}
		if (digits == 0 || channel >= DATA_CHANNELS) {
			return -EINVAL;
		}
		map[channel / 8] |= BIT(channel % 8);
		if (*at == '\0') {
			break;
		}
		if (*at != ',' || *++at == '\0') {
			return -EINVAL;
		}
	}
	return data_map_used(map) >= DATA_CHANNELS_MIN ? 0 : -EINVAL;
}

/* Put all 37 channels back, waiting out the gap the controller needs.
 *
 * The classification belongs to the controller, not to the link: it outlives
 * the connection that carried it, and a narrowed map left behind would
 * narrow the next test's CONNECT_IND.
 */
static void restore_data_map(void)
{
	uint8_t all[DATA_MAP_BYTES];
	int64_t since;
	bool narrowed;
	int err;

	k_mutex_lock(&lock, K_FOREVER);
	narrowed = data_map_used(cn.data_map) != DATA_CHANNELS;
	since = k_uptime_get() - cn.data_map_at;
	k_mutex_unlock(&lock);
	if (!narrowed) {
		return;
	}
	if (since < DATA_MAP_GAP_MS) {
		k_sleep(K_MSEC(DATA_MAP_GAP_MS - since));
	}
	data_map_all(all);
	err = bt_le_set_chan_map(all);
	k_mutex_lock(&lock, K_FOREVER);
	cn.last_err = err;
	/* Only when the controller took it: a status saying all 37 channels
	 * while the controller still holds the narrow map would hide the
	 * narrow map from the next test.
	 */
	if (!err) {
		data_map_all(cn.data_map);
	}
	cn.data_map_at = k_uptime_get();
	k_mutex_unlock(&lock);
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

/* The central's connection has lasted long enough: drop it, and the
 * disconnect schedules the next one.
 */
static void cycle_tick(struct k_work *work)
{
	struct bt_conn *conn;

	ARG_UNUSED(work);
	k_mutex_lock(&lock, K_FOREVER);
	conn = cn.conn ? bt_conn_ref(cn.conn) : NULL;
	k_mutex_unlock(&lock);
	if (conn != NULL) {
		(void)bt_conn_disconnect(conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
		bt_conn_unref(conn);
	}
}

static int start_central_locked(void);

static void reconnect(struct k_work *work)
{
	ARG_UNUSED(work);
	k_mutex_lock(&lock, K_FOREVER);
	if (cn.on && cn.conn == NULL && utdk_role() == UTDK_ROLE_CENTRAL) {
		(void)start_central_locked();
	}
	k_mutex_unlock(&lock);
}

static void autoupdate(struct k_work *work)
{
	struct bt_conn *conn;
	uint16_t units;
	int err;

	ARG_UNUSED(work);
	k_mutex_lock(&lock, K_FOREVER);
	conn = (cn.autoupdate_ms && cn.conn) ? bt_conn_ref(cn.conn) : NULL;
	units = cn.autoupdate_ms * UNITS_PER_MS;
	k_mutex_unlock(&lock);
	if (conn == NULL) {
		return;
	}

	const struct bt_le_conn_param param =
		BT_LE_CONN_PARAM_INIT(units, units, 0, TIMEOUT_UNITS);

	err = bt_conn_le_param_update(conn, &param);
	bt_conn_unref(conn);
	k_mutex_lock(&lock, K_FOREVER);
	cn.last_err = err;
	if (!err) {
		cn.update_ms = cn.autoupdate_ms;
	}
	k_mutex_unlock(&lock);
}

static int start_peripheral_locked(void);

/* The peripheral advertises again after a connection ends, so the central
 * can reconnect without the harness touching it.
 */
static void readvertise(struct k_work *work)
{
	ARG_UNUSED(work);
	k_mutex_lock(&lock, K_FOREVER);
	if (cn.on && cn.conn == NULL && utdk_role() == UTDK_ROLE_PERIPHERAL) {
		(void)start_peripheral_locked();
	}
	k_mutex_unlock(&lock);
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
	if (cn.cycle_ms) {
		k_work_reschedule(&cycle_work, K_MSEC(cn.cycle_ms));
	}
	if (cn.autoupdate_ms && utdk_role() == UTDK_ROLE_CENTRAL) {
		k_work_reschedule(&autoupdate_work, AUTOUPDATE_DELAY);
	}
	k_mutex_unlock(&lock);
	k_work_submit(&setup_work);
	k_sem_give(&connected_sem);
}

/*
 * Always release the link, whatever role is active now: a `role` command can
 * switch away while the disconnect is still in flight, and skipping this
 * would leak the reference and leave `status` reporting a connection that
 * has gone, with no way back except a reset.
 */
static void disconnected(struct bt_conn *conn, uint8_t reason)
{
	(void)k_work_cancel_delayable(&write_work);
	(void)k_work_cancel_delayable(&cycle_work);
	(void)k_work_cancel_delayable(&autoupdate_work);
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

/* The connection object is free again: the central connects once more, the
 * peripheral advertises again.
 */
static void recycled(void)
{
	bool on, cycling;

	if (!role_is_connection()) {
		return;
	}
	k_mutex_lock(&lock, K_FOREVER);
	on = cn.on;
	cycling = cn.cycle_ms != 0;
	k_mutex_unlock(&lock);
	if (!on) {
		return;
	}
	if (utdk_role() == UTDK_ROLE_CENTRAL) {
		if (cycling) {
			k_work_reschedule(&reconnect_work, RECONNECT_GAP);
		}
	} else {
		k_work_submit(&readvertise_work);
	}
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
	.recycled = recycled,
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
	size_t len = utdk_mfg_payload(mfg, cn.chmap, 0, NULL, 0);
	uint32_t options = BT_LE_ADV_OPT_CONN | BT_LE_ADV_OPT_USE_IDENTITY;

	if (!(cn.chmap & UTDK_CHAN_37)) {
		options |= BT_LE_ADV_OPT_DISABLE_CHAN_37;
	}
	if (!(cn.chmap & UTDK_CHAN_38)) {
		options |= BT_LE_ADV_OPT_DISABLE_CHAN_38;
	}
	if (!(cn.chmap & UTDK_CHAN_39)) {
		options |= BT_LE_ADV_OPT_DISABLE_CHAN_39;
	}

	const struct bt_le_adv_param param = BT_LE_ADV_PARAM_INIT(
		options, BT_GAP_ADV_FAST_INT_MIN_2, BT_GAP_ADV_FAST_INT_MAX_2, NULL);

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
	/* Clear the schedules first, so nothing starts the role again. */
	(void)k_work_cancel_delayable(&cycle_work);
	(void)k_work_cancel_delayable(&reconnect_work);
	(void)k_work_cancel_delayable(&autoupdate_work);
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
	bool waiting;

	(void)k_work_cancel_delayable(&write_work);
	k_mutex_lock(&lock, K_FOREVER);
	stop_locked();
	cn.interval_ms = INTERVAL_DEFAULT_MS;
	cn.update_ms = 0;
	cn.dle = true;
	cn.payload_len = PAYLOAD_DEFAULT;
	cn.period_ms = PERIOD_DEFAULT_MS;
	cn.cycle_ms = 0;
	cn.autoupdate_ms = 0;
	cn.chmap = UTDK_CHAN_37;
	/* cn.data_map is put back by restore_data_map() below, which has to
	 * reach the controller and cannot run with the lock held.
	 */
	/* Forget the peer too: leaving the role must not leave a `conn start`
	 * able to open a link to whatever the last test connected to.
	 */
	cn.have_peer = false;
	memset(&cn.peer, 0, sizeof(cn.peer));
	waiting = cn.conn != NULL;
	k_mutex_unlock(&lock);
	/* Wait for the disconnect to complete, so the next role starts from no
	 * link rather than racing the teardown. One connection event plus the
	 * terminate exchange, at the longest interval the shell accepts.
	 */
	for (int i = 0; waiting && i < LEAVE_WAIT_STEPS; i++) {
		k_sleep(LEAVE_WAIT_STEP);
		k_mutex_lock(&lock, K_FOREVER);
		waiting = cn.conn != NULL;
		k_mutex_unlock(&lock);
	}
	restore_data_map();
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

static const char *conn_chmap_name(uint8_t chmap)
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

void conn_status(char *buf, size_t len)
{
	char peer[BT_ADDR_LE_STR_LEN] = "";
	char data_map[2 * DATA_MAP_BYTES + 1] = "";

	k_mutex_lock(&lock, K_FOREVER);
	for (int byte = 0; byte < DATA_MAP_BYTES; byte++) {
		snprintf(&data_map[2 * byte], 3, "%02x", cn.data_map[byte]);
	}
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
		 "\"payload_len\":%u,\"period_ms\":%u,\"conn_chmap\":\"%s\","
		 "\"data_map\":\"%s\",\"data_map_used\":%u,\"data_map_sets\":%u,"
		 "\"cycle_ms\":%u,"
		 "\"autoupdate_ms\":%u,\"writes\":%u,"
		 "\"write_errors\":%u,\"received\":%u,\"updates\":%u,"
		 "\"connects\":%u,\"disconnects\":%u,\"conn_err\":%d",
		 cn.on ? "true" : "false", cn.conn ? "true" : "false", peer,
		 cn.interval_ms, cn.update_ms, cn.dle ? "true" : "false",
		 cn.interval_units, cn.latency, cn.timeout_units,
		 cn.tx_octets, cn.rx_octets, cn.mtu, phy_name(cn.phy),
		 cn.payload_len, cn.period_ms, conn_chmap_name(cn.chmap),
		 data_map, data_map_used(cn.data_map), cn.data_map_sets,
		 cn.cycle_ms, cn.autoupdate_ms,
		 cn.writes, cn.write_errors,
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

static int cmd_chmap(const struct shell *sh, size_t argc, char **argv)
{
	uint8_t chmap;
	int err = require_idle(sh);

	ARG_UNUSED(argc);
	if (err) {
		return err;
	}
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
	k_mutex_lock(&lock, K_FOREVER);
	cn.chmap = chmap;
	k_mutex_unlock(&lock);
	return utdk_ok(sh);
}

static int cmd_cycle(const struct shell *sh, size_t argc, char **argv)
{
	unsigned long value = 0;
	int err = require_idle(sh);

	ARG_UNUSED(argc);
	if (err) {
		return err;
	}
	if (strcmp(argv[1], "off") != 0 &&
	    (number(argv[1], &value) != 0 || value < CYCLE_MIN_MS ||
	     value > CYCLE_MAX_MS)) {
		return utdk_err(sh, "cycle must be off or %d-%d ms", CYCLE_MIN_MS,
				CYCLE_MAX_MS);
	}
	k_mutex_lock(&lock, K_FOREVER);
	cn.cycle_ms = value;
	k_mutex_unlock(&lock);
	return utdk_ok(sh);
}

static int cmd_autoupdate(const struct shell *sh, size_t argc, char **argv)
{
	unsigned long value = 0;
	int err = require_idle(sh);

	ARG_UNUSED(argc);
	if (err) {
		return err;
	}
	if (strcmp(argv[1], "off") != 0 &&
	    (number(argv[1], &value) != 0 || value < INTERVAL_MIN_MS ||
	     value > INTERVAL_MAX_MS)) {
		return utdk_err(sh, "autoupdate must be off or an interval of %d-%d ms",
				INTERVAL_MIN_MS, INTERVAL_MAX_MS);
	}
	k_mutex_lock(&lock, K_FOREVER);
	cn.autoupdate_ms = value;
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

/* Change the live connection's data channel map.
 *
 * The controller holds one channel classification and carries it to every
 * link where it is the central, as an LL_CHANNEL_MAP_IND, so this changes
 * the map of the connection that is up without disturbing it. Two channels
 * (DATA_CHANNELS_MIN) is the smallest map allowed, and the most demanding
 * one: the remapping divides by the number of channels in use.
 */
static int cmd_datamap(const struct shell *sh, size_t argc, char **argv)
{
	uint8_t map[DATA_MAP_BYTES];
	int64_t since;
	uint32_t sets;
	bool connected;
	int err = require_role(sh);

	ARG_UNUSED(argc);
	if (err) {
		return err;
	}
	if (utdk_role() != UTDK_ROLE_CENTRAL) {
		return utdk_err(sh, "only the central can change the connection's "
				"channel map");
	}
	if (parse_data_map(argv[1], map) != 0) {
		return utdk_err(sh, "channel map must be 'all' or a list of at "
				"least %d data channels 0-%d, for example 0,12,36",
				DATA_CHANNELS_MIN, DATA_CHANNELS - 1);
	}
	k_mutex_lock(&lock, K_FOREVER);
	connected = cn.conn != NULL;
	sets = cn.data_map_sets;
	since = k_uptime_get() - cn.data_map_at;
	k_mutex_unlock(&lock);
	if (!connected) {
		return utdk_err(sh, "no connection to update");
	}
	if (sets > 0 && since < DATA_MAP_GAP_MS) {
		return utdk_err(sh, "channel map updates must be %d ms apart; the "
				"last one was %lld ms ago", DATA_MAP_GAP_MS, since);
	}
	err = bt_le_set_chan_map(map);
	k_mutex_lock(&lock, K_FOREVER);
	cn.last_err = err;
	if (!err) {
		memcpy(cn.data_map, map, sizeof(map));
		cn.data_map_sets++;
		cn.data_map_at = k_uptime_get();
	}
	k_mutex_unlock(&lock);
	if (err) {
		return utdk_err(sh, "channel map update failed (%d)", err);
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
	SHELL_CMD_ARG(chmap, NULL, "<37|38|39|all> The peripheral's advertising channels",
		      cmd_chmap, 2, 0),
	SHELL_CMD_ARG(datamap, NULL,
		      "<all|0,12,36> Change the live connection's data channel map",
		      cmd_datamap, 2, 0),
	SHELL_CMD_ARG(cycle, NULL, "<ms|off> Drop and remake the link this often",
		      cmd_cycle, 2, 0),
	SHELL_CMD_ARG(autoupdate, NULL, "<ms|off> Interval to ask for once connected",
		      cmd_autoupdate, 2, 0),
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
		return utdk_err(sh, "usage: conn <peer|interval|dle|payload|period|chmap|"
				"datamap|cycle|autoupdate|start|stop|update|defaults>");
	}
	return utdk_err(sh, "unknown conn command '%s'", argv[1]);
}

SHELL_CMD_ARG_REGISTER(conn, &conn_cmds, "Central and peripheral control", cmd_conn, 1, 1);
