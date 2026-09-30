#!/usr/bin/env python3
"""Builds PROTOCOL.md's test vectors from fixed inputs.

    python3 tests/gen_vectors.py      # prints the JSON block

test_vectors.py checks that PROTOCOL.md carries exactly this output, that
the pure-Python RFC 8032 code and the gateway's backend agree, and that
the gateway's own verifier accepts the vectors.
"""
import hashlib
import json

import o1test_util as U
from o1auth import (body_hash, canonical_request, device_id_for, normalize_code, pair_key,
                    pair_mac, pair_message, pair_proof)


def vectors():
    seed = bytes(range(32))
    pub = U.ed_public(seed)
    dev = device_id_for(pub)
    out = {"device": {"seed_hex": seed.hex(), "public_key_b64url": U.b64u(pub),
                      "device_id": dev}}
    reqs = []
    for method, path, body, ts, nonce in (
        ("POST", "/api/chat",
         b'{"model":"qwen3:8b","messages":[{"role":"user","content":"hello"}],"stream":true}',
         1790000000, U.b64u(bytes(range(16)))),
        ("GET", "/api/tags", b"", 1790000030, "n0nce-0000000000000001"),
        ("GET", "/v1/info", b"", 1790000060, "n0nce-0000000000000002"),
    ):
        canon = canonical_request(method, path, body_hash(body), ts, nonce, dev)
        reqs.append({
            "method": method, "path": path, "body_utf8": body.decode(), "timestamp": ts,
            "nonce": nonce, "body_sha256_hex": hashlib.sha256(body).hexdigest(),
            "canonical_utf8": canon.decode(),
            "headers": {"X-O1-Device": dev, "X-O1-Timestamp": str(ts), "X-O1-Nonce": nonce,
                        "X-O1-Signature": U.b64u(U.ed_sign(seed, canon))},
        })
    out["requests"] = reqs
    code = "7K4M-2QXD-9FHT"
    name = "Patrick's MacBook Pro"
    ts, nonce = 1790000100, U.b64u(bytes(range(16, 32)))
    pub_b64 = U.b64u(pub)
    out["pairing"] = {
        "code_shown": code, "code_normalized": normalize_code(code),
        "key_hex": pair_key(code).hex(), "name": name, "public_key_b64url": pub_b64,
        "timestamp": ts, "nonce": nonce,
        "message_utf8": pair_message(name, pub_b64, ts, nonce).decode(),
        "mac_b64url": U.b64u(pair_mac(code, name, pub_b64, ts, nonce)),
        "request_json": {"name": name, "public_key": pub_b64, "timestamp": ts, "nonce": nonce,
                         "mac": U.b64u(pair_mac(code, name, pub_b64, ts, nonce))},
        "response_device_id": dev,
        "proof_b64url": U.b64u(pair_proof(code, dev, pub_b64, nonce)),
    }
    return out


if __name__ == "__main__":
    print(json.dumps(vectors(), indent=2, ensure_ascii=False))
