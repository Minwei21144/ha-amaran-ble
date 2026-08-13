"""Bluetooth Mesh PDU helpers for Amaran BLE Mesh control."""

from __future__ import annotations

from dataclasses import dataclass

from .const import DEFAULT_LOCAL_ADDRESS, DEFAULT_TTL, TELINK_OPCODE

try:
    from cryptography.hazmat.primitives import cmac
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.ciphers.aead import AESCCM
except ImportError as err:  # pragma: no cover - Home Assistant includes cryptography.
    raise RuntimeError("cryptography is required for Amaran BLE Mesh control") from err

PROXY_TYPE_NETWORK = 0x00
PROXY_TYPE_BEACON = 0x01
PROXY_TYPE_PROXY_CONFIG = 0x02

PROXY_CFG_SET_FILTER_TYPE = 0x00
PROXY_CFG_ADD_ADDRESSES = 0x01
PROXY_FILTER_WHITELIST = 0x00


@dataclass(frozen=True)
class MeshKeys:
    """Derived Bluetooth Mesh security material."""

    net_key: bytes
    network_id: bytes
    beacon_key: bytes
    nid: int
    enc_key: bytes
    priv_key: bytes
    aid: int
    app_key: bytes


@dataclass(frozen=True)
class MeshAccessMessage:
    """One decoded unsegmented Bluetooth Mesh access message."""

    src: int
    dst: int
    sequence: int
    opcode: int
    params: bytes


def _aes_ecb_block(key: bytes, data: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(data) + encryptor.finalize()


def _aes_cmac(key: bytes, message: bytes) -> bytes:
    signer = cmac.CMAC(algorithms.AES(key))
    signer.update(message)
    return signer.finalize()


def s1(message: bytes) -> bytes:
    """Bluetooth Mesh s1 derivation helper."""
    return _aes_cmac(bytes(16), message)


def k1(n: bytes, salt: bytes, p: bytes) -> bytes:
    """Derive a 128-bit key with the Bluetooth Mesh k1 function."""
    return _aes_cmac(_aes_cmac(salt, n), p)


def k2(net_key: bytes) -> tuple[int, bytes, bytes]:
    """Derive NID, EncryptionKey, and PrivacyKey from a NetKey."""
    salt = s1(b"smk2")
    t = _aes_cmac(salt, net_key)
    p = b"\x00"
    t1 = _aes_cmac(t, p + b"\x01")
    t2 = _aes_cmac(t, t1 + p + b"\x02")
    t3 = _aes_cmac(t, t2 + p + b"\x03")
    return t1[15] & 0x7F, t2, t3


def k3(net_key: bytes) -> bytes:
    """Derive the 64-bit Bluetooth Mesh Network ID from a NetKey."""
    salt = s1(b"smk3")
    t = _aes_cmac(salt, net_key)
    return _aes_cmac(t, b"id64\x01")[-8:]


def k4(app_key: bytes) -> int:
    """Derive AID from an AppKey."""
    salt = s1(b"smk4")
    t = _aes_cmac(salt, app_key)
    result = _aes_cmac(t, b"id6\x01")
    return result[15] & 0x3F


def derive_keys(net_key_hex: str, app_key_hex: str) -> MeshKeys:
    """Derive all security material needed to send application messages."""
    net_key = bytes.fromhex(net_key_hex)
    app_key = bytes.fromhex(app_key_hex)
    nid, enc_key, priv_key = k2(net_key)
    return MeshKeys(
        net_key=net_key,
        network_id=k3(net_key),
        beacon_key=k1(net_key, s1(b"nkbk"), b"id128\x01"),
        nid=nid,
        enc_key=enc_key,
        priv_key=priv_key,
        aid=k4(app_key),
        app_key=app_key,
    )


def _opcode_bytes(opcode: int) -> bytes:
    first = opcode & 0xFF
    if first < 0x80:
        return bytes([first])
    if first < 0xC0:
        return bytes([first, (opcode >> 8) & 0xFF])
    return bytes([first, (opcode >> 8) & 0xFF, (opcode >> 16) & 0xFF])


def _app_nonce(sequence: int, src: int, dst: int, iv_index: int) -> bytes:
    return bytes(
        [
            0x01,
            0x00,
            (sequence >> 16) & 0xFF,
            (sequence >> 8) & 0xFF,
            sequence & 0xFF,
            (src >> 8) & 0xFF,
            src & 0xFF,
            (dst >> 8) & 0xFF,
            dst & 0xFF,
            (iv_index >> 24) & 0xFF,
            (iv_index >> 16) & 0xFF,
            (iv_index >> 8) & 0xFF,
            iv_index & 0xFF,
        ]
    )


def _net_nonce(ctl: int, ttl: int, sequence: int, src: int, iv_index: int) -> bytes:
    return bytes(
        [
            0x00,
            (ctl << 7) | (ttl & 0x7F),
            (sequence >> 16) & 0xFF,
            (sequence >> 8) & 0xFF,
            sequence & 0xFF,
            (src >> 8) & 0xFF,
            src & 0xFF,
            0x00,
            0x00,
            (iv_index >> 24) & 0xFF,
            (iv_index >> 16) & 0xFF,
            (iv_index >> 8) & 0xFF,
            iv_index & 0xFF,
        ]
    )


def _proxy_nonce(sequence: int, src: int, iv_index: int) -> bytes:
    return bytes(
        [
            0x03,
            0x00,
            (sequence >> 16) & 0xFF,
            (sequence >> 8) & 0xFF,
            sequence & 0xFF,
            (src >> 8) & 0xFF,
            src & 0xFF,
            0x00,
            0x00,
            (iv_index >> 24) & 0xFF,
            (iv_index >> 16) & 0xFF,
            (iv_index >> 8) & 0xFF,
            iv_index & 0xFF,
        ]
    )


def _aes_ccm_encrypt(key: bytes, nonce: bytes, plaintext: bytes, mic_len: int) -> bytes:
    return AESCCM(key, tag_length=mic_len).encrypt(nonce, plaintext, None)


def _aes_ccm_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, mic_len: int) -> bytes:
    return AESCCM(key, tag_length=mic_len).decrypt(nonce, ciphertext, None)


