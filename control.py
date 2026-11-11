"""Keyboard and mouse input on Wayland, through the desktop's RemoteDesktop portal (no root needed).

The first use shows GNOME's "allow remote control" dialog once; the permission is then remembered
(restore token in TOKEN_FILE). The portal session lives as long as this process's D-Bus connection.
"""
import os
import secrets
import time

from jeepney import DBusAddress, MatchRule, new_method_call
from jeepney.bus_messages import message_bus
from jeepney.io.blocking import open_dbus_connection
from jeepney.wrappers import DBusErrorResponse, unwrap_msg

PORTAL = DBusAddress("/org/freedesktop/portal/desktop", bus_name="org.freedesktop.portal.Desktop",
                     interface="org.freedesktop.portal.RemoteDesktop")
TOKEN_FILE = os.path.expanduser("~/.config/needle/input-token")
BTN_LEFT, BTN_RIGHT = 272, 273
_conn = _session = None


class NoPermission(Exception):
    """The user didn't allow control in the dialog (or it timed out)."""


def _request(method, signature, args, options, timeout):
    """Call a portal method that answers through a Request object; return its results."""
    token = "needle" + secrets.token_hex(4)
    sender = _conn.unique_name[1:].replace(".", "_")
    rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request", member="Response",
                     path=f"/org/freedesktop/portal/desktop/request/{sender}/{token}")
    unwrap_msg(_conn.send_and_get_reply(message_bus.AddMatch(rule), timeout=5))
    options = dict(options, handle_token=("s", token))
    with _conn.filter(rule) as queue:
        unwrap_msg(_conn.send_and_get_reply(new_method_call(PORTAL, method, signature, args + (options,)),
                                            timeout=5))
        code, results = _conn.recv_until_filtered(queue, timeout=timeout).body
    if code != 0:
        raise NoPermission()
    return results


def connect():
    """Open the control session. Shows the permission dialog the first time (waits up to 2 minutes for the
    click). With a saved permission it should be instant, so then it only waits 20 s: a hands-free user can't
    click a dialog, and the voice loop must not be stuck behind one."""
    global _conn, _session
    if _session:
        return
    _conn = open_dbus_connection(bus="SESSION")
    try:
        session = _request("CreateSession", "a{sv}", (), {"session_handle_token": ("s", "needle")}, 10)
        session = session["session_handle"][1]
        select = {"types": ("u", 3), "persist_mode": ("u", 2)}       # keyboard + pointer, remember permission
        saved = os.path.exists(TOKEN_FILE)
        if saved:
            with open(TOKEN_FILE) as f:
                select["restore_token"] = ("s", f.read().strip())
        _request("SelectDevices", "oa{sv}", (session,), select, 10)
        results = _request("Start", "osa{sv}", (session, ""), {}, 20 if saved else 120)
    except (NoPermission, TimeoutError, DBusErrorResponse, OSError, KeyError):
        _conn.close()
        _conn = None
        raise NoPermission() from None
    _session = session
    if "restore_token" in results:
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        with open(TOKEN_FILE, "w") as f:
            f.write(results["restore_token"][1])


def _notify(method, signature, *args):
    global _conn, _session
    for attempt in (1, 2):
        connect()
        try:
            unwrap_msg(_conn.send_and_get_reply(
                new_method_call(PORTAL, method, "oa{sv}" + signature, (_session, {}) + args), timeout=5))
            return
        except (DBusErrorResponse, OSError, TimeoutError):           # session ended (e.g. screen lock): reopen
            _conn.close()
            _conn = _session = None
            if attempt == 2:
                raise


def tap(keysyms):
    """Press the keys in order and release them in reverse: [Control, c] is Ctrl+C."""
    for k in keysyms:
        _notify("NotifyKeyboardKeysym", "iu", k, 1)
    for k in reversed(keysyms):
        _notify("NotifyKeyboardKeysym", "iu", k, 0)


def type_text(text):
    for ch in text:
        tap([{"\n": 0xFF0D, "\t": 0xFF09}.get(ch) or (ord(ch) if ord(ch) < 0x100 else 0x01000000 | ord(ch))])


def click(button=BTN_LEFT, times=1):
    for _ in range(times):
        _notify("NotifyPointerButton", "iu", button, 1)
        _notify("NotifyPointerButton", "iu", button, 0)
        time.sleep(0.08)


def move(dx, dy):
    _notify("NotifyPointerMotion", "dd", float(dx), float(dy))


def scroll(steps):
    """Positive scrolls down."""
    _notify("NotifyPointerAxisDiscrete", "ui", 0, int(steps))
