#!/usr/bin/env python3
"""
GameStream Launcher v2 — Setup & launch for Host, Client, Mobile, or Relay.

Usage:
    python launch.py install           Install all dependencies
    python launch.py host [options]    Start the host (streaming PC)
    python launch.py client <ip> [opt] Start the client (remote PC)
    python launch.py mobile [options]  Start the mobile gateway (Android/iOS)
    python launch.py relay [options]   Start the internet relay server (VPS)
    python launch.py audio-list        List audio devices
    python launch.py gen-cert          Generate TLS certificate
"""

import subprocess, sys, os

def install():
    print("📦  Installing Host dependencies...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "host/requirements.txt", "-q"])
    print("📦  Installing Client dependencies...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "client/requirements.txt", "-q"])
    print("📦  Installing Mobile Gateway dependencies...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "mobile/requirements.txt", "-q"])
    print("✅  Done! You can now run:")
    print("    python launch.py host          Desktop host")
    print("    python launch.py client <ip>   Desktop client (or 'auto' for mDNS)")
    print("    python launch.py mobile        Mobile gateway (Android/iOS)")
    print("    python launch.py relay         Internet relay server (run on VPS)")

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    mode = sys.argv[1].lower()
    base = os.path.dirname(os.path.abspath(__file__))

    if mode == "gui":
        os.chdir(base)
        subprocess.run([sys.executable, "app.py"])
    elif mode == "install":
        os.chdir(base)
        install()
    elif mode == "host":
        os.chdir(base)
        subprocess.run([sys.executable, "host/host.py"] + sys.argv[2:])
    elif mode == "client":
        os.chdir(base)
        subprocess.run([sys.executable, "client/client.py"] + sys.argv[2:])
    elif mode == "mobile":
        os.chdir(base)
        subprocess.run([sys.executable, "mobile/gateway.py"] + sys.argv[2:])
    elif mode == "relay":
        os.chdir(base)
        subprocess.run([sys.executable, "relay.py"] + sys.argv[2:])
    elif mode == "audio-list":
        os.chdir(base)
        subprocess.run([sys.executable, "host/host.py", "--list-audio"])
    elif mode == "gen-cert":
        os.chdir(base)
        sys.path.insert(0, base)
        from shared.crypto import ensure_certificates
        ensure_certificates()
    else:
        print(f"Unknown: {mode}.")
        print("Use: gui, install, host, client, mobile, relay, audio-list, gen-cert")
        sys.exit(1)

if __name__ == "__main__":
    main()
