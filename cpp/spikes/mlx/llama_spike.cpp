// THE MLX SPIKE (CPP_PLAN.md §3.4, NOTES 6b611): one MLX language model
// (Llama 3.2 1B Instruct, 4-bit, mlx-community's weights from the HF cache)
// loaded and run from C++ through MLX's own C++ API, tokens streamed as they
// are decoded. What mlx-lm does in Python, written here for this one
// architecture: the weights' layout, the quantized linears and embedding,
// Llama 3's scaled RoPE, the KV cache (mlx-lm's KVCache: 256-step buffers
// filled by slice_update), causal attention through mx::fast, greedy
// sampling, and mlx-lm's generate_step pipelining (the next token is queued
// with async_eval before the current one is read). The tokenizer's DECODE
// is here (byte-level BPE, from tokenizer.json's vocab); the ENCODE and the
// chat template are not: the prompt's token ids come from a file the bench
// writes with the model's own tokenizer, so both paths see the same ids.
//
//   llama_spike <model dir> <prompt ids file> [max_tokens] [--quiet]
//   llama_spike <model dir> --text "a question" [max_tokens] [--quiet] [--encode-only]
// (--text: the chat template and the encode in C++ too, llama3_tokenizer.hpp)
//
// Prints the text as it streams, then one JSON line: load, time to first
// token, prefill and decode tokens/s, peak memory, and the token ids (so the
// bench can check C++ and mlx-lm chose the same tokens).
#include <chrono>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <map>
#include <optional>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

#include "llama3_tokenizer.hpp"
#include "mlx/mlx.h"

namespace mx = mlx::core;
using mx::array;
using Clock = std::chrono::steady_clock;

static double ms_since(Clock::time_point t) {
  return std::chrono::duration<double, std::milli>(Clock::now() - t).count();
}

// ------------------------------------------------------------------ config

struct Config {
  int hidden = 2048, layers = 16, heads = 32, kv_heads = 8, head_dim = 64, vocab = 128256;
  float eps = 1e-5f, rope_theta = 500000.f;
  int group = 64, bits = 4;
  // llama3 rope scaling
  float factor = 32.f, low = 1.f, high = 4.f;
  int orig_ctx = 8192;
  std::vector<int> eos{128001, 128008, 128009};
};

static std::string slurp(const std::string& p) {
  std::ifstream f(p, std::ios::binary);
  std::ostringstream s;
  s << f.rdbuf();
  return s.str();
}

// a number after "key": in a JSON text; enough for config.json's scalars
static std::optional<double> jnum(const std::string& j, const std::string& key) {
  size_t k = j.find("\"" + key + "\"");
  if (k == std::string::npos) return std::nullopt;
  size_t c = j.find(':', k);
  if (c == std::string::npos) return std::nullopt;
  return std::strtod(j.c_str() + c + 1, nullptr);
}

static Config read_config(const std::string& dir) {
  std::string j = slurp(dir + "/config.json");
  Config c;
  auto set = [&](const char* k, auto& v) {
    if (auto x = jnum(j, k)) v = static_cast<std::remove_reference_t<decltype(v)>>(*x);
  };
  set("hidden_size", c.hidden);
  set("num_hidden_layers", c.layers);
  set("num_attention_heads", c.heads);
  set("num_key_value_heads", c.kv_heads);
  set("head_dim", c.head_dim);
  set("vocab_size", c.vocab);
  set("rms_norm_eps", c.eps);
  set("rope_theta", c.rope_theta);
  set("group_size", c.group);
  set("bits", c.bits);
  set("factor", c.factor);
  set("low_freq_factor", c.low);
  set("high_freq_factor", c.high);
  set("original_max_position_embeddings", c.orig_ctx);
  if (j.find("\"model_type\": \"llama\"") == std::string::npos &&
      j.find("\"model_type\":\"llama\"") == std::string::npos) {
    std::cerr << "this spike runs model_type llama only\n";
    std::exit(2);
  }
  return c;
}

// ------------------------------------------------------------------ the model

struct QLinear {
  array w = array(0), s = array(0), b = array(0);
};

