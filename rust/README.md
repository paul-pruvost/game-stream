# GameStream — réécriture Rust (en cours)

Réécriture progressive des composants performance/réseau en Rust. Objectif :
binaires statiques uniques (zéro dépendance runtime, aucune installation côté
utilisateur — idéal derrière un proxy d'entreprise).

## État

| Composant | État | Note |
|---|---|---|
| `relay/` | ✅ fonctionnel | Relay TCP host↔client. Drop-in du `relay.py` Python (protocole identique). Interop validée. **Binaire unique autonome.** |
| `client/` | 🚧 pipeline réseau+décode | TLS (SChannel) + handshake + AES-256-GCM + réassemblage UDP + décode openh264. Interop validée contre `host.py` (`gamestream-headless`). **Reste : rendu GPU, capture input, audio.** |
| host | ⏳ à venir | Capture (Desktop Duplication) + encode matériel + injection input |

### Décodage H.264 sans FFmpeg/MSVC
Le client décode via `openh264` (C++ compilé depuis les sources avec le g++ de
WinLibs) — pas besoin de FFmpeg, nasm, ni VS Build Tools. Comme openh264 est en
C++, `gamestream-headless.exe` se distribue avec 3 DLLs runtime MinGW
(`libstdc++-6`, `libwinpthread-1`, `libgcc_s_seh-1`), copiées automatiquement par
`build.ps1`. Le relay, lui, est un exe unique sans dépendance.

> Le relay HTTP/WebSocket mobile (port 9951) n'est pas encore porté. Utiliser le
> `relay.py` Python pour le mobile en attendant.

## Prérequis de build (Windows, derrière Netskope)

Le relay n'utilise **aucune dépendance native** → toolchain **GNU** (pas besoin
des Visual Studio Build Tools).

1. **Rust (toolchain GNU)** :
   ```powershell
   # rustup-init téléchargé via le store de certificats Windows (fait confiance au CA Netskope)
   rustup-init.exe -y --default-host x86_64-pc-windows-gnu --profile minimal
   rustup component add rust-mingw
   ```
2. **MinGW-w64 complet** (le `dlltool`/`as` du bundle rustup est incomplet) :
   ```powershell
   winget install -e --id BrechtSanders.WinLibs.POSIX.MSVCRT
   ```
3. **CA Netskope** pour que cargo atteigne crates.io :
   `CARGO_HTTP_CAINFO = C:\Apps\Netskope\certifi_plus_netskope.pem`

## Compiler

```powershell
./rust/build.ps1            # configure l'environnement et compile en release
```

Le binaire est produit dans `$env:USERPROFILE\.gamestream-target\release\gamestream-relay.exe`
(répertoire de build hors du projet pour éviter le verrouillage de fichiers par
l'antivirus / OneDrive).

## Lancer

```powershell
gamestream-relay.exe --port 9950
# puis, côté Python :
python host.py   --relay <ip>:9950 --room XXXX
python client.py --relay <ip>:9950 --room XXXX
```
