import React, { useState, useEffect } from "react";
import {
  View,
  Text,
  TextInput,
  TouchableOpacity,
  StyleSheet,
  ScrollView,
  Alert,
  KeyboardAvoidingView,
  Platform,
} from "react-native";
import AsyncStorage from "@react-native-async-storage/async-storage";

const STORAGE_KEY = "gamestream_servers";

interface SavedServer {
  address: string;
  token: string;
  label?: string;
  lastUsed: number;
}

interface Props {
  onConnect: (wsUrl: string) => void;
}

export default function ConnectScreen({ onConnect }: Props) {
  const [address, setAddress] = useState("");
  const [token, setToken] = useState("");
  const [savedServers, setSavedServers] = useState<SavedServer[]>([]);

  useEffect(() => {
    loadServers();
  }, []);

  async function loadServers() {
    try {
      const raw = await AsyncStorage.getItem(STORAGE_KEY);
      if (raw) {
        const servers: SavedServer[] = JSON.parse(raw);
        servers.sort((a, b) => b.lastUsed - a.lastUsed);
        setSavedServers(servers);
      }
    } catch {}
  }

  async function saveServer(addr: string, tok: string) {
    try {
      let servers = [...savedServers];
      const idx = servers.findIndex((s) => s.address === addr);
      if (idx >= 0) {
        servers[idx].token = tok;
        servers[idx].lastUsed = Date.now();
      } else {
        servers.push({ address: addr, token: tok, lastUsed: Date.now() });
      }
      servers.sort((a, b) => b.lastUsed - a.lastUsed);
      servers = servers.slice(0, 10);
      await AsyncStorage.setItem(STORAGE_KEY, JSON.stringify(servers));
      setSavedServers(servers);
    } catch {}
  }

  async function removeServer(addr: string) {
    const servers = savedServers.filter((s) => s.address !== addr);
    await AsyncStorage.setItem(STORAGE_KEY, JSON.stringify(servers));
    setSavedServers(servers);
  }

  function buildWsUrl(addr: string, tok: string): string {
    let a = addr.trim();
    let t = tok.trim();

    // If user pasted a full URL like http://host:port/?token=xxx
    const urlMatch = a.match(
      /^https?:\/\/([^/]+)(\/[^?]*)?\?.*token=([a-fA-F0-9]+)/
    );
    if (urlMatch) {
      const host = urlMatch[1];
      const path = urlMatch[2] || "";
      t = urlMatch[3];
      // Detect relay URL pattern: /ROOM/
      const roomMatch = path.match(/^\/([A-Fa-f0-9]{4,})\/?$/);
      if (roomMatch) {
        return `ws://${host}/ws/${roomMatch[1]}?token=${t}`;
      }
      return `ws://${host}/ws?token=${t}`;
    }

    // Plain address: check for /ROOM suffix (relay mode)
    const relayMatch = a.match(/^([^/]+)\/([A-Fa-f0-9]{4,})\/?$/);
    if (relayMatch) {
      return `ws://${relayMatch[1]}/ws/${relayMatch[2]}?token=${t}`;
    }

    // Direct connection
    if (!a.includes(":")) {
      a += ":8080";
    }
    return `ws://${a}/ws?token=${t}`;
  }

  function handleConnect() {
    if (!address.trim()) {
      Alert.alert("Error", "Enter the server address");
      return;
    }
    if (!token.trim() && !address.includes("token=")) {
      Alert.alert("Error", "Enter the token");
      return;
    }

    const url = buildWsUrl(address, token);
    saveServer(address.trim(), token.trim());
    onConnect(url);
  }

  function handleSelectServer(server: SavedServer) {
    setAddress(server.address);
    setToken(server.token);
  }

  return (
    <KeyboardAvoidingView
      style={styles.container}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
    >
      <ScrollView
        contentContainerStyle={styles.scroll}
        keyboardShouldPersistTaps="handled"
      >
        <Text style={styles.title}>GameStream</Text>
        <Text style={styles.subtitle}>Remote Play</Text>

        <View style={styles.form}>
          <Text style={styles.label}>Server address</Text>
          <TextInput
            style={styles.input}
            value={address}
            onChangeText={setAddress}
            placeholder="192.168.1.5:8080  or  relay:9951/ABCD"
            placeholderTextColor="#666"
            autoCapitalize="none"
            autoCorrect={false}
            keyboardType="url"
          />

          <Text style={styles.label}>Token</Text>
          <TextInput
            style={styles.input}
            value={token}
            onChangeText={setToken}
            placeholder="a1b2c3d4"
            placeholderTextColor="#666"
            autoCapitalize="none"
            autoCorrect={false}
          />

          <TouchableOpacity style={styles.connectBtn} onPress={handleConnect}>
            <Text style={styles.connectBtnText}>Connect</Text>
          </TouchableOpacity>
        </View>

        {savedServers.length > 0 && (
          <View style={styles.savedSection}>
            <Text style={styles.savedTitle}>Recent servers</Text>
            {savedServers.map((server) => (
              <TouchableOpacity
                key={server.address}
                style={styles.savedItem}
                onPress={() => handleSelectServer(server)}
                onLongPress={() => {
                  Alert.alert("Remove", `Remove ${server.address}?`, [
                    { text: "Cancel", style: "cancel" },
                    {
                      text: "Remove",
                      style: "destructive",
                      onPress: () => removeServer(server.address),
                    },
                  ]);
                }}
              >
                <Text style={styles.savedAddr}>{server.address}</Text>
                <Text style={styles.savedToken}>
                  token: {server.token.slice(0, 4)}...
                </Text>
              </TouchableOpacity>
            ))}
          </View>
        )}

        <View style={styles.helpSection}>
          <Text style={styles.helpTitle}>How to connect</Text>
          <Text style={styles.helpText}>
            1. Start GameStream on your PC (Mobile mode){"\n"}
            2. Enter the displayed address and token{"\n"}
            3. Or paste the full URL shown on the PC{"\n"}
            {"\n"}
            For relay: use address format relay:9951/ROOM
          </Text>
        </View>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: "#111",
  },
  scroll: {
    flexGrow: 1,
    justifyContent: "center",
    padding: 24,
  },
  title: {
    fontSize: 32,
    fontWeight: "700",
    color: "#fff",
    textAlign: "center",
  },
  subtitle: {
    fontSize: 16,
    color: "#888",
    textAlign: "center",
    marginBottom: 32,
  },
  form: {
    backgroundColor: "#1a1a1a",
    borderRadius: 16,
    padding: 20,
    marginBottom: 20,
  },
  label: {
    fontSize: 14,
    color: "#aaa",
    marginBottom: 6,
    marginTop: 12,
  },
  input: {
    backgroundColor: "#222",
    borderRadius: 10,
    padding: 14,
    fontSize: 16,
    color: "#fff",
    borderWidth: 1,
    borderColor: "#333",
  },
  connectBtn: {
    backgroundColor: "#4caf50",
    borderRadius: 10,
    padding: 16,
    marginTop: 20,
    alignItems: "center",
  },
  connectBtnText: {
    color: "#fff",
    fontSize: 17,
    fontWeight: "600",
  },
  savedSection: {
    marginBottom: 20,
  },
  savedTitle: {
    fontSize: 14,
    color: "#888",
    marginBottom: 8,
    textTransform: "uppercase",
    letterSpacing: 1,
  },
  savedItem: {
    backgroundColor: "#1a1a1a",
    borderRadius: 10,
    padding: 14,
    marginBottom: 6,
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
  },
  savedAddr: {
    color: "#fff",
    fontSize: 15,
  },
  savedToken: {
    color: "#666",
    fontSize: 12,
  },
  helpSection: {
    backgroundColor: "#1a1a1a",
    borderRadius: 16,
    padding: 20,
  },
  helpTitle: {
    fontSize: 14,
    color: "#888",
    marginBottom: 8,
    textTransform: "uppercase",
    letterSpacing: 1,
  },
  helpText: {
    fontSize: 13,
    color: "#666",
    lineHeight: 20,
  },
});