struct Layer {
  array in_norm = array(0), post_norm = array(0);
  QLinear q, k, v, o, gate, up, down;
  // the KV cache (mlx-lm's KVCache, step 256)
  std::optional<array> keys, values;
  int offset = 0;
};

struct Model {
  Config c;
  QLinear embed;
  array norm = array(0);
  array freqs = array(0);
  std::vector<Layer> layers;

  array qlin(const array& x, const QLinear& l) const {
    return mx::quantized_matmul(x, l.w, l.s, l.b, /*transpose=*/true, c.group, c.bits);
  }

  array embed_tokens(const array& ids) const {  // nn.QuantizedEmbedding
    return mx::dequantize(mx::take(embed.w, ids, 0), mx::take(embed.s, ids, 0), mx::take(embed.b, ids, 0), c.group,
                          c.bits);
  }

  void cache_update(Layer& L, const array& k, const array& v, array& K, array& V) const {
    const int n = k.shape(2), prev = L.offset;
    const int step = 256;
    if (!L.keys || prev + n > L.keys->shape(2)) {
      int steps = (step + n - 1) / step;
      mx::Shape ks{1, c.kv_heads, steps * step, c.head_dim};
      array nk = mx::zeros(ks, k.dtype()), nv = mx::zeros(ks, v.dtype());
      if (L.keys) {
        array ok = *L.keys, ov = *L.values;
        if (prev % step != 0) {
          ok = mx::slice(ok, {0, 0, 0, 0}, {1, c.kv_heads, prev, c.head_dim});
          ov = mx::slice(ov, {0, 0, 0, 0}, {1, c.kv_heads, prev, c.head_dim});
        }
        L.keys = mx::concatenate({ok, nk}, 2);
        L.values = mx::concatenate({ov, nv}, 2);
      } else {
        L.keys = nk;
        L.values = nv;
      }
    }
    L.offset += n;
    L.keys = mx::slice_update(*L.keys, k, {0, 0, prev, 0}, {1, c.kv_heads, L.offset, c.head_dim});
    L.values = mx::slice_update(*L.values, v, {0, 0, prev, 0}, {1, c.kv_heads, L.offset, c.head_dim});
    if (L.offset < L.keys->shape(2)) {
      K = mx::slice(*L.keys, {0, 0, 0, 0}, {1, c.kv_heads, L.offset, c.head_dim});
      V = mx::slice(*L.values, {0, 0, 0, 0}, {1, c.kv_heads, L.offset, c.head_dim});
    } else {
      K = *L.keys;
      V = *L.values;
    }
  }

  // tokens: [1, L] int32; returns the hidden state [1, L, hidden] after the final norm
  array forward(const array& tokens) {
    const int Lq = tokens.shape(1);
    array h = embed_tokens(tokens);
    const float scale = 1.0f / std::sqrt(static_cast<float>(c.head_dim));
    for (Layer& L : layers) {
      const int offset = L.offset;
      array x = mx::fast::rms_norm(h, L.in_norm, c.eps);
      array q = mx::transpose(mx::reshape(qlin(x, L.q), {1, Lq, c.heads, c.head_dim}), {0, 2, 1, 3});
      array k = mx::transpose(mx::reshape(qlin(x, L.k), {1, Lq, c.kv_heads, c.head_dim}), {0, 2, 1, 3});
      array v = mx::transpose(mx::reshape(qlin(x, L.v), {1, Lq, c.kv_heads, c.head_dim}), {0, 2, 1, 3});
      q = mx::fast::rope(q, c.head_dim, false, std::nullopt, 1.0f, offset, freqs);
      k = mx::fast::rope(k, c.head_dim, false, std::nullopt, 1.0f, offset, freqs);
      array K = k, V = v;
      cache_update(L, k, v, K, V);
      array o = mx::fast::scaled_dot_product_attention(q, K, V, scale, Lq > 1 ? "causal" : "");
      o = mx::reshape(mx::transpose(o, {0, 2, 1, 3}), {1, Lq, c.heads * c.head_dim});
      h = mx::add(h, qlin(o, L.o));
      array y = mx::fast::rms_norm(h, L.post_norm, c.eps);
      array g = qlin(y, L.gate);
      array u = qlin(y, L.up);
      h = mx::add(h, qlin(mx::multiply(mx::multiply(g, mx::sigmoid(g)), u), L.down));  // swiglu
    }
    return mx::fast::rms_norm(h, norm, c.eps);
  }

