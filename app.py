#!/usr/bin/env python3
"""GameStream — Desktop launcher GUI."""

import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox

# When frozen (PyInstaller), __file__ is inside the _MEI temp dir.
# Use the exe's own directory as the working dir instead.
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
    _MEIPASS  = sys._MEIPASS          # extracted bundle (scripts, .pyd, .dll)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    _MEIPASS  = None
sys.path.insert(0, BASE_DIR)

from shared.pairing import KnownHosts


# ── Persistent config ────────────────────────────────────────────────

_CONFIG_PATH = os.path.join(BASE_DIR, "gamestream_config.json")

_CONFIG_DEFAULTS = {
    # Host
    "host_fps": "60",
    "host_bitrate": "8",
    "host_monitor": "0",
    "host_audio": True,
    "host_sw_encode": False,
    "host_mode": "lan",
    "host_relay_local": True,
    "host_relay_addr": "",
    "host_relay_port": "9950",
    # Client
    "client_mode": "lan",
    "client_relay_addr": "",
    "client_room": "",
    "client_fullscreen": False,
    "client_grab": False,
    "client_no_audio": False,
    # Mobile
    "mobile_fps": "30",
    "mobile_port": "8080",
    "mobile_monitor": "0",
    "mobile_use_relay": False,
    "mobile_relay_addr": "",
}


def _load_config() -> dict:
    cfg = dict(_CONFIG_DEFAULTS)
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        cfg.update(saved)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return cfg


def _save_config(cfg: dict):
    try:
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


