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
     'if not self.nonces.check_and_add(dev_id + ":" + pre["nonce"], self.clock()):', "if False:", ["test_gateway"]),
    ("sig: signature not verified", "lib/o1auth.py",
     'if not ed25519_verify(dev["public_key"], msg, pre["sig"]):', "if False:", ["test_gateway", "test_vectors"]),
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
    ("gpu: fit estimate ignored", "bin/ollama1-gateway", "            if need <= budget:\n                return c",
     "            if True:\n                return c", ["test_gateway"]),
    ("gpu: CPU spill not refused", "bin/ollama1-gateway", "if share < 1.0:", "if False:", ["test_gateway"]),
    ("gpu: load options passed through", "bin/ollama1-gateway",
     "clean = {k: v for k, v in opts.items() if k in SAMPLING_OPTIONS}", "clean = dict(opts)", ["test_gateway"]),
    ("gpu: cloud models allowed", "lib/o1ollama.py",
     'if entry.get("remote_model") or entry.get("remote_host"):', "if False:", ["test_gateway"]),
    ("pair: wrong-code limit gone", "lib/o1pair.py", "        if left <= 0:", "        if False:", ["test_gateway"]),
    ("pair: rate limit gone", "bin/ollama1-gateway",
     'if now - gw.pair["last_try"] < PAIR_MIN_INTERVAL:', "if False:", ["test_gateway"]),
    ("pair: window reusable after a success", "lib/o1pair.py",
     '    close_window(w["id"])  # one success closes the window', "    pass", ["test_gateway"]),
    ("pair: root step skips the code", "lib/o1pair.py",
     'if not check_pair_mac(w["code"], name, pub_b64, ts, nonce, mac):', "if False:", ["test_gateway"]),
    ("pair: nonce reuse accepted", "lib/o1pair.py", 'if nonce in w.get("nonces", []):', "if False:",
     ["test_gateway"]),
    ("pair: window never expires", [
        ("lib/o1pair.py", 'if float(w["expires_at"]) <= now or not w["id"] or not w["code"]:', "if False:"),
        ("lib/o1pair.py", 'and w["expires_at"] > now:', ":")], ["test_gateway"]),
    ("pair: allowed on the LAN listener", "bin/ollama1-gateway",
     '            if lan:\n                raise GatewayError(403, "pair_closed"',
     '            if False:\n                raise GatewayError(403, "pair_closed"', ["test_gateway"]),
    ("F1: gateway errors keep the connection open", "bin/ollama1-gateway",
     '                self.send_header("Connection", "close")\n                self.close_connection = True\n',
     "                pass\n", ["test_gateway"]),
    ("F1: panel errors keep the connection open", "bin/ollama1-admin",
     '                self.send_header("Connection", "close")\n                self.close_connection = True\n',
     "                pass\n", ["test_admin"]),
    ("F3: body read before the signature headers", "bin/ollama1-gateway",
     "                    pre = gw.verifier.precheck(method, self.path, self.headers)\n"
     "                    body = self.read_body(max_body)\n",
     "                    body = self.read_body(max_body)\n"
     "                    pre = gw.verifier.precheck(method, self.path, self.headers)\n", ["test_gateway"]),
    ("F3: no cap on requests in flight", "bin/ollama1-gateway",
     "if not gw.inflight.acquire(blocking=False):", "if False:", ["test_gateway"]),
    ("F5: exception mid-stream breaks the stream", "bin/ollama1-gateway",
     "            except Exception as e:\n                # e.g. Ollama restarting",
     "            except ZeroDivisionError as e:\n                # e.g. Ollama restarting", ["test_gateway"]),
    ("KV: models kept across devices", "bin/ollama1-gateway",
     "if self.last_device is None or self.last_device == dev_id:", "if True:", ["test_gateway"]),
    ("dash: pairing code drawn blank", "lib/o1big.py", "for c in g[i])", "for c in g)", ["test_big"]),
    ("ram: flagged models held to VRAM", "bin/ollama1-gateway",
     "        if not ram:\n            return self.ensure_on_gpu(name)", "        if True:\n            return self.ensure_on_gpu(name)",
     ["test_gateway"]),
    ("ram: every model may use RAM", "bin/ollama1-gateway", "            ram = gw.is_ram_model(name)",
     "            ram = True", ["test_gateway"]),
    ("ram: swap not checked", "bin/ollama1-gateway",
     "if swapped > (512 << 20) or int(after.get(\"MemAvailable\", 1 << 40)) < (1 << 30):", "if False:",
     ["test_gateway"]),
    ("ram: loads next to other models", "bin/ollama1-gateway", "        if not victims:\n            return",
     "        if True:\n            return", ["test_gateway"]),
    ("ram: no context step-down", "bin/ollama1-gateway", "            tries += [c for c in (4096, 2048) if c < n_ctx]",
     "            pass", ["test_gateway"]),
    ("allow: unknown flags accepted", "lib/o1common.py", "        elif any(fl not in ALLOW_FLAGS for fl in flags):",
     "        elif False:", ["test_gateway"]),
    ("access: 'none' mode on by default", "bin/ollama1-gateway", '        if self.cfg.get("access") == "none":',
     '        if self.cfg.get("access") != "cloudflare_only":', ["test_gateway"]),
    ("none: any Host accepted", "bin/ollama1-gateway",
     'if not re.match(r"^(127\\.0\\.0\\.1|localhost)(:[0-9]{1,5})?$", host):', "if False:", ["test_gateway"]),
    ("none: Origin accepted", "bin/ollama1-gateway", '            if headers.get("Origin") is not None:',
     "            if False:", ["test_gateway"]),
    ("none: non-JSON POST accepted", "bin/ollama1-gateway", '            if method == "POST" and (headers.get',
     '            if False and (headers.get', ["test_gateway"]),
    ("none: starts next to a tunnel", "bin/ollama1-gateway",
     '    if cfg.get("access") == "none" and cfg.get("tunnel_id"):', "    if False:", ["test_gateway"]),
    ("ram: spilled model kept next to a GPU-only job", "bin/ollama1-gateway",
     'return self.is_ram_model(n) or int(m.get("size_vram") or 0) < int(m.get("size") or 0)',
     "return self.is_ram_model(n)", ["test_gateway"]),
    ("mem: mmap left on for ram models", "bin/ollama1-gateway", '            clean["use_mmap"] = False',
     "            pass", ["test_gateway"]),
    ("mem: warm load with mmap", "bin/ollama1-gateway", '            opts["use_mmap"] = False   # see shape()',
     "            pass", ["test_gateway"]),
    ("mem: margin 6 GiB again", "bin/ollama1-gateway",
     'margin = max(8 << 30, int(mi.get("MemTotal", 0) * 0.12), int(self.cfg.get("ram_margin_gib") or 8) << 30)',
     "margin = 6 << 30", ["test_gateway"]),
    ("mem: Ollama's cap ignored", "bin/ollama1-gateway", "        if cap:\n", "        if False:\n",
     ["test_gateway"]),
    ("mem: compute buffers not counted", "lib/o1ollama.py",
     "(RAM_COMPUTE_BYTES if ram else (256 << 20))", "(256 << 20)", ["test_gateway"]),
    ("mem: OOM not reported", "bin/ollama1-gateway",
     "        if before is not None and after is not None and after > before:", "        if False:",
     ["test_gateway"]),
    ("mem: Ollama dying taken for the client leaving", "bin/ollama1-gateway",
     '        except OSError:\n            raise GatewayError(502, "ollama", "Ollama stopped while loading the model")',
     '        except ZeroDivisionError:\n            raise GatewayError(502, "ollama", "Ollama stopped while loading the model")',
     ["test_gateway"]),
    ("repack: VRAM counted while repacking is on", "bin/ollama1-gateway",
     '        host_only = ram and not self.cfg.get("ollama_no_repack")', "        host_only = False", ["test_gateway"]),
    ("repack: mmap forced off with repacking off", "bin/ollama1-gateway",
     '        if ram and not self.cfg.get("ollama_no_repack"):\n            # With repacking on',
     '        if ram:\n            # With repacking on', ["test_gateway"]),
    ("info: every detected field served", "bin/ollama1-gateway",
     '                return self.send_json(200, {"gpu": {"vendor": g.get("vendor"), "name": g.get("name"),\n'
     '                                                    "vram_bytes": g.get("vram_bytes")}})',
     '                return self.send_json(200, {"gpu": dict(g)})', ["test_gateway"]),
    ("info: revision table ignored", "lib/o1gpu.py", "        name = AMD_BY_REVISION.get(key)", "        name = None",
     ["test_gpu"]),
    ("setup: no mkfs after a crash before it", "lib/setuplib.sh",
     "    run mkfs.ext4 -F -q -L o1data", "    true && : mkfs.ext4 -F -q -L o1data", ["test_setuplib"]),
    ("setup: array mkfs without setup's marker", "lib/setuplib.sh",
     '    [ -f "$STATE_DIR/raid.mkfs-pending" ] || fsck_hint', "    true || fsck_hint", ["test_setuplib"]),
    ("setup: models mkfs without setup's marker", "lib/setuplib.sh",
     '    [ -f "$STATE_DIR/models.mkfs-pending" ] || fsck_hint', "    true || fsck_hint", ["test_setuplib"]),
    ("setup: blkid read errors ignored", "lib/setuplib.sh",
     '    *) echo "blkid could not read $1 (exit $rc)" >&2; return 3 ;;', "    *) ;;", ["test_setuplib"]),
    ("KV: device switch fails open", "bin/ollama1-gateway", "        if not cleared:", "        if False:",
     ["test_gateway"]),
    ("cf: foreign CNAME changed", "bin/ollama1-cf-access", "        if foreign:", "        if False:",
     ["test_cloudflare"]),
    ("cf: redirects followed", "bin/ollama1-cf-access", "OPENER = urllib.request.build_opener(NoRedirect)",
     "OPENER = urllib.request.build_opener()", ["test_cloudflare"]),
    ("cf: new secret shown only at the end", "bin/ollama1-cf-access",
     '        say("created service token %s (never expires)" % TOKEN_NAME)\n'
     '        if show_secret(tok["client_id"], tok.get("client_secret"), test_mode):',
     '        say("created service token %s (never expires)" % TOKEN_NAME)\n'
     '        if False:', ["test_cloudflare"]),
    ("cf: unshown secret not replaced", "bin/ollama1-cf-access",
     "            rotate = True\n        if rotate is None:", "            pass\n        if rotate is None:",
     ["test_cloudflare"]),
    ("cf: service token expires in a year", "bin/ollama1-cf-access", '"duration": "forever"',
     '"duration": "8760h"', ["test_cloudflare"]),
    ("setup: a mirror on the disks is wiped again", "lib/setuplib.sh",
     '      [ -n "$line" ] && md_line_is_ours "$line" && echo "$dev"', "      true", ["test_setuplib"]),
    ("setup: fstab line without a UUID", [
        ("lib/setuplib.sh", '  [[ "$line" =~ ^UUID=[0-9A-Fa-f-]{8,}[[:space:]] ]] || die', "  true || die"),
        ("lib/setuplib.sh", '  [ -n "$uuid" ] || die "no filesystem UUID on $md; fstab left as it was"', "  true")],
     ["test_setuplib"]),
    ("setup: unparseable free space accepted", "lib/setuplib.sh",
     "  case \"$n\" in ''|*[!0-9]*) return 1 ;; esac", "  true", ["test_setuplib"]),
    ("setup: unexpected signature wiped", "lib/setuplib.sh",
     """    case " $* " in *" $t "*) ;; *) echo "$dev carries '$t'"; bad=1 ;; esac""",
     """    case " $* " in *) ;; esac""", ["test_setuplib"]),
    ("setup: fstab/crypttab/swap users ignored", "lib/setuplib.sh", "  return $found", "  return 1",
     ["test_setuplib"]),
    ("setup: wrong serial wiped", "lib/setuplib.sh",
     '    serial_is "$d2" "$s2" || die', '    true || die', ["test_setuplib"]),
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
    ("cf: duplicate CNAMEs kept", "bin/ollama1-cf-access", "        for r in extra:", "        for r in []:",
     ["test_cloudflare"]),
    ("cf: a new CNAME on every run", "bin/ollama1-cf-access", "        if not cnames:", "        if True:",
     ["test_cloudflare"]),
    ("cf: API token saved to disk", "bin/ollama1-cf-access", "    del token\n",
     "    open(os.path.join(Paths.etc, 'cf-token'), 'w').write(token)\n    del token\n", ["test_cloudflare"]),
    ("cf: tunnel credential readable by all", "bin/ollama1-cf-access",
     "write_json_atomic(creds_path, creds, mode=0o600)", "write_json_atomic(creds_path, creds, mode=0o644)",
     ["test_cloudflare"]),
    ("cf: a new tunnel on every run", "bin/ollama1-cf-access", "    if found:\n", "    if False:\n",
     ["test_cloudflare"]),
]


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    survived = []
    ran = 0
    for m in MUTANTS:
        if len(m) == 3:
            name, rel, tests = m
            old = new = None
        else:
            name, rel, old, new, tests = m
        if only and only not in name:
            continue
        ran += 1
        work = tempfile.mkdtemp(prefix="o1mut-")
        try:
            for d in ("lib", "bin", "config", "systemd"):
                shutil.copytree(os.path.join(KIT, d), os.path.join(work, d))
            edits = rel if isinstance(rel, list) else [(rel, old, new)]
            broken = False
            for erel, eold, enew in edits:
                path = os.path.join(work, erel)
                src = open(path).read()
                if src.count(eold) != 1:
                    print("BROKEN  %-50s  (the original text appears %d times)" % (name, src.count(eold)))
                    broken = True
                    break
                open(path, "w").write(src.replace(eold, enew))
            if broken:
                survived.append(name)
                continue
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