  // the next token from the last position (greedy: what mlx-lm does at temp 0)
  array step(const array& tokens) {
    array h = forward(tokens);
    const int Lq = h.shape(1);
    array last = mx::slice(h, {0, Lq - 1, 0}, {1, Lq, c.hidden});
    array logits = qlin(last, embed);  // tied embeddings: embed_tokens.as_linear
    return mx::argmax(mx::reshape(logits, {c.vocab}), false);
  }

  std::vector<array> cache_state() const {
    std::vector<array> s;
    for (auto& L : layers)
      if (L.keys) {
        s.push_back(*L.keys);
        s.push_back(*L.values);
      }
    return s;
  }
};

static Model load_model(const std::string& dir) {
  Model m;
  m.c = read_config(dir);
  auto [w, meta] = mx::load_safetensors(dir + "/model.safetensors");
  auto get = [&](const std::string& k) {
    auto it = w.find(k);
    if (it == w.end()) {
      std::cerr << "missing weight " << k << "\n";
      std::exit(2);
    }
    return it->second;
  };
  auto ql = [&](const std::string& p) { return QLinear{get(p + ".weight"), get(p + ".scales"), get(p + ".biases")}; };
  m.embed = ql("model.embed_tokens");
  m.norm = get("model.norm.weight");
  for (int i = 0; i < m.c.layers; ++i) {
    std::string p = "model.layers." + std::to_string(i);
    Layer L;
    L.in_norm = get(p + ".input_layernorm.weight");
    L.post_norm = get(p + ".post_attention_layernorm.weight");
    L.q = ql(p + ".self_attn.q_proj");
    L.k = ql(p + ".self_attn.k_proj");
    L.v = ql(p + ".self_attn.v_proj");
    L.o = ql(p + ".self_attn.o_proj");
    L.gate = ql(p + ".mlp.gate_proj");
    L.up = ql(p + ".mlp.up_proj");
    L.down = ql(p + ".mlp.down_proj");
    m.layers.push_back(std::move(L));
  }
  // Llama3RoPE's frequencies (mlx_lm/models/rope_utils.py), in doubles
  const Config& c = m.c;
  std::vector<float> f;
  const double lw = c.orig_ctx / c.low, hw = c.orig_ctx / c.high;
  for (int i = 0; i < c.head_dim; i += 2) {
    double fr = std::pow(static_cast<double>(c.rope_theta), static_cast<double>(i) / c.head_dim);
    double wl = 2 * M_PI * fr;
    double out = wl > lw ? fr * c.factor : fr;
    if (wl > hw && wl < lw) {
      double sm = (c.orig_ctx / wl - c.low) / (c.high - c.low);
      out = fr / ((1 - sm) / c.factor + sm);
    }
    f.push_back(static_cast<float>(out));
  }
  m.freqs = array(f.data(), {static_cast<int>(f.size())}, mx::float32);
  std::vector<array> all;
  for (auto& [k, v] : w) all.push_back(v);
  mx::eval(all);
  return m;
}

// ------------------------------------------------------------------ decode

