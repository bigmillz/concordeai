"""The C++ Llama 3 encode (llama3_tokenizer.hpp) against transformers'
apply_chat_template, prompt by prompt (encode only, no model run).

    python3 tok_check.py <llama_spike> <model dir>
"""
import os
import subprocess
import sys

PROMPTS = [
    "Name three uses of a lighthouse, in one sentence each.",
    "What's 17 * 23? Show your working.",
    "I've got a sailboat; it's 23ft long and I'd like to winterize it. What're the steps?",
    "  leading and trailing spaces  ",
    "Line one\nLine two\n\n\nLine five after blanks\r\n  indented\tTabbed",
    "Code: def f(x):\n    return x**2  # square\n\n    print(f(12345678))",
    "Punctuation!!! ... ??? (brackets) [square] {curly} <angle> \"quotes\" 'single' — dash – en",
    "Café, naïve, résumé, Zürich, São Paulo, smörgåsbord.",
    "Emoji 🙂👍🏽 and symbols ©®™ and math ∑∫√ 3.14159",
    "Ünïcödé ΑΒΓ Ελληνικά Русский текст 日本語のテキスト 中文 한국어 עברית العربية",
    "URLs: https://example.com/a/b?c=1&d=2 and email me@example.org",
    "Numbers: 1, 12, 123, 1234, 12345, 1,234,567.89 and 2026-11-11T02:00:00Z",
    "THE 'S 'T 'RE 'VE 'M 'LL 'D contractions in CAPS and 's lower",
]


def main():
    spike, model = sys.argv[1], sys.argv[2]
    os.environ["HF_HUB_OFFLINE"] = "1"
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(model)
    bad = 0
    for p in PROMPTS:
        ref = tok.apply_chat_template([{"role": "user", "content": p}], add_generation_prompt=True)
        ref = list(ref["input_ids"]) if hasattr(ref, "keys") else list(ref)
        out = subprocess.run([spike, model, "--text", p, "--encode-only"], capture_output=True, text=True)
        got = [int(x) for x in out.stdout.split()]
        ok = got == ref
        bad += not ok
        print(("  ok   " if ok else "  DIFF ") + repr(p[:60]) + ("" if ok else "\n    ref %s\n    got %s" % (ref, got)))
    print("%d/%d identical" % (len(PROMPTS) - bad, len(PROMPTS)))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
