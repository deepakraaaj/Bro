"""The "Bro" button in the top bar (next to CopyQ). Left click: listen for one command. Right click: menu with
"Listen now" and the hands-free switch (always listening for "Hey Jarvis"). Hands-free is off by default, so
the microphone stays closed until you ask.

It speaks the StatusNotifierItem protocol (what CopyQ's icon uses) directly over D-Bus with jeepney, so no
extra system package is needed. start() runs it in a background thread inside the voice service.
"""
import os
import threading

from jeepney import (DBusAddress, HeaderFields, MatchRule, MessageType, new_error, new_method_call,
                     new_method_return, new_signal)
from jeepney.io.blocking import open_dbus_connection
from jeepney.bus_messages import message_bus

SETTING = os.path.expanduser("~/.config/needle/handsfree")   # exists = hands-free on (remembered across restarts)
ITEM_PATH, MENU_PATH = "/StatusNotifierItem", "/MenuBar"
ITEM_IFACE, MENU_IFACE = "org.kde.StatusNotifierItem", "com.canonical.dbusmenu"
LOOK = {  # status -> (icon, text next to it)
    "off": ("microphone-disabled-symbolic", "Bro"),
    "ready": ("audio-input-microphone-symbolic", "Bro"),
    "listening": ("microphone-sensitivity-high-symbolic", "Listening…"),
    "thinking": ("audio-input-microphone-symbolic", "Thinking…"),
}

clicked = threading.Event()          # set when the user asks to listen now
_state = {"status": "off", "handsfree": os.path.exists(SETTING), "revision": 1}
_lock = threading.Lock()
_changed = threading.Event()
_calls_logged = [0]


def handsfree():
    return _state["handsfree"]


def set_status(status):
    """off (mic closed), ready (hands-free, waiting for "Hey Jarvis"), listening, thinking."""
    with _lock:
        if _state["status"] != status:
            _state["status"] = status
            _changed.set()


def _set_handsfree(on):
    with _lock:
        _state["handsfree"] = on
        _state["revision"] += 1
    if on:
        os.makedirs(os.path.dirname(SETTING), exist_ok=True)
        open(SETTING, "w").close()
    elif os.path.exists(SETTING):
        os.remove(SETTING)
    _changed.set()


def _item_props():
    icon, label = LOOK.get(_state["status"], LOOK["off"])
    hint = "hands-free: say \"Hey Jarvis\"" if _state["handsfree"] else "click to talk, mic is off"
    return {
        "Category": ("s", "ApplicationStatus"), "Id": ("s", "needle"), "Title": ("s", "Bro"),
        "Status": ("s", "Active"), "WindowId": ("i", 0), "IconName": ("s", icon), "IconThemePath": ("s", ""),
        "AttentionIconName": ("s", ""), "OverlayIconName": ("s", ""), "Menu": ("o", MENU_PATH),
        "ItemIsMenu": ("b", False), "XAyatanaLabel": ("s", label), "XAyatanaLabelGuide": ("s", "Listening…"),
        "ToolTip": ("(sa(iiay)ss)", (icon, [], "Bro", hint)),
    }


def _menu_layout():
    items = [
        (1, {"label": ("s", "Listen now")}),
        (2, {"label": ("s", "Hands-free (always listen for “Hey Jarvis”)"),
             "toggle-type": ("s", "checkmark"), "toggle-state": ("i", int(_state["handsfree"]))}),
    ]
    children = [("(ia{sv}av)", (i, props, [])) for i, props in items]
    return _state["revision"], (0, {"children-display": ("s", "submenu")}, children)


def _menu_item_props(ids):
    _, (_, _, children) = _menu_layout()
    return [(i, props) for _, (i, props, _) in children if not ids or i in ids]


INTROSPECT = """<node>
 <interface name="org.kde.StatusNotifierItem">
  <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
  <signal name="NewIcon"/><signal name="NewTitle"/><signal name="NewToolTip"/>
  <signal name="NewStatus"><arg type="s"/></signal>
  <signal name="XAyatanaNewLabel"><arg type="s"/><arg type="s"/></signal>
 </interface>
 <interface name="com.canonical.dbusmenu">
  <method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
   <arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/></method>
  <method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/>
   <arg type="a(ia{sv})" direction="out"/></method>
  <method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/>
   <arg type="u" direction="in"/></method>
  <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
  <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
 </interface>
</node>"""