// byte-level BPE (GPT-2's bytes_to_unicode): each token is a string of
// stand-in code points, one per byte
static std::unordered_map<int, std::string> load_vocab(const std::string& dir) {
  std::string j = slurp(dir + "/tokenizer.json");
  std::unordered_map<uint32_t, uint8_t> back;
  {
    std::vector<int> bs;
    for (int b = '!'; b <= '~'; ++b) bs.push_back(b);
    for (int b = 0xA1; b <= 0xAC; ++b) bs.push_back(b);
    for (int b = 0xAE; b <= 0xFF; ++b) bs.push_back(b);
    std::vector<int> cs = bs;
    int n = 0;
    for (int b = 0; b < 256; ++b)
      if (std::find(bs.begin(), bs.end(), b) == bs.end()) {
        bs.push_back(b);
        cs.push_back(256 + n++);
      }
    for (size_t i = 0; i < bs.size(); ++i) back[static_cast<uint32_t>(cs[i])] = static_cast<uint8_t>(bs[i]);
  }
  auto utf8_cps = [](const std::string& s) {
    std::vector<uint32_t> cp;
    for (size_t i = 0; i < s.size();) {
      unsigned char c = static_cast<unsigned char>(s[i]);
      int n = c < 0x80 ? 1 : c < 0xE0 ? 2 : c < 0xF0 ? 3 : 4;
      uint32_t v = n == 1 ? c : n == 2 ? (c & 0x1F) : n == 3 ? (c & 0x0F) : (c & 0x07);
      for (int k = 1; k < n && i + k < s.size(); ++k) v = (v << 6) | (static_cast<unsigned char>(s[i + k]) & 0x3F);
      cp.push_back(v);
      i += static_cast<size_t>(n);
    }
    return cp;
  };
  // a JSON string at i (just past its quote): decoded to UTF-8
  auto jstr = [&](size_t& i) {
    std::string o;
    while (i < j.size() && j[i] != '"') {
      if (j[i] == '\\') {
        char e = j[++i];
        if (e == 'u') {
          uint32_t v = static_cast<uint32_t>(std::stoul(j.substr(i + 1, 4), nullptr, 16));
          i += 4;
          if (v >= 0xD800 && v < 0xDC00 && j[i + 1] == '\\') {
            uint32_t lo = static_cast<uint32_t>(std::stoul(j.substr(i + 3, 4), nullptr, 16));
            v = 0x10000 + ((v - 0xD800) << 10) + (lo - 0xDC00);
            i += 6;
          }
          if (v < 0x80) o += static_cast<char>(v);
          else if (v < 0x800) o += {static_cast<char>(0xC0 | (v >> 6)), static_cast<char>(0x80 | (v & 0x3F))};
          else if (v < 0x10000)
            o += {static_cast<char>(0xE0 | (v >> 12)), static_cast<char>(0x80 | ((v >> 6) & 0x3F)),
                  static_cast<char>(0x80 | (v & 0x3F))};
          else
            o += {static_cast<char>(0xF0 | (v >> 18)), static_cast<char>(0x80 | ((v >> 12) & 0x3F)),
                  static_cast<char>(0x80 | ((v >> 6) & 0x3F)), static_cast<char>(0x80 | (v & 0x3F))};
        } else {
          o += e == 'n' ? '\n' : e == 't' ? '\t' : e == 'r' ? '\r' : e;
        }
        ++i;
      } else {
        o += j[i++];
      }
    }
    ++i;
    return o;
  };
  std::unordered_map<int, std::string> vocab;
  size_t v = j.find("\"vocab\"");
  size_t i = j.find('{', v) + 1;
  while (i < j.size()) {
    while (j[i] == ' ' || j[i] == '\n' || j[i] == ',') ++i;
    if (j[i] == '}') break;
    ++i;  // the key's quote
    std::string tok = jstr(i);
    size_t c = j.find(':', i);
    int id = std::atoi(j.c_str() + c + 1);
    i = j.find_first_of(",}", c);
    std::string bytes;
    for (uint32_t cp : utf8_cps(tok)) {
      auto it = back.find(cp);
      if (it != back.end()) bytes += static_cast<char>(it->second);
    }
    vocab[id] = bytes;
  }
  return vocab;
}

