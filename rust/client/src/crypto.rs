//! AES-256-GCM stream decryption, matching shared/crypto.py (SessionCipher).
//!
//! Packet layout: [12B nonce][ciphertext][16B GCM tag]. The Python side uses
//! `cryptography`'s AESGCM, which appends the tag to the ciphertext — exactly
//! what the RustCrypto `aes-gcm` AEAD trait expects.

use aes_gcm::aead::{Aead, KeyInit};
use aes_gcm::{Aes256Gcm, Key, Nonce};

const NONCE_LEN: usize = 12;
const TAG_LEN: usize = 16;

pub struct SessionCipher {
    cipher: Aes256Gcm,
}

impl SessionCipher {
    /// `key` must be 32 bytes (AES-256).
    pub fn new(key: &[u8]) -> Option<Self> {
        if key.len() != 32 {
            return None;
        }
        let key = Key::<Aes256Gcm>::from_slice(key);
        Some(Self {
            cipher: Aes256Gcm::new(key),
        })
    }

    /// Decrypt `[nonce][ciphertext+tag]`; returns plaintext or None on auth fail.
    pub fn decrypt(&self, packet: &[u8]) -> Option<Vec<u8>> {
        if packet.len() < NONCE_LEN + TAG_LEN {
            return None;
        }
        let nonce = Nonce::from_slice(&packet[..NONCE_LEN]);
        self.cipher.decrypt(nonce, &packet[NONCE_LEN..]).ok()
    }
}

/// Decode a hex string (the session_key from the host config) into bytes.
pub fn hex_decode(s: &str) -> Option<Vec<u8>> {
    if s.len() % 2 != 0 {
        return None;
    }
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).ok())
        .collect()
}