def _handle(conn, msg):
    """Answer one D-Bus method call from the top bar."""
    path, iface = msg.header.fields.get(HeaderFields.path), msg.header.fields.get(HeaderFields.interface)
    member, body = msg.header.fields.get(HeaderFields.member), msg.body

    def reply(signature=None, *values):
        conn.send(new_method_return(msg, signature, values) if signature else new_method_return(msg))

    if member == "Introspect":
        return reply("s", INTROSPECT)
    if iface == "org.freedesktop.DBus.Properties":
        props = _item_props() if path == ITEM_PATH else {
            "Version": ("u", 3), "TextDirection": ("s", "ltr"), "Status": ("s", "normal"),
            "IconThemePath": ("as", [])}
        if member == "GetAll":
            return reply("a{sv}", props)
        if member == "Get" and body[1] in props:
            return reply("v", props[body[1]])
        return conn.send(new_error(msg, "org.freedesktop.DBus.Error.UnknownProperty"))
    if path == ITEM_PATH:
        if member in ("Activate", "SecondaryActivate"):      # left or middle click: listen now
            clicked.set()
        return reply()
    if path == MENU_PATH:
        if member == "GetLayout":
            return reply("u(ia{sv}av)", *_menu_layout())
        if member == "GetGroupProperties":
            return reply("a(ia{sv})", _menu_item_props(body[0]))
        if member == "GetProperty":
            props = dict(_menu_item_props([body[0]])).get(body[0], {})
            return reply("v", props.get(body[1], ("s", "")))
        if member in ("Event", "EventGroup"):
            events = [body[:2]] if member == "Event" else [(e[0], e[1]) for e in body[0]]
            for item, kind in events:
                if kind == "clicked" and item == 1:
                    clicked.set()
                elif kind == "clicked" and item == 2:
                    _set_handsfree(not _state["handsfree"])
            return reply() if member == "Event" else reply("ai", [])
        if member == "AboutToShow":
            return reply("b", False)
        if member == "AboutToShowGroup":
            return reply("aiai", [], [])
    conn.send(new_error(msg, "org.freedesktop.DBus.Error.UnknownMethod"))


def _announce(conn):
    """Tell the top bar the icon, label or menu changed."""
    item = DBusAddress(ITEM_PATH, interface=ITEM_IFACE)
    icon, label = LOOK.get(_state["status"], LOOK["off"])
    conn.send(new_signal(item, "NewIcon"))
    conn.send(new_signal(item, "NewToolTip"))
    conn.send(new_signal(item, "XAyatanaNewLabel", "ss", (label, "Listening…")))
    conn.send(new_signal(DBusAddress(MENU_PATH, interface=MENU_IFACE), "LayoutUpdated", "ui",
                         (_state["revision"], 0)))


def _serve():
    conn = open_dbus_connection(bus="SESSION")
    name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
    watcher = DBusAddress("/StatusNotifierWatcher", bus_name="org.kde.StatusNotifierWatcher",
                          interface="org.kde.StatusNotifierWatcher")
    # Only send here, never wait for a reply: jeepney drops other messages while it waits, and the top bar
    # asks for the icon's properties right after it is registered. Every reply is read in the loop below.
    conn.send(message_bus.RequestName(name))
    owner_changed = MatchRule(type="signal", interface="org.freedesktop.DBus", member="NameOwnerChanged")
    owner_changed.add_arg_condition(0, "org.kde.StatusNotifierWatcher")
    conn.send(message_bus.AddMatch(owner_changed))
    conn.send(new_method_call(watcher, "RegisterStatusNotifierItem", "s", (name,)))
    while True:
        try:
            msg = conn.receive(timeout=0.2)
        except TimeoutError:
            msg = None
        if (msg is not None and msg.header.message_type == MessageType.signal
                and msg.header.fields.get(HeaderFields.member) == "NameOwnerChanged" and msg.body[2]):
            conn.send(new_method_call(watcher, "RegisterStatusNotifierItem", "s", (name,)))   # top bar restarted
        if msg is not None and msg.header.message_type == MessageType.method_call:
            if _calls_logged[0] < 12:            # show in the journal that the top bar is talking to us
                _calls_logged[0] += 1
                print(f"Top-bar button: {msg.header.fields.get(HeaderFields.member)} "
                      f"{msg.header.fields.get(HeaderFields.path)}", flush=True)
            try:
                _handle(conn, msg)
            except Exception:                    # a malformed request must not kill the button
                conn.send(new_error(msg, "org.freedesktop.DBus.Error.Failed"))
        if _changed.is_set():
            _changed.clear()
            _announce(conn)


def start():
    """Show the button. Returns at once; the button lives in a daemon thread. Restarts it if it fails
    (e.g. GNOME Shell restarted), so the user never loses the only way to talk."""
    def run():
        while True:
            try:
                _serve()
            except Exception as error:
                print(f"Top-bar button failed ({type(error).__name__}), retrying in 5 s", flush=True)
                threading.Event().wait(5)
    threading.Thread(target=run, daemon=True, name="tray").start()