int main(int argc, char** argv) {
  if (argc < 3) {
    std::cerr << "usage: llama_spike <model dir> (<prompt ids> | --text \"question\") [max_tokens] [--quiet]\n";
    return 2;
  }
  const std::string dir = argv[1];
  const bool text = std::string(argv[2]) == "--text";
  const int arg_max = text ? 4 : 3;
  const int max_tokens = (argc > arg_max && argv[arg_max][0] != '-') ? std::atoi(argv[arg_max]) : 256;
  bool quiet = false, encode_only = false, json_stderr = false;
  for (int i = 1; i < argc; ++i) {
    quiet = quiet || std::string(argv[i]) == "--quiet";
    json_stderr = json_stderr || std::string(argv[i]) == "--json-stderr";  // the app's panel: stdout is the text alone
    encode_only = encode_only || std::string(argv[i]) == "--encode-only";
  }
  std::vector<int> prompt;
  double encode_ms = 0;
  if (text) {
    if (argc < 4) return 2;
    auto te = Clock::now();
    llama3::Tokenizer tk;
    if (!tk.load(dir)) {
      std::cerr << "can't read " << dir << "/tokenizer.json\n";
      return 2;
    }
    prompt = tk.encode_chat(argv[3]);
    encode_ms = ms_since(te);
    if (encode_only) {
      for (size_t i = 0; i < prompt.size(); ++i) std::cout << (i ? " " : "") << prompt[i];
      std::cout << "\n";
      return 0;
    }
  } else {
    std::ifstream f(argv[2]);
    for (int x; f >> x;) prompt.push_back(x);
  }
  if (prompt.empty()) {
    std::cerr << "no prompt ids in " << argv[2] << "\n";
    return 2;
  }
  // what mlx-lm's wired_limit() does, and its generation stream
  if (mx::metal::is_available()) {
    auto& info = mx::device_info();
    auto it = info.find("max_recommended_working_set_size");
    if (it != info.end()) mx::set_wired_limit(std::get<size_t>(it->second));
  }
  mx::set_default_stream(mx::new_stream(mx::Device::gpu));

  auto t0 = Clock::now();
  Model m = load_model(dir);
  auto vocab = load_vocab(dir);
  const double load_ms = ms_since(t0);

  // prefill all but the last prompt token (only the cache is evaluated, as
  // in generate_step), then the last one through step()
  auto t1 = Clock::now();
  const int P = static_cast<int>(prompt.size());
  if (P > 1) {
    array ids(prompt.data(), {1, P - 1}, mx::int32);
    m.forward(ids);
    mx::eval(m.cache_state());
  }
  int last = prompt.back();
  array y = m.step(array(&last, {1, 1}, mx::int32));
  mx::async_eval(y);
  std::vector<int> out;
  double ttft_ms = 0;
  auto t_first = Clock::now();
  for (int n = 0;; ++n) {
    std::optional<array> next;
    if (n != max_tokens) {
      next = m.step(mx::reshape(mx::astype(y, mx::int32), {1, 1}));
      mx::async_eval(*next);
    }
    if (n == 0) {
      mx::eval(y);
      ttft_ms = ms_since(t1);
      t_first = Clock::now();
    }
    if (n == max_tokens) break;
    int tok = y.item<int>();
    out.push_back(tok);
    if (!quiet) {
      auto it = vocab.find(tok);
      if (it != vocab.end()) std::cout << it->second << std::flush;
    }
    if (std::find(m.c.eos.begin(), m.c.eos.end(), tok) != m.c.eos.end()) break;
    if (n % 256 == 0) mx::clear_cache();
    y = *next;
  }
  const double decode_ms = ms_since(t_first);
  const int gen = static_cast<int>(out.size());
  std::cout << "\n";
  std::ostringstream ids;
  for (size_t i = 0; i < out.size(); ++i) ids << (i ? "," : "") << out[i];
  std::fprintf(json_stderr ? stderr : stdout,
      "{\"impl\": \"cpp\", \"encode_ms\": %.1f, \"load_ms\": %.1f, \"prompt_tokens\": %d, \"ttft_ms\": %.1f, \"prefill_tps\": %.1f, "
      "\"gen_tokens\": %d, \"decode_tps\": %.1f, \"peak_mem_mb\": %.1f, \"ids\": [%s]}\n",
      encode_ms, load_ms, P, ttft_ms, P / (ttft_ms / 1000.0), gen, gen > 1 ? (gen - 1) / (decode_ms / 1000.0) : 0.0,
      mx::get_peak_memory() / 1048576.0, ids.str().c_str());
  return 0;
}
