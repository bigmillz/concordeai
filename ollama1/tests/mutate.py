#!/usr/bin/env python3
"""Mutation checks: break each key protection on purpose, in a copy of the
kit, and make sure the tests notice. A mutant the tests don't kill means a
protection has no test watching it.

    python3 tests/mutate.py            # all mutants
    python3 tests/mutate.py skew       # the ones whose name contains "skew"
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(HERE)

# (name, file, original text, mutated text, test modules that must fail)
MUTANTS = [
    ("sig: timestamp skew not checked", "lib/o1auth.py",
     "if abs(now - ts) > self.skew:", "if False:", ["test_gateway"]),
    ("sig: nonce replay not checked", "lib/o1auth.py",
     'if not self.nonces.check_and_add(dev_id + ":" + nonce, now):', "if False:", ["test_gateway"]),
    ("sig: signature not verified", "lib/o1auth.py",
     'if not ed25519_verify(dev["public_key"], msg, sig):', "if False:", ["test_gateway", "test_vectors"]),
    ("sig: unpaired device accepted", "lib/o1auth.py",
     "dev = self.devices().get(dev_id)",
     "dev = self.devices().get(dev_id) or next(iter(self.devices().values()), None)", ["test_gateway"]),
    ("sig: body not covered", "lib/o1auth.py",
     '"ollama1-req-v1", method.upper(), path, body_sha_hex,', '"ollama1-req-v1", method.upper(), path, "",',
     ["test_gateway", "test_vectors"]),
    ("sig: pre-restart signatures accepted", "lib/o1auth.py",
     "if ts < self.start_time:", "if False:", ["test_gateway"]),
    ("jwt: signature not verified", "lib/o1jwt.py",
     "if not rsa_pkcs1_sha256_verify(", "if False and not rsa_pkcs1_sha256_verify(", ["test_gateway", "test_jwt"]),
    ("jwt: audience not checked", "lib/o1jwt.py",
     "if not any(isinstance(a, str) and hmac.compare_digest(a, self.aud) for a in auds):", "if False:",
     ["test_gateway", "test_jwt"]),
    ("jwt: issuer not checked", "lib/o1jwt.py", "if payload.get(\"iss\") != self.issuer:", "if False:",
     ["test_gateway"]),
    ("jwt: expiry not checked", "lib/o1jwt.py", "now > exp + LEEWAY", "now > exp + 10**9",
     ["test_gateway", "test_jwt"]),
    ("jwt: not-before not checked", "lib/o1jwt.py", "now + LEEWAY < nbf", "False", ["test_gateway"]),
    ("jwt: http certs URL allowed", "lib/o1jwt.py", 'if not url.startswith("https://") and not allow_insecure:',
     "if False:", ["test_jwt"]),
    ("gateway: service token not pinned", "bin/ollama1-gateway",
     'if want and claims.get("common_name") != want:', "if False:", ["test_gateway"]),
    ("gateway: host not checked", "bin/ollama1-gateway",
     'if host != self.cfg["hostname_gateway"].lower():', "if False:", ["test_gateway"]),
    ("gateway: LAN listener open when LAN mode is off", "bin/ollama1-gateway",
     'if not self.cfg.get("lan_mode"):', "if False:", ["test_gateway"]),
    ("gateway: LAN listener open to any address", "bin/ollama1-gateway",
     "if ipaddress.ip_address(client_ip) not in self.lan_net:", "if False:", ["test_gateway"]),
    ("gpu: fit estimate ignored", "bin/ollama1-gateway", "if need > budget:", "if False:", ["test_gateway"]),
    ("gpu: CPU spill not refused", "bin/ollama1-gateway", "if share < 1.0:", "if False:", ["test_gateway"]),
    ("gpu: load options passed through", "bin/ollama1-gateway",
     "clean = {k: v for k, v in opts.items() if k in SAMPLING_OPTIONS}", "clean = dict(opts)", ["test_gateway"]),
    ("gpu: cloud models allowed", "lib/o1ollama.py",
     'if entry.get("remote_model") or entry.get("remote_host"):', "if False:", ["test_gateway"]),
    ("pair: wrong-code limit gone", "bin/ollama1-gateway", "if left <= 0:", "if False:", ["test_gateway"]),
    ("pair: rate limit gone", "bin/ollama1-gateway",
     'if now - st["last_try"] < PAIR_MIN_INTERVAL:', "if False:", ["test_gateway"]),
    ("pair: window reusable after a success", "bin/ollama1-gateway",
     'if st["used"] or st["failures"] >= PAIR_MAX_FAILURES:', 'if st["failures"] >= PAIR_MAX_FAILURES:',
     ["test_gateway"]),
    ("pair: gateway skips the code", "bin/ollama1-gateway",
     'if not check_pair_mac(w["code"], name, pub_b64, ts, nonce, mac):', "if False:", ["test_gateway"]),
    ("pair: root commit skips the code", "lib/o1pair.py",
     'if not check_pair_mac(w["code"], name, pub_b64, ts, nonce, mac):', "if False:", ["test_gateway"]),
    ("pair: window never expires", "lib/o1pair.py",
     'if float(w["expires_at"]) <= now or not w["id"] or not w["code"]:', "if False:", ["test_gateway"]),
    ("stateless: request body written to disk", "bin/ollama1-gateway",
     "            name, entry = gw.local_model(obj.get(\"model\"))\n",
     "            name, entry = gw.local_model(obj.get(\"model\"))\n"
     "            open(os.path.join(Paths.gw_state, 'last.json'), 'w').write(json.dumps(obj))\n",
     ["test_stateless"]),
    ("stateless: answer logged", "bin/ollama1-gateway",
     "                    self.chunk(line)\n",
     "                    self.chunk(line)\n                    log(\"debug\", reason=line)\n",
     ["test_stateless"]),
    ("stateless: default request log back on", "bin/ollama1-gateway",
     "        def log_message(self, fmt, *args):  # the default logs request lines; we log our own\n            pass",
     "        def log_message(self, fmt, *args):  # the default logs request lines; we log our own\n"
     "            BaseHTTPRequestHandler.log_message(self, fmt, *args)",
     ["test_stateless"]),
    ("admin: any Access user accepted", "bin/ollama1-admin",
     "if not email or not hmac.compare_digest(email.encode(), want.encode()):", "if not email:",
     ["test_admin"]),
    ("admin: CSRF token not checked", "bin/ollama1-admin",
     "if not hmac.compare_digest(tok.encode(), self.csrf(email).encode()):", "if False:", ["test_admin"]),
    ("admin: Origin not checked", "bin/ollama1-admin",
     'if (headers.get("Origin") or "").lower() != self.origin:', "if False:", ["test_admin"]),
    ("admin: pull beyond the allow-list", "bin/ollama1-admin", "if arg not in allowed:", "if False:",
     ["test_admin"]),
    ("admin: reboot without confirmation", "bin/ollama1-admin",
     'if act == "reboot" and obj.get("confirm") != "reboot":', "if False:", ["test_admin"]),
    ("updater: checksum ignored", "bin/ollama1-update-ollama", "if got != want:", "if False:", ["test_updater"]),
    ("updater: GitHub digest ignored", "bin/ollama1-update-ollama",
     'if gh and gh != "sha256:" + want:', "if False:", ["test_updater"]),
    ("updater: archive paths trusted", "bin/ollama1-update-ollama",
     'tf.extractall(dest, filter="data")', 'tf.extractall(dest, filter="fully_trusted")', ["test_updater"]),
    ("updater: missing checksum line accepted", "bin/ollama1-update-ollama",
     "            if not want:\n", "            if False:\n", ["test_updater"]),
]


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    survived = []
    ran = 0
    for name, rel, old, new, tests in MUTANTS:
        if only and only not in name:
            continue
        ran += 1
        work = tempfile.mkdtemp(prefix="o1mut-")
        try:
            for d in ("lib", "bin", "config", "systemd"):
                shutil.copytree(os.path.join(KIT, d), os.path.join(work, d))
            path = os.path.join(work, rel)
            src = open(path).read()
            if src.count(old) != 1:
                print("BROKEN  %-50s  (the original text appears %d times)" % (name, src.count(old)))
                survived.append(name)
                continue
            open(path, "w").write(src.replace(old, new))
            env = dict(os.environ, OLLAMA1_TEST_LIB=os.path.join(work, "lib"),
                       OLLAMA1_TEST_BIN=os.path.join(work, "bin"), PYTHONWARNINGS="ignore")
            env.pop("OLLAMA1_PREFIX", None)
            r = subprocess.run([sys.executable, "-m", "unittest"] + tests, cwd=HERE, env=env,
                               capture_output=True, text=True, timeout=600)
            if r.returncode == 0:
                print("SURVIVED %-50s  (%s still pass)" % (name, " ".join(tests)))
                survived.append(name)
            else:
                print("killed  %s" % name)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    print("\n%d mutants, %d killed, %d survived" % (ran, ran - len(survived), len(survived)))
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
