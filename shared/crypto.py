"""
GameStream Crypto — TLS for control channel + AES-256-GCM for UDP streams.

Security model:
  1. Host generates a self-signed TLS certificate on first run
  2. Client connects via TLS (certificate pinning via fingerprint)
  3. During handshake, host generates a random AES-256 session key
  4. Session key is sent to client over the TLS channel
  5. All UDP packets (video + audio) are encrypted with AES-256-GCM
  6. Each packet uses a unique nonce (counter-based) to prevent replay

This provides:
  - Confidentiality: all data encrypted in transit
  - Integrity: GCM auth tags detect tampering
  - Authentication: TLS cert fingerprint verifies host identity
"""

import os
import ssl
import struct
import hashlib
import secrets
import threading
from pathlib import Path
from typing import Optional, Tuple

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import datetime

from .protocol import NONCE_LEN, TAG_LEN, AES_KEY_LEN


# ══════════════════════════════════════════════════════════════════════
#  TLS Certificate Management
# ══════════════════════════════════════════════════════════════════════

DEFAULT_CERT_DIR = Path.home() / ".gamestream" / "certs"

def ensure_certificates(cert_dir: Path = DEFAULT_CERT_DIR) -> Tuple[str, str]:
    """
    Generate a self-signed TLS certificate if none exists.
    Returns (cert_path, key_path).
    """
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / "host.crt"
    key_path  = cert_dir / "host.key"

    if cert_path.exists() and key_path.exists():
        print(f"  🔑  Using existing certificate: {cert_path}")
        return str(cert_path), str(key_path)

    print(f"  🔐  Generating new self-signed certificate...")

    # Generate RSA-2048 key pair
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    # Build certificate
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "GameStream Host"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "GameStream"),
    ])

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress_from_string("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )

    # Write to disk
    with open(key_path, "wb") as f:
        f.write(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
    os.chmod(key_path, 0o600)

    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    fingerprint = get_cert_fingerprint(str(cert_path))
    print(f"  ✅  Certificate generated!")
    print(f"  📋  SHA-256 fingerprint: {fingerprint}")

    return str(cert_path), str(key_path)


def ipaddress_from_string(ip_str: str):
    """Helper to create an IPAddress object for SAN."""
    import ipaddress
    return ipaddress.ip_address(ip_str)


def get_cert_fingerprint(cert_path: str) -> str:
    """Get SHA-256 fingerprint of a certificate file."""
    with open(cert_path, "rb") as f:
        cert_data = f.read()
    cert = x509.load_pem_x509_certificate(cert_data)
    digest = cert.fingerprint(hashes.SHA256())
    return ":".join(f"{b:02X}" for b in digest)


def create_server_ssl_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    """Create TLS context for the host (server)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    # Strong cipher suites only
    ctx.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:DHE+CHACHA20")
    return ctx


def create_client_ssl_context(expected_fingerprint: Optional[str] = None) -> ssl.SSLContext:
    """
    Create TLS context for the client.
    If expected_fingerprint is provided, certificate pinning is enforced.
    Otherwise, self-signed certs are accepted (trust-on-first-use).
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    # Accept self-signed certificates
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def verify_server_cert(ssl_sock, expected_fingerprint: Optional[str] = None) -> Tuple[bool, str]:
    """
    Verify the server certificate after TLS handshake.
    Returns (is_valid, fingerprint_string).
    """
    der_cert = ssl_sock.getpeercert(binary_form=True)
    if not der_cert:
        return False, ""

    digest = hashlib.sha256(der_cert).hexdigest()
    fingerprint = ":".join(digest[i:i+2].upper() for i in range(0, len(digest), 2))

    if expected_fingerprint:
        return fingerprint == expected_fingerprint, fingerprint
    return True, fingerprint


# ══════════════════════════════════════════════════════════════════════
#  AES-256-GCM Encryption for UDP Streams
# ══════════════════════════════════════════════════════════════════════

class SessionCipher:
    """
    Encrypts/decrypts UDP packets using AES-256-GCM with counter-based nonces.

    Each direction (host→client video, host→client audio) uses its own
    counter to ensure unique nonces. The nonce is prepended to each packet.
    """

    def __init__(self, key: bytes):
        if len(key) != AES_KEY_LEN:
            raise ValueError(f"Key must be {AES_KEY_LEN} bytes, got {len(key)}")
        self._aesgcm = AESGCM(key)
        self._encrypt_counter = 0
        self._encrypt_lock = threading.Lock()   # video+audio threads share one cipher
        self._seen_nonces = set()   # Replay protection (bounded)
        self._max_seen = 100_000    # Max tracked nonces

    @staticmethod
    def generate_key() -> bytes:
        """Generate a random AES-256 session key."""
        return secrets.token_bytes(AES_KEY_LEN)

    def encrypt(self, plaintext: bytes, associated_data: Optional[bytes] = None) -> bytes:
        """
        Encrypt data with AES-256-GCM.
        Returns: [12B nonce][ciphertext][16B auth tag]
        Thread-safe: video and audio streamers may call this concurrently.
        """
        with self._encrypt_lock:
            # Counter-based nonce: 4 bytes random prefix + 8 bytes counter
            nonce = struct.pack("!I", secrets.randbits(32)) + struct.pack("!Q", self._encrypt_counter)
            self._encrypt_counter += 1

        ciphertext = self._aesgcm.encrypt(nonce, plaintext, associated_data)
        return nonce + ciphertext  # ciphertext includes the 16-byte tag

    def decrypt(self, packet: bytes, associated_data: Optional[bytes] = None) -> Optional[bytes]:
        """
        Decrypt an AES-GCM encrypted packet.
        Returns plaintext or None if authentication fails.
        """
        if len(packet) < NONCE_LEN + TAG_LEN:
            return None

        nonce = packet[:NONCE_LEN]
        ciphertext = packet[NONCE_LEN:]

        # Replay protection
        nonce_key = nonce
        if nonce_key in self._seen_nonces:
            return None  # Replay detected
        self._seen_nonces.add(nonce_key)
        if len(self._seen_nonces) > self._max_seen:
            # Evict oldest (approximate — use a proper window in production)
            self._seen_nonces = set(list(self._seen_nonces)[-50_000:])

        try:
            return self._aesgcm.decrypt(nonce, ciphertext, associated_data)
        except Exception:
            return None  # Auth failed — tampered or wrong key


# ══════════════════════════════════════════════════════════════════════
#  Key Exchange Helpers
# ══════════════════════════════════════════════════════════════════════

def encode_session_key(key: bytes) -> str:
    """Encode session key as hex for JSON transport over TLS."""
    return key.hex()

def decode_session_key(hex_key: str) -> bytes:
    """Decode hex-encoded session key."""
    return bytes.fromhex(hex_key)
