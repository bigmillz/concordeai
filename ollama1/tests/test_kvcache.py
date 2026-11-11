#!/usr/bin/env python3
"""The K/V cache estimate the gateway's fit check uses (6b660): counted the
way llama.cpp allocates it. Every expected figure below is the size the
server's own log printed ("llama_kv_cache: size = ...", "RS buffer size")
for the real model on 2026-11-11 (Ollama 0.40.15, -ub 1024), except gpt-oss,
whose windowed layers the kit counts at 1,280 cells where that run used 768
(a smaller micro-batch): over, never under."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get("OLLAMA1_TEST_LIB") or os.path.join(os.path.dirname(HERE), "lib"))  # mutate.py's copy
import o1ollama  # noqa: E402

MIB, GIB = 1 << 20, 1 << 30

# /api/show model_info of the server's models (only the keys the estimate reads)
GEMMA4_12B = {
    "general.architecture": "gemma4", "gemma4.block_count": 48, "gemma4.embedding_length": 3840,
    "gemma4.attention.head_count": 16, "gemma4.attention.head_count_kv": [8, 8, 8, 8, 8, 1] * 8,
    "gemma4.attention.key_length": 512, "gemma4.attention.value_length": 512,
    "gemma4.attention.key_length_swa": 256, "gemma4.attention.value_length_swa": 256,
    "gemma4.attention.shared_kv_layers": 0, "gemma4.attention.sliding_window": 1024,
    "gemma4.attention.sliding_window_pattern": [True, True, True, True, True, False] * 8,
    "gemma4.context_length": 262144}
GEMMA4_E2B = {
    "general.architecture": "gemma4", "gemma4.block_count": 35, "gemma4.embedding_length": 1536,
    "gemma4.attention.head_count": 8, "gemma4.attention.head_count_kv": 1,
    "gemma4.attention.key_length": 512, "gemma4.attention.value_length": 512,
    "gemma4.attention.key_length_swa": 256, "gemma4.attention.value_length_swa": 256,
    "gemma4.attention.shared_kv_layers": 20, "gemma4.attention.sliding_window": 512,
    "gemma4.attention.sliding_window_pattern": [True, True, True, True, False] * 7,
    "gemma4.context_length": 131072}
GPT_OSS_20B = {
    "general.architecture": "gpt-oss", "gpt-oss.block_count": 24, "gpt-oss.embedding_length": 2880,
    "gpt-oss.attention.head_count": 64, "gpt-oss.attention.head_count_kv": 8,
    "gpt-oss.attention.key_length": 64, "gpt-oss.attention.value_length": 64,
    "gpt-oss.attention.sliding_window": 128, "gpt-oss.context_length": 131072}
MIMO_9B = {   # qwen35: three recurrent layers to one attention layer
    "general.architecture": "qwen35", "qwen35.block_count": 32, "qwen35.embedding_length": 4096,
    "qwen35.attention.head_count": 16, "qwen35.attention.head_count_kv": 4,
    "qwen35.attention.key_length": 256, "qwen35.attention.value_length": 256,
    "qwen35.attention.recurrent_layers": [True, True, True, False] * 8, "qwen35.full_attention_interval": 4,
    "qwen35.ssm.conv_kernel": 4, "qwen35.ssm.group_count": 16, "qwen35.ssm.inner_size": 4096,
    "qwen35.ssm.state_size": 128, "qwen35.context_length": 262144}
LLAMA_1B = {
    "general.architecture": "llama", "llama.block_count": 16, "llama.embedding_length": 2048,
    "llama.attention.head_count": 32, "llama.attention.head_count_kv": 8,
    "llama.attention.key_length": 64, "llama.attention.value_length": 64, "llama.context_length": 131072}

BUDGET_16G = 17094934528 - 768 * MIB      # the server's card less the gateway's reserve (fit_check's budget)


class MeasuredSizes(unittest.TestCase):
    def test_gemma4_12b_16k(self):
        # 8 global layers x 16,384 cells (256 MiB) + 40 windowed layers x 2,048 cells (640 MiB)
        self.assertEqual(o1ollama.kv_cache_bytes(GEMMA4_12B, 16384), 896 * MIB)

    def test_gemma4_e2b_16k_shared_layers_hold_nothing(self):
        # 15 own-cache layers: 3 global x 16,384 (96 MiB) + 12 windowed x 1,536 (18 MiB); 20 shared: none
        self.assertEqual(o1ollama.kv_cache_bytes(GEMMA4_E2B, 16384), 114 * MIB)

    def test_gpt_oss_32k_alternating_window(self):
        # 12 full layers x 32,768 (768 MiB) + 12 windowed layers x 1,280 cells (30 MiB)
        self.assertEqual(o1ollama.kv_cache_bytes(GPT_OSS_20B, 32768), 798 * MIB)

    def test_qwen35_recurrent_layers_hold_state_not_cache(self):
        # 8 attention layers x 16,384 (512 MiB) + 24 recurrent states (50.25 MiB, f32)
        self.assertEqual(o1ollama.kv_cache_bytes(MIMO_9B, 16384), 512 * MIB + 201 * MIB // 4)

    def test_plain_model_unchanged(self):
        self.assertEqual(o1ollama.kv_cache_bytes(LLAMA_1B, 32768), 1024 * MIB)

    def test_window_wider_than_the_context(self):
        # under the window every layer holds the whole (short) context
        self.assertEqual(o1ollama.kv_cache_bytes(GEMMA4_12B, 1024),
                         1024 * (8 * 2048 + 40 * 8192))

    def test_q8_cache_halves_it(self):
        self.assertEqual(o1ollama.kv_cache_bytes(LLAMA_1B, 8192, bytes_per_elem=1), 128 * MIB)


class StaysSafe(unittest.TestCase):
    def test_unknown_window_layout_counts_every_layer(self):
        info = {k.replace("gpt-oss", "newarch"): v for k, v in GPT_OSS_20B.items()}
        info["general.architecture"] = "newarch"
        self.assertEqual(o1ollama.kv_cache_bytes(info, 32768), 24 * 2048 * 32768)

    def test_window_grows_with_context_only_up_to_its_size(self):
        a = o1ollama.kv_cache_bytes(GEMMA4_12B, 32768)
        b = o1ollama.kv_cache_bytes(GEMMA4_12B, 65536)
        self.assertEqual(b - a, 8 * 2048 * 32768)          # only the 8 global layers grow

    def test_missing_metadata(self):
        self.assertIsNone(o1ollama.kv_cache_bytes({}, 8192))
        self.assertIsNone(o1ollama.kv_cache_bytes({"general.architecture": "gemma4"}, 8192))
        self.assertIsNone(o1ollama.kv_cache_bytes(None, 8192))


class AdmitsOnTheCard(unittest.TestCase):
    """What the fit check now lets through on the server's 16 GB card (sizes from /api/tags)."""

    def fits(self, size, info, ctx):
        need, _ = o1ollama.fit_estimate(size, info, ctx)
        return need <= BUDGET_16G

    def test_gemma4_12b_at_16k_and_beyond(self):
        for ctx in (16384, 32768, 65536, 131072):
            self.assertTrue(self.fits(8_021_618_941, GEMMA4_12B, ctx), ctx)

    def test_gpt_oss_20b_at_32k_not_128k(self):
        self.assertTrue(self.fits(13_793_441_244, GPT_OSS_20B, 32768))
        self.assertTrue(self.fits(13_793_441_244, GPT_OSS_20B, 49152))
        self.assertFalse(self.fits(13_793_441_244, GPT_OSS_20B, 131072))

    def test_estimate_stays_above_what_the_card_held(self):
        # nvidia-smi on 2026-11-11, whole card (including ~0.2 GB for the desktop): the estimate must not be
        # under the model's share, and not wildly over it (gpt-oss sits ~1.4 GiB over: the 5% weights margin
        # and its 13.8 GB file, more than the card holds of it; the cache part is exact)
        for size, info, ctx, measured in ((8_021_618_941, GEMMA4_12B, 16384, 9638 * MIB),
                                          (8_021_618_941, GEMMA4_12B, 65536, 10598 * MIB),
                                          (13_793_441_244, GPT_OSS_20B, 32768, 13480 * MIB),
                                          (13_793_441_244, GPT_OSS_20B, 65536, 14280 * MIB)):
            need, _ = o1ollama.fit_estimate(size, info, ctx)
            self.assertGreaterEqual(need, measured - 228 * MIB - 600 * MIB, (info["general.architecture"], ctx))
            self.assertLessEqual(need, measured + 3 * GIB // 2, (info["general.architecture"], ctx))


if __name__ == "__main__":
    unittest.main()