def _build_script_cmd(script: str, *extra_args: str) -> list:
    """
    Build the command list to run a bundled script as a subprocess.

    Frozen (PyInstaller onefile):
      Re-invoke the exe itself with  --_subprocess <module>  so the
      correct Python runtime (the one embedded in the exe) is used.
      This avoids the "python312.dll conflicts" error that occurs when
      a system Python 3.11 tries to load .pyd files compiled for 3.12.

    Normal (plain python):
      Just invoke sys.executable with -u and the script path.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--_subprocess", script] + list(extra_args)
    script_path = os.path.join(BASE_DIR, script.replace("/", os.sep))
    return [sys.executable, "-u", script_path] + list(extra_args)


PYTHON = sys.executable   # kept for reference; use _build_script_cmd instead

# ── Theme ─────────────────────────────────────────────────────────────

BG       = "#0d1117"
SURFACE  = "#161b22"
CARD     = "#21262d"
BORDER   = "#30363d"
ACCENT   = "#58a6ff"
GREEN    = "#3fb950"
YELLOW   = "#d29922"
RED      = "#f85149"
TEXT     = "#c9d1d9"
DIM      = "#8b949e"
DARK     = "#484f58"
WHITE    = "#f0f6fc"

FONT      = ("Segoe UI", 11)
FONT_SM   = ("Segoe UI", 9)
FONT_B    = ("Segoe UI", 11, "bold")
FONT_LG   = ("Segoe UI", 16, "bold")
FONT_XL   = ("Segoe UI", 24, "bold")
FONT_MONO = ("Consolas", 10)


def _local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ── Widget factories ──────────────────────────────────────────────────

def _btn(parent, text, cmd, accent=False, danger=False, w=20, bg_=None):
    if bg_:
        bg, fg, hv = bg_, TEXT, BORDER
    elif danger:
        bg, fg, hv = "#3d1214", RED, "#5c1d20"
    elif accent:
        bg, fg, hv = ACCENT, BG, "#79c0ff"
    else:
        bg, fg, hv = CARD, TEXT, BORDER
    b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                  activebackground=hv, activeforeground=fg,
                  font=FONT, relief="flat", cursor="hand2",
                  bd=0, padx=16, pady=8, width=w)
    b.bind("<Enter>", lambda e: b.configure(bg=hv))
    b.bind("<Leave>", lambda e: b.configure(bg=bg))
    return b


def _entry(parent, var=None, w=20, show=None):
    return tk.Entry(parent, textvariable=var, width=w, show=show,
                    bg=CARD, fg=TEXT, insertbackground=TEXT,
                    font=FONT, relief="flat", bd=0,
                    highlightthickness=1, highlightbackground=BORDER,
                    highlightcolor=ACCENT)


def _lbl(parent, text, font=FONT, fg=TEXT, bg_=BG, **kw):
    return tk.Label(parent, text=text, bg=bg_, fg=fg, font=font, **kw)


def _check(parent, text, var, bg_=BG):
    return tk.Checkbutton(parent, text=text, variable=var,
                          bg=bg_, fg=TEXT, selectcolor=CARD,
                          activebackground=bg_, activeforeground=TEXT,
                          font=FONT, bd=0, highlightthickness=0)


def _radio(parent, text, var, val, bg_=BG):
    return tk.Radiobutton(parent, text=text, variable=var, value=val,
                          bg=bg_, fg=TEXT, selectcolor=CARD,
                          activebackground=bg_, activeforeground=TEXT,
                          font=FONT, bd=0, highlightthickness=0)


def _sep(parent):
    tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", pady=12)


def _spacer(parent, h=12):
    tk.Frame(parent, bg=BG, height=h).pack()


# ══════════════════════════════════════════════════════════════════════
#  Application
# ══════════════════════════════════════════════════════════════════════

class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("GameStream")
        self.geometry("560x700")
        self.configure(bg=BG)
        self.resizable(False, False)

        self._frame = None
        self._procs: list[subprocess.Popen] = []
        self._log_w: tk.Text | None = None
        self._status_w: tk.Label | None = None
        self._stop_ev = threading.Event()
        self._kh = KnownHosts()
        self._cfg = _load_config()
        self._local_ip = _local_ip()

        self.update_idletasks()
        self._dark_titlebar()
        self.protocol("WM_DELETE_WINDOW", self._quit)

        self._home()

    # ── Helpers ───────────────────────────────────────────────────────

    def _dark_titlebar(self):
        """Enable dark title bar on Windows 10/11."""
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            val = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))
        except Exception:
            pass

    def _new_frame(self) -> tk.Frame:
        if self._frame:
            self._frame.destroy()
        self._frame = tk.Frame(self, bg=BG)
        self._frame.pack(fill="both", expand=True, padx=28, pady=20)
        return self._frame

    # ══════════════════════════════════════════════════════════════════
    #  HOME
    # ══════════════════════════════════════════════════════════════════

    def _home(self):
        f = self._new_frame()

        _lbl(f, "GameStream", font=FONT_XL, fg=WHITE).pack(pady=(50, 2))
        _lbl(f, "Stream & control your PC — encrypted, low latency",
             font=FONT_SM, fg=DIM).pack(pady=(0, 16))

        ip = _local_ip()
        ip_frame = tk.Frame(f, bg=SURFACE, padx=12, pady=6)
        ip_frame.pack(pady=(0, 40))
        _lbl(ip_frame, f"Your IP :  {ip}", font=FONT_MONO, fg=DIM, bg_=SURFACE).pack()

        _btn(f, "Stream my screen   (Host)", self._page_host,
             accent=True, w=34).pack(pady=8)
        _btn(f, "Connect to a PC    (Client)", self._page_client,
             w=34).pack(pady=8)
        _btn(f, "Mobile control     (Phone)", self._page_mobile,
             w=34).pack(pady=8)

        _spacer(f, 30)
        _btn(f, "Quit", self.destroy, w=34).pack()

    # ══════════════════════════════════════════════════════════════════
    #  HOST SETUP
    # ══════════════════════════════════════════════════════════════════

    def _page_host(self):
        f = self._new_frame()

        # ── header
        hdr = tk.Frame(f, bg=BG)
        hdr.pack(fill="x", pady=(0, 10))
        _btn(hdr, "< Back", self._home, w=7).pack(side="left")
        _lbl(hdr, "Host Setup", font=FONT_LG, fg=WHITE).pack(side="left", padx=16)

        # ── mode radio
        c = self._cfg
        mode = tk.StringVar(value=c.get("host_mode", "lan"))
        mf = tk.Frame(f, bg=BG)
        mf.pack(fill="x", pady=6)
        _lbl(mf, "Mode :").pack(side="left")
        _radio(mf, "LAN", mode, "lan").pack(side="left", padx=(16, 6))
        _radio(mf, "Internet (relay)", mode, "inet").pack(side="left")

        _sep(f)

        # ── settings
        g = tk.Frame(f, bg=BG)
        g.pack(fill="x")

        fps_v = tk.StringVar(value=c.get("host_fps", "60"))
        br_v  = tk.StringVar(value=c.get("host_bitrate", "8"))
        mon_v = tk.StringVar(value=c.get("host_monitor", "0"))
        aud_v = tk.IntVar(value=1 if c.get("host_audio", True) else 0)
        sw_v  = tk.IntVar(value=1 if c.get("host_sw_encode", False) else 0)

        for i, (label, var, width) in enumerate([
            ("FPS :", fps_v, 6),
            ("Bitrate (Mbps) :", br_v, 6),
            ("Monitor :", mon_v, 4),
        ]):
            _lbl(g, label).grid(row=i, column=0, sticky="w", pady=4)
            _entry(g, var, w=width).grid(row=i, column=1, sticky="w", padx=8, pady=4)

        chk = tk.Frame(g, bg=BG)
        chk.grid(row=3, column=0, columnspan=2, sticky="w", pady=6)
        _check(chk, "Audio", aud_v).pack(side="left", padx=(0, 20))
        _check(chk, "Software encode (CPU)", sw_v).pack(side="left")

        # ── internet options (toggled)
        inet_box = tk.Frame(f, bg=BG)
        local_v = tk.IntVar(value=1 if c.get("host_relay_local", True) else 0)
        raddr_v = tk.StringVar(value=c.get("host_relay_addr", ""))
        rport_v = tk.StringVar(value=c.get("host_relay_port", "9950"))

        def _build_inet():
            for w in inet_box.winfo_children():
                w.destroy()
            _sep(inet_box)
            _lbl(inet_box, "Internet settings", fg=YELLOW).pack(anchor="w")
            _check(inet_box, "Run relay on this machine", local_v).pack(anchor="w", pady=4)
            r1 = tk.Frame(inet_box, bg=BG); r1.pack(fill="x", pady=3)
            _lbl(r1, "Relay address :").pack(side="left")
            _entry(r1, raddr_v, w=22).pack(side="left", padx=8)
            _lbl(r1, "IP or domain", font=FONT_SM, fg=DARK).pack(side="left", padx=4)
            r2 = tk.Frame(inet_box, bg=BG); r2.pack(fill="x", pady=3)
            _lbl(r2, "Relay port :").pack(side="left")
            _entry(r2, rport_v, w=8).pack(side="left", padx=8)
            _lbl(inet_box, "Open TCP ports 9950 & 9951 on your router",
                 font=FONT_SM, fg=DARK).pack(anchor="w", pady=(4, 0))

        def _toggle(*_):
            if mode.get() == "inet":
                inet_box.pack(fill="x")
                _build_inet()
            else:
                inet_box.pack_forget()
        mode.trace_add("write", _toggle)

        # ── start button
        _spacer(f, 8)

        def _start():
            try:
                fps = int(fps_v.get())
                bitrate = int(float(br_v.get()) * 1_000_000)
                monitor = int(mon_v.get())
            except ValueError:
                messagebox.showerror("Erreur", "Valeur invalide")
                return

            cmd = _build_script_cmd("host/host.py",
                   "--fps", str(fps), "--bitrate", str(bitrate),
                   "--monitor", str(monitor))
            if not aud_v.get():
                cmd.append("--no-audio")
            if sw_v.get():
                cmd.append("--sw-encode")

            cmds = []
            room = ""

            if mode.get() == "inet":
                port = rport_v.get() or "9950"
                room = secrets.token_hex(2).upper()
                if local_v.get():
                    cmds.append(_build_script_cmd("relay.py", "--port", port))
                    target = f"localhost:{port}"
                else:
                    addr = raddr_v.get().strip()
                    if not addr:
                        messagebox.showerror("Erreur", "Entrer l'adresse du relay")
                        return
                    target = f"{addr}:{port}" if ":" not in addr else addr
                cmd.extend(["--relay", target, "--room", room])

            cmds.append(cmd)

            # Save config for next launch
            self._cfg.update({
                "host_fps": fps_v.get(), "host_bitrate": br_v.get(),
                "host_monitor": mon_v.get(), "host_audio": bool(aud_v.get()),
                "host_sw_encode": bool(sw_v.get()), "host_mode": mode.get(),
                "host_relay_local": bool(local_v.get()),
                "host_relay_addr": raddr_v.get(), "host_relay_port": rport_v.get(),
            })
            _save_config(self._cfg)

            info = "LAN"
            if mode.get() == "inet":
                info = f"Internet — Room : {room}"
            self._launch(cmds, f"Host ({info})")

        _btn(f, "Start Streaming", _start, accent=True, w=34).pack(pady=(12, 0))

    # ══════════════════════════════════════════════════════════════════
    #  CLIENT
    # ══════════════════════════════════════════════════════════════════

    def _page_client(self):
        f = self._new_frame()

        # ── header
        hdr = tk.Frame(f, bg=BG)
        hdr.pack(fill="x", pady=(0, 10))
        _btn(hdr, "< Back", self._home, w=7).pack(side="left")
        _lbl(hdr, "Connect", font=FONT_LG, fg=WHITE).pack(side="left", padx=16)

        # ── mode radio
        c = self._cfg
        mode = tk.StringVar(value=c.get("client_mode", "lan"))
        mf = tk.Frame(f, bg=BG)
        mf.pack(fill="x", pady=6)
        _lbl(mf, "Mode :").pack(side="left")
        _radio(mf, "LAN", mode, "lan").pack(side="left", padx=(16, 6))
        _radio(mf, "Internet (relay)", mode, "inet").pack(side="left")

        # ── shared options
        of = tk.Frame(f, bg=BG)
        of.pack(fill="x", pady=4)
        fs_v   = tk.IntVar(value=1 if c.get("client_fullscreen") else 0)
        grab_v = tk.IntVar(value=1 if c.get("client_grab") else 0)
        na_v   = tk.IntVar(value=1 if c.get("client_no_audio") else 0)
        _check(of, "Fullscreen", fs_v).pack(side="left", padx=(0, 12))
        _check(of, "Grab mouse", grab_v).pack(side="left", padx=12)
        _check(of, "No audio", na_v).pack(side="left", padx=12)

        _sep(f)

        body = tk.Frame(f, bg=BG)
        body.pack(fill="both", expand=True)

        ip_v    = tk.StringVar()
        port_v  = tk.StringVar(value="9900")
        _default_relay = c.get("client_relay_addr") or f"{self._local_ip}:9950"
        relay_v = tk.StringVar(value=_default_relay)
        room_v  = tk.StringVar(value=c.get("client_room", ""))

        def _client_opts():
            o = []
            if fs_v.get():   o.append("--fullscreen")
            if grab_v.get(): o.append("--grab-mouse")
            if na_v.get():   o.append("--no-audio")
            return o

        def _save_client_cfg():
            self._cfg.update({
                "client_mode": mode.get(),
                "client_relay_addr": relay_v.get(),
                "client_room": room_v.get(),
                "client_fullscreen": bool(fs_v.get()),
                "client_grab": bool(grab_v.get()),
                "client_no_audio": bool(na_v.get()),
            })
            _save_config(self._cfg)

        def _connect_ip(host, port="9900"):
            h = host.strip()
            if not h:
                messagebox.showerror("Erreur", "Entrer l'adresse IP")
                return
            _save_client_cfg()
            cmd = _build_script_cmd("client/client.py", h,
                   "--port", str(port)) + _client_opts()
            self._launch([cmd], f"Client -> {h}")

        def _connect_relay():
            a = relay_v.get().strip()
            r = room_v.get().strip().upper()
            if not a or not r:
                messagebox.showerror("Erreur",
                                     "Entrer l'adresse relay et le code room")
                return
            _save_client_cfg()
            cmd = _build_script_cmd("client/client.py",
                   "--relay", a, "--room", r) + _client_opts()
            self._launch([cmd], f"Client -> relay {a} room {r}")

        # ── LAN content ──────────────────────────────────────────────

        def _show_lan():
            for w in body.winfo_children():
                w.destroy()

            paired = self._kh.all()

            # — Paired devices
            if paired:
                _lbl(body, "Paired devices", fg=GREEN).pack(anchor="w", pady=(0, 6))

                # Scrollable area
                outer = tk.Frame(body, bg=BG)
                outer.pack(fill="both", expand=True)

                canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
                vsb = tk.Scrollbar(outer, orient="vertical", command=canvas.yview,
                                   bg=CARD, troughcolor=BG)
                inner = tk.Frame(canvas, bg=BG)

                inner.bind("<Configure>",
                           lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
                canvas.create_window((0, 0), window=inner, anchor="nw",
                                     tags="inner")
                canvas.configure(yscrollcommand=vsb.set)

                # Resize inner frame when canvas resizes
                def _resize(e):
                    canvas.itemconfig("inner", width=e.width)
                canvas.bind("<Configure>", _resize)

                # Mouse wheel scroll
                def _wheel(e):
                    canvas.yview_scroll(-1 * (e.delta // 120), "units")
                canvas.bind_all("<MouseWheel>", _wheel)

                canvas.pack(side="left", fill="both", expand=True)
                if len(paired) > 3:
                    vsb.pack(side="right", fill="y")

                for addr, info in paired.items():
                    card = tk.Frame(inner, bg=CARD, padx=14, pady=10)
                    card.pack(fill="x", pady=3, padx=(0, 4))

                    name = info.get("name", "")
                    fp   = info.get("fingerprint", "")[:26] + "…"
                    seen = info.get("last_seen", "")[:10]

                    left = tk.Frame(card, bg=CARD)
                    left.pack(side="left", fill="x", expand=True)

                    tk.Label(left, text=name or addr, bg=CARD, fg=WHITE,
                             font=FONT_B).pack(anchor="w")
                    tk.Label(left, text=addr, bg=CARD, fg=DIM,
                             font=FONT_SM).pack(anchor="w")
                    tk.Label(left, text=f"{seen}   {fp}",
                             bg=CARD, fg=DARK, font=FONT_SM).pack(anchor="w")

                    h, p = addr.rsplit(":", 1)
                    _btn(card, "Connect",
                         lambda _h=h, _p=p: _connect_ip(_h, _p),
                         accent=True, w=10).pack(side="right", padx=(8, 0))

            else:
                _lbl(body, "No paired devices", fg=DIM).pack(pady=(8, 0))
                _lbl(body, "Connect once to a host and it will appear here",
                     font=FONT_SM, fg=DARK).pack(pady=(2, 0))

            # — Manual
            _sep(body)
            _lbl(body, "Manual connection", fg=DIM).pack(anchor="w", pady=(0, 6))

            row = tk.Frame(body, bg=BG)
            row.pack(fill="x", pady=4)
            _lbl(row, "IP :").pack(side="left")
            _entry(row, ip_v, w=16).pack(side="left", padx=8)
            _lbl(row, "Port :").pack(side="left")
            _entry(row, port_v, w=6).pack(side="left", padx=8)

            _btn(body, "Connect",
                 lambda: _connect_ip(ip_v.get(), port_v.get()),
                 accent=True, w=34).pack(pady=(12, 0))

        # ── Internet content ─────────────────────────────────────────

        def _show_inet():
            for w in body.winfo_children():
                w.destroy()

            _lbl(body, "Internet connection", fg=YELLOW).pack(anchor="w", pady=(0, 12))

            r1 = tk.Frame(body, bg=BG); r1.pack(fill="x", pady=4)
            _lbl(r1, "Relay address :").pack(side="left")
            _entry(r1, relay_v, w=24).pack(side="left", padx=8)
            _lbl(r1, "ex: 1.2.3.4:9950", font=FONT_SM, fg=DARK).pack(side="left", padx=4)

            r2 = tk.Frame(body, bg=BG); r2.pack(fill="x", pady=4)
            _lbl(r2, "Room code :").pack(side="left")
            room_entry = _entry(r2, room_v, w=10)
            room_entry.pack(side="left", padx=8)

            _lbl(body, "Get the room code from the host",
                 font=FONT_SM, fg=DARK).pack(anchor="w", pady=(6, 0))

            _btn(body, "Connect", _connect_relay,
                 accent=True, w=34).pack(pady=(24, 0))

        # ── mode toggle
        def _on_mode(*_):
            (_show_lan if mode.get() == "lan" else _show_inet)()
        mode.trace_add("write", _on_mode)
        _show_lan()

    # ══════════════════════════════════════════════════════════════════
    #  MOBILE
    # ══════════════════════════════════════════════════════════════════

    def _page_mobile(self):
        f = self._new_frame()

        # ── header
        hdr = tk.Frame(f, bg=BG)
        hdr.pack(fill="x", pady=(0, 10))
        _btn(hdr, "< Back", self._home, w=7).pack(side="left")
        _lbl(hdr, "Mobile Control", font=FONT_LG, fg=WHITE).pack(side="left", padx=16)

        _sep(f)

        # ── settings
        c = self._cfg
        g = tk.Frame(f, bg=BG)
        g.pack(fill="x")

        fps_v  = tk.StringVar(value=c.get("mobile_fps", "30"))
        port_v = tk.StringVar(value=c.get("mobile_port", "8080"))
        mon_v  = tk.StringVar(value=c.get("mobile_monitor", "0"))

        for i, (label, var, width) in enumerate([
            ("FPS :", fps_v, 6),
            ("HTTP Port :", port_v, 6),
            ("Monitor :", mon_v, 4),
        ]):
            _lbl(g, label).grid(row=i, column=0, sticky="w", pady=4)
            _entry(g, var, w=width).grid(row=i, column=1, sticky="w", padx=8, pady=4)

        # ── relay options
        _sep(f)
        use_relay = tk.IntVar(value=1 if c.get("mobile_use_relay") else 0)
        _check(f, "Use relay (Internet access)", use_relay).pack(anchor="w")

        relay_box = tk.Frame(f, bg=BG)
        _default_mobile_relay = c.get("mobile_relay_addr") or f"{self._local_ip}:9951"
        raddr_v = tk.StringVar(value=_default_mobile_relay)

        def _build_relay():
            for w in relay_box.winfo_children():
                w.destroy()
            if use_relay.get():
                relay_box.pack(fill="x", pady=4)
                r1 = tk.Frame(relay_box, bg=BG); r1.pack(fill="x", pady=3)
                _lbl(r1, "Relay address :").pack(side="left")
                _entry(r1, raddr_v, w=22).pack(side="left", padx=8)
                _lbl(r1, "ex: 1.2.3.4:9951", font=FONT_SM, fg=DARK).pack(side="left", padx=4)
            else:
                relay_box.pack_forget()
        use_relay.trace_add("write", lambda *_: _build_relay())
        if use_relay.get():
            _build_relay()

        _spacer(f, 8)

        ip = self._local_ip
        _lbl(f, f"In LAN: no relay needed — phone connects directly",
             font=FONT_SM, fg=GREEN).pack(anchor="w", pady=(4, 0))
        _lbl(f, f"URL will appear in the log below — open it on your phone",
             font=FONT_SM, fg=DIM).pack(anchor="w", pady=(2, 0))

        # ── start button
        def _start():
            try:
                fps = int(fps_v.get())
                port = int(port_v.get())
                monitor = int(mon_v.get())
            except ValueError:
                messagebox.showerror("Erreur", "Valeur invalide")
                return

            cmds = []

            cmd = _build_script_cmd("mobile/gateway.py",
                                    "--fps", str(fps),
                                    "--port", str(port),
                                    "--monitor", str(monitor))

            if use_relay.get():
                addr = raddr_v.get().strip()
                if not addr:
                    messagebox.showerror("Erreur", "Entrer l'adresse du relay")
                    return
                # If relay is on this machine, start it automatically
                is_local = addr.startswith("localhost") or addr.startswith("127.") \
                           or addr.startswith(ip)
                if is_local:
                    rport = addr.rsplit(":", 1)[1] if ":" in addr else "9951"
                    cmds.append(_build_script_cmd("relay.py",
                                                  "--port", "9950",
                                                  "--http-port", rport))
                cmd.extend(["--relay", addr])

            cmds.append(cmd)

            # Save config
            self._cfg.update({
                "mobile_fps": fps_v.get(), "mobile_port": port_v.get(),
                "mobile_monitor": mon_v.get(),
                "mobile_use_relay": bool(use_relay.get()),
                "mobile_relay_addr": raddr_v.get(),
            })
            _save_config(self._cfg)

            self._launch(cmds, f"Mobile Gateway (port {port})")

        _btn(f, "Start Mobile Gateway", _start, accent=True, w=34).pack(pady=(16, 0))

    # ══════════════════════════════════════════════════════════════════
    #  RUNNING SCREEN
    # ══════════════════════════════════════════════════════════════════

    def _page_running(self, title: str):
        f = self._new_frame()
        self._stop_ev.clear()

        # header
        _lbl(f, title, font=FONT_LG, fg=GREEN).pack(anchor="w")

        self._status_w = _lbl(f, "Starting…", fg=YELLOW)
        self._status_w.pack(anchor="w", pady=(2, 8))

        # log
        log_frame = tk.Frame(f, bg=BORDER, padx=1, pady=1)
        log_frame.pack(fill="both", expand=True)

        log = tk.Text(log_frame, bg=SURFACE, fg=TEXT, font=FONT_MONO,
                      relief="flat", bd=0, wrap="word",
                      insertbackground=TEXT, padx=10, pady=8)
        sb = tk.Scrollbar(log_frame, command=log.yview,
                          bg=CARD, troughcolor=SURFACE)
        log.configure(yscrollcommand=sb.set, state="disabled")
        sb.pack(side="right", fill="y")
        log.pack(fill="both", expand=True)
        self._log_w = log

        # stop
        _spacer(f, 10)
        _btn(f, "Stop", self._stop_all, danger=True, w=34).pack()

    def _log(self, text: str):
        """Thread-safe append to log widget."""
        if self._log_w:
            self.after(0, self._log_append, text)

    def _log_append(self, text: str):
        w = self._log_w
        if not w:
            return
        # Handle \r overwrite lines (stats)
        if "\r" in text:
            text = text.split("\r")[-1]
        text = text.strip()
        if not text:
            return

        w.configure(state="normal")
        w.insert("end", text + "\n")
        w.see("end")
        w.configure(state="disabled")

        # Auto-update status from keywords
        lo = text.lower()
        if not self._status_w:
            return

        # Detect URL with token (mobile gateway) and show it prominently
        if "token=" in text and ("http://" in lo or "https://" in lo):
            import re
            urls = re.findall(r'https?://\S+', text)
            if urls:
                self._status_w.configure(text=urls[0], fg=ACCENT)
                return

        if any(k in lo for k in ("connected", "paired", "listening", "ready")):
            self._status_w.configure(text="Running", fg=GREEN)
        elif "waiting" in lo or "starting" in lo:
            self._status_w.configure(text="Waiting…", fg=YELLOW)
        elif "error" in lo or "failed" in lo:
            self._status_w.configure(text="Error", fg=RED)
        elif "shut" in lo or "exit" in lo:
            self._status_w.configure(text="Stopped", fg=DIM)

    # ══════════════════════════════════════════════════════════════════
    #  PROCESS MANAGEMENT
    # ══════════════════════════════════════════════════════════════════

    def _launch(self, cmds: list[list[str]], title: str):
        """Show running page and start subprocess(es)."""
        self._page_running(title)
        self._procs = []

        def _go():
            for i, cmd in enumerate(cmds):
                if self._stop_ev.is_set():
                    return
                # Delay between processes (e.g. relay then host)
                if i > 0:
                    time.sleep(2.0)
                    if self._stop_ev.is_set():
                        return

                self._log(f"$ {' '.join(cmd)}")
                env = {
                    **os.environ,
                    "PYTHONUNBUFFERED": "1",
                    "PYTHONIOENCODING": "utf-8",
                }
                flags = 0
                if sys.platform == "win32":
                    flags = subprocess.CREATE_NO_WINDOW
                try:
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        bufsize=1, cwd=BASE_DIR, env=env,
                        creationflags=flags,
                    )
                    self._procs.append(proc)
                    threading.Thread(target=self._reader, args=(proc,),
                                     daemon=True).start()
                except Exception as e:
                    self._log(f"[ERREUR] {e}")

            # Wait for all to exit
            while not self._stop_ev.is_set():
                alive = [p for p in self._procs if p.poll() is None]
                if not alive:
                    break
                time.sleep(0.5)

            if not self._stop_ev.is_set():
                self._log("[Tous les processus terminés]")
                self.after(0, self._on_finished)

        threading.Thread(target=_go, daemon=True).start()

    def _reader(self, proc: subprocess.Popen):
        """Read stdout of a subprocess line-by-line."""
        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                if self._stop_ev.is_set():
                    break
                self._log(line)
        except Exception:
            pass

    def _on_finished(self):
        """Called when all processes have exited on their own."""
        if self._status_w:
            self._status_w.configure(text="Stopped", fg=DIM)
        if self._frame:
            _spacer(self._frame, 6)
            _btn(self._frame, "Back", self._home, w=34).pack()

    def _stop_all(self):
        """Terminate all running processes and go home."""
        self._stop_ev.set()
        for p in self._procs:
            try:
                p.terminate()
            except Exception:
                pass
        self._procs.clear()
        self._log_w = None
        self._status_w = None
        self._home()

    def _quit(self):
        """Window close handler."""
        self._stop_ev.set()
        for p in self._procs:
            try:
                p.terminate()
            except Exception:
                pass
        self.destroy()


# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # ── Frozen subprocess dispatch ────────────────────────────────────────
    # When the PyInstaller exe is invoked with --_subprocess <module> it
    # runs the requested module in-process using the embedded Python runtime,
    # avoiding the "pythonXYZ.dll conflicts" error that arises when a system
    # Python (different version) tries to load .pyd files from the bundle.
    if getattr(sys, "frozen", False) and len(sys.argv) > 2 and sys.argv[1] == "--_subprocess":
        # Force UTF-8 + line-buffered so print() output reaches the GUI log
        # immediately (unbuffered), and box-drawing chars / emoji don't crash
        # on Windows consoles whose default encoding is cp1252.
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                                   line_buffering=True)
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace",
                                   line_buffering=True)

        _module = sys.argv[2]
        sys.argv = [sys.argv[0]] + sys.argv[3:]   # strip our flags, keep the rest
        _mp = sys._MEIPASS
        sys.path.insert(0, _mp)
        if _module == "relay.py" or _module == "relay":
            import relay as _m
        elif _module in ("host/host.py", "host"):
            sys.path.insert(0, os.path.join(_mp, "host"))
            import host as _m
        elif _module in ("client/client.py", "client"):
            sys.path.insert(0, os.path.join(_mp, "client"))
            import client as _m
        elif _module in ("mobile/gateway.py", "gateway"):
            sys.path.insert(0, os.path.join(_mp, "mobile"))
            import gateway as _m
        else:
            print(f"[subprocess] unknown module: {_module}", file=sys.stderr)
            sys.exit(1)
        _m.main()
        sys.exit(0)

    App().mainloop()
