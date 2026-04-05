import React, { useRef, useCallback } from "react";
import {
  View,
  Text,
  StyleSheet,
  PanResponder,
  TouchableOpacity,
  Dimensions,
  GestureResponderEvent,
} from "react-native";

interface Props {
  onJoystick: (axis: "left" | "right", x: number, y: number) => void;
  onButton: (button: string, pressed: boolean) => void;
}

const JOYSTICK_RADIUS = 60;
const KNOB_RADIUS = 24;
const DEAD_ZONE = 0.15;

function clamp(v: number, min: number, max: number) {
  return Math.max(min, Math.min(max, v));
}

function Joystick({
  onMove,
}: {
  onMove: (x: number, y: number) => void;
}) {
  const centerRef = useRef({ x: 0, y: 0 });
  const knobRef = useRef({ x: 0, y: 0 });
  const [knobPos, setKnobPos] = React.useState({ x: 0, y: 0 });
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const startSending = useCallback(
    (x: number, y: number) => {
      if (intervalRef.current) clearInterval(intervalRef.current);
      intervalRef.current = setInterval(() => {
        onMove(knobRef.current.x, knobRef.current.y);
      }, 50);
      knobRef.current = { x, y };
      onMove(x, y);
    },
    [onMove]
  );

  const stopSending = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
    onMove(0, 0);
  }, [onMove]);

  const panResponder = useRef(
    PanResponder.create({
      onStartShouldSetPanResponder: () => true,
      onMoveShouldSetPanResponder: () => true,

      onPanResponderGrant: (evt: GestureResponderEvent) => {
        const { locationX, locationY } = evt.nativeEvent;
        centerRef.current = { x: locationX, y: locationY };
        setKnobPos({ x: 0, y: 0 });
        startSending(0, 0);
      },

      onPanResponderMove: (_evt, gesture) => {
        const dx = gesture.dx;
        const dy = gesture.dy;
        const dist = Math.sqrt(dx * dx + dy * dy);
        const maxDist = JOYSTICK_RADIUS - KNOB_RADIUS;
        const scale = dist > maxDist ? maxDist / dist : 1;

        const px = dx * scale;
        const py = dy * scale;
        setKnobPos({ x: px, y: py });

        let nx = clamp(px / maxDist, -1, 1);
        let ny = clamp(py / maxDist, -1, 1);
        if (Math.abs(nx) < DEAD_ZONE) nx = 0;
        if (Math.abs(ny) < DEAD_ZONE) ny = 0;

        knobRef.current = { x: nx, y: ny };
      },

      onPanResponderRelease: () => {
        setKnobPos({ x: 0, y: 0 });
        knobRef.current = { x: 0, y: 0 };
        stopSending();
      },
    })
  ).current;

  return (
    <View style={styles.joystickBase} {...panResponder.panHandlers}>
      <View
        style={[
          styles.joystickKnob,
          {
            transform: [
              { translateX: knobPos.x },
              { translateY: knobPos.y },
            ],
          },
        ]}
      />
    </View>
  );
}

function ActionButton({
  label,
  color,
  onPress,
}: {
  label: string;
  color: string;
  onPress: (pressed: boolean) => void;
}) {
  return (
    <View
      style={styles.actionBtn}
      onTouchStart={() => onPress(true)}
      onTouchEnd={() => onPress(false)}
    >
      <Text style={[styles.actionBtnText, { color }]}>{label}</Text>
    </View>
  );
}

