import React, { useEffect, useRef, useState, useCallback } from "react";
import {
  View,
  Image,
  Text,
  TouchableOpacity,
  StyleSheet,
  PanResponder,
  GestureResponderEvent,
  PanResponderGestureState,
} from "react-native";
import * as ScreenOrientation from "expo-screen-orientation";
import GamepadOverlay from "./GamepadOverlay";

// Binary frame type prefixes (must match gateway.py)
const FRAME_TYPE_JPEG = 0x00;

type InputMode = "mouse" | "gamepad";

interface Config {
  width: number;
  height: number;
  fps: number;
  codec: string;
  audio: boolean;
}

interface Props {
  wsUrl: string;
  onDisconnect: () => void;
}

// ── Base64 encode/decode that works in Hermes ─────────────────────────

const B64 =
  "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
const B64_LOOKUP = new Uint8Array(128);
for (let i = 0; i < B64.length; i++) B64_LOOKUP[B64.charCodeAt(i)] = i;

function uint8ToBase64(u8: Uint8Array): string {
  const len = u8.length;
  const parts: string[] = [];
  let i = 0;
  while (i < len) {
    const a = u8[i++];
    const b = i < len ? u8[i++] : 0;
    const c = i < len ? u8[i++] : 0;
    const pad = i - len;
    const triplet = (a << 16) | (b << 8) | c;
    parts.push(
      B64[(triplet >> 18) & 0x3f],
      B64[(triplet >> 12) & 0x3f],
      pad > 1 ? "=" : B64[(triplet >> 6) & 0x3f],
      pad > 0 ? "=" : B64[triplet & 0x3f]
    );
  }
  return parts.join("");
}

function base64ToUint8(b64: string): Uint8Array {
  // Strip padding
  let str = b64;
  while (str.endsWith("=")) str = str.slice(0, -1);
  const n = str.length;
  const out = new Uint8Array(Math.floor((n * 3) / 4));
  let j = 0;
  for (let i = 0; i < n; i += 4) {
    const a = B64_LOOKUP[str.charCodeAt(i)];
    const b = i + 1 < n ? B64_LOOKUP[str.charCodeAt(i + 1)] : 0;
    const c = i + 2 < n ? B64_LOOKUP[str.charCodeAt(i + 2)] : 0;
    const d = i + 3 < n ? B64_LOOKUP[str.charCodeAt(i + 3)] : 0;
    const triplet = (a << 18) | (b << 12) | (c << 6) | d;
    out[j++] = (triplet >> 16) & 0xff;
    if (i + 2 < n) out[j++] = (triplet >> 8) & 0xff;
    if (i + 3 < n) out[j++] = triplet & 0xff;
  }
  return out.subarray(0, j);
}