def _pecb(priv_key: bytes, iv_index: int, privacy_random7: bytes) -> bytes:
    block = bytearray(16)
    block[5] = (iv_index >> 24) & 0xFF
    block[6] = (iv_index >> 16) & 0xFF
    block[7] = (iv_index >> 8) & 0xFF
    block[8] = iv_index & 0xFF
    block[9:16] = privacy_random7[:7]
    return _aes_ecb_block(priv_key, bytes(block))


def _network_pdu(
    *,
    nid: int,
    enc_key: bytes,
    priv_key: bytes,
    sequence: int,
    src: int,
    iv_index: int,
    ctl: int,
    ttl: int,
    nonce: bytes,
    net_payload: bytes,
) -> bytes:
    # Bluetooth Mesh control messages use an 8-byte NetMIC. Access messages
    # use four bytes. Amaran 1.0.71's libmesh stack follows this distinction for
    # its Proxy Configuration PDUs.
    net_mic_len = 8 if ctl else 4
    encrypted_payload = _aes_ccm_encrypt(
        enc_key,
        nonce,
        net_payload,
        net_mic_len,
    )
    pecb = _pecb(priv_key, iv_index, encrypted_payload[:7])
    header = bytes(
        [
            (ctl << 7) | (ttl & 0x7F),
            (sequence >> 16) & 0xFF,
            (sequence >> 8) & 0xFF,
            sequence & 0xFF,
            (src >> 8) & 0xFF,
            src & 0xFF,
        ]
    )
    obfuscated = bytes(header[index] ^ pecb[index] for index in range(6))
    ivi = iv_index & 0x01
    return bytes([(ivi << 7) | (nid & 0x7F)]) + obfuscated + encrypted_payload


def build_access_proxy_pdu(
    keys: MeshKeys,
    *,
    sequence: int,
    dst: int,
    params: bytes,
    src: int = DEFAULT_LOCAL_ADDRESS,
    iv_index: int = 0,
    opcode: int = TELINK_OPCODE,
    ttl: int = DEFAULT_TTL,
) -> bytes:
    """Build a Proxy Network PDU for a Telink access message."""
    access_pdu = _opcode_bytes(opcode) + params
    upper = _aes_ccm_encrypt(keys.app_key, _app_nonce(sequence, src, dst, iv_index), access_pdu, 4)
    lower = bytes([0x40 | (keys.aid & 0x3F)]) + upper
    net_payload = dst.to_bytes(2, "big") + lower
    network_pdu = _network_pdu(
        nid=keys.nid,
        enc_key=keys.enc_key,
        priv_key=keys.priv_key,
        sequence=sequence,
        src=src,
        iv_index=iv_index,
        ctl=0,
        ttl=ttl,
        nonce=_net_nonce(0, ttl, sequence, src, iv_index),
        net_payload=net_payload,
    )
    return bytes([PROXY_TYPE_NETWORK]) + network_pdu


def _parse_opcode(access_pdu: bytes) -> tuple[int, bytes] | None:
    """Parse a Bluetooth Mesh access opcode and return opcode plus params."""
    if not access_pdu:
        return None
    first = access_pdu[0]
    if first < 0x80:
        return first, access_pdu[1:]
    if first < 0xC0:
        if len(access_pdu) < 2:
            return None
        return first | (access_pdu[1] << 8), access_pdu[2:]
    if len(access_pdu) < 3:
        return None
    return first | (access_pdu[1] << 8) | (access_pdu[2] << 16), access_pdu[3:]


