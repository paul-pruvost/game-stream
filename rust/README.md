# GameStream — réécriture Rust (en cours)

Réécriture progressive des composants performance/réseau en Rust. Objectif :
binaires statiques uniques (zéro dépendance runtime, aucune installation côté
utilisateur — idéal derrière un proxy d'entreprise).

## État

| Composant | État | Note |
|---|---|---|
| `relay/` | ✅ fonctionnel | Relay TCP host↔client. Drop-in du `relay.py` Python (protocole identique). Interop validée avec le host/client Python. |
| host | ⏳ à venir | Capture (Desktop Duplication) + encode matériel + injection input |
| client | ⏳ à venir | Réception + décode + rendu GPU + capture input |

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