export default function StreamScreen({ wsUrl, onDisconnect }: Props) {
  const wsRef = useRef<WebSocket | null>(null);
  const [frameUri, setFrameUri] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [config, setConfig] = useState<Config | null>(null);
  const [mode, setMode] = useState<InputMode>("mouse");
  const [showHud, setShowHud] = useState(true);
  const [fps, setFps] = useState(0);
  const [latency, setLatency] = useState(0);
  const [debug, setDebug] = useState("init");

  const frameCountRef = useRef(0);
  const msgCountRef = useRef(0);
  const fpsIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pingIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pingTsRef = useRef(0);

  // Lock landscape
  useEffect(() => {
    ScreenOrientation.lockAsync(
      ScreenOrientation.OrientationLock.LANDSCAPE
    ).catch(() => {});
    return () => {
      ScreenOrientation.unlockAsync().catch(() => {});
    };
  }, []);

  // Process raw bytes (Uint8Array) — extract frame type and display JPEG
  const processFrame = useCallback((data: Uint8Array) => {
    if (data.length < 2) return;
    const frameType = data[0];
    if (frameType === FRAME_TYPE_JPEG) {
      const payload = data.subarray(1);
      const b64 = uint8ToBase64(payload);
      setFrameUri(`data:image/jpeg;base64,${b64}`);
      frameCountRef.current++;
    }
  }, []);

  // WebSocket connection
  useEffect(() => {
    const ws = new WebSocket(wsUrl);
    // Try blob — more reliable on RN Android than arraybuffer
    ws.binaryType = "blob";
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      setDebug("ws open");
      // Request JPEG codec (simplest to display in RN)
      ws.send(JSON.stringify({ type: "codec_pref", codec: "jpeg" }));
    };

    ws.onmessage = (event: MessageEvent) => {
      msgCountRef.current++;
      const data = event.data;

      // ── Case 1: Text string ────────────────────────────────────
      if (typeof data === "string") {
        // Could be JSON text OR base64-encoded binary (RN Android quirk)
        if (data.length > 0 && data[0] === "{") {
          // Looks like JSON
          try {
            handleJsonMessage(JSON.parse(data));
          } catch {}
        } else if (data.length > 10) {
          // Likely base64-encoded binary frame from RN WebSocket
          try {
            const bytes = base64ToUint8(data);
            if (msgCountRef.current <= 3) {
              setDebug(
                `b64 str len=${data.length} bytes=${bytes.length} type=${bytes[0]}`
              );
            }
            processFrame(bytes);
          } catch {}
        }
        return;
      }

      // ── Case 2: ArrayBuffer ────────────────────────────────────
      if (data instanceof ArrayBuffer) {
        if (msgCountRef.current <= 3) {
          setDebug(`arraybuffer len=${data.byteLength}`);
        }
        processFrame(new Uint8Array(data));
        return;
      }

      // ── Case 3: Blob ───────────────────────────────────────────
      if (data instanceof Blob) {
        if (msgCountRef.current <= 3) {
          setDebug(`blob size=${data.size}`);
        }
        const reader = new FileReader();
        reader.onload = () => {
          if (reader.result instanceof ArrayBuffer) {
            processFrame(new Uint8Array(reader.result));
          }
        };
        reader.readAsArrayBuffer(data);
        return;
      }

      // ── Case 4: Unknown ────────────────────────────────────────
      setDebug(`unknown type: ${typeof data}`);
    };

    ws.onerror = (e: any) => {
      setConnected(false);
      setDebug(`ws error: ${e?.message || "unknown"}`);
    };

    ws.onclose = (e: any) => {
      setConnected(false);
      setDebug(`ws closed: code=${e?.code}`);
    };

    // FPS counter
    fpsIntervalRef.current = setInterval(() => {
      setFps(frameCountRef.current);
      frameCountRef.current = 0;
    }, 1000);

    // Ping for latency
    pingIntervalRef.current = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) {
        pingTsRef.current = Date.now();
        ws.send(JSON.stringify({ type: "ping", ts: pingTsRef.current }));
      }
    }, 2000);

    return () => {
      ws.close();
      wsRef.current = null;
      if (fpsIntervalRef.current) clearInterval(fpsIntervalRef.current);
      if (pingIntervalRef.current) clearInterval(pingIntervalRef.current);
    };
  }, [wsUrl, processFrame]);

  function handleJsonMessage(msg: any) {
    switch (msg.type) {
      case "config":
        setConfig({
          width: msg.width,
          height: msg.height,
          fps: msg.fps,
          codec: msg.codec,
          audio: msg.audio,
        });
        setDebug(`config: ${msg.width}x${msg.height} ${msg.codec}`);
        break;
      case "pong":
        if (pingTsRef.current) {
          setLatency(Date.now() - pingTsRef.current);
        }
        break;
    }
  }

  const sendInput = useCallback((event: any) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(event));
    }
  }, []);

  // ── Mouse mode touch handling ───────────────────────────────────────

  const prevMoveRef = useRef({ x: 0, y: 0 });

  const panResponder = useRef(
    PanResponder.create({
      onStartShouldSetPanResponder: () => true,
      onMoveShouldSetPanResponder: () => true,

      onPanResponderGrant: (evt: GestureResponderEvent) => {
        const { pageX, pageY } = evt.nativeEvent;
        prevMoveRef.current = { x: pageX, y: pageY };
      },

      onPanResponderMove: (evt: GestureResponderEvent) => {
        const { pageX, pageY } = evt.nativeEvent;
        const dx = pageX - prevMoveRef.current.x;
        const dy = pageY - prevMoveRef.current.y;
        prevMoveRef.current = { x: pageX, y: pageY };

        if (Math.abs(dx) > 0 || Math.abs(dy) > 0) {
          sendInput({
            type: "mouse_move_rel",
            dx: Math.round(dx),
            dy: Math.round(dy),
          });
        }
      },

      onPanResponderRelease: (
        _evt: GestureResponderEvent,
        gesture: PanResponderGestureState
      ) => {
        const dist = Math.abs(gesture.dx) + Math.abs(gesture.dy);
        if (dist < 10) {
          sendInput({ type: "mouse_down", button: 1 });
          setTimeout(() => sendInput({ type: "mouse_up", button: 1 }), 50);
        }
      },
    })
  ).current;

  // Double tap for right click
  const lastTapRef = useRef(0);
  function handleDoubleTap() {
    const now = Date.now();
    if (now - lastTapRef.current < 300) {
      sendInput({ type: "mouse_down", button: 3 });
      setTimeout(() => sendInput({ type: "mouse_up", button: 3 }), 50);
    }
    lastTapRef.current = now;
  }

  // ── Gamepad input handlers ──────────────────────────────────────────

  const handleJoystick = useCallback(
    (axis: "left" | "right", x: number, y: number) => {
      sendInput({ type: "gamepad_axis", axis, x, y });
    },
    [sendInput]
  );

  const handleButton = useCallback(
    (button: string, pressed: boolean) => {
      sendInput({ type: "gamepad_button", button, pressed });
    },
    [sendInput]
  );

  // ── Render ──────────────────────────────────────────────────────────

  return (
    <View style={styles.container}>
      {/* Video frame */}
      {frameUri ? (
        <Image
          source={{ uri: frameUri }}
          style={styles.video}
          resizeMode="contain"
          fadeDuration={0}
        />
      ) : (
        <View style={styles.placeholder}>
          <Text style={styles.placeholderText}>
            {connected ? "Waiting for video..." : "Connecting..."}
          </Text>
        </View>
      )}

      {/* Touch area for mouse mode */}
      {mode === "mouse" && (
        <View
          style={styles.touchArea}
          {...panResponder.panHandlers}
          onTouchStart={handleDoubleTap}
        />
      )}

      {/* Gamepad overlay */}
      {mode === "gamepad" && (
        <GamepadOverlay
          onJoystick={handleJoystick}
          onButton={handleButton}
        />
      )}

      {/* HUD */}
      {showHud && (
        <View style={styles.hud}>
          <View style={styles.hudLeft}>
            <View
              style={[
                styles.dot,
                { backgroundColor: connected ? "#4caf50" : "#f44336" },
              ]}
            />
            <Text style={styles.hudText}>
              {fps} FPS | {latency}ms
              {config ? ` | ${config.width}x${config.height}` : ""}
            </Text>
          </View>

          <View style={styles.hudRight}>
            <TouchableOpacity
              style={[
                styles.modeBtn,
                mode === "mouse" && styles.modeBtnActive,
              ]}
              onPress={() => setMode("mouse")}
            >
              <Text style={styles.modeBtnText}>Mouse</Text>
            </TouchableOpacity>
            <TouchableOpacity
              style={[
                styles.modeBtn,
                mode === "gamepad" && styles.modeBtnActive,
              ]}
              onPress={() => setMode("gamepad")}
            >
              <Text style={styles.modeBtnText}>Gamepad</Text>
            </TouchableOpacity>

            <TouchableOpacity
              style={styles.disconnectBtn}
              onPress={onDisconnect}
            >
              <Text style={styles.disconnectText}>X</Text>
            </TouchableOpacity>
          </View>
        </View>
      )}

      {/* Debug line — shows data type received */}
      <View style={styles.debugBar}>
        <Text style={styles.debugText}>
          dbg: {debug} | msgs:{msgCountRef.current}
        </Text>
      </View>

      {/* Toggle HUD */}
      {!showHud && (
        <TouchableOpacity
          style={styles.showHudBtn}
          onPress={() => setShowHud(true)}
        >
          <Text style={styles.showHudText}>...</Text>
        </TouchableOpacity>
      )}
      {showHud && (
        <TouchableOpacity
          style={styles.hideHudBtn}
          onPress={() => setShowHud(false)}
        >
          <Text style={styles.hideHudText}>Hide</Text>
        </TouchableOpacity>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: "#000",
  },
  video: {
    position: "absolute",
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
  },
  placeholder: {
    flex: 1,
    justifyContent: "center",
    alignItems: "center",
  },
  placeholderText: {
    color: "#888",
    fontSize: 18,
  },
  touchArea: {
    position: "absolute",
    top: 40,
    left: 0,
    right: 0,
    bottom: 0,
    zIndex: 5,
  },
  hud: {
    position: "absolute",
    top: 0,
    left: 0,
    right: 0,
    height: 36,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    backgroundColor: "rgba(0,0,0,0.6)",
    paddingHorizontal: 12,
    zIndex: 20,
  },
  hudLeft: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
  },
  dot: {
    width: 8,
    height: 8,
    borderRadius: 4,
  },
  hudText: {
    color: "#ccc",
    fontSize: 12,
  },
  hudRight: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
  },
  modeBtn: {
    paddingHorizontal: 10,
    paddingVertical: 4,
    borderRadius: 6,
    backgroundColor: "rgba(255,255,255,0.1)",
  },
  modeBtnActive: {
    backgroundColor: "rgba(255,255,255,0.25)",
  },
  modeBtnText: {
    color: "#ccc",
    fontSize: 12,
    fontWeight: "500",
  },
  disconnectBtn: {
    width: 28,
    height: 28,
    borderRadius: 14,
    backgroundColor: "rgba(244,67,54,0.4)",
    justifyContent: "center",
    alignItems: "center",
    marginLeft: 8,
  },
  disconnectText: {
    color: "#fff",
    fontSize: 14,
    fontWeight: "700",
  },
  debugBar: {
    position: "absolute",
    bottom: 0,
    left: 0,
    right: 0,
    height: 20,
    backgroundColor: "rgba(0,0,0,0.8)",
    justifyContent: "center",
    paddingHorizontal: 8,
    zIndex: 30,
  },
  debugText: {
    color: "#ff0",
    fontSize: 10,
    fontFamily: "monospace",
  },
  showHudBtn: {
    position: "absolute",
    top: 4,
    right: 12,
    zIndex: 25,
    padding: 6,
  },
  showHudText: {
    color: "rgba(255,255,255,0.4)",
    fontSize: 16,
    fontWeight: "700",
  },
  hideHudBtn: {
    position: "absolute",
    bottom: 24,
    right: 12,
    zIndex: 25,
    padding: 4,
  },
  hideHudText: {
    color: "rgba(255,255,255,0.3)",
    fontSize: 11,
  },
});