def parse_access_proxy_pdu(
    keys: MeshKeys,
    data: bytes,
    *,
    iv_index: int = 0,
) -> MeshAccessMessage | None:
    """Decode one unsegmented Proxy Network PDU into an access message.

    Segmented access messages are intentionally ignored for now. The Amaran
    Telink status payloads observed so far fit in one unsegmented access PDU.
    """
    if len(data) < 15 or (data[0] & 0x3F) != PROXY_TYPE_NETWORK:
        return None

    network_pdu = data[1:]
    if (network_pdu[0] & 0x7F) != keys.nid:
        return None

    encrypted_payload = network_pdu[7:]
    if len(encrypted_payload) < 8:
        return None

    iv_candidates = list(dict.fromkeys((iv_index, max(0, iv_index - 1), iv_index + 1)))
    for candidate_iv_index in iv_candidates:
        if (network_pdu[0] >> 7) != (candidate_iv_index & 0x01):
            continue

        try:
            pecb = _pecb(keys.priv_key, candidate_iv_index, encrypted_payload[:7])
            obfuscated = network_pdu[1:7]
            header = bytes(obfuscated[index] ^ pecb[index] for index in range(6))
            ctl_ttl = header[0]
            ctl = (ctl_ttl >> 7) & 0x01
            ttl = ctl_ttl & 0x7F
            sequence = (header[1] << 16) | (header[2] << 8) | header[3]
            src = (header[4] << 8) | header[5]
            net_mic_len = 8 if ctl else 4
            net_plain = _aes_ccm_decrypt(
                keys.enc_key,
                _net_nonce(ctl, ttl, sequence, src, candidate_iv_index),
                encrypted_payload,
                net_mic_len,
            )
        except Exception:
            continue

        if ctl or len(net_plain) < 4:
            continue

        dst = int.from_bytes(net_plain[:2], "big")
        lower_transport = net_plain[2:]
        if len(lower_transport) < 2:
            continue

        first_lower = lower_transport[0]
        if first_lower & 0x80:
            continue
        if not first_lower & 0x40:
            continue
        if (first_lower & 0x3F) != keys.aid:
            continue

        encrypted_upper = lower_transport[1:]
        try:
            access_pdu = _aes_ccm_decrypt(
                keys.app_key,
                _app_nonce(sequence, src, dst, candidate_iv_index),
                encrypted_upper,
                4,
            )
        except Exception:
            continue

        parsed_opcode = _parse_opcode(access_pdu)
        if parsed_opcode is None:
            continue
        opcode, params = parsed_opcode
        return MeshAccessMessage(
            src=src,
            dst=dst,
            sequence=sequence,
            opcode=opcode,
            params=params,
        )

    return None


def build_proxy_config_pdu(
    keys: MeshKeys,
    *,
    sequence: int,
    opcode: int,
    params: bytes,
    src: int = DEFAULT_LOCAL_ADDRESS,
    iv_index: int = 0,
) -> bytes:
    """Build a Proxy Configuration PDU."""
    transport_pdu = bytes([opcode & 0x7F]) + params
    net_payload = b"\x00\x00" + transport_pdu
    network_pdu = _network_pdu(
        nid=keys.nid,
        enc_key=keys.enc_key,
        priv_key=keys.priv_key,
        sequence=sequence,
        src=src,
        iv_index=iv_index,
        ctl=1,
        ttl=0,
        nonce=_proxy_nonce(sequence, src, iv_index),
        net_payload=net_payload,
    )
    return bytes([PROXY_TYPE_PROXY_CONFIG]) + network_pdu


def build_set_filter_type_pdu(keys: MeshKeys, *, sequence: int, iv_index: int = 0) -> bytes:
    """Build the Proxy Config Set Filter Type PDU used by Amaran lights."""
    return build_proxy_config_pdu(
        keys,
        sequence=sequence,
        opcode=PROXY_CFG_SET_FILTER_TYPE,
        params=bytes([PROXY_FILTER_WHITELIST]),
        iv_index=iv_index,
    )


def build_add_addresses_pdu(
    keys: MeshKeys,
    *,
    sequence: int,
    addresses: list[int],
    iv_index: int = 0,
) -> bytes:
    """Build the Proxy Config Add Addresses PDU."""
    payload = b"".join((address & 0xFFFF).to_bytes(2, "big") for address in addresses)
    return build_proxy_config_pdu(
        keys,
        sequence=sequence,
        opcode=PROXY_CFG_ADD_ADDRESSES,
        params=payload,
        iv_index=iv_index,
    )


def build_secure_network_beacon(
    keys: MeshKeys,
    *,
    iv_index: int = 0,
    flags: int = 0,
) -> bytes:
    """Build the Secure Network Beacon sent by Amaran 1.0.71 on login."""
    beacon_body = (
        bytes([flags & 0xFF])
        + keys.network_id
        + (iv_index & 0xFFFFFFFF).to_bytes(4, "big")
    )
    authentication = _aes_cmac(keys.beacon_key, beacon_body)[:8]
    return bytes([PROXY_TYPE_BEACON, 0x01]) + beacon_body + authentication


def parse_iv_index_from_proxy(data: bytes) -> int | None:
    """Parse IV Index from a Secure Network Beacon proxy notification."""
    if len(data) < 23:
        return None
    if (data[0] & 0x3F) != PROXY_TYPE_BEACON or data[1] != 0x01:
        return None
    return int.from_bytes(data[11:15], "big")