export default function GamepadOverlay({ onJoystick, onButton }: Props) {
  const handleLeftJoystick = useCallback(
    (x: number, y: number) => onJoystick("left", x, y),
    [onJoystick]
  );

  return (
    <View style={styles.container} pointerEvents="box-none">
      {/* Left joystick */}
      <View style={styles.leftZone}>
        <Joystick onMove={handleLeftJoystick} />
      </View>

      {/* Right buttons (ABXY) */}
      <View style={styles.rightZone}>
        <View style={styles.buttonGrid}>
          <View style={styles.buttonRow}>
            <View style={styles.spacer} />
            <ActionButton
              label="Y"
              color="#ff9800"
              onPress={(p) => onButton("y", p)}
            />
            <View style={styles.spacer} />
          </View>
          <View style={styles.buttonRow}>
            <ActionButton
              label="X"
              color="#2196f3"
              onPress={(p) => onButton("x", p)}
            />
            <View style={styles.btnGap} />
            <ActionButton
              label="B"
              color="#f44336"
              onPress={(p) => onButton("b", p)}
            />
          </View>
          <View style={styles.buttonRow}>
            <View style={styles.spacer} />
            <ActionButton
              label="A"
              color="#4caf50"
              onPress={(p) => onButton("a", p)}
            />
            <View style={styles.spacer} />
          </View>
        </View>
      </View>

      {/* Shoulder buttons */}
      <View style={styles.shoulderLeft}>
        <TouchableOpacity
          style={styles.shoulderBtn}
          onPressIn={() => onButton("lb", true)}
          onPressOut={() => onButton("lb", false)}
        >
          <Text style={styles.shoulderText}>LB</Text>
        </TouchableOpacity>
        <TouchableOpacity
          style={styles.shoulderBtn}
          onPressIn={() => onButton("lt", true)}
          onPressOut={() => onButton("lt", false)}
        >
          <Text style={styles.shoulderText}>LT</Text>
        </TouchableOpacity>
      </View>

      <View style={styles.shoulderRight}>
        <TouchableOpacity
          style={styles.shoulderBtn}
          onPressIn={() => onButton("rt", true)}
          onPressOut={() => onButton("rt", false)}
        >
          <Text style={styles.shoulderText}>RT</Text>
        </TouchableOpacity>
        <TouchableOpacity
          style={styles.shoulderBtn}
          onPressIn={() => onButton("rb", true)}
          onPressOut={() => onButton("rb", false)}
        >
          <Text style={styles.shoulderText}>RB</Text>
        </TouchableOpacity>
      </View>

      {/* Center buttons */}
      <View style={styles.centerBtns}>
        <TouchableOpacity
          style={styles.centerBtn}
          onPressIn={() => onButton("select", true)}
          onPressOut={() => onButton("select", false)}
        >
          <Text style={styles.centerBtnText}>SELECT</Text>
        </TouchableOpacity>
        <TouchableOpacity
          style={styles.centerBtn}
          onPressIn={() => onButton("start", true)}
          onPressOut={() => onButton("start", false)}
        >
          <Text style={styles.centerBtnText}>START</Text>
        </TouchableOpacity>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    position: "absolute",
    top: 36,
    left: 0,
    right: 0,
    bottom: 0,
    zIndex: 15,
  },
  leftZone: {
    position: "absolute",
    bottom: 20,
    left: 20,
  },
  rightZone: {
    position: "absolute",
    bottom: 20,
    right: 20,
  },
  joystickBase: {
    width: JOYSTICK_RADIUS * 2,
    height: JOYSTICK_RADIUS * 2,
    borderRadius: JOYSTICK_RADIUS,
    backgroundColor: "rgba(255,255,255,0.08)",
    borderWidth: 2,
    borderColor: "rgba(255,255,255,0.15)",
    justifyContent: "center",
    alignItems: "center",
  },
  joystickKnob: {
    width: KNOB_RADIUS * 2,
    height: KNOB_RADIUS * 2,
    borderRadius: KNOB_RADIUS,
    backgroundColor: "rgba(255,255,255,0.25)",
    borderWidth: 2,
    borderColor: "rgba(255,255,255,0.3)",
  },
  buttonGrid: {
    alignItems: "center",
  },
  buttonRow: {
    flexDirection: "row",
    alignItems: "center",
  },
  actionBtn: {
    width: 52,
    height: 52,
    borderRadius: 26,
    backgroundColor: "rgba(255,255,255,0.08)",
    borderWidth: 2,
    borderColor: "rgba(255,255,255,0.2)",
    justifyContent: "center",
    alignItems: "center",
  },
  actionBtnText: {
    fontSize: 18,
    fontWeight: "700",
  },
  btnGap: {
    width: 20,
  },
  spacer: {
    width: 52,
  },
  shoulderLeft: {
    position: "absolute",
    top: 8,
    left: 8,
    flexDirection: "row",
    gap: 6,
  },
  shoulderRight: {
    position: "absolute",
    top: 8,
    right: 8,
    flexDirection: "row",
    gap: 6,
  },
  shoulderBtn: {
    paddingHorizontal: 16,
    paddingVertical: 8,
    borderRadius: 8,
    backgroundColor: "rgba(0,0,0,0.4)",
    borderWidth: 1.5,
    borderColor: "rgba(255,255,255,0.2)",
  },
  shoulderText: {
    color: "#ccc",
    fontSize: 13,
    fontWeight: "600",
  },
  centerBtns: {
    position: "absolute",
    bottom: 80,
    left: "50%",
    marginLeft: -60,
    flexDirection: "row",
    gap: 16,
  },
  centerBtn: {
    paddingHorizontal: 12,
    paddingVertical: 5,
    borderRadius: 6,
    borderWidth: 1.5,
    borderColor: "rgba(255,255,255,0.15)",
    backgroundColor: "rgba(0,0,0,0.4)",
  },
  centerBtnText: {
    color: "#999",
    fontSize: 10,
    fontWeight: "600",
    letterSpacing: 1,
    textTransform: "uppercase",
  },
});
