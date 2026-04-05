# 🎮 GameStream

Stream ton écran et prends le contrôle d'un PC distant depuis un autre PC ou un téléphone — chiffré, faible latence, sans logiciel tiers.

---

## Installation

```bash
pip install -r requirements.txt
```

> **Windows** — pour la capture DRM-compatible (YouTube, Netflix…), décommenter `dxcam` dans `requirements.txt` puis :
> ```bash
> pip install dxcam
> ```

---

## Composants

| Fichier | Rôle |
|---|---|
| `host/host.py` | PC dont l'écran est streamé |
| `client/client.py` | PC qui regarde et contrôle |
| `mobile/gateway.py` | Accès depuis un navigateur mobile |
| `relay.py` | Serveur relais pour les connexions internet |

---

## PC → PC

### Même réseau (LAN)

**PC hôte** (celui dont on stream l'écran) :
```bash
python host/host.py
```
L'IP locale est affichée au démarrage, ex : `192.168.1.10`

**PC client** :
```bash
python client/client.py 192.168.1.10
```

Découverte automatique sur le LAN (sans taper l'IP) :
```bash
python client/client.py --auto
```

---

### Réseaux différents (internet)

Un serveur relais doit tourner sur une machine accessible depuis internet.  
Option simple : le faire tourner sur le PC hôte avec le **port 9950 TCP ouvert** sur ta box (redirection de port).

**1. Sur le PC hôte — lancer le relay :**
```bash
python relay.py
```

**2. Sur le PC hôte — lancer le stream :**
```bash
python host/host.py --relay <IP_PUBLIQUE>:9950
```
Un **code room** est affiché (ex : `AB12`). Communique-le au client.

**3. Sur le PC client :**
```bash
python client/client.py --relay <IP_PUBLIQUE>:9950 --room AB12
```

> Trouver ton IP publique : `curl ifconfig.me`

---

## Mobile (navigateur)

### Même réseau (LAN)

```bash
python mobile/gateway.py
```

L'URL complète est affichée :
```
http://192.168.1.10:8080/?token=a1b2c3d4
```
Ouvre-la sur ton téléphone.

---

### Réseaux différents (internet)

Ports à ouvrir sur ta box : **9950 TCP** et **9951 TCP**.

**1. Lancer le relay :**
```bash
python relay.py
```

**2. Lancer la gateway :**
```bash
python mobile/gateway.py --relay <IP_PUBLIQUE>:9951
```

L'URL à ouvrir sur le téléphone est affichée :
```
http://<IP_PUBLIQUE>:9951/AB12/?token=a1b2c3d4
```

---

## Options principales

### host.py

| Option | Défaut | Description |
|---|---|---|
| `--fps 60` | 60 | Images par seconde |
| `--bitrate 8000000` | 8 Mbps | Débit vidéo H.264 |
| `--monitor 0` | 0 | Index du moniteur à capturer |
| `--no-audio` | — | Désactiver l'audio |
| `--no-encryption` | — | Désactiver le chiffrement TLS |
| `--sw-encode` | — | Forcer l'encodage logiciel (CPU) |
| `--list-audio` | — | Lister les périphériques audio |
| `--relay host:port` | — | Adresse du serveur relais |
| `--room XXXX` | auto | Code room (généré automatiquement si absent) |

### client.py

| Option | Défaut | Description |
|---|---|---|
| `--auto` | — | Découverte automatique mDNS (LAN) |
| `--fullscreen` | — | Démarrer en plein écran |
| `--grab-mouse` | — | Verrouiller la souris (mode FPS) |
| `--no-audio` | — | Désactiver l'audio |
| `--relay host:port` | — | Adresse du serveur relais |
| `--room XXXX` | — | Code room du serveur relais |

### mobile/gateway.py

| Option | Défaut | Description |
|---|---|---|
| `--fps 30` | 30 | Images par seconde |
| `--scale 0.75` | 0.75 | Résolution (0.5 = moitié) |
| `--quality 55` | 55 | Qualité JPEG (fallback) |
| `--no-h264` | — | Forcer JPEG (anciens navigateurs) |
| `--relay host:port` | — | Adresse du serveur relais |
| `--room XXXX` | auto | Code room (généré automatiquement si absent) |

### relay.py

| Option | Défaut | Description |
|---|---|---|
| `--port 9950` | 9950 | Port TCP — relay PC↔PC |
| `--http-port 9951` | 9951 | Port HTTP — relay mobile |

---

## Contrôles clavier (client desktop)

| Touche | Action |
|---|---|
| `F11` | Basculer plein écran |
| `F10` | Verrouiller / libérer la souris |
| `F9` | Afficher / masquer les stats |
| `Ctrl+Shift+Q` | Quitter |

---

## Architecture

```
┌─────────────┐  TLS TCP :9900   ┌──────────────┐
│  client.py  │◄────────────────►│  host.py     │
│             │  UDP :9901 H.264 │  (PC hôte)   │
│             │◄────────────────  │              │
│             │  UDP :9902 Opus  │              │
└─────────────┘                   └──────────────┘

┌─────────────┐  WS :8080        ┌──────────────┐
│  Téléphone  │◄────────────────►│ gateway.py   │
│  (browser)  │  H.264 / JPEG   │  (PC hôte)   │
└─────────────┘                   └──────────────┘

        ── Mode internet via relay.py ──

┌──────────────────────────────────────────────┐
│                  relay.py                    │
│  TCP  :9950  →  host.py  ↔  client.py        │
│  HTTP :9951  →  gateway.py  ↔  téléphone     │
└──────────────────────────────────────────────┘
```

---

## Sécurité

| Canal | Chiffrement |
|---|---|
| Contrôle PC↔PC | TLS (certificat auto-signé, empreinte vérifiable) |
| Vidéo / Audio PC↔PC | AES-256-GCM |
| Mobile | WebSocket + token d'authentification par session |
| Relay | Aucune donnée stockée — transit uniquement |

---

## Dépannage

**Pas de vidéo sur mobile**
→ Le navigateur bascule automatiquement en JPEG si H.264 n'est pas supporté.
Si ça ne fonctionne pas : relancer avec `--no-h264`.

**Audio non détecté (Windows)**
→ Clic droit icône son → Sons → Enregistrement → clic droit dans la liste → *Afficher les périphériques désactivés* → activer **Mélange stéréo**.

**Encodeur H.264 matériel indisponible**
→ Bascule automatique sur `libx264` (logiciel) puis MJPEG.

**Connexion refusée en mode relay**
→ Vérifier que les ports sont bien ouverts en TCP sur la box (redirection de port vers l'IP locale du PC hôte).
