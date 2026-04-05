import React, { useState, useCallback } from "react";
import { View, Text, StyleSheet, ScrollView } from "react-native";
import { StatusBar } from "expo-status-bar";
import ConnectScreen from "./src/ConnectScreen";
import StreamScreen from "./src/StreamScreen";

// Error boundary to surface real crash messages
class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <View style={ebStyles.container}>
          <Text style={ebStyles.title}>App Error</Text>
          <ScrollView style={ebStyles.scroll}>
            <Text style={ebStyles.msg}>{this.state.error.message}</Text>
            <Text style={ebStyles.stack}>
              {this.state.error.stack?.slice(0, 2000)}
            </Text>
          </ScrollView>
        </View>
      );
    }
    return this.props.children;
  }
}

const ebStyles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: "#111",
    padding: 24,
    paddingTop: 60,
  },
  title: { color: "#f44", fontSize: 20, fontWeight: "700", marginBottom: 12 },
  scroll: { flex: 1 },
  msg: { color: "#fff", fontSize: 14, marginBottom: 12 },
  stack: { color: "#888", fontSize: 11 },
});

function AppInner() {
  const [wsUrl, setWsUrl] = useState<string | null>(null);

  const handleConnect = useCallback((url: string) => {
    setWsUrl(url);
  }, []);

  const handleDisconnect = useCallback(() => {
    setWsUrl(null);
  }, []);

  return (
    <>
      <StatusBar hidden={!!wsUrl} style="light" />
      {wsUrl ? (
        <StreamScreen wsUrl={wsUrl} onDisconnect={handleDisconnect} />
      ) : (
        <ConnectScreen onConnect={handleConnect} />
      )}
    </>
  );
}

export default function App() {
  return (
    <ErrorBoundary>
      <AppInner />
    </ErrorBoundary>
  );
}
